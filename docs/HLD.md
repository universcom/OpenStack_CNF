# CNF — High Level Design (HLD)

**Project**: CNF v0.1.0
**Date**: 2026-04-27

---

## 1. Business Problem

Modern multi-datacenter OpenStack deployments suffer from:

- **Resource stranding** — one cluster is over-provisioned while another is at capacity
- **VM lock-in** — moving a VM requires manual effort, downtime, and IP changes
- **No federation** — each OpenStack cluster is an isolated island
- **Operational toil** — migrations require coordinating Nova, Cinder, Neutron, Ceph, and networking manually

CNF solves these problems with a lightweight, decentralized federation layer on top of existing OpenStack.

---

## 2. System Context

```mermaid
graph TB
    subgraph Actors["External Actors"]
        ADMIN["👤 OpenStack Admin"]
        OPS["👷 DevOps / Operator"]
        MON["📊 Monitoring System"]
    end

    subgraph CNF["CNF System"]
        A1["CNF Agent\nCluster 1 (MASTER)"]
        A2["CNF Agent\nCluster 2 (WORKER)"]
        A3["CNF Agent\nCluster 3 (WORKER)"]
        A1 <-->|"gRPC mTLS"| A2
        A1 <-->|"gRPC mTLS"| A3
    end

    subgraph OS["OpenStack Clusters"]
        OS1["Cluster 1\nNova · Neutron · Cinder · Ceph"]
        OS2["Cluster 2\nNova · Neutron · Cinder · Ceph"]
        OS3["Cluster 3\nNova · Neutron · Cinder · Ceph"]
    end

    ADMIN -->|"openstack cnf ...\n(OSC plugin)"| A1
    OPS -->|"REST API\ncurl / HTTP client"| A1
    MON -->|"Prometheus scrape\n:9090/metrics"| A1
    MON -->|"Prometheus scrape"| A2

    A1 --- OS1
    A2 --- OS2
    A3 --- OS3
```

---

## 3. Major Subsystems

```mermaid
graph TB
    subgraph Agent["CNF Agent"]
        subgraph API_SS["API Subsystem"]
            REST["FastAPI REST :8080"]
            OSC["OSC CLI Plugin"]
        end
        subgraph FED_SS["Federation Subsystem"]
            RAFT["Leader Election\nRaft / etcd"]
            GRPC["gRPC Peer Comm\nCNFControl · CNFPeer"]
            PROXY["Request Proxy\nWorker → Master"]
        end
        subgraph MIG_SS["Migration Subsystem"]
            ENGINE["Migration Engine\nState Machine"]
            CELERY["Celery Tasks\nAsync execution"]
        end
        subgraph STOR_SS["Storage Subsystem"]
            CEPH["Ceph RBD\nPromote / Demote"]
        end
        subgraph NET_SS["Network Subsystem"]
            BGP["FRR / BGP\nAnnounce / Withdraw"]
        end
        subgraph COMP_SS["Compute Subsystem"]
            OSSDK["OpenStack SDK\nNova · Neutron · Cinder"]
        end
        subgraph SCHED_SS["Scheduler Subsystem"]
            SCHED["Policy Evaluator\nmaster only · every 30s"]
        end
        subgraph OBS_SS["Observability Subsystem"]
            PROM["Prometheus :9090"]
            LOG["Structured JSON logs"]
        end
    end

    REST --> PROXY
    PROXY --> GRPC
    REST --> ENGINE
    CELERY --> ENGINE
    ENGINE --> CEPH
    ENGINE --> BGP
    ENGINE --> OSSDK
    RAFT -->|"on_become_master"| SCHED
    SCHED --> ENGINE
    ENGINE --> PROM
    ENGINE --> LOG
```

---

## 4. Request Flow — User-Initiated Migration

```mermaid
flowchart TD
    U["👤 User"]
    OSC["openstack cnf vm live-migrate"]
    W_API["REST API\n(Worker Agent)"]
    M_API["REST API\n(Master Agent)"]
    DB[("PostgreSQL\nMigration record")]
    Q[("Redis\nCelery Queue")]
    CW["Celery Worker"]
    ENG["LiveMigrationEngine"]
    SRC["Source Cluster\ngRPC"]
    DST["Dest Cluster\ngRPC"]

    U --> OSC
    OSC -->|"POST /v1/vms/{id}/live-migrate"| W_API
    W_API -->|"Is master? NO\ngRPC proxy"| M_API
    M_API -->|"Create Migration\nstate=PENDING"| DB
    M_API -->|"Enqueue task"| Q
    M_API -->|"202 Accepted\n+ migration_id"| U

    Q -->|"Dequeue"| CW
    CW --> ENG
    ENG -->|"PREFLIGHT → DISK →\nMEMORY → CUTOVER →\nBGP → CLEANUP → DONE"| SRC
    ENG --> DST
    ENG -->|"Persist state\nat every transition"| DB

    U -->|"Poll GET /v1/migrations/{id}"| W_API
    W_API -->|"Returns state + progress"| U
```

---

## 5. Auto-Rebalancing Flow (Policy-Driven)

```mermaid
flowchart TD
    START(["Scheduler.run()\nevery 30s — master only"])

    CHECK{{"Is master?"}}
    SLEEP["Sleep 30s"]
    COLLECT["Collect ClusterSnapshot\nfrom all peers via gRPC"]
    LOAD["Load enabled Policies\nfrom PostgreSQL"]
    FOREACH{{"More policies?"}}
    WINDOW{{"In time window?"}}
    EVAL{{"Trigger condition met?\ne.g. cpu_pct > 80"}}
    TARGET["Find target cluster\ne.g. least_loaded"]
    SELECTVM["Select VMs to migrate\nfrom overloaded cluster"]
    TRIGGER["POST /v1/vms/{id}/live-migrate\n(internal call)"]
    LOG["Log policy applied"]
    DONE(["End cycle"])

    START --> CHECK
    CHECK -->|No| SLEEP --> CHECK
    CHECK -->|Yes| COLLECT
    COLLECT --> LOAD
    LOAD --> FOREACH
    FOREACH -->|No more| DONE
    FOREACH -->|Next policy| WINDOW
    WINDOW -->|Outside window| FOREACH
    WINDOW -->|In window| EVAL
    EVAL -->|Not triggered| FOREACH
    EVAL -->|Triggered| TARGET
    TARGET --> SELECTVM
    SELECTVM --> TRIGGER
    TRIGGER --> LOG
    LOG --> FOREACH
```

---

## 6. Leader Failover Flow

```mermaid
sequenceDiagram
    participant A1 as Agent-1 (MASTER)
    participant etcd as etcd
    participant A2 as Agent-2 (WORKER)
    participant A3 as Agent-3 (WORKER)

    Note over A1: Holds /cnf/raft/master (TTL=10s)

    loop Every 500ms
        A1->>etcd: Refresh lease
        etcd-->>A1: OK
    end

    Note over A1: 💥 CRASH

    etcd->>etcd: Lease expires after 10s
    etcd-->>A2: Watch event — master key deleted
    etcd-->>A3: Watch event — master key deleted

    Note over A2,A3: Wait election_timeout_ms (1500ms)

    A2->>etcd: PUT /cnf/raft/master = "cluster-2"\nIF NOT EXISTS
    etcd-->>A2: ✅ OK — lease granted
    A2->>A2: _on_become_master()\nrole = MASTER\nScheduler starts

    A3->>etcd: PUT /cnf/raft/master = "cluster-3"\nIF NOT EXISTS
    etcd-->>A3: ❌ FAIL — key exists
    A3->>A3: role = WORKER
```

---

## 7. Deployment Model

```mermaid
graph LR
    subgraph DEV["🧪 Development"]
        DC["Docker Compose\nFull stack local\npostgres + redis + etcd\n+ 2 CNF agents"]
    end

    subgraph STG["🔧 Staging / Bare Metal"]
        ANS["Ansible + systemd\nDirect install on\ncontroller host\npython3.11 + pip"]
    end

    subgraph PROD["🚀 Production"]
        HELM["Helm DaemonSet\nKubernetes\nOne pod per\ncontrol-plane node\nhostNetwork: true"]
    end

    DEV -->|"promote config"| STG
    STG -->|"promote config"| PROD
```

---

## 8. Non-Functional Requirements

| Requirement | Target | Mechanism |
| --- | --- | --- |
| **Availability** | 99.9% federation API | Leader re-election < 15s |
| **Live migration downtime** | < 5 seconds | QEMU pre-copy + BGP fast convergence |
| **Migration reliability** | Retry on transient failures | Celery retry + rollback handlers |
| **Observability** | All state transitions logged | structlog + Prometheus |
| **Security** | Encrypted peer comm | mTLS on gRPC |
| **Scalability** | 10+ clusters, 1000+ VMs | Async Python, Celery, connection pool |

---

## 9. Constraints and Assumptions

```mermaid
mindmap
    root((CNF\nConstraints))
        Storage
            Ceph RBD mirroring must be\npre-configured between clusters
            VM disks must be Ceph-backed
        Network
            BGP peering must be established\nFRR installed on controller nodes
            VM IPs must be in\nBGP-routable address space
        Database
            PostgreSQL accessible from\nall CNF agents
            Shared or multi-master replication
        Consensus
            etcd cluster needs ≥3 nodes\nfor production HA
        Runtime
            Python 3.11+ on bare metal\nor Docker/container runtime
        OpenStack
            Nova + Neutron + Cinder + Keystone\nmust be operational per cluster
```

---

*See [Architecture](Architecture.md) for detailed component design. See [LLD](LLD.md) for module-level design.*
