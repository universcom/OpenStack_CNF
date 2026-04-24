"""
cnf.agent.raft — Leader election via etcd distributed locking.

Each CNF node competes for a lease in etcd. The holder of the lease
is the master. On lease expiry or node failure, any worker can acquire
it and become the new master.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
from typing import Any

import etcd3  # type: ignore

from cnf.config import RaftConfig, get_settings
from cnf.utils.logging import get_logger, RAFT_LEADER_CHANGES

logger = get_logger(__name__)

OnBecomeMasterFn = Callable[[], Coroutine[Any, Any, None]]
OnLoseMasterFn   = Callable[[], Coroutine[Any, Any, None]]


class NodeRole(str, Enum):
    MASTER = "master"
    WORKER = "worker"


@dataclass
class ElectionState:
    role: NodeRole = NodeRole.WORKER
    master_id: str = ""
    term: int = 0
    last_heartbeat: float = 0.0


class RaftElection:
    """
    Simplified leader election using etcd lease-based locking.

    The master holds a lease on key /cnf/raft/master. Workers watch
    the key and attempt to acquire it when it expires.
    """

    MASTER_KEY = "/cnf/raft/master"
    PEERS_PREFIX = "/cnf/raft/peers/"

    def __init__(
        self,
        cluster_id: str,
        grpc_addr: str,
        config: RaftConfig | None = None,
        on_become_master: OnBecomeMasterFn | None = None,
        on_lose_master: OnLoseMasterFn | None = None,
    ) -> None:
        self.cluster_id = cluster_id
        self.grpc_addr = grpc_addr
        self.cfg = config or get_settings().raft
        self.on_become_master = on_become_master
        self.on_lose_master = on_lose_master

        self.state = ElectionState()
        self._client: etcd3.Etcd3Client | None = None
        self._lease: Any = None
        self._running = False
        self._loop_task: asyncio.Task | None = None

    def _get_client(self) -> etcd3.Etcd3Client:
        if self._client is None:
            endpoint = self.cfg.etcd_endpoints[0]
            host, _, port = endpoint.partition(":")
            kwargs: dict[str, Any] = {
                "host": host,
                "port": int(port) if port else 2379,
                "timeout": 5,
            }
            if self.cfg.etcd_tls_cert and self.cfg.etcd_tls_key:
                kwargs.update({
                    "ca_cert": self.cfg.etcd_tls_ca,
                    "cert_key": self.cfg.etcd_tls_key,
                    "cert_cert": self.cfg.etcd_tls_cert,
                })
            self._client = etcd3.client(**kwargs)
        return self._client

    async def start(self) -> None:
        self._running = True
        await self._register_peer()
        self._loop_task = asyncio.create_task(self._election_loop())
        logger.info("raft_election_started", cluster_id=self.cluster_id)

    async def stop(self) -> None:
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        await self._revoke_lease()
        logger.info("raft_election_stopped", cluster_id=self.cluster_id)

    async def _register_peer(self) -> None:
        """Announce this node in the peers directory."""
        loop = asyncio.get_running_loop()
        client = self._get_client()
        peer_key = f"{self.PEERS_PREFIX}{self.cluster_id}"
        await loop.run_in_executor(
            None,
            lambda: client.put(peer_key, self.grpc_addr),
        )

    async def _election_loop(self) -> None:
        while self._running:
            try:
                await self._try_acquire_master()
                await asyncio.sleep(self.cfg.heartbeat_interval_ms / 1000)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("election_loop_error", error=str(e))
                await asyncio.sleep(2)

    async def _try_acquire_master(self) -> None:
        loop = asyncio.get_running_loop()
        client = self._get_client()
        ttl = self.cfg.lease_ttl_seconds

        def _acquire():
            # Atomic compare-and-swap: only set if key doesn't exist
            lease = client.lease(ttl=ttl)
            success, _ = client.transaction(
                compare=[client.transactions.version(self.MASTER_KEY) == 0],
                success=[client.transactions.put(self.MASTER_KEY, self.cluster_id, lease=lease)],
                failure=[],
            )
            return success, lease

        def _read_master():
            val, _ = client.get(self.MASTER_KEY)
            return val.decode() if val else ""

        def _refresh_lease(lease):
            list(client.refresh_lease(lease.id))

        if self.state.role == NodeRole.MASTER and self._lease:
            # Refresh our existing lease
            try:
                await loop.run_in_executor(None, lambda: _refresh_lease(self._lease))
                self.state.last_heartbeat = time.time()
            except Exception as e:
                logger.warning("master_lease_refresh_failed", error=str(e))
                await self._on_lose_master()
        else:
            # Try to become master
            try:
                success, lease = await loop.run_in_executor(None, _acquire)
                if success:
                    self._lease = lease
                    await self._on_become_master()
                else:
                    master_id = await loop.run_in_executor(None, _read_master)
                    if master_id and self.state.master_id != master_id:
                        self.state.master_id = master_id
                        logger.info("raft_master_observed", master_id=master_id)
            except Exception as e:
                logger.error("raft_acquire_error", error=str(e))

    async def _on_become_master(self) -> None:
        prev_role = self.state.role
        self.state.role = NodeRole.MASTER
        self.state.master_id = self.cluster_id
        self.state.term += 1
        self.state.last_heartbeat = time.time()

        logger.info(
            "raft_became_master",
            cluster_id=self.cluster_id,
            term=self.state.term,
        )
        RAFT_LEADER_CHANGES.inc()

        if prev_role != NodeRole.MASTER and self.on_become_master:
            await self.on_become_master()

    async def _on_lose_master(self) -> None:
        logger.warning("raft_lost_master", cluster_id=self.cluster_id)
        self.state.role = NodeRole.WORKER
        self._lease = None

        if self.on_lose_master:
            await self.on_lose_master()

    async def _revoke_lease(self) -> None:
        if self._lease and self._client:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self._client.revoke_lease(self._lease.id),
                )
            except Exception:
                pass

    # ── Public API ────────────────────────────────────────────────────

    @property
    def is_master(self) -> bool:
        return self.state.role == NodeRole.MASTER

    @property
    def master_id(self) -> str:
        return self.state.master_id

    async def get_peers(self) -> dict[str, str]:
        """Return {cluster_id: grpc_addr} for all registered peers."""
        loop = asyncio.get_running_loop()
        client = self._get_client()

        def _list():
            return list(client.get_prefix(self.PEERS_PREFIX))

        entries = await loop.run_in_executor(None, _list)
        peers: dict[str, str] = {}
        for value, meta in entries:
            key = meta.key.decode()
            cluster_id = key.removeprefix(self.PEERS_PREFIX)
            peers[cluster_id] = value.decode()
        return peers

    async def force_elect(self, target_cluster_id: str) -> bool:
        """
        Admin-initiated master transfer.
        Revokes current master lease, allowing target to win next election.
        """
        if self.state.master_id != self.cluster_id:
            logger.warning("force_elect_not_master")
            return False
        await self._revoke_lease()
        logger.info("force_elect_lease_revoked", target=target_cluster_id)
        return True
