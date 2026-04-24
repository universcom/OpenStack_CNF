"""tests/test_migration/test_raft.py — unit tests for Raft leader election."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cnf.agent.raft import ElectionState, NodeRole, RaftElection


@pytest.fixture
def election() -> RaftElection:
    on_master = AsyncMock()
    on_lose  = AsyncMock()
    return RaftElection(
        cluster_id=str(uuid.uuid4()),
        grpc_addr="localhost:50051",
        on_become_master=on_master,
        on_lose_master=on_lose,
    )


class TestRaftElection:

    def test_initial_state_is_worker(self, election: RaftElection):
        assert election.is_master is False
        assert election.state.role == NodeRole.WORKER

    @pytest.mark.asyncio
    async def test_become_master_sets_role_and_fires_callback(self, election: RaftElection):
        await election._on_become_master()
        assert election.is_master is True
        assert election.state.role == NodeRole.MASTER
        election.on_become_master.assert_called_once()

    @pytest.mark.asyncio
    async def test_lose_master_reverts_to_worker(self, election: RaftElection):
        await election._on_become_master()
        assert election.is_master is True

        await election._on_lose_master()
        assert election.is_master is False
        assert election.state.role == NodeRole.WORKER
        election.on_lose_master.assert_called_once()

    @pytest.mark.asyncio
    async def test_term_increments_on_each_election(self, election: RaftElection):
        assert election.state.term == 0
        await election._on_become_master()
        assert election.state.term == 1

    @pytest.mark.asyncio
    async def test_force_elect_requires_being_master(self, election: RaftElection):
        # Should return False if not currently master
        result = await election.force_elect("other-cluster")
        assert result is False

    @pytest.mark.asyncio
    async def test_force_elect_revokes_lease_when_master(self, election: RaftElection):
        await election._on_become_master()
        mock_lease = MagicMock()
        election._lease = mock_lease

        with patch.object(election, "_revoke_lease", AsyncMock()) as mock_revoke:
            result = await election.force_elect("other-cluster")

        assert result is True
        mock_revoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_master_id_updated_on_election(self, election: RaftElection):
        assert election.master_id == ""
        await election._on_become_master()
        assert election.master_id == election.cluster_id
