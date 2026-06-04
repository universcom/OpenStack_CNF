# CNF — UML Diagrams

**Project**: CNF v0.1.0

> All diagrams use [Mermaid](https://mermaid.js.org/) and render natively in GitHub, GitLab, VS Code (Mermaid extension), and Obsidian.

---

## 1. Use Case Diagram

```mermaid
graph LR
    subgraph Actors
        ADMIN["👤 OpenStack Admin"]
        AUTO["🤖 Scheduler (automated)"]
        MON["📊 Monitoring System"]
    end

    subgraph UC["CNF Use Cases"]
        UC1["List Clusters"]
        UC2["View Cluster Metrics"]
        UC3["Register New Cluster"]
        UC4["List VMs Across Clusters"]
        UC5["Cold Migrate VM"]
        UC6["Live Migrate VM"]
        UC7["Monitor Migration Progress"]
        UC8["Abort Migration"]
        UC9["View Migration Audit Events"]
        UC10["Show Current Master"]
        UC11["Force Master Transfer"]
        UC12["Manage Policies"]
        UC13["Auto Rebalance via Policy"]
        UC14["Scrape Prometheus Metrics"]
    end

    ADMIN --> UC1
    ADMIN --> UC2
    ADMIN --> UC3
    ADMIN --> UC4
    ADMIN --> UC5
    ADMIN --> UC6
    ADMIN --> UC7
    ADMIN --> UC8
    ADMIN --> UC9
    ADMIN --> UC10
    ADMIN --> UC11
    ADMIN --> UC12
    AUTO --> UC13
    UC13 -->|"triggers"| UC6
    MON --> UC14
    MON --> UC2
```

---

## 2. Class Diagram — Full System

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
        +start() None
        +run_forever() None
        +stop() None
        +is_master bool
        +master_grpc_addr str
    }

    class RaftElection {
        +str cluster_id
        +str grpc_addr
        +RaftConfig config
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

    class MigrationContext {
        +UUID migration_id
        +str vm_id
        +str source_cluster_id
        +str dest_cluster_id
        +MigrationType type
        +dict options
        +VMDetails vm
        +list rbd_images
        +list vm_ips
        +MigrationState state
        +float progress
    }

    class OpenStackClient {
        +get_vm(vm_id) VMDetails
        +list_vms() list
        +create_server(spec) VMDetails
        +delete_server(vm_id) None
        +get_cluster_metrics() dict
    }

    class CephRBDClient {
        +CephConfig config
        +promote_image(pool, image) None
        +demote_image(pool, image) None
        +get_mirror_status(pool, image) MirrorStatus
    }

    class FRRClient {
        +BGPConfig config
        +announce(prefix, next_hop) None
        +withdraw(prefix) None
        +check_convergence(prefix) bool
    }

    class Cluster {
        +UUID id
        +str name
        +str auth_url
        +str grpc_addr
        +str role
        +str status
    }

    class Migration {
        +UUID id
        +str vm_id
        +str type
        +str state
        +float progress_pct
    }

    class Policy {
        +UUID id
        +str name
        +bool enabled
        +int priority
        +dict rule
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
    MigrationEngine --> Migration : persists to
    Scheduler --> OpenStackClient : uses
    Scheduler --> Policy : reads
    Scheduler --> MigrationEngine : triggers
    Cluster "1" --> "many" Migration : source
    Cluster "1" --> "many" Migration : destination
```

---

## 3. Sequence Diagram — Live Migration

```mermaid
sequenceDiagram
    actor User
    participant CLI as OSC CLI
    participant WrkAPI as REST API (Worker)
    participant MstAPI as REST API (Master)
    participant Redis as Redis Queue
    participant Worker as Celery Worker
    participant Engine as LiveMigrationEngine
    participant SrcGRPC as Source gRPC
    participant DstGRPC as Dest gRPC
    participant Ceph as Ceph RBD
    participant BGP as FRR/BGP
    participant DB as PostgreSQL

    User->>CLI: openstack cnf vm live-migrate vm-1 --to cluster-2
    CLI->>WrkAPI: POST /v1/vms/vm-1/live-migrate

    WrkAPI->>WrkAPI: Am I master? NO
    WrkAPI->>MstAPI: gRPC proxy: ExecuteLiveMigration

    MstAPI->>DB: INSERT migration (state=PENDING)
    MstAPI->>Redis: Enqueue run_live_migration task
    MstAPI-->>CLI: 202 Accepted + migration_id

    Redis->>Worker: Dequeue task
    Worker->>Engine: execute()

    rect rgb(230, 245, 255)
        Note over Engine,SrcGRPC: PREFLIGHT
        Engine->>SrcGRPC: GetVM → status=ACTIVE ✓
        Engine->>Ceph: get_mirror_status → lag=12MB ✓
        Engine->>DB: state=PREFLIGHT, progress=10%
    end

    rect rgb(255, 248, 220)
        Note over Engine: DISK (VM still running)
        Engine->>Ceph: Verify Ceph sync status
        Engine->>DB: state=DISK, progress=25%
    end

    rect rgb(220, 255, 220)
        Note over Engine,DstGRPC: MEMORY (VM still running)
        Engine->>SrcGRPC: Open QEMU memory tunnel
        SrcGRPC-->>DstGRPC: Pre-copy dirty memory pages
        Engine->>DB: state=MEMORY, progress=50%
    end

    rect rgb(255, 235, 235)
        Note over Engine: CUTOVER (VM paused ~milliseconds)
        Engine->>Ceph: Flush final dirty RBD blocks
        Engine->>Ceph: demote_image(source)
        Engine->>Ceph: promote_image(dest)
        Engine->>DstGRPC: Create + resume VM in Nova
        Engine->>DB: state=CUTOVER, progress=75%
    end

    rect rgb(240, 230, 255)
        Note over BGP: BGP phase
        Engine->>BGP: withdraw(vm_ip, src_nexthop)
        Engine->>BGP: announce(vm_ip, dst_nexthop)
        Engine->>BGP: check_convergence() ~5s
        Engine->>DB: state=BGP, progress=90%
    end

    rect rgb(235, 255, 235)
        Note over SrcGRPC: CLEANUP
        Engine->>SrcGRPC: Delete VM from Nova
        Engine->>DB: state=DONE, progress=100%
    end

    User->>CLI: openstack cnf vm status migration-id
    CLI->>WrkAPI: GET /v1/migrations/migration-id
    WrkAPI-->>CLI: state=DONE, progress=100%
```

---

## 4. Sequence Diagram — Leader Election & Failover

```mermaid
sequenceDiagram
    participant A1 as Agent-1 (Cluster-1)
    participant A2 as Agent-2 (Cluster-2)
    participant A3 as Agent-3 (Cluster-3)
    participant etcd as etcd cluster

    Note over A1,A3: Startup — all agents register peers

    A1->>etcd: PUT /cnf/raft/peers/cluster-1 = "os1:50051"
    A2->>etcd: PUT /cnf/raft/peers/cluster-2 = "os2:50051"
    A3->>etcd: PUT /cnf/raft/peers/cluster-3 = "os3:50051"

    Note over A1,etcd: Election race

    A1->>etcd: PUT /cnf/raft/master = "cluster-1" IF NOT EXISTS
    etcd-->>A1: ✅ OK — lease granted (TTL=10s)
    A1->>A1: role = MASTER, Scheduler starts

    A2->>etcd: PUT /cnf/raft/master = "cluster-2" IF NOT EXISTS
    etcd-->>A2: ❌ FAIL — key exists
    A2->>A2: role = WORKER

    A3->>etcd: PUT /cnf/raft/master = "cluster-3" IF NOT EXISTS
    etcd-->>A3: ❌ FAIL — key exists
    A3->>A3: role = WORKER

    loop Heartbeat every 500ms
        A1->>etcd: Refresh lease
        etcd-->>A1: OK
    end

    Note over A1: 💥 Agent-1 crashes

    etcd->>etcd: Lease TTL expires (10s)
    etcd-->>A2: Watch event — master key deleted
    etcd-->>A3: Watch event — master key deleted

    Note over A2,A3: Wait election_timeout_ms = 1500ms

    A2->>etcd: PUT /cnf/raft/master = "cluster-2" IF NOT EXISTS
    etcd-->>A2: ✅ OK — new lease granted
    A2->>A2: role = MASTER\n_on_become_master()\nScheduler starts

    A3->>etcd: PUT /cnf/raft/master = "cluster-3" IF NOT EXISTS
    etcd-->>A3: ❌ FAIL
    A3->>A3: role = WORKER
```

---

## 5. State Diagram — Migration States

```mermaid
stateDiagram-v2
    direction LR

    [*] --> PENDING : Migration created

    PENDING --> PREFLIGHT : Engine starts

    PREFLIGHT --> DISK : VM state OK\nRBD lag < 50MB

    DISK --> MEMORY : LIVE — VM still running\nCeph sync verified
    DISK --> CUTOVER : COLD — VM stopped\nRBD demoted + promoted

    MEMORY --> CUTOVER : Pre-copy done\nVM paused ~ms

    CUTOVER --> BGP : VM created at dest\nVolume registered

    BGP --> CLEANUP : IP routes converged\n~5s BGP convergence

    CLEANUP --> DONE : Source VM deleted

    PREFLIGHT --> FAILED : Verification error
    DISK --> FAILED : Storage error
    MEMORY --> FAILED : QEMU error
    CUTOVER --> FAILED : Nova / Cinder error
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

## 6. State Diagram — Raft Leader Election

```mermaid
stateDiagram-v2
    [*] --> FOLLOWER : Agent starts

    FOLLOWER --> CANDIDATE : election_timeout elapsed\n(1500ms)

    CANDIDATE --> LEADER : CAS on etcd master key\nsucceeded

    CANDIDATE --> FOLLOWER : CAS failed\n(another node won)

    LEADER --> FOLLOWER : Lease lost\n(crash or network split)

    LEADER --> LEADER : Heartbeat refresh\nevery 500ms

    note right of LEADER
        Runs Scheduler
        Accepts API writes directly
        Updates DB role = MASTER
    end note

    note right of FOLLOWER
        Proxies write requests
        to master via gRPC
        Watches etcd master key
    end note
```

---

## 7. Activity Diagram — Scheduler Policy Evaluation

```mermaid
flowchart TD
    START(["Scheduler loop starts\nevery 30s"]) --> MC

    MC{{"Is master?"}} -->|No| SLP["Sleep 30s"] --> MC
    MC -->|Yes| COL["Collect ClusterSnapshot\nfrom all peers via gRPC"]

    COL --> LOAD["Load enabled Policies\nfrom PostgreSQL"]

    LOAD --> FP{{"More policies?"}}
    FP -->|No| SLP2["Sleep 30s"] --> MC

    FP -->|Next policy| WIN{{"In time window?\ne.g. 02:00–06:00"}}
    WIN -->|Outside window| FP

    WIN -->|In window| TRIG{{"Trigger condition met?\ne.g. cpu_pct > 80"}}
    TRIG -->|Not triggered| FP

    TRIG -->|Triggered| TARGET["Find target cluster\nstrategy: least_loaded"]
    TARGET --> SELVM["Select VMs to migrate\nfrom overloaded cluster"]

    SELVM --> FVM{{"More VMs?"}}
    FVM -->|No| FP

    FVM -->|Next VM| MIG["POST /v1/vms/{id}/live-migrate"]
    MIG --> LOG["Log policy applied\nPrometheus counter++"]
    LOG --> FVM
```

---

## 8. Component Diagram (C4 Style)

```mermaid
graph TB
    subgraph EXT["External Actors"]
        USER["👤 Operator"]
        PROM_SRV["📊 Prometheus Server"]
    end

    subgraph AGENT["CNF Agent (per controller node)"]
        REST_C["REST API\nFastAPI :8080"]
        PROXY_C["Proxy Middleware"]
        GRPC_C["gRPC Server :50051\nCNFControl + CNFPeer"]
        RAFT_C["Raft Election\netcd lease"]
        ENG_C["Migration Engine\nState Machine"]
        SCHED_C["Scheduler\nmaster only"]
        METR_C["Metrics Collector\nevery 30s"]
        PROM_C["Prometheus Exporter\n:9090"]
    end

    subgraph DEPS["Dependencies"]
        OS_C["OpenStack SDK\nNova · Neutron · Cinder"]
        CEP_C["Ceph RBD Client\nlibrbd + rbd CLI"]
        BGP_C["FRR Client\nvtysh"]
        DB_C[("PostgreSQL")]
        RD_C[("Redis")]
        ET_C[("etcd")]
    end

    USER -->|"HTTP"| REST_C
    PROM_SRV -->|"scrape"| PROM_C

    REST_C --> PROXY_C
    PROXY_C -->|"if worker"| GRPC_C
    PROXY_C -->|"if master"| ENG_C
    GRPC_C --> ENG_C
    RAFT_C -->|"on_become_master"| SCHED_C
    SCHED_C --> ENG_C
    METR_C --> PROM_C
    METR_C --> DB_C

    ENG_C --> OS_C
    ENG_C --> CEP_C
    ENG_C --> BGP_C
    ENG_C --> DB_C
    RAFT_C --> ET_C
    ENG_C --> RD_C
```

---

## 9. Deployment Diagram — Kubernetes

```mermaid
graph TB
    subgraph K8S["Kubernetes Cluster"]
        subgraph NS["Namespace: openstack"]
            subgraph DS["DaemonSet: cnf"]
                subgraph CP1["Control-Plane Node 1"]
                    POD1["cnf Pod\nREST :8080 | gRPC :50051 | Metrics :9090\nhostNetwork: true\nMounts: /etc/ceph · /var/run/libvirt · /var/run/frr"]
                end
                subgraph CP2["Control-Plane Node 2"]
                    POD2["cnf Pod\nREST :8080 | gRPC :50051 | Metrics :9090\nhostNetwork: true"]
                end
                subgraph CP3["Control-Plane Node 3"]
                    POD3["cnf Pod\nREST :8080 | gRPC :50051 | Metrics :9090\nhostNetwork: true"]
                end
            end
            SECS["Secrets\ncnf-db-secret\ncnf-openstack-secret\ncnf-ceph-secret\ncnf-grpc-tls"]
        end
    end

    subgraph SHARED["Shared Infrastructure"]
        PG_D[("PostgreSQL")]
        RD_D[("Redis")]
        ET_D[("etcd\n3-node")]
    end

    subgraph OPENSTACK["OpenStack"]
        NOV["Nova"]
        NEU["Neutron"]
        CIN["Cinder"]
    end

    subgraph STORAGE["Storage + Network"]
        CEP_D["Ceph Cluster\nRBD mirroring"]
        FRR_D["FRR daemon\n(per node, host process)"]
    end

    POD1 <-->|"gRPC mTLS"| POD2
    POD2 <-->|"gRPC mTLS"| POD3
    POD1 <-->|"gRPC mTLS"| POD3

    POD1 -.->|"reads"| SECS
    POD2 -.->|"reads"| SECS
    POD3 -.->|"reads"| SECS

    POD1 --> PG_D
    POD1 --> RD_D
    POD1 --> ET_D
    POD1 --> NOV
    POD1 --> NEU
    POD1 --> CIN
    POD1 --> CEP_D
    POD1 --> FRR_D
```

---

## 10. Entity-Relationship Diagram

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
    MIGRATION ||--o{ MIGRATION_EVENT : "has events"
```

---

## 11. Sequence Diagram — Cold Migration

```mermaid
sequenceDiagram
    actor User
    participant API as REST API (Master)
    participant Redis as Redis Queue
    participant Worker as Celery Worker
    participant Engine as ColdMigrationEngine
    participant SrcOS as Source OpenStack
    participant Ceph as Ceph RBD
    participant DstOS as Dest OpenStack
    participant BGP as FRR/BGP
    participant DB as PostgreSQL

    User->>API: POST /v1/vms/vm-1/migrate
    API->>DB: INSERT migration (PENDING)
    API->>Redis: Enqueue run_cold_migration
    API-->>User: 202 Accepted + migration_id

    Redis->>Worker: Dequeue
    Worker->>Engine: execute()

    rect rgb(230,245,255)
        Note over Engine,SrcOS: PREFLIGHT
        Engine->>SrcOS: GetVM → ACTIVE or SHUTOFF ✓
        Engine->>Ceph: get_mirror_status → lag < 50MB ✓
        Engine->>DB: state=PREFLIGHT
    end

    rect rgb(255,248,220)
        Note over Engine,Ceph: DISK (VM stopped)
        Engine->>SrcOS: Stop VM
        Engine->>Ceph: Wait final RBD sync
        Engine->>Ceph: demote_image(source)
        Engine->>Ceph: promote_image(dest)
        Engine->>DB: state=DISK
    end

    rect rgb(255,235,235)
        Note over Engine,DstOS: CUTOVER
        Engine->>DstOS: Register volume in Cinder
        Engine->>DstOS: Create VM in Nova
        Engine->>DB: state=CUTOVER
    end

    rect rgb(240,230,255)
        Note over BGP: BGP
        Engine->>BGP: withdraw(vm_ip, src_nexthop)
        Engine->>BGP: announce(vm_ip, dst_nexthop)
        Engine->>BGP: check_convergence() ~5s
        Engine->>DB: state=BGP
    end

    rect rgb(235,255,235)
        Note over SrcOS: CLEANUP
        Engine->>SrcOS: Delete VM from Nova
        Engine->>DB: state=DONE, progress=100%
    end

    User->>API: GET /v1/migrations/migration-id
    API-->>User: state=DONE ✓
```

---

*Diagrams render with [Mermaid](https://mermaid.js.org/). View them in GitHub, GitLab, VS Code, or [mermaid.live](https://mermaid.live).*
