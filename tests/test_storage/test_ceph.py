"""tests/test_storage/test_ceph.py — unit tests for Ceph RBD client."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cnf.storage.ceph import CephRBDClient, CephError, CephSyncTimeout, MirrorStatus


@pytest.fixture
def ceph_client():
    return CephRBDClient()


class TestCephRBDClient:

    @pytest.mark.asyncio
    async def test_get_mirror_status_synced(self, ceph_client):
        mock_data = {
            "state": "up+replaying",
            "description": "replaying, master_position=tag[1,~0] entry=client.4211.1:1",
        }
        with patch.object(ceph_client, "_run", AsyncMock(return_value=mock_data)):
            status = await ceph_client.get_mirror_status("vms", "volume-vol-001")

        assert status.state == "up+replaying"
        assert status.synced is True
        assert status.lag_bytes == 0

    @pytest.mark.asyncio
    async def test_get_mirror_status_with_lag(self, ceph_client):
        mock_data = {
            "state": "up+replaying",
            "description": "replaying, entries_behind_master=1024,master_position=...",
        }
        with patch.object(ceph_client, "_run", AsyncMock(return_value=mock_data)):
            status = await ceph_client.get_mirror_status("vms", "volume-vol-001")

        assert status.lag_bytes == 1024 * 4096  # 4 MB
        assert status.synced is False

    @pytest.mark.asyncio
    async def test_preflight_check_passes(self, ceph_client):
        mock_info = MagicMock()
        mock_info.mirroring_enabled = True
        mock_status = MirrorStatus(
            image="volume-vol-001", pool="vms",
            state="up+replaying", description="replaying",
            lag_bytes=0, lag_seconds=0, synced=True,
        )
        with patch.object(ceph_client, "get_image_info", AsyncMock(return_value=mock_info)):
            with patch.object(ceph_client, "get_mirror_status", AsyncMock(return_value=mock_status)):
                ok, reason = await ceph_client.preflight_check("vms", "volume-vol-001")

        assert ok is True
        assert reason == "ok"

    @pytest.mark.asyncio
    async def test_preflight_check_fails_mirroring_disabled(self, ceph_client):
        mock_info = MagicMock()
        mock_info.mirroring_enabled = False
        with patch.object(ceph_client, "get_image_info", AsyncMock(return_value=mock_info)):
            ok, reason = await ceph_client.preflight_check("vms", "volume-vol-001")

        assert ok is False
        assert "Mirroring not enabled" in reason

    @pytest.mark.asyncio
    async def test_preflight_check_fails_lag_too_high(self, ceph_client):
        mock_info = MagicMock()
        mock_info.mirroring_enabled = True
        mock_status = MirrorStatus(
            image="volume-vol-001", pool="vms",
            state="up+replaying", description="replaying",
            lag_bytes=200 * 1024 * 1024,  # 200 MB — over threshold
            lag_seconds=0, synced=False,
        )
        with patch.object(ceph_client, "get_image_info", AsyncMock(return_value=mock_info)):
            with patch.object(ceph_client, "get_mirror_status", AsyncMock(return_value=mock_status)):
                ok, reason = await ceph_client.preflight_check(
                    "vms", "volume-vol-001", max_lag_bytes=50 * 1024 * 1024
                )

        assert ok is False
        assert "lag too high" in reason

    @pytest.mark.asyncio
    async def test_wait_for_sync_succeeds(self, ceph_client):
        synced_status = MirrorStatus(
            image="vol", pool="vms", state="up+replaying",
            description="", lag_bytes=0, lag_seconds=0, synced=True,
        )
        with patch.object(ceph_client, "get_mirror_status", AsyncMock(return_value=synced_status)):
            status = await ceph_client.wait_for_sync("vms", "vol", timeout_seconds=10)

        assert status.synced is True

    @pytest.mark.asyncio
    async def test_wait_for_sync_timeout(self, ceph_client):
        lagging = MirrorStatus(
            image="vol", pool="vms", state="up+replaying",
            description="", lag_bytes=999_999_999, lag_seconds=0, synced=False,
        )
        with patch.object(ceph_client, "get_mirror_status", AsyncMock(return_value=lagging)):
            with pytest.raises(CephSyncTimeout):
                await ceph_client.wait_for_sync(
                    "vms", "vol",
                    max_lag_bytes=1,
                    timeout_seconds=1,
                    poll_interval=1,
                )

    @pytest.mark.asyncio
    async def test_demote_calls_rbd(self, ceph_client):
        with patch.object(ceph_client, "_run_plain", AsyncMock(return_value="")) as mock_run:
            await ceph_client.demote("vms", "volume-vol-001")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "demote" in args
        assert "vms/volume-vol-001" in args

    @pytest.mark.asyncio
    async def test_promote_calls_rbd(self, ceph_client):
        with patch.object(ceph_client, "_run_plain", AsyncMock(return_value="")) as mock_run:
            await ceph_client.promote("vms", "volume-vol-001")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "promote" in args
        assert "--force" not in args

    @pytest.mark.asyncio
    async def test_promote_force_adds_flag(self, ceph_client):
        with patch.object(ceph_client, "_run_plain", AsyncMock(return_value="")) as mock_run:
            await ceph_client.promote("vms", "volume-vol-001", force=True)

        args = mock_run.call_args[0][0]
        assert "--force" in args
