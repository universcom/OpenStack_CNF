"""tests/test_api/test_endpoints.py — integration tests for CNF REST API."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest


class TestHealthEndpoints:
    @pytest.mark.asyncio
    async def test_healthz(self, api_client):
        resp = await api_client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_readyz_returns_role(self, api_client):
        resp = await api_client.get("/readyz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready"] is True
        assert data["role"] in ("master", "worker")


class TestClusterEndpoints:
    @pytest.mark.asyncio
    async def test_list_clusters_empty(self, api_client):
        with patch("cnf.api.v1.clusters.get_session") as mock_gs:
            mock_session = AsyncMock()
            mock_session.execute.return_value.scalars.return_value.all.return_value = []
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = await api_client.get("/v1/clusters")

        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_register_cluster(self, api_client):
        body = {
            "name":      "openstack-3",
            "auth_url":  "http://os3.example.com:5000/v3",
            "grpc_addr": "os3.example.com:50051",
            "region":    "RegionOne",
            "bgp_as":    65003,
        }
        with patch("cnf.api.v1.clusters.get_session") as mock_gs:
            mock_session = AsyncMock()
            mock_session.add = AsyncMock()
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = await api_client.post("/v1/clusters", json=body)

        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "openstack-3"
        assert data["bgp_as"] == 65003
        assert "id" in data

    @pytest.mark.asyncio
    async def test_get_cluster_not_found(self, api_client):
        with patch("cnf.api.v1.clusters.get_session") as mock_gs:
            mock_session = AsyncMock()
            mock_session.get.return_value = None
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = await api_client.get(f"/v1/clusters/{uuid.uuid4()}")

        assert resp.status_code == 404


class TestVMEndpoints:
    @pytest.mark.asyncio
    async def test_list_vms(self, api_client, mock_os_client):
        with patch("cnf.api.v1.vms._os_client", mock_os_client):
            resp = await api_client.get("/v1/vms")

        assert resp.status_code == 200
        vms = resp.json()
        assert len(vms) == 1
        assert vms[0]["id"] == "vm-uuid-001"

    @pytest.mark.asyncio
    async def test_get_vm(self, api_client, mock_os_client):
        with patch("cnf.api.v1.vms._os_client", mock_os_client):
            resp = await api_client.get("/v1/vms/vm-uuid-001")

        assert resp.status_code == 200
        assert resp.json()["name"] == "test-vm"
        assert resp.json()["status"] == "ACTIVE"

    @pytest.mark.asyncio
    async def test_get_vm_not_found(self, api_client, mock_os_client):
        from cnf.openstack.client import VMNotFound
        mock_os_client.get_vm.side_effect = VMNotFound("vm-not-there")

        with patch("cnf.api.v1.vms._os_client", mock_os_client):
            resp = await api_client.get("/v1/vms/does-not-exist")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_trigger_cold_migration(self, api_client, mock_os_client):
        body = {
            "dest_cluster_id": str(uuid.uuid4()),
            "dest_host":       "compute-1.os2.local",
        }
        with patch("cnf.api.v1.vms._os_client", mock_os_client):
            with patch("cnf.api.v1.vms.get_session") as mock_gs:
                mock_session = AsyncMock()
                mock_session.add = AsyncMock()
                mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)
                with patch("cnf.api.v1.vms._run_migration", AsyncMock()):
                    resp = await api_client.post("/v1/vms/vm-uuid-001/migrate", json=body)

        assert resp.status_code == 202
        data = resp.json()
        assert "migration_id" in data
        assert data["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_trigger_live_migration(self, api_client, mock_os_client):
        body = {
            "dest_cluster_id": str(uuid.uuid4()),
            "dest_host":       "compute-1.os2.local",
            "dest_next_hop":   "10.0.0.1",
        }
        with patch("cnf.api.v1.vms._os_client", mock_os_client):
            with patch("cnf.api.v1.vms.get_session") as mock_gs:
                mock_session = AsyncMock()
                mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)
                with patch("cnf.api.v1.vms._run_migration", AsyncMock()):
                    resp = await api_client.post("/v1/vms/vm-uuid-001/live-migrate", json=body)

        assert resp.status_code == 202


class TestMigrationEndpoints:
    @pytest.mark.asyncio
    async def test_list_migrations_empty(self, api_client):
        with patch("cnf.api.v1.migrations.get_session") as mock_gs:
            mock_session = AsyncMock()
            mock_session.execute.return_value.scalars.return_value.all.return_value = []
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = await api_client.get("/v1/migrations")

        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_migrations_invalid_state(self, api_client):
        resp = await api_client.get("/v1/migrations?state=invalid_state")
        assert resp.status_code == 400


class TestMasterEndpoints:
    @pytest.mark.asyncio
    async def test_show_master(self, api_client):
        resp = await api_client.get("/v1/master")
        assert resp.status_code == 200
        data = resp.json()
        assert "master_cluster_id" in data
        assert "this_cluster_role" in data
