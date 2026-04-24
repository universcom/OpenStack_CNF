"""cnf.api.v1.migrations — Migration status and control endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from cnf.db.models import Migration, MigrationEvent, MigrationState
from cnf.db.session import get_session

router = APIRouter()


@router.get("")
async def list_migrations(
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    async with get_session() as session:
        q = select(Migration).order_by(Migration.created_at.desc()).limit(limit).offset(offset)
        if state:
            try:
                q = q.where(Migration.state == MigrationState(state))
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid state: {state}")
        result = await session.execute(q)
        migrations = result.scalars().all()

    return [
        {
            "id": m.id, "vm_id": m.vm_id,
            "source_cluster_id": m.source_cluster_id,
            "dest_cluster_id": m.dest_cluster_id,
            "type": m.type.value, "state": m.state.value,
            "progress_pct": m.progress_pct,
            "error": m.error,
            "started_at": m.started_at.isoformat() if m.started_at else None,
            "finished_at": m.finished_at.isoformat() if m.finished_at else None,
        }
        for m in migrations
    ]


@router.get("/{migration_id}")
async def get_migration(migration_id: str):
    async with get_session() as session:
        migration = await session.get(Migration, migration_id)
    if not migration:
        raise HTTPException(status_code=404, detail="Migration not found")
    return {
        "id": migration.id, "vm_id": migration.vm_id,
        "source_cluster_id": migration.source_cluster_id,
        "dest_cluster_id": migration.dest_cluster_id,
        "type": migration.type.value, "state": migration.state.value,
        "progress_pct": migration.progress_pct,
        "error": migration.error, "options": migration.options,
        "rbd_images": migration.rbd_images, "vm_ips": migration.vm_ips,
        "started_at": migration.started_at.isoformat() if migration.started_at else None,
        "finished_at": migration.finished_at.isoformat() if migration.finished_at else None,
    }


@router.get("/{migration_id}/events")
async def get_migration_events(migration_id: str):
    async with get_session() as session:
        result = await session.execute(
            select(MigrationEvent)
            .where(MigrationEvent.migration_id == migration_id)
            .order_by(MigrationEvent.occurred_at)
        )
        events = result.scalars().all()
    return [
        {
            "state": e.state, "message": e.message,
            "data": e.data, "occurred_at": e.occurred_at.isoformat(),
        }
        for e in events
    ]


@router.post("/{migration_id}/abort")
async def abort_migration(migration_id: str):
    async with get_session() as session:
        migration = await session.get(Migration, migration_id)
        if not migration:
            raise HTTPException(status_code=404, detail="Migration not found")
        if migration.state in (MigrationState.DONE, MigrationState.FAILED, MigrationState.ABORTED):
            raise HTTPException(status_code=409, detail=f"Migration already in terminal state: {migration.state.value}")

    # Signal the running engine to abort (via Redis pub/sub in production)
    # For now, mark as aborted in DB
    async with get_session() as session:
        migration = await session.get(Migration, migration_id)
        if migration:
            migration.state = MigrationState.ABORTED
            migration.error = "Aborted by user"

    return {"message": "Abort signal sent", "migration_id": migration_id}
