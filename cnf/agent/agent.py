"""
cnf.agent.agent — Main CNF agent process.

Responsibilities:
  - Start gRPC server (CNFControl + CNFPeer services)
  - Start FastAPI REST server
  - Participate in Raft leader election
  - Collect and broadcast cluster metrics
  - Accept and route migration requests
"""
from __future__ import annotations

import asyncio
import signal
import uuid
from datetime import datetime, timezone

from cnf.agent.raft import NodeRole, RaftElection
from cnf.config import get_settings
from cnf.db.session import dispose_engine, init_db
from cnf.db.models import Cluster, ClusterRole, ClusterStatus
from cnf.db.session import get_session
from cnf.openstack.client import OpenStackClient
from cnf.scheduler.scheduler import Scheduler
from cnf.utils.logging import configure_logging, get_logger, start_metrics_server

logger = get_logger(__name__)


class CNFAgent:
    """
    The CNF agent runs on each OpenStack controller node.
    It is both a gRPC server and a REST API server.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._os_client = OpenStackClient()
        self._election: RaftElection | None = None
        self._scheduler: Scheduler | None = None
        self._grpc_server = None
        self._api_server = None
        self._metrics_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        configure_logging()
        logger.info(
            "cnf_agent_starting",
            cluster_id=self.settings.cluster_id,
            cluster_name=self.settings.cluster_name,
        )

        await init_db()
        await self._upsert_self_cluster()

        # Start Prometheus metrics
        start_metrics_server()

        # Start Raft election
        self._election = RaftElection(
            cluster_id=self.settings.cluster_id,
            grpc_addr=self.settings.cluster_grpc_addr,
            config=self.settings.raft,
            on_become_master=self._on_become_master,
            on_lose_master=self._on_lose_master,
        )
        await self._election.start()

        # Start gRPC server
        await self._start_grpc()

        # Start REST API
        await self._start_api()

        # Start periodic metrics collection
        self._metrics_task = asyncio.create_task(self._metrics_loop())

        # Start scheduler
        self._scheduler = Scheduler(os_client=self._os_client)
        asyncio.create_task(self._scheduler.run())

        self._running = True
        logger.info("cnf_agent_started", cluster_id=self.settings.cluster_id)

        # Handle OS signals for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

    async def stop(self) -> None:
        logger.info("cnf_agent_stopping")
        self._running = False

        if self._metrics_task:
            self._metrics_task.cancel()

        if self._election:
            await self._election.stop()

        if self._grpc_server:
            self._grpc_server.stop(grace=5)

        await dispose_engine()
        logger.info("cnf_agent_stopped")

    async def run_forever(self) -> None:
        await self.start()
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    # ── Cluster registration ──────────────────────────────────────────

    async def _upsert_self_cluster(self) -> None:
        """Register/update this cluster's record in the local database."""
        async with get_session() as session:
            cluster = await session.get(Cluster, self.settings.cluster_id)
            if cluster is None:
                cluster = Cluster(
                    id=self.settings.cluster_id,
                    name=self.settings.cluster_name,
                    auth_url=self.settings.openstack.auth_url,
                    region=self.settings.openstack.region_name,
                    grpc_addr=self.settings.cluster_grpc_addr,
                    bgp_as=self.settings.bgp.as_number,
                )
                session.add(cluster)
            cluster.status = ClusterStatus.ONLINE
            cluster.grpc_addr = self.settings.cluster_grpc_addr

    # ── Raft callbacks ────────────────────────────────────────────────

    async def _on_become_master(self) -> None:
        logger.info("became_master", cluster_id=self.settings.cluster_id)
        async with get_session() as session:
            cluster = await session.get(Cluster, self.settings.cluster_id)
            if cluster:
                cluster.role = ClusterRole.MASTER
        if self._scheduler:
            self._scheduler.set_master(True)

    async def _on_lose_master(self) -> None:
        logger.info("lost_master", cluster_id=self.settings.cluster_id)
        async with get_session() as session:
            cluster = await session.get(Cluster, self.settings.cluster_id)
            if cluster:
                cluster.role = ClusterRole.WORKER
        if self._scheduler:
            self._scheduler.set_master(False)

    # ── gRPC server ───────────────────────────────────────────────────

    async def _start_grpc(self) -> None:
        from cnf.grpc.server import create_grpc_server
        self._grpc_server = await create_grpc_server(agent=self)
        logger.info("grpc_server_started", port=self.settings.grpc.port)

    # ── REST API ──────────────────────────────────────────────────────

    async def _start_api(self) -> None:
        import uvicorn
        from cnf.api.app import create_app

        app = create_app(agent=self)
        config = uvicorn.Config(
            app=app,
            host=self.settings.api.host,
            port=self.settings.api.port,
            log_config=None,   # We use structlog
            access_log=False,
        )
        server = uvicorn.Server(config)
        asyncio.create_task(server.serve())
        logger.info("rest_api_started", port=self.settings.api.port)

    # ── Metrics collection ────────────────────────────────────────────

    async def _metrics_loop(self) -> None:
        """Collect local cluster metrics and store in DB every N seconds."""
        from cnf.db.models import ClusterMetric
        interval = self.settings.metrics.collection_interval_seconds

        while self._running:
            await asyncio.sleep(interval)
            try:
                raw = await self._os_client.get_cluster_metrics()
                async with get_session() as session:
                    metric = ClusterMetric(
                        cluster_id=self.settings.cluster_id,
                        cpu_used_pct=raw["cpu_used_pct"],
                        ram_used_pct=raw["ram_used_pct"],
                        disk_used_pct=raw["disk_used_pct"],
                        vm_count=raw["vm_count"],
                        network_bw_mbps=raw["network_bw_mbps"],
                    )
                    session.add(metric)

                from cnf.utils.logging import CLUSTER_CPU, CLUSTER_RAM, CLUSTER_VMS
                CLUSTER_CPU.labels(cluster_id=self.settings.cluster_id).set(
                    raw["cpu_used_pct"]
                )
                CLUSTER_RAM.labels(cluster_id=self.settings.cluster_id).set(
                    raw["ram_used_pct"]
                )
                CLUSTER_VMS.labels(
                    cluster_id=self.settings.cluster_id,
                    cluster_name=self.settings.cluster_name,
                ).set(raw["vm_count"])

            except Exception as e:
                logger.warning("metrics_collection_failed", error=str(e))

    # ── Public helpers ────────────────────────────────────────────────

    @property
    def is_master(self) -> bool:
        return self._election is not None and self._election.is_master

    @property
    def master_grpc_addr(self) -> str:
        """Return the gRPC address of the current master (for request proxying)."""
        if self._election:
            peers = {}  # populated via async get_peers()
            return peers.get(self._election.master_id, "")
        return ""

    async def get_peers(self) -> dict[str, str]:
        if self._election:
            return await self._election.get_peers()
        return {}
