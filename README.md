# CNF — Cluster Nova Federation

**CNF (Cluster Nova Federation)** — Cross-cluster VM distribution, migration, and live-migration for OpenStack environments. CNF federates multiple Nova compute clusters, enabling seamless workload mobility across independent OpenStack deployments with policy-driven scheduling and minimal downtime.

---

## Architecture

CNF runs **directly on each OpenStack controller node** — no external control plane needed. One node wins master election via Raft (etcd), and all nodes expose the full CLI and REST API. Commands sent to any worker are transparently proxied to the current master.

```
openstack-1 (controller)          openstack-2 (controller)          openstack-3 (controller)
┌─────────────────────────┐       ┌─────────────────────────┐       ┌─────────────────────────┐
│  Nova / Neutron / KS    │       │  Nova / Neutron / KS    │       │  Nova / Neutron / KS    │
│  CNF MASTER  ◄──gRPC───►│       │  CNF WORKER  ◄──gRPC───►│       │  CNF WORKER             │
│  CLI / REST :8080        │       │  CLI / REST :8080        │       │  CLI / REST :8080        │
│  BGP Speaker (FRR)      │       │  BGP Speaker (FRR)       │       │  BGP Speaker (FRR)      │
│  Ceph OSD + rbd-mirror  │       │  Ceph OSD + rbd-mirror  │       │  Ceph OSD + rbd-mirror  │
└─────────────────────────┘       └─────────────────────────┘       └─────────────────────────┘
          │                                    │                                    │
          └────────────────────────────────────┼────────────────────────────────────┘
                              Cross-cluster Ceph RBD pool (shared)
                              BGP Route Reflector (VM IP portability)
```

### Key design decisions

| Concern | Solution |
|---|---|
| Leader election | Raft via etcd lease — auto-failover in seconds |
| Inter-node comms | mTLS gRPC (CNFControl + CNFPeer services) |
| VM disk at migration | Ceph RBD mirror — disk pre-synced, only memory transferred |
| IP portability | BGP via FRR vtysh — VMs keep IPs across cluster boundaries |
| OpenStack integration | `python-openstackclient` plugin — `openstack cnf *` commands |
| Async execution | Celery + Redis — migration tasks survive agent restarts |
| Observability | Prometheus metrics + structlog JSON + OpenTelemetry |

---

## Quick start

### Prerequisites

- Python 3.11+
- PostgreSQL 14+
- Redis 7+
- etcd 3.5+
- OpenStack (Keystone, Nova, Neutron, Cinder) — one deployment per cluster
- Ceph with `rbd-mirror` configured between clusters
- FRR for BGP advertisement

### Install

```bash
pip install openstack-cnf

# Compile gRPC stubs (required after fresh install)
python -m grpc_tools.protoc \
  -I proto \
  --python_out=cnf/grpc \
  --grpc_python_out=cnf/grpc \
  proto/cnf.proto
```

### Configure

```yaml
# /etc/cnf/cnf.yaml (on each controller node)
cluster_id:        "uuid-for-this-cluster"
cluster_name:      "openstack-1"
cluster_grpc_addr: "os1-controller.example.com:50051"

peer_clusters:
  - "os2-controller.example.com:50051"
  - "os3-controller.example.com:50051"

ceph:
  pool: "vms"
  mirror_mode: "image"

bgp:
  as_number: 65001
  route_reflector_addr: "rr.example.com"

raft:
  etcd_endpoints: ["etcd.example.com:2379"]

database:
  url: "postgresql+asyncpg://cnf:secret@pg.example.com:5432/cnf"
```

### Run

```bash
cnf-agent                     # foreground
systemctl start cnf-agent     # via systemd (see deploy/ansible/)
```

### Register OpenStack service endpoint

```bash
openstack service create --name cnf --description "Cluster Nova Federation" cnf
openstack endpoint create --region RegionOne cnf \
  --publicurl   http://<controller>:8080/v1 \
  --internalurl http://<controller>:8080/v1 \
  --adminurl    http://<controller>:8080/v1
```

---

## Usage

### CLI

```bash
# Cluster management
openstack cnf cluster list
openstack cnf cluster show   <cluster-id>
openstack cnf cluster metrics <cluster-id>
openstack cnf cluster register openstack-4 \
  --auth-url http://os4:5000/v3 \
  --grpc-addr os4-ctrl:50051 \
  --bgp-as 65004

# VM operations — from any cluster
openstack cnf vm list
openstack cnf vm status   <vm-id>
openstack cnf vm migrate  <vm-id> --to <cluster-id>
openstack cnf vm live-migrate <vm-id> --to <cluster-id> --dest-next-hop 10.0.0.1

# Federation state
openstack cnf master show
openstack cnf master elect <cluster-id>

# Policy-driven scheduler
openstack cnf policy list
openstack cnf policy set rebalance-cpu \
  --rule '{"trigger":"cpu_pct>80","action":"live_migrate","target":"least_loaded"}'
```

### REST API

```bash
# List clusters
curl http://localhost:8080/v1/clusters

# Trigger live migration
curl -X POST http://localhost:8080/v1/vms/<vm-id>/live-migrate \
  -H "X-Auth-Token: $OS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"dest_cluster_id":"<cluster-id>","dest_host":"compute-1.os2","dest_next_hop":"10.0.0.1"}'

# Poll migration status
curl http://localhost:8080/v1/migrations/<migration-id>
```

---

## Migration flow

### Live migration (Ceph + BGP)

```
1. PREFLIGHT   — verify VM is ACTIVE, RBD mirror lag within threshold
2. DISK        — check Ceph sync; VM still running and writing
3. MEMORY      — open QEMU tunnel; pre-copy memory pages to destination
4. CUTOVER     — flush last RBD dirty blocks; demote source; promote dest
5. BGP         — destination announces VM IP; source withdraws it
6. CLEANUP     — remove source VM record from Nova
```

### Cold migration

```
1. PREFLIGHT   — verify VM is ACTIVE or SHUTOFF, RBD lag check
2. DISK        — stop VM; wait for final RBD sync; demote/promote
3. CUTOVER     — register volume in destination Cinder
4. BGP         — IP handoff via BGP
5. CLEANUP     — delete source VM
```

---

## Development

```bash
# Clone and install dev dependencies
git clone https://github.com/your-org/OpenStack_NCF
cd OpenStack_NCF
pip install -e ".[dev]"

# Start local infrastructure
docker compose up -d postgres redis etcd

# Run database migrations
alembic upgrade head

# Run tests
pytest

# Run with coverage
pytest --cov=cnf --cov-report=html

# Lint
ruff check cnf tests
mypy cnf
```

### Full local stack (two simulated clusters)

```bash
docker compose up --build
```

Services:
- CNF Agent 1 (master candidate): http://localhost:8081
- CNF Agent 2 (worker):           http://localhost:8082
- Prometheus:                      http://localhost:9093
- Grafana:                         http://localhost:3000 (admin/admin)

---

## Deployment

### Ansible

```bash
cd deploy/ansible
ansible-playbook -i inventory/production site.yml \
  -e cnf_version=0.1.0 \
  -e @vars/production.yml
```

### Helm (Kubernetes-managed OpenStack)

```bash
helm upgrade --install cnf deploy/helm/cnf \
  --namespace openstack \
  --set cluster.id=$(uuidgen) \
  --set cluster.name=openstack-1 \
  --set cluster.grpcAddr=os1-ctrl:50051 \
  --set peers=["os2-ctrl:50051","os3-ctrl:50051"] \
  --values deploy/helm/cnf/values.yaml
```

---

## Project structure

```
OpenStack_NCF/
├── proto/cnf.proto              # gRPC service definitions
├── cnf/
│   ├── main.py                  # Entry point (cnf-agent)
│   ├── config.py                # Pydantic settings
│   ├── agent/
│   │   ├── agent.py             # Main agent process
│   │   └── raft.py              # Leader election (etcd)
│   ├── api/
│   │   ├── app.py               # FastAPI factory + proxy middleware
│   │   └── v1/                  # REST route handlers
│   ├── grpc/
│   │   └── server.py            # CNFControl + CNFPeer servicers
│   ├── migration/
│   │   └── engine.py            # Cold + live migration state machines
│   ├── storage/
│   │   └── ceph.py              # Ceph RBD promote/demote/sync
│   ├── network/
│   │   └── bgp.py               # FRR vtysh BGP announce/withdraw
│   ├── scheduler/
│   │   └── scheduler.py         # Policy-driven VM placement
│   ├── openstack/
│   │   └── client.py            # OpenStack SDK wrapper
│   ├── osc/                     # python-openstackclient plugin
│   │   └── v1/                  # openstack cnf * commands
│   ├── db/
│   │   ├── models.py            # SQLAlchemy ORM models
│   │   ├── session.py           # Async session management
│   │   └── migrations/          # Alembic migrations
│   └── tasks/
│       └── migration_tasks.py   # Celery async task definitions
├── tests/                       # pytest test suite
├── deploy/
│   ├── helm/cnf/                # Helm chart (DaemonSet on controllers)
│   └── ansible/roles/cnf/       # Ansible role
└── docker-compose.yml           # Local dev stack
```

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).
