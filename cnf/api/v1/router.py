"""cnf.api.v1.router — aggregates all v1 API routes."""
from fastapi import APIRouter
from cnf.api.v1 import clusters, vms, master, migrations, metrics

v1_router = APIRouter()
v1_router.include_router(clusters.router,   prefix="/clusters",   tags=["clusters"])
v1_router.include_router(vms.router,        prefix="/vms",        tags=["vms"])
v1_router.include_router(master.router,     prefix="/master",     tags=["master"])
v1_router.include_router(migrations.router, prefix="/migrations", tags=["migrations"])
v1_router.include_router(metrics.router,    prefix="/metrics",    tags=["metrics"])
