# CNF — Architecture

**Project**: CNF v0.1.0

---

## 1. System Overview

CNF is a decentralized, federated OpenStack management system that enables seamless VM workload distribution, cold migration, and live migration across independent OpenStack clusters — without any external control plane.

---

## 2. Federation Topology

```mermaid
graph TB
    subgraph Fed["CNF Federation"]
        subgraph C1["OpenStack Cluster 1 — MASTER"]
            A1["🖥️ CNF Agent\nREST :8080 | gRPC :50051 | Metrics :9090"]
            OS1["Nova · Neutron · Cinder · Keystone"]
            CEPH1[("Ceph RBD\nPrimary")]
            FRR1["FRR / BGP\nAS 65001"]
            A1 --- OS1
            A1 --- CEPH1
            A1 --- FRR1
        end

        subgraph C2["OpenStack Cluster 2 — WORKER"]
            A2["🖥️ CNF Agent\nREST :8080 | gRPC :50051 | Metrics :9090"]
            OS2["Nova · Neutron · Cinder · Keystone"]
            CEPH2[("Ceph RBD\nMirror")]
            FRR2["FRR / BGP\nAS 65002"]
            A2 --- OS2
            A2 --- CEPH2
            A2 --- FRR2
        end

        subgraph C3["OpenStack Cluster 3 — WORKER"]
            A3["🖥️ CNF Agent\nREST :8080 | gRPC :50051 | Metrics :9090"]
            OS3["Nova · Neutron · Cinder · Keystone"]
            CEPH3[("Ceph RBD\nMirror")]
            FRR3["FRR / BGP\nAS 65003"]
            A3 --- OS3
            A3 --- CEPH3
            A3 --- FRR3
        end

        subgraph Infra["Shared Infrastructure"]
            PG[("🐘 PostgreSQL\n:5432")]
            RD[("🔴 Redis\n:6379")]
            ET[("etcd\n:2379")]
        end

        A1 <-->|"gRPC mTLS"| A2
        A1 <-->|"gRPC mTLS"| A3
        A2 <-->|"gRPC mTLS"| A3

        CEPH1 <-->|"RBD Mirror"| CEPH2
        CEPH1 <-->|"RBD Mirror"| CEPH3

        FRR1 <-->|"BGP"| FRR2
        FRR2 <-->|"BGP"| FRR3

        A1 --> PG
        A2 --> PG
        A3 --> PG
        A1 --> RD
        A2 --> RD
        A3 --> RD
        A1 --> ET
        A2 --> ET
        A3 --> ET
    end
```

---

## 3. Layered Architecture

```mermaid
block-beta
    columns 1
    block:P["🖥️ Presentation Layer"]
        CLI["openstack cnf ... (OSC Plugin)"]
        REST["REST API :8080"]
        SW["Swagger UI"]
    end
    block:A["⚙️ Application Layer"]
        FA["FastAPI App"]
        PM["Proxy Middleware"]
        CT["Celery Tasks"]
        AR["API v1 Router\n/clusters /vms /migrations /master /metrics"]
    end
    block:B["🧠 Business Logic Layer"]
        AG["CNF Agent"]
        ME["Migration Engine"]
        SC["Scheduler"]
        LE["Leader Election (Raft/etcd)"]
    end
    block:I["🔌 Integration Layer"]
        OS["OpenStack SDK\nNova · Neutron · Cinder"]
        CB["Ceph RBD Client"]
        BG["FRR/BGP Client"]
    end
    block:IN["🏗️ Infrastructure Layer"]
        PG["PostgreSQL"]
        RD["Redis"]
        ET["etcd"]
        GR["gRPC (mTLS)"]
        PR["Prometheus :9090"]
    end
```

---

## 4. CNF Agent — Internal Component View

```mermaid
graph TB
    subgraph Agent["CNF Agent Process"]
        API["REST API\nFastAPI :8080"]
        PMW["Proxy Middleware\n(worker → master forward)"]
        GRPC["gRPC Server :50051\nCNFControl + CNFPeer"]
        RAFT["Raft Election\netcd lease TTL"]
        ENG["Migration Engine\nState Machine"]
        SCHED["Scheduler\n(master only)"]
        METR["Metrics Loop\nevery 30s"]
        PROM["Prometheus\n:9090"]

        API --> PMW
        PMW --> ENG
        GRPC --> ENG
        RAFT -->|"on_become_master"| SCHED
        SCHED --> ENG
        METR --> PROM
    end

    subgraph Deps["Dependencies"]
        OSC["OpenStack SDK"]
        CEP["Ceph RBD"]
        BGP["FRR vtysh"]
        DB[("PostgreSQL")]
        RDS[("Redis")]
        ETC[("etcd")]
    end

    ENG --> OSC
    ENG --> CEP
    ENG --> BGP
    ENG --> DB
    Agent --> RDS
    RAFT --> ETC
    METR --> DB
```

---

## 5. Leader Election

```mermaid
stateDiagram-v2
    [*] --> FOLLOWER : Agent starts

    FOLLOWER --> CANDIDATE : election_timeout_ms elapsed\n(default 1500ms)

    CANDIDATE --> LEADER : CAS on /cnf/raft/master\n(etcd put_if_not_exists)

    CANDIDATE --> FOLLOWER : CAS failed\n(another node won)

    LEADER --> FOLLOWER : Lost lease\n(crash or network partition)

    LEADER --> LEADER : Heartbeat every 500ms\n(refresh lease TTL=10s)

    note right of LEADER
        Runs Scheduler
        Accepts direct API writes
        Updates role=MASTER in DB
    end note

    note right of FOLLOWER
        Proxies all write requests
        to master via gRPC
    end note
```

---

## 6. Cold Migration Flow

```mermaid
sequenceDiagram
    participant U as 👤 User
    participant API as REST API
    participant Q as Redis Queue
    participant W as Celery Worker
    participant SRC as Source Cluster
    participant DST as Dest Cluster
    participant CEPH as Ceph RBD
    participant BGP as FRR/BGP

    U->>API: POST /v1/vms/{vm_id}/migrate
    API->>Q: Enqueue cold migration task
    API-->>U: 202 Accepted + migration_id

    Q->>W: Dequeue task

    W->>SRC: GetVM → verify ACTIVE or SHUTOFF
    W->>CEPH: get_mirror_status → lag < 50MB ✓

    Note over W,SRC: DISK phase
    W->>SRC: Stop VM
    W->>CEPH: Wait final RBD sync
    W->>CEPH: demote_image (source → non-primary)
    W->>CEPH: promote_image (dest → primary)

    Note over W,DST: CUTOVER phase
    W->>DST: Register volume in Cinder
    W->>DST: Create VM in Nova

    Note over BGP: BGP phase
    W->>BGP: withdraw(vm_ip, source_nexthop)
    W->>BGP: announce(vm_ip, dest_nexthop)
    W->>BGP: check_convergence() ~5s

    Note over W,SRC: CLEANUP phase
    W->>SRC: Delete VM from Nova

    W-->>U: Migration DONE ✓
```

---

## 7. Live Migration Flow

```mermaid
sequenceDiagram
    participant U as 👤 User
    participant API as REST API
    participant Q as Redis Queue
    participant W as Celery Worker
    participant SRC as Source Cluster
    participant DST as Dest Cluster
    participant CEPH as Ceph RBD
    participant BGP as FRR/BGP

    U->>API: POST /v1/vms/{vm_id}/live-migrate
    API->>Q: Enqueue live migration task
    API-->>U: 202 Accepted + migration_id

    Q->>W: Dequeue task

    Note over W,CEPH: PREFLIGHT
    W->>SRC: GetVM → verify ACTIVE ✓
    W->>CEPH: get_mirror_status → lag < 50MB ✓

    Note over W: DISK (VM still running!)
    W->>CEPH: Verify Ceph sync status

    Note over W,DST: MEMORY (VM still running!)
    W->>SRC: Open QEMU memory tunnel
    W->>DST: Accept incoming memory pages
    SRC-->>DST: Pre-copy dirty pages (iterative)

    Note over W: CUTOVER (VM paused ~ms)
    W->>SRC: Flush final dirty RBD blocks
    W->>CEPH: demote_image (source)
    W->>CEPH: promote_image (dest)
    W->>DST: Create + resume VM in Nova

    Note over BGP: BGP phase
    W->>BGP: withdraw(vm_ip, source_nexthop)
    W->>BGP: announce(vm_ip, dest_nexthop)
    W->>BGP: check_convergence() ~5s

    Note over W,SRC: CLEANUP
    W->>SRC: Remove VM record from Nova

    W-->>U: Migration DONE ✓ (zero-downtime)
```

---

## 8. Migration State Machine

```mermaid
stateDiagram-v2
    direction LR
    [*] --> PENDING : Migration created

    PENDING --> PREFLIGHT : Engine starts

    PREFLIGHT --> DISK : Checks passed\n(VM state + RBD lag OK)

    DISK --> MEMORY : Live only\n(Ceph sync verified,\nVM still running)
    DISK --> CUTOVER : Cold only\n(VM stopped,\nRBD promoted/demoted)

    MEMORY --> CUTOVER : Memory pre-copy done\n(VM paused ms)

    CUTOVER --> BGP : Volume at dest,\nVM created

    BGP --> CLEANUP : IP routes converged\n(~5s BGP convergence)

    CLEANUP --> DONE : Source VM deleted

    PREFLIGHT --> FAILED : Verification error
    DISK --> FAILED : RBD/storage error
    MEMORY --> FAILED : QEMU tunnel error
    CUTOVER --> FAILED : Nova/Cinder error
    BGP --> FAILED : FRR/routing error
    CLEANUP --> FAILED : Cleanup error

    PENDING --> ABORTED : User abort
    PREFLIGHT --> ABORTED : User abort
    DISK --> ABORTED : User abort
    MEMORY --> ABORTED : User abort

    DONE --> [*]
    FAILED --> [*]
    ABORTED --> [*]
```

---

## 9. Storage Architecture (Ceph RBD)

```mermaid
graph LR
    subgraph SRC_CEPH["Source Ceph Cluster"]
        SI["Pool: vms\nImage: vm-uuid-123\n🟢 PRIMARY\nup+replaying"]
    end

    subgraph DST_CEPH["Destination Ceph Cluster"]
        DI["Pool: vms\nImage: vm-uuid-123\n🔵 NON-PRIMARY\nup+replaying"]
    end

    SI -->|"RBD Mirror\n(continuous sync)"| DI

    subgraph CUTOVER["During CUTOVER"]
        SI2["Source\n🔴 DEMOTED\nnon-primary"]
        DI2["Dest\n🟢 PROMOTED\nprimary"]
        SI2 -.->|"Lag threshold\n< 50MB / 30s"| DI2
    end

    SI -->|"demote_image()"| SI2
    DI -->|"promote_image()"| DI2
```

---

## 10. Network / BGP Architecture

```mermaid
graph TB
    subgraph BGP_TOPO["BGP Federation Topology"]
        RR["Route Reflector\n(optional)"]

        subgraph CL1["Cluster 1 — AS 65001"]
            FRR1["FRR Daemon\n10.0.1.50/32 → nexthop1"]
        end
        subgraph CL2["Cluster 2 — AS 65002"]
            FRR2["FRR Daemon"]
        end
        subgraph CL3["Cluster 3 — AS 65003"]
            FRR3["FRR Daemon"]
        end

        RR <-->|BGP| FRR1
        RR <-->|BGP| FRR2
        RR <-->|BGP| FRR3
        FRR1 <-->|iBGP| FRR2
        FRR2 <-->|iBGP| FRR3
    end

    subgraph FLOW["Migration IP Handoff"]
        direction LR
        BEF["Before Migration\nVM IP 10.0.1.50/32\nannounced by Cluster 1"]
        AFT["After Migration\nVM IP 10.0.1.50/32\nannounced by Cluster 2"]
        BEF -->|"withdraw + announce\nBGP convergence ~5s"| AFT
    end
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
    MIGRATION ||--o{ MIGRATION_EVENT : "has events"
```

---

## 12. Security Architecture

```mermaid
graph TB
    subgraph SEC["Security Boundaries"]
        subgraph EXT["External Access"]
            REST_SEC["REST API :8080\n→ JWT auth (optional)\n→ CORS policy\n→ Rate limiting"]
        end

        subgraph PEER["Peer Communication"]
            GRPC_SEC["gRPC :50051\n→ mTLS (mutual TLS)\n→ CA + server cert + client cert\n→ /etc/cnf/tls/"]
        end

        subgraph PROC["Process Isolation"]
            CONT_SEC["Container user: cnf (non-root)\nK8s: no privileged (except hostNetwork)\nSecrets via K8s Secrets / env vars"]
        end
    end

    CERTS["/etc/cnf/tls/\nca.crt\nserver.crt\nserver.key"]
    GRPC_SEC --> CERTS
```

---

## 13. Technology Stack

| Layer | Technology | Version | Purpose |
| --- | --- | --- | --- |
| Language | Python | 3.11+ | Core agent runtime |
| API Framework | FastAPI | ≥ 0.110 | REST API server |
| ASGI Server | Uvicorn | ≥ 0.27 | Async HTTP |
| RPC Framework | gRPC | ≥ 1.62 | Peer communication |
| ORM | SQLAlchemy | ≥ 2.0 | Async ORM |
| Database | PostgreSQL | ≥ 14 | Persistent state |
| Task Queue | Celery + Redis | ≥ 5.3 | Async migration tasks |
| Leader Election | etcd | ≥ 3.5 | Distributed consensus |
| Storage | Ceph RBD | ≥ Pacific | VM disk mirroring |
| Networking | FRR | ≥ 8.0 | BGP route management |
| OpenStack | openstacksdk | ≥ 3.0 | Nova/Neutron/Cinder |
| Observability | Prometheus + structlog | latest | Metrics + logging |
| Config | Pydantic | ≥ 2.0 | Settings validation |
| DB Migrations | Alembic | ≥ 1.13 | Schema versioning |

---

*For deployment details see [Deployment Plan](DeploymentPlan.md). For startup order see [Startup Guide](StartupGuide.md).*
