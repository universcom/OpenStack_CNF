"""
cnf.utils.logging — structured logging via structlog.
cnf.utils.metrics — Prometheus counters and histograms.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from cnf.config import get_settings

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level)

    # Suppress noisy third-party loggers
    for noisy in ("urllib3", "asyncio", "grpc", "celery.utils.functional"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


# ─────────────────────────────────────────────
# Prometheus metrics
# ─────────────────────────────────────────────

MIGRATION_TOTAL = Counter(
    "cnf_migrations_total",
    "Total VM migrations initiated",
    ["type", "source_cluster", "dest_cluster", "status"],
)

MIGRATION_DURATION = Histogram(
    "cnf_migration_duration_seconds",
    "Duration of VM migration operations",
    ["type"],
    buckets=[5, 15, 30, 60, 120, 300, 600, 1800, 3600],
)

MIGRATION_ACTIVE = Gauge(
    "cnf_migrations_active",
    "Number of currently active migrations",
    ["type"],
)

CLUSTER_VMS = Gauge(
    "cnf_cluster_vm_count",
    "Number of VMs per cluster",
    ["cluster_id", "cluster_name"],
)

CLUSTER_CPU = Gauge(
    "cnf_cluster_cpu_used_pct",
    "CPU usage percentage per cluster",
    ["cluster_id"],
)

CLUSTER_RAM = Gauge(
    "cnf_cluster_ram_used_pct",
    "RAM usage percentage per cluster",
    ["cluster_id"],
)

BGP_ANNOUNCEMENTS = Counter(
    "cnf_bgp_announcements_total",
    "Total BGP prefix announcements",
    ["cluster_id", "action"],
)

RBD_PROMOTIONS = Counter(
    "cnf_rbd_promotions_total",
    "Total Ceph RBD image promotions",
    ["cluster_id", "status"],
)

GRPC_REQUESTS = Counter(
    "cnf_grpc_requests_total",
    "Total gRPC requests",
    ["method", "status"],
)

RAFT_LEADER_CHANGES = Counter(
    "cnf_raft_leader_changes_total",
    "Total Raft leader change events",
)


def start_metrics_server() -> None:
    settings = get_settings()
    if settings.metrics.enabled:
        start_http_server(settings.metrics.port)
        get_logger("metrics").info(
            "prometheus_metrics_server_started",
            port=settings.metrics.port,
        )
