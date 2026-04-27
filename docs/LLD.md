# CNF — Low Level Design (LLD)

**Project**: CNF v0.1.0
**Date**: 2026-04-27

---

## 1. Module Map

```mermaid
graph TD
    subgraph Entry["Entry Points"]
        MAIN["cnf/main.py\ncnf-agent executable"]
        MANAGE["cnf-manage\nAlembic CLI wrapper"]
    end

    subgraph Core["Core"]
        CFG["cnf/config.py\nPydantic Settings hierarchy"]
        AGENT["cnf/agent/agent.py\nCNFAgent — orchestrator"]
        RAFT["cnf/agent/raft.py\nRaftElection — etcd leader"]
    end

    subgraph API["API Layer"]
        APP["cnf/api/app.py\nFastAPI factory + middleware"]
        V1["cnf/api/v1/\nclusters · vms · migrations\nmaster · metrics"]
    end

    subgraph GRPC_MOD["gRPC Layer"]
        SRV["cnf/grpc/server.py\nCNFControlServicer\nCNFPeerServicer"]
        PROTO["proto/cnf.proto\nService definitions"]
    end

    subgraph BIZ["Business Logic"]
        ENG["cnf/migration/engine.py\nCold/Live state machines"]
        SCHED["cnf/scheduler/scheduler.py\nPolicy evaluator"]
    end

    subgraph Integrations["Integrations"]
        CEP["cnf/storage/ceph.py\nCephRBDClient"]
        BGP["cnf/network/bgp.py\nFRRClient"]
        OSC_MOD["cnf/openstack/client.py\nOpenStackClient"]
        OSC_PLUGIN["cnf/osc/\nOpenStack CLI plugin"]
    end

    subgraph Persistence["Persistence"]
        MODELS["cnf/db/models.py\nSQLAlchemy ORM"]
        SESSION["cnf/db/session.py\nasync engine + session"]
        TASKS["cnf/tasks/migration_tasks.py\nCelery task definitions"]
    end

    subgraph Util["Utilities"]
        LOG["cnf/utils/logging.py\nstructlog + Prometheus registry"]
    end

    MAIN --> AGENT
    AGENT --> CFG
    AGENT --> RAFT
    AGENT --> APP
    AGENT --> SRV
    AGENT --> ENG
    AGENT --> SCHED
    APP --> V1
    V1 --> ENG
    V1 --> MODELS
    ENG --> CEP
    ENG --> BGP
    ENG --> OSC_MOD
    ENG --> MODELS
    ENG --> TASKS
    SRV --> ENG
    SRV --> MODELS
    RAFT --> SESSION
    SCHED --> OSC_MOD
    SCHED --> MODELS
    PROTO --> SRV
```

---

## 2. Class Diagram — Core Components

```mermaid
classDiagram
    class CNFAgent {
        +Settings settings
        +RaftElection election
        +OpenStackClient os_client
        +Scheduler scheduler
        +Server grpc_server
        -bool _running
        -bool _is_master
        -Task _metrics_task
        +start() None
        +run_forever() None
        +stop() None
        +is_master bool
        +master_grpc_addr str
        -_upsert_self_cluster() None
        -_on_become_master() None
        -_on_lose_master() None
        -_metrics_loop() None
        -_start_grpc() None
        -_start_api() None
    }

    class RaftElection {
        +str cluster_id
        +str grpc_addr
        +RaftConfig config
        -bool _running
        -str _current_master
        +start() None
        +stop() None
        +get_peers() dict
        +force_elect(cluster_id) None
        -_try_acquire_master() bool
        -_election_loop() None
    }

    class Scheduler {
        +OpenStackClient os_client
        -bool _is_master
        +set_master(flag) None
        +run() None
        -_evaluate() None
        -_collect_snapshots() list
        -_load_policies() list
        -_apply_policy(policy, snaps) None
    }

    class MigrationEngine {
        <<abstract>>
        +MigrationContext context
        +execute() None
        +abort() None
        -_update_state(state, pct) None
        -_register_rollback(fn) None
        -_run_rollbacks() None
    }

    class ColdMigrationEngine {
        -_preflight() None
        -_disk() None
        -_cutover() None
        -_bgp() None
        -_cleanup() None
    }

    class LiveMigrationEngine {
        -_preflight() None
        -_disk() None
        -_memory() None
        -_cutover() None
        -_bgp() None
        -_cleanup() None
    }

    class OpenStackClient {
        +get_vm(vm_id) VMDetails
        +list_vms() list
        +create_server(spec) VMDetails
        +delete_server(vm_id) None
        +get_cluster_metrics() dict
        +get_volume(vol_id) dict
        +get_network(net_id) dict
    }

    class CephRBDClient {
        +CephConfig config
        +promote_image(pool, image) None
        +demote_image(pool, image) None
        +get_mirror_status(pool, image) MirrorStatus
        +create_snapshot(pool, image, snap) None
        +delete_snapshot(pool, image, snap) None
    }

    class FRRClient {
        +BGPConfig config
        +announce(prefix, next_hop) None
        +withdraw(prefix) None
        +check_convergence(prefix) bool
    }

    class MigrationContext {
        +UUID migration_id
        +str vm_id
        +str source_cluster_id
        +str dest_cluster_id
        +MigrationType migration_type
        +dict options
        +VMDetails vm
        +list rbd_images
        +list vm_ips
        +str dest_nova_vm_id
        +Stub source_grpc_client
        +Stub dest_grpc_client
        +MigrationState state
        +float progress
        +str error
        +datetime started_at
    }

    CNFAgent --> RaftElection : uses
    CNFAgent --> Scheduler : owns
    CNFAgent --> OpenStackClient : uses
    MigrationEngine <|-- ColdMigrationEngine : extends
    MigrationEngine <|-- LiveMigrationEngine : extends
    MigrationEngine --> MigrationContext : owns
    MigrationEngine --> CephRBDClient : uses
    MigrationEngine --> FRRClient : uses
    MigrationEngine --> OpenStackClient : uses
    Scheduler --> OpenStackClient : uses
```

---

## 3. Configuration Hierarchy

```mermaid
graph TD
    S["Settings (root)"]

    S --> CLID["cluster_id: UUID"]
    S --> CLNAME["cluster_name: str"]
    S --> CLGRPC["cluster_grpc_addr: str"]
    S --> PEERS["peer_clusters: List[str]"]
    S --> LOG_CONF["log_level / log_format"]

    S --> CEPH_C["CephConfig"]
    S --> BGP_C["BGPConfig"]
    S --> GRPC_C["GRPCConfig"]
    S --> RAFT_C["RaftConfig"]
    S --> API_C["APIConfig"]
    S --> OS_C["OpenStackConfig"]
    S --> DB_C["DatabaseConfig"]
    S --> CEL_C["CeleryConfig"]
    S --> MET_C["MetricsConfig"]

    CEPH_C --> C1["conf_path\nkeyring_path\npool\nmirror_mode\nlag_threshold_bytes: 50MB\nlag_threshold_seconds: 30"]
    BGP_C --> B1["enabled\nas_number\nroute_reflector_addr\nbfd_enabled\nhold_time: 9s\nkeepalive: 3s\nconvergence_wait: 5s"]
    GRPC_C --> G1["port: 50051\ntls_enabled\ntls_cert / tls_key / tls_ca\nmax_message_length: 64MB"]
    RAFT_C --> R1["election_timeout_ms: 1500\nheartbeat_interval_ms: 500\nlease_ttl_seconds: 10\netcd_endpoints"]
    API_C --> A1["host: 0.0.0.0\nport: 8080\ncors_origins\njwt_secret"]
    OS_C --> O1["auth_url\nusername / password\nproject_name\nregion_name\ninterface: internal"]
    DB_C --> D1["url: postgresql+asyncpg://...\npool_size: 10\nmax_overflow: 20"]
    CEL_C --> CL1["broker_url: redis://...\nresult_backend\ntask_soft_time_limit: 3600s\ntask_time_limit: 7200s"]
    MET_C --> M1["port: 9090\ninterval: 30s"]
```

---

## 4. Migration Engine — State Machine

```mermaid
stateDiagram-v2
    direction LR
    [*] --> PENDING

    PENDING --> PREFLIGHT : execute() called

    PREFLIGHT --> DISK : VM state OK\nRBD lag < 50MB

    state fork_state <<fork>>
    DISK --> fork_state

    fork_state --> MEMORY : LIVE migration\nVM still running
    fork_state --> CUTOVER : COLD migration\nVM stopped

    MEMORY --> CUTOVER : Pre-copy done\nVM paused ~ms

    CUTOVER --> BGP : Volume at dest\nVM created at dest

    BGP --> CLEANUP : IP converged\n~5s

    CLEANUP --> DONE : Source VM deleted

    PREFLIGHT --> FAILED : Check failed
    DISK --> FAILED : Storage error
    MEMORY --> FAILED : QEMU error
    CUTOVER --> FAILED : Nova/Cinder error
    BGP --> FAILED : FRR error
    CLEANUP --> FAILED : Cleanup error

    PENDING --> ABORTED : abort()
    PREFLIGHT --> ABORTED : abort()
    DISK --> ABORTED : abort()
    MEMORY --> ABORTED : abort()

    DONE --> [*]
    FAILED --> [*]
    ABORTED --> [*]
```

---

## 5. Agent Startup Sequence

```mermaid
sequenceDiagram
    participant P as cnf-agent process
    participant LOG as structlog
    participant DB as PostgreSQL
    participant PROM as Prometheus
    participant ETCD as etcd
    participant GRPC as gRPC Server
    participant API as FastAPI Server
    participant BG as Background Tasks

    P->>LOG: configure_logging(format, level)
    Note over LOG: JSON renderer (prod) or\nConsole renderer (dev)

    P->>DB: init_db(settings.database.url)
    DB-->>P: ✅ tables created

    P->>DB: _upsert_self_cluster()
    DB-->>P: ✅ cluster record upserted (status=ONLINE)

    P->>PROM: start_metrics_server(:9090)
    PROM-->>P: ✅ HTTP server running

    P->>ETCD: election.start()
    ETCD-->>P: peer registered at /cnf/raft/peers/<id>
    Note over P,ETCD: _election_loop() starts in background\nAttempts CAS on /cnf/raft/master

    alt Wins election
        ETCD-->>P: lease granted → MASTER
        P->>P: _on_become_master()
    else Loses election
        ETCD-->>P: CAS fail → WORKER
        P->>P: role = WORKER
    end

    P->>GRPC: _start_grpc() → bind :50051
    GRPC-->>P: ✅ CNFControl + CNFPeer serving

    P->>API: _start_api() → uvicorn :8080
    API-->>P: ✅ FastAPI serving

    P->>BG: create_task(_metrics_loop)
    Note over BG: Every 30s:\nos_client.get_cluster_metrics()\n→ insert ClusterMetric\n→ update Prometheus gauges

    P->>BG: create_task(scheduler.run)
    Note over BG: Sleeps if WORKER\nEvaluates policies if MASTER

    P->>P: register SIGTERM / SIGINT handlers
    Note over P: ✅ Agent fully started
```

---

## 6. gRPC Service Interface

```mermaid
classDiagram
    class CNFControl {
        <<service>>
        +GetClusterInfo(ClusterInfoRequest) ClusterInfo
        +GetClusterMetrics(ClusterMetricsRequest) ClusterMetrics
        +ListVMs(ListVMsRequest) ListVMsResponse
        +ExecuteMigration(MigrationRequest) MigrationStatus
        +ExecuteLiveMigration(MigrationRequest) MigrationStatus
        +GetMigrationStatus(MigrationStatusRequest) MigrationStatus
        +AbortMigration(AbortRequest) AbortResponse
        +PromoteRBDImage(RBDImageRequest) RBDImageResponse
        +DemoteRBDImage(RBDImageRequest) RBDImageResponse
        +GetRBDMirrorLag(RBDMirrorLagRequest) RBDMirrorLagResponse
        +AnnounceIP(IPRequest) IPResponse
        +WithdrawIP(IPRequest) IPResponse
    }

    class CNFPeer {
        <<service>>
        +Heartbeat(HeartbeatRequest) HeartbeatResponse
        +RequestVote(VoteRequest) VoteResponse
        +AppendEntries(AppendEntriesRequest) AppendEntriesResponse
        +SyncState(SyncStateRequest) SyncStateResponse
    }

    class ClusterInfo {
        +string id
        +string name
        +string role
        +string auth_url
        +string region
        +string grpc_addr
        +string status
        +string updated_at
    }

    class MigrationStatus {
        +string migration_id
        +string vm_id
        +string source_cluster_id
        +string dest_cluster_id
        +string type
        +string state
        +float progress_pct
        +string error
        +string started_at
        +string finished_at
    }

    CNFControl ..> ClusterInfo : returns
    CNFControl ..> MigrationStatus : returns
```

---

## 7. REST API Route Map

```mermaid
graph LR
    subgraph Health["Health"]
        H1["GET /healthz"]
        H2["GET /readyz"]
    end

    subgraph Clusters["Clusters /v1/clusters"]
        C1["GET /"]
        C2["POST /"]
        C3["GET /{cluster_id}"]
        C4["GET /{cluster_id}/metrics"]
    end

    subgraph VMs["VMs /v1/vms"]
        V1["GET /"]
        V2["GET /{vm_id}"]
        V3["POST /{vm_id}/migrate"]
        V4["POST /{vm_id}/live-migrate"]
    end

    subgraph Migrations["Migrations /v1/migrations"]
        M1["GET /"]
        M2["GET /{migration_id}"]
        M3["GET /{migration_id}/events"]
        M4["POST /{migration_id}/abort"]
    end

    subgraph Master["Master /v1/master"]
        MA1["GET /"]
        MA2["POST /elect"]
    end

    subgraph Metrics["Metrics /v1/metrics"]
        ME1["GET /"]
    end

    V3 -->|"202 Accepted\nmigration_id"| CW["Celery Worker"]
    V4 -->|"202 Accepted\nmigration_id"| CW
```

---

## 8. FastAPI Middleware Chain

```mermaid
flowchart TD
    REQ["Incoming HTTP Request"] --> CORS

    CORS["CORS Middleware\nAllow-Origin: * (configurable)"] --> PROXY

    PROXY{"Proxy Middleware\nAm I master?"} -->|YES| ROUTE
    PROXY -->|NO — forward to master| GRPC_FWD["gRPC proxy\nto master agent"]

    ROUTE["FastAPI Route Handler"] --> EH

    EH{"Exception?"} -->|No| RESP["Return Response"]
    EH -->|Yes| ERR["500 handler\nlog + return detail"]

    GRPC_FWD --> MASTER["Master Agent\nProcesses request directly"]
    MASTER --> RESP
```

---

## 9. Celery Task Queue

```mermaid
graph LR
    subgraph API["API Handler (master)"]
        ENQUEUE["task.delay(migration_id, context)"]
    end

    subgraph Redis["Redis Broker"]
        Q[("Queue:\nmigrations")]
    end

    subgraph Workers["Celery Worker Pool"]
        W1["Worker 1"]
        W2["Worker 2"]
        W3["Worker N"]
    end

    subgraph Retry["Retry Logic"]
        R1["Cold: max_retries=2\nbackoff=30s"]
        R2["Live: max_retries=1\nbackoff=10s"]
    end

    ENQUEUE --> Q
    Q --> W1
    Q --> W2
    Q --> W3

    W1 -->|"on failure"| R1
    W1 -->|"on failure"| R2
    R1 --> Q
    R2 --> Q

    W1 --> ENG["MigrationEngine\n.execute()"]
    W2 --> ENG
    W3 --> ENG
```

---

## 10. Prometheus Metrics Registry

```mermaid
mindmap
    root((Prometheus\nMetrics))
        Counters
            cnf_migrations_total\nlabels type · source · dest · status
            cnf_bgp_announcements_total\nlabels prefix · cluster_id · action
            cnf_rbd_promotions_total\nlabels pool · image
            cnf_grpc_requests_total\nlabels method · status
            cnf_raft_leader_changes_total
        Histograms
            cnf_migration_duration_seconds\nlabels type\nbuckets 5s→3600s
        Gauges
            cnf_migrations_active\nlabels type
            cnf_cluster_vm_count\nlabels cluster_id · cluster_name
            cnf_cluster_cpu_used_pct\nlabels cluster_id
            cnf_cluster_ram_used_pct\nlabels cluster_id
            cnf_cluster_disk_used_pct\nlabels cluster_id
```

---

## 11. Database Schema

```mermaid
erDiagram
    CLUSTER {
        uuid id PK
        varchar name
        text auth_url
        varchar region
        varchar grpc_addr
        integer bgp_as
        varchar bgp_speaker_addr
        varchar role
        varchar status
        jsonb ceph_mon_addrs
        jsonb extra
        timestamptz created_at
        timestamptz updated_at
    }

    CLUSTER_METRIC {
        uuid id PK
        uuid cluster_id FK
        float cpu_used_pct
        float ram_used_pct
        float disk_used_pct
        integer vm_count
        float network_bw_mbps
        timestamptz collected_at
    }

    MIGRATION {
        uuid id PK
        varchar vm_id
        varchar vm_name
        uuid source_cluster_id FK
        uuid dest_cluster_id FK
        varchar type
        varchar state
        float progress_pct
        text error
        jsonb rbd_images
        jsonb vm_ips
        jsonb options
        varchar initiated_by
        timestamptz started_at
        timestamptz finished_at
    }

    MIGRATION_EVENT {
        uuid id PK
        uuid migration_id FK
        varchar state
        text message
        jsonb data
        timestamptz occurred_at
    }

    POLICY {
        uuid id PK
        varchar name
        text description
        boolean enabled
        integer priority
        jsonb rule
        timestamptz created_at
        timestamptz updated_at
    }

    CLUSTER ||--o{ CLUSTER_METRIC : "has metrics"
    CLUSTER ||--o{ MIGRATION : "source of"
    CLUSTER ||--o{ MIGRATION : "destination of"
    MIGRATION ||--o{ MIGRATION_EVENT : "has audit events"
```

---

*For high-level view see [HLD](HLD.md). For deployment specifics see [Deployment Plan](DeploymentPlan.md).*
