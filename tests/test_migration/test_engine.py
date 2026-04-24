"""tests/test_migration/test_engine.py — unit tests for cold and live migration engines."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cnf.db.models import MigrationState, MigrationType
from cnf.migration.engine import (
    ColdMigrationEngine,
    LiveMigrationEngine,
    MigrationContext,
    MigrationError,
)


def _make_ctx(live: bool = False) -> MigrationContext:
    return MigrationContext(
        migration_id=str(uuid.uuid4()),
        vm_id="vm-uuid-001",
        source_cluster_id="cluster-src",
        dest_cluster_id="cluster-dst",
        migration_type=MigrationType.LIVE if live else MigrationType.COLD,
        options={"dest_host": "compute-1.os2.local", "dest_next_hop": "10.0.0.1"},
    )


# ─────────────────────────────────────────────
# Cold migration
# ─────────────────────────────────────────────

class TestColdMigration:
    @pytest.mark.asyncio
    async def test_successful_cold_migration(
        self, mock_os_client, mock_ceph, mock_bgp, db_session
    ):
        ctx = _make_ctx(live=False)

        with patch("cnf.migration.engine.get_session") as mock_gs:
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            engine = ColdMigrationEngine(
                ctx=ctx,
                source_os=mock_os_client,
                dest_os=mock_os_client,
                ceph=mock_ceph,
                bgp=mock_bgp,
            )
            result = await engine.run()

        assert result is True
        mock_os_client.stop_vm.assert_called_once_with("vm-uuid-001")
        mock_ceph.demote.assert_called_once()
        mock_bgp.handoff.assert_called_once()

    @pytest.mark.asyncio
    async def test_cold_migration_fails_if_vm_not_active_or_shutoff(
        self, mock_os_client, mock_ceph, mock_bgp, db_session
    ):
        from cnf.openstack.client import VMDetails
        mock_os_client.get_vm.return_value = VMDetails(
            id="vm-uuid-001", name="test", status="ERROR",
            host="h1", flavor_id="f1", image_id="i1",
        )
        ctx = _make_ctx(live=False)

        with patch("cnf.migration.engine.get_session") as mock_gs:
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            engine = ColdMigrationEngine(ctx, mock_os_client, mock_os_client, mock_ceph, mock_bgp)
            result = await engine.run()

        assert result is False
        mock_ceph.demote.assert_not_called()

    @pytest.mark.asyncio
    async def test_cold_migration_aborts_on_rbd_preflight_failure(
        self, mock_os_client, mock_ceph, mock_bgp, db_session
    ):
        mock_ceph.preflight_check.return_value = (False, "mirror lag too high: 100MB")
        ctx = _make_ctx(live=False)

        with patch("cnf.migration.engine.get_session") as mock_gs:
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            engine = ColdMigrationEngine(ctx, mock_os_client, mock_os_client, mock_ceph, mock_bgp)
            result = await engine.run()

        assert result is False
        mock_ceph.demote.assert_not_called()
        mock_bgp.handoff.assert_not_called()

    @pytest.mark.asyncio
    async def test_rollback_promotes_rbd_on_disk_failure(
        self, mock_os_client, mock_ceph, mock_bgp, db_session
    ):
        mock_ceph.demote.side_effect = Exception("Ceph demote failed")
        ctx = _make_ctx(live=False)
        ctx.rbd_images = ["volume-vol-001"]

        with patch("cnf.migration.engine.get_session") as mock_gs:
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            engine = ColdMigrationEngine(ctx, mock_os_client, mock_os_client, mock_ceph, mock_bgp)
            result = await engine.run()

        assert result is False


# ─────────────────────────────────────────────
# Live migration
# ─────────────────────────────────────────────

class TestLiveMigration:
    @pytest.mark.asyncio
    async def test_live_migration_requires_active_vm(
        self, mock_os_client, mock_ceph, mock_bgp, db_session
    ):
        from cnf.openstack.client import VMDetails
        mock_os_client.get_vm.return_value = VMDetails(
            id="vm-uuid-001", name="test", status="SHUTOFF",
            host="h1", flavor_id="f1", image_id="i1",
        )
        ctx = _make_ctx(live=True)

        with patch("cnf.migration.engine.get_session") as mock_gs:
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            engine = LiveMigrationEngine(ctx, mock_os_client, mock_os_client, mock_ceph, mock_bgp)
            result = await engine.run()

        assert result is False

    @pytest.mark.asyncio
    async def test_live_migration_requires_dest_host(
        self, mock_os_client, mock_ceph, mock_bgp, db_session
    ):
        ctx = _make_ctx(live=True)
        ctx.options = {}  # no dest_host

        with patch("cnf.migration.engine.get_session") as mock_gs:
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

            engine = LiveMigrationEngine(ctx, mock_os_client, mock_os_client, mock_ceph, mock_bgp)

            # Patch QEMU tunnel so it doesn't try real libvirt
            with patch("cnf.migration.engine.QEMUMigrationTunnel") as mock_tunnel_cls:
                mock_tunnel = AsyncMock()
                mock_tunnel_cls.return_value = mock_tunnel
                result = await engine.run()

        assert result is False
