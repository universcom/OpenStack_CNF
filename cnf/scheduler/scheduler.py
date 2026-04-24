"""
cnf.scheduler.scheduler — Policy-driven VM placement and migration scheduler.

The scheduler runs only on the master node. It:
  - Collects metrics from all clusters via gRPC
  - Evaluates policies against current cluster state
  - Triggers migrations when policies are violated
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from cnf.config import get_settings
from cnf.db.models import Cluster, ClusterMetric, Policy
from cnf.db.session import get_session
from cnf.openstack.client import OpenStackClient
from cnf.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ClusterSnapshot:
    cluster_id: str
    cluster_name: str
    grpc_addr: str
    cpu_used_pct: float = 0.0
    ram_used_pct: float = 0.0
    disk_used_pct: float = 0.0
    vm_count: int = 0


class Scheduler:
    """
    Runs on master node. Evaluates policies every N seconds and
    triggers live migrations to rebalance workloads.
    """

    def __init__(self, os_client: OpenStackClient) -> None:
        self.os_client = os_client
        self._is_master = False
        self._running = False
        self.settings = get_settings()

    def set_master(self, is_master: bool) -> None:
        self._is_master = is_master
        logger.info("scheduler_master_changed", is_master=is_master)

    async def run(self) -> None:
        self._running = True
        interval = self.settings.metrics.collection_interval_seconds

        while self._running:
            await asyncio.sleep(interval)
            if not self._is_master:
                continue
            try:
                await self._evaluate()
            except Exception as e:
                logger.error("scheduler_evaluate_error", error=str(e))

    async def stop(self) -> None:
        self._running = False

    async def _evaluate(self) -> None:
        snapshots = await self._collect_snapshots()
        policies  = await self._load_policies()

        for policy in policies:
            if not policy.enabled:
                continue
            try:
                await self._apply_policy(policy, snapshots)
            except Exception as e:
                logger.error("policy_apply_error", policy=policy.name, error=str(e))

    async def _collect_snapshots(self) -> list[ClusterSnapshot]:
        """Gather latest metrics from all registered clusters."""
        snapshots: list[ClusterSnapshot] = []
        async with get_session() as session:
            result = await session.execute(select(Cluster))
            clusters = result.scalars().all()

        for cluster in clusters:
            async with get_session() as session:
                m_result = await session.execute(
                    select(ClusterMetric)
                    .where(ClusterMetric.cluster_id == cluster.id)
                    .order_by(ClusterMetric.collected_at.desc())
                    .limit(1)
                )
                metric = m_result.scalar_one_or_none()

            snapshots.append(ClusterSnapshot(
                cluster_id=cluster.id,
                cluster_name=cluster.name,
                grpc_addr=cluster.grpc_addr,
                cpu_used_pct=metric.cpu_used_pct  if metric else 0.0,
                ram_used_pct=metric.ram_used_pct  if metric else 0.0,
                disk_used_pct=metric.disk_used_pct if metric else 0.0,
                vm_count=metric.vm_count            if metric else 0,
            ))
        return snapshots

    async def _load_policies(self) -> list[Policy]:
        async with get_session() as session:
            result = await session.execute(
                select(Policy)
                .where(Policy.enabled == True)
                .order_by(Policy.priority)
            )
            return list(result.scalars().all())

    async def _apply_policy(
        self, policy: Policy, snapshots: list[ClusterSnapshot]
    ) -> None:
        rule = policy.rule
        trigger = rule.get("trigger", "")
        action  = rule.get("action", "")
        target  = rule.get("target", "least_loaded")

        logger.debug("policy_evaluating", policy=policy.name, trigger=trigger)

        # Find clusters violating the trigger
        violating = [
            s for s in snapshots if self._eval_trigger(trigger, s)
        ]
        if not violating:
            return

        # Find best destination
        candidates = [s for s in snapshots if s not in violating]
        if not candidates:
            logger.warning("no_migration_candidates", policy=policy.name)
            return

        dest = self._pick_dest(candidates, target)

        for source_snap in violating:
            logger.info(
                "policy_triggered",
                policy=policy.name,
                source_cluster=source_snap.cluster_id,
                dest_cluster=dest.cluster_id,
                action=action,
            )
            # Trigger migration for one VM from overloaded cluster
            await self._trigger_rebalance(source_snap, dest, action)

    def _eval_trigger(self, trigger: str, snap: ClusterSnapshot) -> bool:
        """
        Evaluate a simple trigger expression against a cluster snapshot.
        Supported: cpu_pct > N, ram_pct > N, vm_count > N
        """
        try:
            ctx = {
                "cpu_pct":  snap.cpu_used_pct,
                "ram_pct":  snap.ram_used_pct,
                "disk_pct": snap.disk_used_pct,
                "vm_count": snap.vm_count,
            }
            return bool(eval(trigger, {"__builtins__": {}}, ctx))  # noqa: S307
        except Exception:
            return False

    def _pick_dest(
        self, candidates: list[ClusterSnapshot], strategy: str
    ) -> ClusterSnapshot:
        if strategy == "least_loaded":
            return min(candidates, key=lambda s: s.cpu_used_pct + s.ram_used_pct)
        if strategy == "least_vms":
            return min(candidates, key=lambda s: s.vm_count)
        return candidates[0]

    async def _trigger_rebalance(
        self,
        source: ClusterSnapshot,
        dest: ClusterSnapshot,
        action: str,
    ) -> None:
        """Pick one VM from the source cluster and migrate it to dest."""
        vms = await self.os_client.list_vms()
        active_vms = [v for v in vms if v.status == "ACTIVE"]
        if not active_vms:
            return

        vm = active_vms[0]
        live = "live" in action

        import uuid
        from cnf.db.models import Migration, MigrationState, MigrationType
        migration_id = str(uuid.uuid4())

        async with get_session() as session:
            migration = Migration(
                id=migration_id,
                vm_id=vm.id,
                vm_name=vm.name,
                source_cluster_id=source.cluster_id,
                dest_cluster_id=dest.cluster_id,
                type=MigrationType.LIVE if live else MigrationType.COLD,
                state=MigrationState.PENDING,
                initiated_by="scheduler",
                options={"dest_host": "", "dest_next_hop": ""},
            )
            session.add(migration)

        logger.info(
            "scheduler_migration_queued",
            migration_id=migration_id,
            vm_id=vm.id,
            type="live" if live else "cold",
            source=source.cluster_id,
            dest=dest.cluster_id,
        )
        # Actual execution is picked up by Celery worker
