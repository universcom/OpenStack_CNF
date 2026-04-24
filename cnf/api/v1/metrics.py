"""cnf.api.v1.metrics — Cluster metrics endpoint."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from cnf.config import get_settings
from cnf.db.models import Cluster, ClusterMetric
from cnf.db.session import get_session

router = APIRouter()


@router.get("")
async def all_cluster_metrics():
    """Return latest metrics for all registered clusters."""
    settings = get_settings()
    async with get_session() as session:
        clusters_result = await session.execute(select(Cluster))
        clusters = clusters_result.scalars().all()

    out = []
    for cluster in clusters:
        async with get_session() as session:
            result = await session.execute(
                select(ClusterMetric)
                .where(ClusterMetric.cluster_id == cluster.id)
                .order_by(ClusterMetric.collected_at.desc())
                .limit(1)
            )
            metric = result.scalar_one_or_none()
        out.append({
            "cluster_id":   cluster.id,
            "cluster_name": cluster.name,
            "role":         cluster.role.value,
            "status":       cluster.status.value,
            "metrics": {
                "cpu_used_pct":  metric.cpu_used_pct  if metric else None,
                "ram_used_pct":  metric.ram_used_pct  if metric else None,
                "disk_used_pct": metric.disk_used_pct if metric else None,
                "vm_count":      metric.vm_count       if metric else None,
                "collected_at":  metric.collected_at.isoformat() if metric else None,
            },
        })
    return out
