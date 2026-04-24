"""cnf.api.v1.clusters — Cluster management endpoints."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from cnf.db.models import Cluster, ClusterMetric, ClusterRole, ClusterStatus
from cnf.db.session import get_session

router = APIRouter()


class ClusterCreate(BaseModel):
    name: str
    auth_url: str
    region: str = "RegionOne"
    grpc_addr: str
    bgp_as: int | None = None
    bgp_speaker_addr: str | None = None


class ClusterResponse(BaseModel):
    id: str
    name: str
    auth_url: str
    region: str
    grpc_addr: str
    role: str
    status: str
    bgp_as: int | None = None


@router.get("", response_model=list[ClusterResponse])
async def list_clusters():
    async with get_session() as session:
        result = await session.execute(select(Cluster))
        clusters = result.scalars().all()
    return [
        ClusterResponse(
            id=c.id, name=c.name, auth_url=c.auth_url,
            region=c.region, grpc_addr=c.grpc_addr,
            role=c.role.value, status=c.status.value,
            bgp_as=c.bgp_as,
        )
        for c in clusters
    ]


@router.post("", response_model=ClusterResponse, status_code=201)
async def register_cluster(body: ClusterCreate):
    async with get_session() as session:
        cluster = Cluster(
            id=str(uuid.uuid4()),
            name=body.name,
            auth_url=body.auth_url,
            region=body.region,
            grpc_addr=body.grpc_addr,
            bgp_as=body.bgp_as,
            bgp_speaker_addr=body.bgp_speaker_addr,
            role=ClusterRole.WORKER,
            status=ClusterStatus.OFFLINE,
        )
        session.add(cluster)
    return ClusterResponse(
        id=cluster.id, name=cluster.name, auth_url=cluster.auth_url,
        region=cluster.region, grpc_addr=cluster.grpc_addr,
        role=cluster.role.value, status=cluster.status.value,
        bgp_as=cluster.bgp_as,
    )


@router.get("/{cluster_id}", response_model=ClusterResponse)
async def get_cluster(cluster_id: str):
    async with get_session() as session:
        cluster = await session.get(Cluster, cluster_id)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return ClusterResponse(
        id=cluster.id, name=cluster.name, auth_url=cluster.auth_url,
        region=cluster.region, grpc_addr=cluster.grpc_addr,
        role=cluster.role.value, status=cluster.status.value,
        bgp_as=cluster.bgp_as,
    )


@router.get("/{cluster_id}/metrics")
async def cluster_metrics(cluster_id: str):
    async with get_session() as session:
        result = await session.execute(
            select(ClusterMetric)
            .where(ClusterMetric.cluster_id == cluster_id)
            .order_by(ClusterMetric.collected_at.desc())
            .limit(1)
        )
        metric = result.scalar_one_or_none()
    if not metric:
        raise HTTPException(status_code=404, detail="No metrics found")
    return {
        "cluster_id":    cluster_id,
        "cpu_used_pct":  metric.cpu_used_pct,
        "ram_used_pct":  metric.ram_used_pct,
        "disk_used_pct": metric.disk_used_pct,
        "vm_count":      metric.vm_count,
        "collected_at":  metric.collected_at.isoformat(),
    }
