"""cnf.tasks.migration_tasks — Celery tasks for async migration execution."""
from __future__ import annotations

import asyncio
from typing import Any

from celery import Celery
from celery.utils.log import get_task_logger

from cnf.config import get_settings

logger = get_task_logger(__name__)

_settings = get_settings()
app = Celery(
    "cnf",
    broker=_settings.celery.broker_url,
    backend=_settings.celery.result_backend,
)
app.conf.update(
    task_serializer=_settings.celery.task_serializer,
    result_serializer=_settings.celery.result_serializer,
    task_soft_time_limit=_settings.celery.task_soft_time_limit,
    task_time_limit=_settings.celery.task_time_limit,
    worker_prefetch_multiplier=_settings.celery.worker_prefetch_multiplier,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)


def _run_async(coro) -> Any:
    """Run an async coroutine from a synchronous Celery task."""
    return asyncio.get_event_loop().run_until_complete(coro)


@app.task(bind=True, name="cnf.tasks.run_cold_migration", max_retries=2)
def run_cold_migration(
    self,
    migration_id: str,
    vm_id: str,
    source_cluster: str,
    dest_cluster: str,
    options: dict,
):
    logger.info(f"[cold] starting migration {migration_id} vm={vm_id}")
    try:
        _run_async(_execute_migration(migration_id, vm_id, source_cluster, dest_cluster, options, live=False))
    except Exception as exc:
        logger.error(f"[cold] migration {migration_id} failed: {exc}")
        raise self.retry(exc=exc, countdown=30)


@app.task(bind=True, name="cnf.tasks.run_live_migration", max_retries=1)
def run_live_migration(
    self,
    migration_id: str,
    vm_id: str,
    source_cluster: str,
    dest_cluster: str,
    options: dict,
):
    logger.info(f"[live] starting migration {migration_id} vm={vm_id}")
    try:
        _run_async(_execute_migration(migration_id, vm_id, source_cluster, dest_cluster, options, live=True))
    except Exception as exc:
        logger.error(f"[live] migration {migration_id} failed: {exc}")
        raise self.retry(exc=exc, countdown=10)


async def _execute_migration(
    migration_id: str,
    vm_id: str,
    source_cluster: str,
    dest_cluster: str,
    options: dict,
    live: bool,
) -> None:
    from cnf.migration.engine import (
        ColdMigrationEngine, LiveMigrationEngine,
        MigrationContext, MigrationType,
    )
    from cnf.openstack.client import OpenStackClient
    from cnf.storage.ceph import CephRBDClient
    from cnf.network.bgp import BGPManager

    settings = get_settings()
    ctx = MigrationContext(
        migration_id=migration_id,
        vm_id=vm_id,
        source_cluster_id=source_cluster,
        dest_cluster_id=dest_cluster,
        migration_type=MigrationType.LIVE if live else MigrationType.COLD,
        options=options,
    )

    source_os = OpenStackClient()
    dest_os = OpenStackClient()
    ceph = CephRBDClient()
    bgp = BGPManager()

    if live:
        engine = LiveMigrationEngine(ctx, source_os, dest_os, ceph, bgp)
    else:
        engine = ColdMigrationEngine(ctx, source_os, dest_os, ceph, bgp)

    await engine.run()
