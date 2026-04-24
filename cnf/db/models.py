"""
cnf.db.models — SQLAlchemy 2.0 async ORM models.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────

class ClusterRole(str, enum.Enum):
    MASTER = "master"
    WORKER = "worker"


class ClusterStatus(str, enum.Enum):
    ONLINE   = "online"
    DEGRADED = "degraded"
    OFFLINE  = "offline"


class MigrationType(str, enum.Enum):
    COLD = "cold"
    LIVE = "live"


class MigrationState(str, enum.Enum):
    PENDING   = "pending"
    PREFLIGHT = "preflight"
    DISK      = "disk"
    MEMORY    = "memory"
    CUTOVER   = "cutover"
    BGP       = "bgp"
    CLEANUP   = "cleanup"
    DONE      = "done"
    FAILED    = "failed"
    ABORTED   = "aborted"


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    auth_url: Mapped[str] = mapped_column(String(512), nullable=False)
    region: Mapped[str] = mapped_column(String(128), default="RegionOne")
    grpc_addr: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[ClusterRole] = mapped_column(
        Enum(ClusterRole), default=ClusterRole.WORKER
    )
    status: Mapped[ClusterStatus] = mapped_column(
        Enum(ClusterStatus), default=ClusterStatus.OFFLINE
    )
    bgp_as: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bgp_speaker_addr: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ceph_mon_addrs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    migrations_as_source: Mapped[list["Migration"]] = relationship(
        back_populates="source_cluster_rel",
        foreign_keys="Migration.source_cluster_id",
    )
    migrations_as_dest: Mapped[list["Migration"]] = relationship(
        back_populates="dest_cluster_rel",
        foreign_keys="Migration.dest_cluster_id",
    )
    metrics: Mapped[list["ClusterMetric"]] = relationship(back_populates="cluster")


class ClusterMetric(Base):
    __tablename__ = "cluster_metrics"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cluster_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("clusters.id"), nullable=False
    )
    cpu_used_pct: Mapped[float] = mapped_column(Float, default=0.0)
    ram_used_pct: Mapped[float] = mapped_column(Float, default=0.0)
    disk_used_pct: Mapped[float] = mapped_column(Float, default=0.0)
    vm_count: Mapped[int] = mapped_column(BigInteger, default=0)
    network_bw_mbps: Mapped[float] = mapped_column(Float, default=0.0)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    cluster: Mapped["Cluster"] = relationship(back_populates="metrics")


class Migration(Base):
    __tablename__ = "migrations"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    vm_id: Mapped[str] = mapped_column(String(255), nullable=False)
    vm_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_cluster_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("clusters.id"), nullable=False
    )
    dest_cluster_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("clusters.id"), nullable=False
    )
    type: Mapped[MigrationType] = mapped_column(Enum(MigrationType), nullable=False)
    state: Mapped[MigrationState] = mapped_column(
        Enum(MigrationState), default=MigrationState.PENDING
    )
    progress_pct: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    rbd_images: Mapped[list | None] = mapped_column(JSON, nullable=True)
    vm_ips: Mapped[list | None] = mapped_column(JSON, nullable=True)
    options: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    initiated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    source_cluster_rel: Mapped["Cluster"] = relationship(
        back_populates="migrations_as_source",
        foreign_keys=[source_cluster_id],
    )
    dest_cluster_rel: Mapped["Cluster"] = relationship(
        back_populates="migrations_as_dest",
        foreign_keys=[dest_cluster_id],
    )
    events: Mapped[list["MigrationEvent"]] = relationship(back_populates="migration")


class MigrationEvent(Base):
    __tablename__ = "migration_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    migration_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("migrations.id"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    migration: Mapped["Migration"] = relationship(back_populates="events")


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    priority: Mapped[int] = mapped_column(default=100)
    # JSON-encoded rule expression, e.g.:
    # {"trigger": "cpu_pct > 80", "action": "live_migrate",
    #  "target": "least_loaded", "window": "02:00-06:00"}
    rule: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
