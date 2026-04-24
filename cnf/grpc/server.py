"""
cnf.grpc.server — gRPC server implementing CNFControl and CNFPeer.
Stubs are generated from proto/cnf.proto via grpc_tools.protoc.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import grpc
from grpc import aio

from cnf.config import get_settings
from cnf.utils.logging import get_logger, GRPC_REQUESTS

if TYPE_CHECKING:
    from cnf.agent.agent import CNFAgent

logger = get_logger(__name__)


async def create_grpc_server(agent: "CNFAgent") -> aio.Server:
    settings = get_settings()
    cfg = settings.grpc

    server = aio.server(
        options=[
            ("grpc.max_send_message_length", cfg.max_message_length),
            ("grpc.max_receive_message_length", cfg.max_message_length),
            ("grpc.keepalive_time_ms", cfg.keepalive_time_ms),
            ("grpc.keepalive_timeout_ms", cfg.keepalive_timeout_ms),
        ]
    )

    # Register servicers (generated stubs must be present after proto compile)
    try:
        from cnf.grpc import cnf_pb2_grpc
        cnf_pb2_grpc.add_CNFControlServicer_to_server(
            CNFControlServicer(agent), server
        )
        cnf_pb2_grpc.add_CNFPeerServicer_to_server(
            CNFPeerServicer(agent), server
        )
    except ImportError:
        logger.warning(
            "grpc_stubs_not_generated",
            hint="Run: python -m grpc_tools.protoc -I proto --python_out=cnf/grpc "
                 "--grpc_python_out=cnf/grpc proto/cnf.proto",
        )

    addr = f"[::]:{cfg.port}"

    if cfg.tls_enabled:
        try:
            with open(cfg.tls_cert, "rb") as f:
                cert = f.read()
            with open(cfg.tls_key, "rb") as f:
                key = f.read()
            with open(cfg.tls_ca, "rb") as f:
                ca = f.read()
            creds = grpc.ssl_server_credentials(
                [(key, cert)],
                root_certificates=ca,
                require_client_auth=True,
            )
            server.add_secure_port(addr, creds)
            logger.info("grpc_tls_enabled", port=cfg.port)
        except FileNotFoundError:
            logger.warning("grpc_tls_certs_missing_falling_back_to_insecure")
            server.add_insecure_port(addr)
    else:
        server.add_insecure_port(addr)

    await server.start()
    return server


# ─────────────────────────────────────────────
# CNFControl servicer
# ─────────────────────────────────────────────

class CNFControlServicer:
    """Handles commands issued by the master to this worker node."""

    def __init__(self, agent: "CNFAgent") -> None:
        self.agent = agent

    async def GetClusterInfo(self, request, context):
        GRPC_REQUESTS.labels(method="GetClusterInfo", status="ok").inc()
        try:
            from cnf.grpc import cnf_pb2
            settings = self.agent.settings
            return cnf_pb2.GetClusterInfoResponse(
                cluster=cnf_pb2.ClusterInfo(
                    id=settings.cluster_id,
                    name=settings.cluster_name,
                    role="master" if self.agent.is_master else "worker",
                    auth_url=settings.openstack.auth_url,
                    region=settings.openstack.region_name,
                    grpc_addr=settings.cluster_grpc_addr,
                    status="online",
                )
            )
        except Exception as e:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            raise

    async def GetClusterMetrics(self, request, context):
        GRPC_REQUESTS.labels(method="GetClusterMetrics", status="ok").inc()
        try:
            from cnf.grpc import cnf_pb2
            raw = await self.agent._os_client.get_cluster_metrics()
            return cnf_pb2.GetClusterMetricsResponse(
                metrics=cnf_pb2.ClusterMetrics(
                    cluster_id=self.agent.settings.cluster_id,
                    cpu_used_pct=raw["cpu_used_pct"],
                    ram_used_pct=raw["ram_used_pct"],
                    disk_used_pct=raw["disk_used_pct"],
                    vm_count=raw["vm_count"],
                    network_bw_mbps=raw["network_bw_mbps"],
                )
            )
        except Exception as e:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            raise

    async def ExecuteMigration(self, request, context):
        GRPC_REQUESTS.labels(method="ExecuteMigration", status="ok").inc()
        from cnf.grpc import cnf_pb2
        # Trigger cold migration via Celery task
        from cnf.tasks.migration_tasks import run_cold_migration
        run_cold_migration.delay(
            migration_id=request.migration_id,
            vm_id=request.vm_id,
            source_cluster=request.source_cluster,
            dest_cluster=request.dest_cluster,
            options=dict(request.options),
        )
        return cnf_pb2.MigrationResponse(
            migration_id=request.migration_id,
            accepted=True,
            message="Migration queued",
        )

    async def ExecuteLiveMigration(self, request, context):
        GRPC_REQUESTS.labels(method="ExecuteLiveMigration", status="ok").inc()
        from cnf.grpc import cnf_pb2
        from cnf.tasks.migration_tasks import run_live_migration
        run_live_migration.delay(
            migration_id=request.migration_id,
            vm_id=request.vm_id,
            source_cluster=request.source_cluster,
            dest_cluster=request.dest_cluster,
            options=dict(request.options),
        )
        return cnf_pb2.MigrationResponse(
            migration_id=request.migration_id,
            accepted=True,
            message="Live migration queued",
        )

    async def PromoteRBDImage(self, request, context):
        from cnf.grpc import cnf_pb2
        from cnf.storage.ceph import CephRBDClient
        ceph = CephRBDClient()
        try:
            await ceph.promote(request.pool, request.image, force=request.force)
            return cnf_pb2.RBDPromoteResponse(success=True, message="promoted")
        except Exception as e:
            return cnf_pb2.RBDPromoteResponse(success=False, message=str(e))

    async def DemoteRBDImage(self, request, context):
        from cnf.grpc import cnf_pb2
        from cnf.storage.ceph import CephRBDClient
        ceph = CephRBDClient()
        try:
            await ceph.demote(request.pool, request.image)
            return cnf_pb2.RBDDemoteResponse(success=True, message="demoted")
        except Exception as e:
            return cnf_pb2.RBDDemoteResponse(success=False, message=str(e))

    async def GetRBDMirrorLag(self, request, context):
        from cnf.grpc import cnf_pb2
        from cnf.storage.ceph import CephRBDClient
        ceph = CephRBDClient()
        status = await ceph.get_mirror_status(request.pool, request.image)
        return cnf_pb2.RBDMirrorLagResponse(
            lag_bytes=status.lag_bytes,
            lag_seconds=status.lag_seconds,
            synced=status.synced,
        )

    async def AnnounceIP(self, request, context):
        from cnf.grpc import cnf_pb2
        from cnf.network.bgp import FRRClient, BGPRoute
        frr = FRRClient()
        try:
            await frr.announce(BGPRoute(prefix=request.prefix, next_hop=request.next_hop))
            return cnf_pb2.BGPAnnounceResponse(success=True)
        except Exception as e:
            return cnf_pb2.BGPAnnounceResponse(success=False)

    async def WithdrawIP(self, request, context):
        from cnf.grpc import cnf_pb2
        from cnf.network.bgp import FRRClient
        frr = FRRClient()
        try:
            await frr.withdraw(request.prefix)
            return cnf_pb2.BGPWithdrawResponse(success=True)
        except Exception as e:
            return cnf_pb2.BGPWithdrawResponse(success=False)


# ─────────────────────────────────────────────
# CNFPeer servicer
# ─────────────────────────────────────────────

class CNFPeerServicer:
    """Handles Raft peer messages — heartbeats, vote requests, state sync."""

    def __init__(self, agent: "CNFAgent") -> None:
        self.agent = agent

    async def Heartbeat(self, request, context):
        from cnf.grpc import cnf_pb2
        return cnf_pb2.HeartbeatResponse(ok=True, term=0)

    async def RequestVote(self, request, context):
        from cnf.grpc import cnf_pb2
        # etcd handles actual election; this is for direct Raft fallback
        return cnf_pb2.RequestVoteResponse(vote_granted=True, term=request.term)

    async def AppendEntries(self, request, context):
        from cnf.grpc import cnf_pb2
        return cnf_pb2.AppendEntriesResponse(success=True, term=request.term)

    async def SyncState(self, request, context):
        from cnf.grpc import cnf_pb2
        from cnf.db.session import get_session
        from cnf.db.models import Cluster, Migration, MigrationState
        from sqlalchemy import select

        clusters_data = []
        migrations_data = []

        async with get_session() as session:
            c_result = await session.execute(select(Cluster))
            for c in c_result.scalars().all():
                clusters_data.append(cnf_pb2.ClusterInfo(
                    id=c.id, name=c.name, role=c.role.value,
                    auth_url=c.auth_url, region=c.region,
                    grpc_addr=c.grpc_addr, status=c.status.value,
                ))

            m_result = await session.execute(
                select(Migration).where(
                    Migration.state.not_in([
                        MigrationState.DONE,
                        MigrationState.FAILED,
                        MigrationState.ABORTED,
                    ])
                )
            )
            for m in m_result.scalars().all():
                migrations_data.append(cnf_pb2.MigrationStatus(
                    migration_id=m.id, vm_id=m.vm_id,
                    source_cluster=m.source_cluster_id,
                    dest_cluster=m.dest_cluster_id,
                    type=m.type.value, state=m.state.value,
                    progress_pct=m.progress_pct,
                ))

        return cnf_pb2.SyncStateResponse(
            clusters=clusters_data,
            active_migrations=migrations_data,
        )
