# CNF — Startup Guide

**Project**: CNF v0.1.0

> Step-by-step startup guide for all CNF containers/services on controller nodes, in the correct order.

---

## 1. Required Startup Order

```mermaid
flowchart TD
    S1["Step 1\n🏗️ Infrastructure\nPostgreSQL · Redis · etcd"]
    S2["Step 2\n🗄️ Database Schema Migration\ncnf-manage db upgrade head"]
    S3["Step 3\n🖥️ CNF Agents\n(one per controller node)"]
    S4["Step 4\n⚙️ Celery Workers\n(migration task executor)"]
    S5["Step 5\n🕐 Celery Beat\n(periodic scheduler — optional)"]
    S6["Step 6\n✅ Verify & Validate"]

    S1 -->|"all healthy"| S2
    S2 -->|"tables created"| S3
    S3 -->|"agents healthy + leader elected"| S4
    S4 -->|"workers pinging"| S5
    S5 --> S6
```

---

## 2. Container Dependency Graph

```mermaid
graph TD
    subgraph INFRA["Infrastructure (must be healthy first)"]
        PG["🐘 PostgreSQL\n:5432"]
        RD["🔴 Redis\n:6379"]
        ET["etcd\n:2379"]
    end

    subgraph SCHEMA["Schema Migration (run once)"]
        DB_MIG["cnf-manage db upgrade head\n→ creates all tables"]
    end

    subgraph AGENTS["CNF Agents (parallel start)"]
        A1["CNF Agent — Node 1\n🟡 MASTER candidate\nREST :8080 | gRPC :50051 | Metrics :9090"]
        A2["CNF Agent — Node 2\n🔵 WORKER\nREST :8080 | gRPC :50051 | Metrics :9090"]
        A3["CNF Agent — Node 3\n🔵 WORKER\nREST :8080 | gRPC :50051 | Metrics :9090"]
    end

    subgraph ASYNC["Async Workers (start after agents)"]
        CW["⚙️ Celery Worker\nmigration tasks"]
        CB["🕐 Celery Beat\nperiodic tasks"]
    end

    subgraph OBSERV["Observability (optional)"]
        PR["📊 Prometheus\n:9093"]
        GR["📈 Grafana\n:3000"]
    end

    PG -->|"healthy"| DB_MIG
    RD -->|"healthy"| DB_MIG
    ET -->|"healthy"| DB_MIG

    DB_MIG --> A1
    DB_MIG --> A2
    DB_MIG --> A3

    PG -->|"healthcheck"| A1
    RD -->|"healthcheck"| A1
    ET -->|"healthcheck"| A1
    PG -->|"healthcheck"| A2
    RD -->|"healthcheck"| A2
    ET -->|"healthcheck"| A2

    A1 -->|"agents healthy"| CW
    A1 -->|"agents healthy"| CB

    A1 -->|"scrape :9090"| PR
    A2 -->|"scrape :9090"| PR
    PR --> GR
```

---

## 3. Step 1 — Start Infrastructure

### Kubernetes

```bash
# Verify PostgreSQL
kubectl -n openstack exec -it \
  $(kubectl -n openstack get pod -l app=postgresql -o name | head -1) \
  -- pg_isready -U cnf
# Expected: /var/run/postgresql:5432 - accepting connections

# Verify Redis
kubectl -n openstack exec -it \
  $(kubectl -n openstack get pod -l app=redis -o name | head -1) \
  -- redis-cli ping
# Expected: PONG

# Verify etcd
kubectl -n openstack exec -it \
  $(kubectl -n openstack get pod -l app=etcd -o name | head -1) \
  -- etcdctl endpoint health
# Expected: 127.0.0.1:2379 is healthy
```

### Bare Metal (systemd)

```bash
sudo systemctl start postgresql redis etcd
sudo systemctl enable postgresql redis etcd

# Verify
sudo systemctl status postgresql redis etcd
pg_isready -h localhost -U cnf
redis-cli ping                              # → PONG
etcdctl endpoint health --endpoints=localhost:2379
```

### Docker Compose (Development)

```bash
cd openstack-cnf
docker compose up -d postgres redis etcd

# Wait for all three to be healthy
docker compose ps
# STATE column must show: healthy
```

---

## 4. Step 2 — Database Schema Migration

```mermaid
flowchart LR
    MIG["cnf-manage db upgrade head"]

    subgraph TABLES["Tables created"]
        T1["clusters"]
        T2["cluster_metrics"]
        T3["migrations"]
        T4["migration_events"]
        T5["policies"]
    end

    MIG --> T1
    MIG --> T2
    MIG --> T3
    MIG --> T4
    MIG --> T5
```

```bash
# Kubernetes
kubectl -n openstack run cnf-migrate \
  --image=registry.example.com/cnf/agent:0.1.0 \
  --rm -it --restart=Never \
  --env="CNF_DATABASE__URL=postgresql+asyncpg://cnf:PASS@postgres:5432/cnf" \
  -- cnf-manage db upgrade head

# Bare Metal
export CNF_DATABASE__URL="postgresql+asyncpg://cnf:PASS@localhost:5432/cnf"
cnf-manage db upgrade head

# Docker Compose
docker compose run --rm cnf-agent-1 cnf-manage db upgrade head

# Verify
psql -h localhost -U cnf -d cnf -c "\dt"
# Expected: clusters, cluster_metrics, migrations, migration_events, policies
```

---

## 5. Step 3 — Start CNF Agents

### Agent Internal Startup Sequence

```mermaid
sequenceDiagram
    participant P as cnf-agent process
    participant LOG as structlog
    participant DB as PostgreSQL
    participant ETCD as etcd
    participant GRPC as gRPC :50051
    participant API as FastAPI :8080
    participant PROM as Prometheus :9090
    participant BG as Background Tasks

    P->>LOG: configure_logging(format, level)

    P->>DB: init_db() — create tables (idempotent)
    DB-->>P: ✅

    P->>DB: _upsert_self_cluster() — status=ONLINE
    DB-->>P: ✅ cluster record upserted

    P->>PROM: start_metrics_server(:9090)
    PROM-->>P: ✅ HTTP server ready

    P->>ETCD: election.start()
    Note over P,ETCD: Register /cnf/raft/peers/<id>\nStart _election_loop()

    alt Wins CAS on /cnf/raft/master
        ETCD-->>P: ✅ MASTER — lease granted TTL=10s
        P->>P: _on_become_master() — Scheduler starts
    else Loses CAS
        ETCD-->>P: WORKER — key already held
        P->>P: role=WORKER — proxy mode
    end

    P->>GRPC: bind :50051 (CNFControl + CNFPeer)
    GRPC-->>P: ✅ serving

    P->>API: uvicorn :8080 (FastAPI + proxy MW)
    API-->>P: ✅ serving

    P->>BG: create_task(_metrics_loop)\nevery 30s → ClusterMetric → Prometheus
    P->>BG: create_task(scheduler.run)\nevaluates policies every 30s if MASTER

    Note over P: ✅ Agent fully started\nSIGTERM / SIGINT handlers registered
```

### Agents on Kubernetes

```bash
helm upgrade --install cnf deploy/helm/cnf/ \
  --namespace openstack \
  --set cluster.id="$(uuidgen | tr '[:upper:]' '[:lower:]')" \
  --set cluster.name="openstack-prod-1" \
  --set cluster.grpcAddr="$(hostname -f):50051" \
  --set 'peers=["os2-ctrl:50051","os3-ctrl:50051"]' \
  --set image.tag="0.1.0" \
  --values deploy/helm/cnf/values.yaml

# Monitor rollout
kubectl -n openstack rollout status daemonset/cnf

# Stream logs
kubectl -n openstack logs -l app=cnf -f
```

**Expected log events (in order):**

```json
{"event": "db_initialized", "level": "info"}
{"event": "cluster_upserted", "cluster_id": "...", "level": "info"}
{"event": "metrics_server_started", "port": 9090, "level": "info"}
{"event": "raft_election_started", "level": "info"}
{"event": "became_master", "cluster_id": "...", "level": "info"}
{"event": "grpc_server_started", "port": 50051, "level": "info"}
{"event": "api_server_started", "port": 8080, "level": "info"}
{"event": "metrics_loop_started", "interval": 30, "level": "info"}
```

### Agents on Bare Metal (systemd)

```bash
# On EACH controller node
sudo systemctl start cnf-agent
sudo systemctl enable cnf-agent
sudo systemctl status cnf-agent   # Active: active (running)

# Stream logs
sudo journalctl -u cnf-agent -f
```

### Agents on Docker Compose

```bash
docker compose up -d cnf-agent-1 cnf-agent-2
docker compose logs -f cnf-agent-1 cnf-agent-2
```

---

## 6. Step 4 — Start Celery Workers

```mermaid
flowchart LR
    A["CNF Agents\n(must be running)"]
    W["Celery Worker\n--queues=migrations"]
    Q[("Redis\nmigrations queue")]
    E["MigrationEngine\n.execute()"]

    A -->|"API enqueues tasks"| Q
    W -->|"dequeue"| Q
    W --> E
```

### Celery Worker on Kubernetes

```bash
kubectl -n openstack apply -f deploy/k8s/celery-worker.yaml
kubectl -n openstack rollout status deployment/cnf-celery-worker

# Verify
kubectl -n openstack exec -it \
  $(kubectl -n openstack get pod -l app=cnf-celery-worker -o name | head -1) \
  -- celery -A cnf.tasks inspect ping
# Expected: celery@hostname: OK (pong)
```

### Celery Worker on Bare Metal

```bash
sudo systemctl start cnf-celery-worker
sudo systemctl enable cnf-celery-worker
sudo systemctl status cnf-celery-worker

celery -A cnf.tasks inspect ping
```

### Celery Worker on Docker Compose

```bash
docker compose up -d celery-worker
docker compose logs -f celery-worker
```

---

## 7. Step 5 — Start Celery Beat (Optional)

Celery Beat drives periodic tasks such as scheduled policy evaluations and cleanup.

### Celery Beat on Kubernetes

```bash
kubectl -n openstack apply -f deploy/k8s/celery-beat.yaml
kubectl -n openstack rollout status deployment/cnf-celery-beat
```

### Celery Beat on Bare Metal

```bash
sudo systemctl start cnf-celery-beat
sudo systemctl enable cnf-celery-beat
```

### Celery Beat on Docker Compose

```bash
docker compose up -d celery-beat
```

---

## 8. Step 6 — Verification

```mermaid
flowchart TD
    V1{"/healthz → 200\non all nodes?"}
    V2{"/readyz → 200\non all nodes?"}
    V3{"/v1/master →\nexactly ONE master?"}
    V4{"/v1/clusters →\nall clusters registered?"}
    V5{"Prometheus :9090\nmetrics flowing?"}
    V6{"Celery workers\nping OK?"}
    V7{"Test migration\non non-prod VM?"}
    OK(["✅ All checks passed\nSystem ready"])
    FAIL["❌ Investigate\nCheck logs + infra"]

    V1 -->|Pass| V2
    V2 -->|Pass| V3
    V3 -->|Pass| V4
    V4 -->|Pass| V5
    V5 -->|Pass| V6
    V6 -->|Pass| V7
    V7 -->|Pass| OK

    V1 -->|Fail| FAIL
    V2 -->|Fail| FAIL
    V3 -->|Fail| FAIL
    V4 -->|Fail| FAIL
    V5 -->|Fail| FAIL
    V6 -->|Fail| FAIL
    V7 -->|Fail| FAIL
```

```bash
# Health checks
curl http://<node>:8080/healthz    # → {"status":"ok"}
curl http://<node>:8080/readyz     # → {"status":"ready"}

# Verify exactly one MASTER
curl http://<node>:8080/v1/master  # → {"my_role":"MASTER"} on one node only

# All clusters visible
curl http://<node>:8080/v1/clusters

# Prometheus metrics
curl http://<node>:9090/metrics | grep "^cnf_"

# Celery worker
celery -A cnf.tasks inspect ping
```

---

## 9. Graceful Shutdown Sequence

```mermaid
sequenceDiagram
    participant OS as OS Signal (SIGTERM)
    participant AG as CNFAgent
    participant EL as Raft Election
    participant GS as gRPC Server
    participant BG as Background Tasks
    participant DB as PostgreSQL

    OS->>AG: SIGTERM / SIGINT

    AG->>AG: _running = False

    AG->>BG: Cancel _metrics_task

    AG->>EL: election.stop()
    EL->>EL: _running = False
    EL->>EL: Cancel _election_loop task
    EL-->>AG: ✅ Lease revoked\n(master key released in etcd)

    AG->>GS: grpc_server.stop(grace=5s)
    Note over GS: Drain in-flight RPCs\nup to 5 seconds
    GS-->>AG: ✅ Stopped

    AG->>DB: dispose_engine()
    DB-->>AG: ✅ Connection pool closed

    Note over AG: ✅ Agent shut down cleanly
```

---

## 10. Troubleshooting

```mermaid
flowchart TD
    ISSUE["🚨 Issue"] --> TYPE

    TYPE{{"What kind of issue?"}}

    TYPE -->|"Agent exits immediately"| DB_ERR["DB not reachable\n→ Check postgres + CNF_DATABASE__URL"]
    TYPE -->|"No master after 30s"| ETCD_ERR["etcd not reachable\n→ Check etcd health + CNF_RAFT__ETCD_ENDPOINTS"]
    TYPE -->|"gRPC server won't start"| PORT_ERR["Port 50051 in use\n→ Kill existing process or change port"]
    TYPE -->|"REST API not reachable"| API_ERR["Port 8080 in use\n→ Change CNF_API__PORT"]
    TYPE -->|"Celery not picking tasks"| REDIS_ERR["Redis not reachable\n→ Check Redis + CNF_CELERY__BROKER_URL"]
    TYPE -->|"RBD operations fail"| CEPH_ERR["Ceph config not mounted\n→ Check /etc/ceph/ceph.conf + keyring"]
    TYPE -->|"BGP routes not propagating"| BGP_ERR["FRR not running\n→ Check frr.conf + vtysh + BGP peering"]
    TYPE -->|"Two masters elected"| BRAIN["etcd split-brain\n→ Restart all agents + check etcd cluster health"]
```

| Symptom | Likely Cause | Fix |
| --- | --- | --- |
| Agent exits immediately | DB not reachable | Check postgres health + `CNF_DATABASE__URL` |
| `db_initialized` never logged | DB connection timeout | Check firewall on port 5432 |
| No master elected after 30s | etcd not reachable | Check etcd + `CNF_RAFT__ETCD_ENDPOINTS` |
| gRPC server not starting | Port 50051 in use | Kill existing process or change port |
| REST API not reachable | Port 8080 in use | Change `CNF_API__PORT` |
| Celery not picking tasks | Redis not reachable | Check Redis + `CNF_CELERY__BROKER_URL` |
| RBD operations failing | Ceph config not mounted | Check `/etc/ceph/ceph.conf` + keyring |
| BGP routes not propagating | FRR not running | Check `frr.conf` + vtysh + BGP peering |

---

## 11. Quick-Reference Commands

```bash
# ── Health ────────────────────────────────────────────
curl http://<node>:8080/healthz
curl http://<node>:8080/readyz

# ── Federation state ──────────────────────────────────
curl http://<node>:8080/v1/master
curl http://<node>:8080/v1/clusters
curl http://<node>:8080/v1/clusters/<id>/metrics

# ── VM operations ─────────────────────────────────────
openstack cnf vm list
openstack cnf vm status <vm-id>
openstack cnf vm migrate <vm-id> --to <cluster-id>
openstack cnf vm live-migrate <vm-id> --to <cluster-id>

# ── Migration status ───────────────────────────────────
curl http://<node>:8080/v1/migrations
curl http://<node>:8080/v1/migrations/<id>
curl http://<node>:8080/v1/migrations/<id>/events

# ── Celery ────────────────────────────────────────────
celery -A cnf.tasks inspect ping
celery -A cnf.tasks inspect active
celery -A cnf.tasks inspect reserved

# ── Logs (systemd) ────────────────────────────────────
sudo journalctl -u cnf-agent -f
sudo journalctl -u cnf-celery-worker -f

# ── Logs (Kubernetes) ─────────────────────────────────
kubectl -n openstack logs -l app=cnf -f
kubectl -n openstack logs -l app=cnf-celery-worker -f
```

---

*For deployment prerequisites see [Deployment Plan](DeploymentPlan.md). For architecture details see [Architecture](Architecture.md).*
