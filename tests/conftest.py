"""
tests/conftest.py — shared pytest fixtures for CNF test suite.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cnf.config import Settings
from cnf.db.models import Base, Cluster, ClusterRole, ClusterStatus


# ─────────────────────────────────────────────
# Settings override for tests
# ─────────────────────────────────────────────

@pytest.fixture(autouse=True)
def override_settings(monkeypatch):
    """Replace settings with in-memory / test-safe defaults."""
    monkeypatch.setenv("CNF_CLUSTER_ID",    str(uuid.uuid4()))
    monkeypatch.setenv("CNF_CLUSTER_NAME",  "test-cluster-1")
    monkeypatch.setenv("CNF_CLUSTER_GRPC_ADDR", "localhost:50051")
    monkeypatch.setenv("CNF_DB_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("CNF_METRICS_ENABLED", "false")
    monkeypatch.setenv("CNF_BGP_ENABLED", "false")
    monkeypatch.setenv("CNF_GRPC_TLS_ENABLED", "false")


# ─────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session


# ─────────────────────────────────────────────
# Sample cluster fixture
# ─────────────────────────────────────────────

@pytest_asyncio.fixture
async def cluster_os1(db_session: AsyncSession) -> Cluster:
    c = Cluster(
        id=str(uuid.uuid4()),
        name="openstack-1",
        auth_url="http://os1.example.com:5000/v3",
        region="RegionOne",
        grpc_addr="os1.example.com:50051",
        role=ClusterRole.MASTER,
        status=ClusterStatus.ONLINE,
        bgp_as=65001,
    )
    db_session.add(c)
    await db_session.commit()
    return c


@pytest_asyncio.fixture
async def cluster_os2(db_session: AsyncSession) -> Cluster:
    c = Cluster(
        id=str(uuid.uuid4()),
        name="openstack-2",
        auth_url="http://os2.example.com:5000/v3",
        region="RegionOne",
        grpc_addr="os2.example.com:50051",
        role=ClusterRole.WORKER,
        status=ClusterStatus.ONLINE,
        bgp_as=65002,
    )
    db_session.add(c)
    await db_session.commit()
    return c


# ─────────────────────────────────────────────
# Mock OpenStack client
# ─────────────────────────────────────────────

@pytest.fixture
def mock_os_client():
    from cnf.openstack.client import VMDetails
    client = AsyncMock()
    client.get_vm.return_value = VMDetails(
        id="vm-uuid-001",
        name="test-vm",
        status="ACTIVE",
        host="compute-1.os1.local",
        flavor_id="m1.small",
        image_id="img-001",
        ips=["10.0.1.50"],
        floating_ips=["192.168.100.10"],
        volume_ids=["vol-001"],
        security_groups=["default"],
    )
    client.list_vms.return_value = [client.get_vm.return_value]
    client.get_cluster_metrics.return_value = {
        "cpu_used_pct": 45.0,
        "ram_used_pct": 60.0,
        "disk_used_pct": 30.0,
        "vm_count": 5,
        "network_bw_mbps": 100.0,
    }
    client.stop_vm.return_value = None
    client.delete_vm.return_value = None
    return client


# ─────────────────────────────────────────────
# Mock Ceph RBD client
# ─────────────────────────────────────────────

@pytest.fixture
def mock_ceph():
    from cnf.storage.ceph import MirrorStatus
    ceph = AsyncMock()
    ceph.preflight_check.return_value = (True, "ok")
    ceph.get_mirror_status.return_value = MirrorStatus(
        image="volume-vol-001",
        pool="vms",
        state="up+replaying",
        description="replaying",
        lag_bytes=0,
        lag_seconds=0,
        synced=True,
    )
    ceph.wait_for_sync.return_value = ceph.get_mirror_status.return_value
    ceph.demote.return_value = None
    ceph.promote.return_value = None
    return ceph


# ─────────────────────────────────────────────
# Mock BGP manager
# ─────────────────────────────────────────────

@pytest.fixture
def mock_bgp():
    bgp = AsyncMock()
    bgp.handoff.return_value = None
    bgp.rollback.return_value = None
    return bgp


# ─────────────────────────────────────────────
# FastAPI test client
# ─────────────────────────────────────────────

@pytest_asyncio.fixture
async def api_client(mock_os_client) -> AsyncGenerator[AsyncClient, None]:
    mock_agent = MagicMock()
    mock_agent.is_master = True
    mock_agent.settings.cluster_id = str(uuid.uuid4())
    mock_agent.get_peers = AsyncMock(return_value={})
    mock_agent._election = MagicMock()
    mock_agent._election.master_id = mock_agent.settings.cluster_id
    mock_agent._os_client = mock_os_client

    from cnf.api.app import create_app
    app = create_app(agent=mock_agent)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
