"""
cnf.config — centralised configuration via pydantic-settings.
All values can be overridden by environment variables or /etc/cnf/cnf.yaml.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CephConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CNF_CEPH_")

    conf_path: str = "/etc/ceph/ceph.conf"
    keyring_path: str = "/etc/ceph/ceph.client.cnf.keyring"
    client_name: str = "client.cnf"
    pool: str = "vms"
    mirror_mode: str = "image"            # "image" | "pool"
    mirror_lag_threshold_bytes: int = 50 * 1024 * 1024   # 50 MB
    mirror_lag_threshold_seconds: int = 30


class BGPConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CNF_BGP_")

    enabled: bool = True
    frr_socket: str = "/var/run/frr/vtysh.sock"
    as_number: int = 65000
    route_reflector_addr: str = ""
    bfd_enabled: bool = True
    hold_time: int = 9
    keepalive: int = 3
    convergence_wait_seconds: int = 5


class GRPCConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CNF_GRPC_")

    port: int = 50051
    max_workers: int = 20
    keepalive_time_ms: int = 10_000
    keepalive_timeout_ms: int = 5_000
    max_message_length: int = 64 * 1024 * 1024   # 64 MB
    tls_enabled: bool = True
    tls_cert: str = "/etc/cnf/tls/server.crt"
    tls_key: str = "/etc/cnf/tls/server.key"
    tls_ca: str = "/etc/cnf/tls/ca.crt"


class RaftConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CNF_RAFT_")

    election_timeout_ms: int = 1500
    heartbeat_interval_ms: int = 500
    etcd_endpoints: list[str] = Field(default_factory=lambda: ["localhost:2379"])
    etcd_prefix: str = "/cnf/raft"
    etcd_tls_cert: str = ""
    etcd_tls_key: str = ""
    etcd_tls_ca: str = ""
    lease_ttl_seconds: int = 10


class APIConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CNF_API_")

    host: str = "0.0.0.0"
    port: int = 8080
    workers: int = 4
    reload: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60


class OpenStackConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OS_")

    auth_url: str = ""
    username: str = ""
    password: str = ""
    project_name: str = "admin"
    project_domain_name: str = "Default"
    user_domain_name: str = "Default"
    region_name: str = "RegionOne"
    interface: str = "internal"
    identity_api_version: str = "3"


class DatabaseConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CNF_DB_")

    url: str = "postgresql+asyncpg://cnf:cnf@localhost:5432/cnf"
    pool_size: int = 10
    max_overflow: int = 20
    pool_timeout: int = 30
    echo: bool = False


class CeleryConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CNF_CELERY_")

    broker_url: str = "redis://localhost:6379/0"
    result_backend: str = "redis://localhost:6379/1"
    task_serializer: str = "json"
    result_serializer: str = "json"
    task_soft_time_limit: int = 3600
    task_time_limit: int = 7200
    worker_prefetch_multiplier: int = 1


class MetricsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CNF_METRICS_")

    enabled: bool = True
    port: int = 9090
    collection_interval_seconds: int = 30


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CNF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Identity
    cluster_id: str = Field(default="", description="Unique cluster ID (UUID)")
    cluster_name: str = Field(default="", description="Human-readable cluster name")
    cluster_grpc_addr: str = Field(default="", description="This node's gRPC address host:port")

    # Peer cluster addresses — list of host:port for peer gRPC endpoints
    peer_clusters: list[str] = Field(default_factory=list)

    # Sub-configs
    ceph: CephConfig = Field(default_factory=CephConfig)
    bgp: BGPConfig = Field(default_factory=BGPConfig)
    grpc: GRPCConfig = Field(default_factory=GRPCConfig)
    raft: RaftConfig = Field(default_factory=RaftConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    openstack: OpenStackConfig = Field(default_factory=OpenStackConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    celery: CeleryConfig = Field(default_factory=CeleryConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)

    # Operational
    log_level: str = "INFO"
    log_format: str = "json"   # "json" | "console"
    migration_max_lag_bytes: int = 50 * 1024 * 1024
    migration_memory_chunk_size: int = 64 * 1024  # 64 KB

    @field_validator("cluster_id")
    @classmethod
    def ensure_cluster_id(cls, v: str) -> str:
        if not v:
            import uuid
            return str(uuid.uuid4())
        return v

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Settings":
        """Load settings from a YAML file, then overlay env vars."""
        raw: dict[str, Any] = {}
        p = Path(path)
        if p.exists():
            with p.open() as f:
                raw = yaml.safe_load(f) or {}
        return cls(**raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    config_file = os.environ.get("CNF_CONFIG", "/etc/cnf/cnf.yaml")
    return Settings.from_yaml(config_file)
