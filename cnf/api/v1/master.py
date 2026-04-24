"""cnf.api.v1.master — Master node info and election endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


@router.get("")
async def show_master(request: Request):
    agent = request.app.state.agent
    peers = await agent.get_peers()
    return {
        "master_cluster_id": agent._election.master_id if agent._election else "",
        "this_cluster_id":   agent.settings.cluster_id,
        "this_cluster_role": "master" if agent.is_master else "worker",
        "peers":             peers,
    }


class ElectRequest(BaseModel):
    target_cluster_id: str


@router.post("/elect")
async def force_elect(body: ElectRequest, request: Request):
    agent = request.app.state.agent
    if not agent.is_master:
        raise HTTPException(status_code=403, detail="Only master can initiate election transfer")
    ok = await agent._election.force_elect(body.target_cluster_id)
    return {"success": ok, "target": body.target_cluster_id}
