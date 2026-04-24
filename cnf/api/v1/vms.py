"""cnf.api.v1.vms — VM listing and migration trigger endpoints."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from cnf.config import get_settings
from cnf.db.models import Migration, MigrationState, MigrationType
from cnf.db.session import get_session
from cnf.openstack.client import OpenStackClient, VMNotFound

router = APIRouter()
_os_client = OpenStackClient()


@router.get("")
async def list_vms(cluster_id: str | None = None):
    vms = await _os_client.list_vms()
    return [
        {
            "id": v.id, "name": v.name, "status": v.status,
            "host": v.host, "ips": v.ips, "floating_ips": v.floating_ips,
            "volumes": v.volume_ids,
        }
        for v in vms
    ]


@router.get("/{vm_id}")
async def get_vm(vm_id: str):
    try:
        vm = await _os_client.get_vm(vm_id)
    except VMNotFound:
        raise HTTPException(status_code=404, detail="VM not found")
    return {
        "id": vm.id, "name": vm.name, "status": vm.status,
        "host": vm.host, "flavor_id": vm.flavor_id,
        "ips": vm.ips, "floating_ips": vm.floating_ips,
        "volumes": vm.volume_ids, "metadata": vm.metadata,
    }


class MigrateRequest(BaseModel):
    dest_cluster_id: str
    dest_host: str | None = None
    dest_next_hop: str | None = None


@router.post("/{vm_id}/migrate", status_code=202)
async def migrate_vm(vm_id: str, body: MigrateRequest, background_tasks: BackgroundTasks):
    settings = get_settings()
    migration_id = str(uuid.uuid4())

    async with get_session() as session:
        migration = Migration(
            id=migration_id,
            vm_id=vm_id,
            source_cluster_id=settings.cluster_id,
            dest_cluster_id=body.dest_cluster_id,
            type=MigrationType.COLD,
            state=MigrationState.PENDING,
            options=body.model_dump(),
        )
        session.add(migration)

    background_tasks.add_task(_run_migration, migration_id, vm_id, body, live=False)
    return {"migration_id": migration_id, "status": "accepted"}


@router.post("/{vm_id}/live-migrate", status_code=202)
async def live_migrate_vm(vm_id: str, body: MigrateRequest, background_tasks: BackgroundTasks):
    settings = get_settings()
    migration_id = str(uuid.uuid4())

    async with get_session() as session:
        migration = Migration(
            id=migration_id,
            vm_id=vm_id,
            source_cluster_id=settings.cluster_id,
            dest_cluster_id=body.dest_cluster_id,
            type=MigrationType.LIVE,
            state=MigrationState.PENDING,
            options=body.model_dump(),
        )
        session.add(migration)

    background_tasks.add_task(_run_migration, migration_id, vm_id, body, live=True)
    return {"migration_id": migration_id, "status": "accepted"}


async def _run_migration(
    migration_id: str, vm_id: str, req: MigrateRequest, live: bool
) -> None:
    from cnf.migration.engine import (
        ColdMigrationEngine, LiveMigrationEngine,
        MigrationContext, MigrationType,
    )
    from cnf.storage.ceph import CephRBDClient
    from cnf.network.bgp import BGPManager

    settings = get_settings()
    ctx = MigrationContext(
        migration_id=migration_id,
        vm_id=vm_id,
        source_cluster_id=settings.cluster_id,
        dest_cluster_id=req.dest_cluster_id,
        migration_type=MigrationType.LIVE if live else MigrationType.COLD,
        options=req.model_dump(),
    )

    source_os = OpenStackClient()
    dest_os = OpenStackClient()  # In prod: configured per dest cluster creds
    ceph = CephRBDClient()
    bgp = BGPManager()

    if live:
        engine = LiveMigrationEngine(ctx, source_os, dest_os, ceph, bgp)
    else:
        engine = ColdMigrationEngine(ctx, source_os, dest_os, ceph, bgp)

    await engine.run()
