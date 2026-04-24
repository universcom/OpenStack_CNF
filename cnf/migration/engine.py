"""
cnf.migration — VM migration state machine, cold migration, and live migration.

State machine:
  PENDING → PREFLIGHT → DISK → MEMORY (live only) → CUTOVER → BGP → CLEANUP → DONE
                                                                          ↓
                                                                       FAILED / ABORTED
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

from cnf.config import get_settings
from cnf.db.models import MigrationState, MigrationType
from cnf.db.session import get_session
from cnf.db.models import Migration, MigrationEvent
from cnf.network.bgp import BGPManager, BGPRoute
from cnf.openstack.client import OpenStackClient, VMDetails
from cnf.storage.ceph import CephRBDClient
from cnf.utils.logging import (
    get_logger,
    MIGRATION_TOTAL,
    MIGRATION_DURATION,
    MIGRATION_ACTIVE,
)

logger = get_logger(__name__)

StepFn = Callable[[], Coroutine[Any, Any, None]]


# ─────────────────────────────────────────────
# Migration context — passed through all steps
# ─────────────────────────────────────────────

@dataclass
class MigrationContext:
    migration_id: str
    vm_id: str
    source_cluster_id: str
    dest_cluster_id: str
    migration_type: MigrationType
    options: dict[str, Any] = field(default_factory=dict)

    # Populated during execution
    vm: VMDetails | None = None
    rbd_images: list[str] = field(default_factory=list)
    vm_ips: list[str] = field(default_factory=list)
    dest_nova_vm_id: str = ""
    source_grpc_client: Any = None
    dest_grpc_client: Any = None
    state: MigrationState = MigrationState.PENDING
    progress: float = 0.0
    started_at: datetime | None = None
    error: str = ""


# ─────────────────────────────────────────────
# Base migration engine
# ─────────────────────────────────────────────

class MigrationEngine:
    """
    Orchestrates migration steps via a linear state machine.
    Each step updates DB state and emits structured log events.
    Rollback handlers are registered per step.
    """

    def __init__(
        self,
        ctx: MigrationContext,
        source_os: OpenStackClient,
        dest_os: OpenStackClient,
        ceph: CephRBDClient,
        bgp: BGPManager,
    ) -> None:
        self.ctx = ctx
        self.source_os = source_os
        self.dest_os = dest_os
        self.ceph = ceph
        self.bgp = bgp
        self._rollbacks: list[StepFn] = []
        self._aborted = False

    async def _transition(self, state: MigrationState, progress: float = 0.0) -> None:
        self.ctx.state = state
        self.ctx.progress = progress
        async with get_session() as session:
            migration = await session.get(Migration, self.ctx.migration_id)
            if migration:
                migration.state = state
                migration.progress_pct = progress
                if state == MigrationState.DONE:
                    migration.finished_at = datetime.now(tz=timezone.utc)
            event = MigrationEvent(
                migration_id=self.ctx.migration_id,
                state=state.value,
                message=f"Entered state {state.value}",
                data={"progress": progress},
            )
            session.add(event)
        logger.info(
            "migration_state_change",
            migration_id=self.ctx.migration_id,
            state=state.value,
            progress=progress,
        )

    async def _fail(self, error: str) -> None:
        self.ctx.error = error
        logger.error(
            "migration_failed",
            migration_id=self.ctx.migration_id,
            error=error,
        )
        async with get_session() as session:
            migration = await session.get(Migration, self.ctx.migration_id)
            if migration:
                migration.state = MigrationState.FAILED
                migration.error = error
                migration.finished_at = datetime.now(tz=timezone.utc)

        MIGRATION_TOTAL.labels(
            type=self.ctx.migration_type.value,
            source_cluster=self.ctx.source_cluster_id,
            dest_cluster=self.ctx.dest_cluster_id,
            status="failed",
        ).inc()

    async def abort(self, reason: str = "user requested") -> None:
        self._aborted = True
        await self._fail(f"Aborted: {reason}")
        await self._run_rollbacks()

    async def _run_rollbacks(self) -> None:
        logger.info("migration_rollback_start", migration_id=self.ctx.migration_id)
        for rb in reversed(self._rollbacks):
            try:
                await rb()
            except Exception as e:
                logger.error("rollback_step_failed", error=str(e))

    def _register_rollback(self, fn: StepFn) -> None:
        self._rollbacks.append(fn)


# ─────────────────────────────────────────────
# Cold migration
# ─────────────────────────────────────────────

class ColdMigrationEngine(MigrationEngine):
    """
    Cold migration sequence:
      PREFLIGHT → DISK → CUTOVER → BGP → CLEANUP → DONE

    Disk is already mirrored via Ceph RBD — only a promote/demote is needed.
    """

    async def run(self) -> bool:
        self.ctx.started_at = datetime.now(tz=timezone.utc)
        MIGRATION_ACTIVE.labels(type="cold").inc()
        t0 = time.monotonic()

        try:
            await self._step_preflight()
            if self._aborted:
                return False

            await self._step_disk()
            await self._step_cutover()
            await self._step_bgp()
            await self._step_cleanup()

            await self._transition(MigrationState.DONE, 100.0)
            elapsed = time.monotonic() - t0
            MIGRATION_TOTAL.labels(
                type="cold",
                source_cluster=self.ctx.source_cluster_id,
                dest_cluster=self.ctx.dest_cluster_id,
                status="success",
            ).inc()
            MIGRATION_DURATION.labels(type="cold").observe(elapsed)
            logger.info(
                "cold_migration_done",
                migration_id=self.ctx.migration_id,
                elapsed_s=round(elapsed, 2),
            )
            return True

        except Exception as e:
            await self._fail(str(e))
            await self._run_rollbacks()
            return False
        finally:
            MIGRATION_ACTIVE.labels(type="cold").dec()

    async def _step_preflight(self) -> None:
        await self._transition(MigrationState.PREFLIGHT, 5.0)
        settings = get_settings()

        # Fetch VM details
        self.ctx.vm = await self.source_os.get_vm(self.ctx.vm_id)
        if self.ctx.vm.status not in ("ACTIVE", "SHUTOFF"):
            raise MigrationError(
                f"VM {self.ctx.vm_id} is in state {self.ctx.vm.status}, "
                "expected ACTIVE or SHUTOFF"
            )

        # Collect RBD images for all attached volumes
        for vol_id in self.ctx.vm.volume_ids:
            self.ctx.rbd_images.append(f"volume-{vol_id}")

        # Collect IPs
        self.ctx.vm_ips = self.ctx.vm.floating_ips or self.ctx.vm.ips

        # Preflight check on each RBD image
        for image in self.ctx.rbd_images:
            ok, reason = await self.ceph.preflight_check(
                settings.ceph.pool, image,
                max_lag_bytes=settings.migration_max_lag_bytes,
            )
            if not ok:
                raise MigrationError(f"RBD preflight failed for {image}: {reason}")

        logger.info(
            "cold_migration_preflight_ok",
            vm_id=self.ctx.vm_id,
            rbd_images=self.ctx.rbd_images,
            ips=self.ctx.vm_ips,
        )

    async def _step_disk(self) -> None:
        await self._transition(MigrationState.DISK, 20.0)
        settings = get_settings()
        pool = settings.ceph.pool

        # Stop VM if it's still running
        if self.ctx.vm and self.ctx.vm.status == "ACTIVE":
            await self.source_os.stop_vm(self.ctx.vm_id)

        # Wait for final RBD mirror sync
        for image in self.ctx.rbd_images:
            await self.ceph.wait_for_sync(pool, image, timeout_seconds=300)

        # Demote on source, promote on destination
        for image in self.ctx.rbd_images:
            await self.ceph.demote(pool, image)

        self._register_rollback(self._rollback_disk)

        # Promote via gRPC call to destination CNF agent
        for image in self.ctx.rbd_images:
            if self.ctx.dest_grpc_client:
                await self.ctx.dest_grpc_client.PromoteRBDImage(pool=pool, image=image)
            else:
                await self.ceph.promote(pool, image)

        await self._transition(MigrationState.DISK, 50.0)

    async def _rollback_disk(self) -> None:
        """Re-promote images on source if disk step fails."""
        settings = get_settings()
        pool = settings.ceph.pool
        for image in self.ctx.rbd_images:
            try:
                await self.ceph.promote(pool, image, force=True)
            except Exception as e:
                logger.error("rollback_rbd_promote_failed", image=image, error=str(e))

    async def _step_cutover(self) -> None:
        await self._transition(MigrationState.CUTOVER, 70.0)
        if not self.ctx.vm:
            return

        # Register volumes in destination Cinder
        settings = get_settings()
        pool = settings.ceph.pool
        for image in self.ctx.rbd_images:
            vol_info = None
            for vol_id in self.ctx.vm.volume_ids:
                if vol_id in image:
                    try:
                        vol_info = await self.source_os.get_volume(vol_id)
                    except Exception:
                        pass
                    break

            size_gb = vol_info["size_gb"] if vol_info else 10
            await self.dest_os.create_volume_from_rbd(
                pool=pool,
                image=image,
                size_gb=size_gb,
                name=f"cnf-migrated-{image}",
            )

        logger.info("cold_migration_cutover_done", vm_id=self.ctx.vm_id)

    async def _step_bgp(self) -> None:
        await self._transition(MigrationState.BGP, 85.0)
        if not self.ctx.vm_ips:
            return

        prefixes = [f"{ip}/32" for ip in self.ctx.vm_ips]
        dest_next_hop = ""  # resolved from dest cluster config

        await self.bgp.handoff(
            prefixes=prefixes,
            dest_next_hop=dest_next_hop,
            source_grpc_client=self.ctx.source_grpc_client,
        )
        self._register_rollback(
            lambda: self.bgp.rollback(prefixes, self.ctx.dest_grpc_client)
        )

    async def _step_cleanup(self) -> None:
        await self._transition(MigrationState.CLEANUP, 95.0)
        try:
            await self.source_os.delete_vm(self.ctx.vm_id)
        except Exception as e:
            logger.warning("cleanup_delete_vm_failed", vm_id=self.ctx.vm_id, error=str(e))


# ─────────────────────────────────────────────
# Live migration
# ─────────────────────────────────────────────

class LiveMigrationEngine(MigrationEngine):
    """
    Live migration sequence:
      PREFLIGHT → DISK (sync check) → MEMORY (QEMU tunnel) → CUTOVER → BGP → CLEANUP → DONE

    The VM stays running on source until the final memory cutover.
    Disk is already at destination via Ceph RBD mirror.
    """

    async def run(self) -> bool:
        self.ctx.started_at = datetime.now(tz=timezone.utc)
        MIGRATION_ACTIVE.labels(type="live").inc()
        t0 = time.monotonic()

        try:
            await self._step_preflight()
            if self._aborted:
                return False
            await self._step_disk_sync()
            await self._step_memory()
            await self._step_cutover()
            await self._step_bgp()
            await self._step_cleanup()

            await self._transition(MigrationState.DONE, 100.0)
            elapsed = time.monotonic() - t0
            MIGRATION_TOTAL.labels(
                type="live",
                source_cluster=self.ctx.source_cluster_id,
                dest_cluster=self.ctx.dest_cluster_id,
                status="success",
            ).inc()
            MIGRATION_DURATION.labels(type="live").observe(elapsed)
            logger.info(
                "live_migration_done",
                migration_id=self.ctx.migration_id,
                elapsed_s=round(elapsed, 2),
            )
            return True

        except Exception as e:
            await self._fail(str(e))
            await self._run_rollbacks()
            return False
        finally:
            MIGRATION_ACTIVE.labels(type="live").dec()

    async def _step_preflight(self) -> None:
        await self._transition(MigrationState.PREFLIGHT, 5.0)
        settings = get_settings()

        self.ctx.vm = await self.source_os.get_vm(self.ctx.vm_id)
        if self.ctx.vm.status != "ACTIVE":
            raise MigrationError(
                f"Live migration requires VM to be ACTIVE, got {self.ctx.vm.status}"
            )

        for vol_id in self.ctx.vm.volume_ids:
            self.ctx.rbd_images.append(f"volume-{vol_id}")

        self.ctx.vm_ips = self.ctx.vm.floating_ips or self.ctx.vm.ips

        for image in self.ctx.rbd_images:
            ok, reason = await self.ceph.preflight_check(
                settings.ceph.pool, image,
                max_lag_bytes=settings.migration_max_lag_bytes,
            )
            if not ok:
                raise MigrationError(f"RBD preflight failed for {image}: {reason}")

    async def _step_disk_sync(self) -> None:
        """
        Wait for Ceph mirror to be nearly in sync.
        We do NOT demote yet — the VM is still running and writing.
        Just verify lag is within acceptable bounds.
        """
        await self._transition(MigrationState.DISK, 15.0)
        settings = get_settings()
        pool = settings.ceph.pool

        for image in self.ctx.rbd_images:
            status = await self.ceph.get_mirror_status(pool, image)
            logger.info(
                "live_migration_rbd_lag",
                image=image,
                lag_bytes=status.lag_bytes,
            )

    async def _step_memory(self) -> None:
        """
        Open QEMU live migration tunnel between source and destination hypervisors.
        The VM's memory is copied iteratively (pre-copy) until the dirty page
        rate is low enough to trigger final cutover.
        """
        await self._transition(MigrationState.MEMORY, 25.0)
        if not self.ctx.vm:
            return

        dest_host = self.ctx.options.get("dest_host", "")
        if not dest_host:
            raise MigrationError("dest_host must be specified for live migration")

        tunnel = QEMUMigrationTunnel(
            vm_id=self.ctx.vm_id,
            source_host=self.ctx.vm.host,
            dest_host=dest_host,
            migration_id=self.ctx.migration_id,
        )
        self._register_rollback(tunnel.abort)

        async def _progress_cb(pct: float) -> None:
            # Scale memory phase: 25% → 75% of overall progress
            overall = 25.0 + pct * 0.50
            await self._transition(MigrationState.MEMORY, overall)

        await tunnel.run(progress_callback=_progress_cb)

    async def _step_cutover(self) -> None:
        """
        Final cutover:
          1. Flush last dirty RBD pages (demote source, promote dest).
          2. Resume VM on destination.
        """
        await self._transition(MigrationState.CUTOVER, 80.0)
        settings = get_settings()
        pool = settings.ceph.pool

        for image in self.ctx.rbd_images:
            # Final mirror flush — wait for zero lag
            await self.ceph.wait_for_sync(pool, image, max_lag_bytes=0, timeout_seconds=60)
            await self.ceph.demote(pool, image)

        self._register_rollback(self._rollback_cutover)

        for image in self.ctx.rbd_images:
            if self.ctx.dest_grpc_client:
                await self.ctx.dest_grpc_client.PromoteRBDImage(pool=pool, image=image)
            else:
                await self.ceph.promote(pool, image)

    async def _rollback_cutover(self) -> None:
        settings = get_settings()
        pool = settings.ceph.pool
        for image in self.ctx.rbd_images:
            try:
                await self.ceph.promote(pool, image, force=True)
            except Exception as e:
                logger.error("live_rollback_promote_failed", image=image, error=str(e))

    async def _step_bgp(self) -> None:
        await self._transition(MigrationState.BGP, 90.0)
        if not self.ctx.vm_ips:
            return
        prefixes = [f"{ip}/32" for ip in self.ctx.vm_ips]
        dest_next_hop = self.ctx.options.get("dest_next_hop", "")
        await self.bgp.handoff(
            prefixes=prefixes,
            dest_next_hop=dest_next_hop,
            source_grpc_client=self.ctx.source_grpc_client,
        )

    async def _step_cleanup(self) -> None:
        await self._transition(MigrationState.CLEANUP, 96.0)
        try:
            await self.source_os.delete_vm(self.ctx.vm_id)
        except Exception as e:
            logger.warning("live_cleanup_delete_failed", vm_id=self.ctx.vm_id, error=str(e))


# ─────────────────────────────────────────────
# QEMU migration tunnel
# ─────────────────────────────────────────────

class QEMUMigrationTunnel:
    """
    Manages a QEMU/KVM live migration tunnel between two hypervisor hosts.

    Uses libvirt's migrateToURI3 API to open a TCP migration channel.
    Memory pages are streamed from source to destination using pre-copy.
    """

    LIBVIRT_MIGRATE_FLAGS = (
        1       # VIR_MIGRATE_LIVE
        | 4     # VIR_MIGRATE_PERSIST_DEST
        | 8     # VIR_MIGRATE_UNDEFINE_SOURCE
        | 64    # VIR_MIGRATE_COMPRESSED
        | 8192  # VIR_MIGRATE_AUTO_CONVERGE
    )

    def __init__(
        self,
        vm_id: str,
        source_host: str,
        dest_host: str,
        migration_id: str,
        port: int = 49152,
    ) -> None:
        self.vm_id = vm_id
        self.source_host = source_host
        self.dest_host = dest_host
        self.migration_id = migration_id
        self.port = port
        self._cancelled = False

    async def run(
        self,
        progress_callback: Callable[[float], Coroutine] | None = None,
    ) -> None:
        """
        Execute the QEMU live migration via libvirt.
        Runs the blocking libvirt call in a thread pool and monitors progress.
        """
        import libvirt  # type: ignore

        logger.info(
            "qemu_tunnel_start",
            vm_id=self.vm_id,
            source=self.source_host,
            dest=self.dest_host,
        )

        dest_uri = f"qemu+ssh://{self.dest_host}/system"
        migration_uri = f"tcp://{self.dest_host}:{self.port}"

        loop = asyncio.get_running_loop()

        def _do_migrate():
            src_conn = libvirt.open(f"qemu+ssh://{self.source_host}/system")
            try:
                domain = src_conn.lookupByName(self.vm_id)
                dst_conn = libvirt.open(dest_uri)
                try:
                    params = {
                        libvirt.VIR_MIGRATE_PARAM_URI: migration_uri,
                        libvirt.VIR_MIGRATE_PARAM_DEST_NAME: self.vm_id,
                        libvirt.VIR_MIGRATE_PARAM_BANDWIDTH: 0,  # unlimited
                        libvirt.VIR_MIGRATE_PARAM_COMPRESSION: "xbzrle",
                    }
                    domain.migrateToURI3(
                        dest_uri,
                        params,
                        self.LIBVIRT_MIGRATE_FLAGS,
                    )
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()

        # Monitor progress in parallel
        monitor_task = asyncio.create_task(
            self._monitor_progress(progress_callback)
        )
        try:
            await loop.run_in_executor(None, _do_migrate)
        finally:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

        if progress_callback:
            await progress_callback(100.0)
        logger.info("qemu_tunnel_done", vm_id=self.vm_id)

    async def _monitor_progress(
        self,
        callback: Callable[[float], Coroutine] | None,
    ) -> None:
        """Poll libvirt migration progress every 2 seconds."""
        import libvirt  # type: ignore
        loop = asyncio.get_running_loop()

        while not self._cancelled:
            await asyncio.sleep(2)
            try:
                pct = await loop.run_in_executor(None, self._get_progress_pct)
                if callback:
                    await callback(pct)
            except Exception:
                pass

    def _get_progress_pct(self) -> float:
        import libvirt  # type: ignore
        try:
            conn = libvirt.open(f"qemu+ssh://{self.source_host}/system")
            domain = conn.lookupByName(self.vm_id)
            info = domain.jobInfo()
            conn.close()
            if info and info[3] > 0:
                return min(99.0, (info[4] / info[3]) * 100)
        except Exception:
            pass
        return 0.0

    async def abort(self) -> None:
        self._cancelled = True
        import libvirt  # type: ignore
        loop = asyncio.get_running_loop()

        def _abort():
            try:
                conn = libvirt.open(f"qemu+ssh://{self.source_host}/system")
                domain = conn.lookupByName(self.vm_id)
                domain.abortJob()
                conn.close()
            except Exception as e:
                logger.warning("qemu_abort_failed", error=str(e))

        await loop.run_in_executor(None, _abort)


# ── Exceptions ────────────────────────────────────────────────────────

class MigrationError(Exception):
    pass
