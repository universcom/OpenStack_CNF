# CNF — Deployment Plan

**Project**: CNF v0.1.0
**Date**: 2026-04-27

---

## 1. Deployment Environments

```mermaid
graph LR
    subgraph DEV["🧪 Development"]
        direction TB
        DC_TOOL["Tool: Docker Compose"]
        DC_TARGET["Target: Local workstation"]
        DC_NOTES["Full stack in containers\npostgres · redis · etcd\n2 CNF agents · celery\nprometheus · grafana"]
    end

    subgraph STG["🔧 Staging"]
        direction TB
        ANS_TOOL["Tool: Ansible + systemd"]
        ANS_TARGET["Target: Bare-metal\ncontroller nodes"]
        ANS_NOTES["Direct pip install\nSystemd unit\nReal OpenStack\nReal Ceph + FRR"]
    end

    subgraph PROD["🚀 Production"]
        direction TB
        HLM_TOOL["Tool: Helm DaemonSet"]
        HLM_TARGET["Target: Kubernetes\ncontrol-plane nodes"]
        HLM_NOTES["One pod per node\nhostNetwork: true\nHost mounts:\n/etc/ceph /var/run/libvirt /var/run/frr"]
    end

    DEV -->|"promote config"| STG
    STG -->|"promote config"| PROD
```

---

## 2. Prerequisites

```mermaid
graph TB
    subgraph PER_FED["Required per Federation (shared)"]
        PG["🐘 PostgreSQL 14+\nport 5432\nshared by all agents"]
        RD["🔴 Redis 7+\nport 6379\nCelery broker + results"]
        ET["etcd 3.5+\nport 2379\n3-node HA minimum"]
    end

    subgraph PER_CLUSTER["Required per Cluster"]
        OS["OpenStack\nNova · Neutron · Cinder · Keystone"]
        CEPH_REQ["Ceph cluster\nRBD mirroring pre-configured"]
        FRR_REQ["FRR installed\nBGP daemon running"]
        RT["Python 3.11+ (bare metal)\nOR container runtime (K8s)"]
    end

    subgraph NETWORK["Network Ports Open"]
        P1["8080 — REST API\n(operators / CLI)"]
        P2["50051 — gRPC\n(between controller nodes)"]
        P3["9090 — Prometheus metrics\n(monitoring scraper)"]
        P4["BGP routes\npropagated via FRR / route reflector"]
    end

    subgraph TLS["TLS Certificates (Production)"]
        TLS1["/etc/cnf/tls/ca.crt\nCA certificate"]
        TLS2["/etc/cnf/tls/server.crt\nNode server certificate"]
        TLS3["/etc/cnf/tls/server.key\nNode private key"]
    end
```

---

## 3. Option A — Kubernetes (Helm)

### 3.1 Architecture

```mermaid
graph TB
    subgraph K8S["Kubernetes Cluster — namespace: openstack"]
        subgraph DS["DaemonSet: cnf\nnodeSelector: control-plane\ntolerations: NoSchedule"]
            subgraph N1["Control-Plane Node 1"]
                P1["cnf Pod\nREST :8080\ngRPC :50051\nMetrics :9090\nhostNetwork: true"]
            end
            subgraph N2["Control-Plane Node 2"]
                P2["cnf Pod\nREST :8080\ngRPC :50051\nMetrics :9090\nhostNetwork: true"]
            end
            subgraph N3["Control-Plane Node 3"]
                P3["cnf Pod\nREST :8080\ngRPC :50051\nMetrics :9090\nhostNetwork: true"]
            end
        end

        subgraph SEC["Kubernetes Secrets"]
            S1["cnf-db-secret\nDATABASE_URL\nREDIS_URL"]
            S2["cnf-openstack-secret\nOS_AUTH_URL\nOS_USERNAME\nOS_PASSWORD..."]
            S3["cnf-ceph-secret\nceph-keyring"]
            S4["cnf-grpc-tls\ntls.crt · tls.key · ca.crt"]
        end

        subgraph MOUNTS["Host Volume Mounts"]
            M1["/etc/ceph\nCeph config"]
            M2["/var/run/libvirt\nlibvirt socket"]
            M3["/var/run/frr\nFRR socket"]
        end
    end

    P1 -.->|reads| S1
    P1 -.->|reads| S2
    P1 -.->|reads| S3
    P1 -.->|reads| S4
    P1 -.->|mounts| M1
    P1 -.->|mounts| M2
    P1 -.->|mounts| M3
```

### 3.2 Helm Deployment Steps

```mermaid
flowchart TD
    S1["Step 1\nCreate namespace\nkubectl create namespace openstack"]
    S2["Step 2\nCreate secrets\nDB · OpenStack · Ceph · TLS"]
    S3["Step 3\nhelm upgrade --install cnf deploy/helm/cnf/\n--set cluster.id=...\n--set cluster.name=...\n--set cluster.grpcAddr=..."]
    S4["Step 4\nkubectl rollout status daemonset/cnf"]
    S5["Step 5\nVerify:\ncurl http://node:8080/healthz\ncurl http://node:8080/readyz\ncurl http://node:8080/v1/master"]
    S6{{"All pods\nHealthy?"}}
    DONE(["✅ Deployment complete"])
    FAIL["Check logs:\nkubectl -n openstack logs -l app=cnf"]

    S1 --> S2 --> S3 --> S4 --> S5 --> S6
    S6 -->|Yes| DONE
    S6 -->|No| FAIL
    FAIL --> S5
```

---

## 4. Option B — Ansible (Bare Metal)

### 4.1 Ansible Execution Flow

```mermaid
flowchart TD
    AC["Ansible Control Node"]

    subgraph PLAY["Playbook: deploy/ansible/roles/cnf/tasks/main.yml"]
        T1["1 — Install system packages\npython3.11 · gcc · librados-dev\nlibrbd-dev · libvirt-dev · frr · etcd"]
        T2["2 — Create cnf system user\nhome: /var/lib/cnf"]
        T3["3 — Create directories\n/etc/cnf · /etc/cnf/tls\n/var/log/cnf · /var/lib/cnf"]
        T4["4 — pip install openstack-cnf==0.1.0"]
        T5["5 — Compile gRPC stubs\nfrom proto/cnf.proto"]
        T6["6 — Deploy /etc/cnf/cnf.yaml\n(Jinja2 template)"]
        T7["7 — Deploy TLS certs\n/etc/cnf/tls/"]
        T8["8 — Deploy systemd unit\n/etc/systemd/system/cnf-agent.service"]
        T9["9 — Register in Keystone\n(once per cluster)"]
        T10["10 — systemctl enable + start cnf-agent"]
        T11["11 — Wait /healthz\n12 retries × 5s delay"]
    end

    subgraph NODES["Controller Nodes"]
        N1["os1-ctrl\n192.168.1.10"]
        N2["os2-ctrl\n192.168.1.11"]
        N3["os3-ctrl\n192.168.1.12"]
    end

    AC -->|"ansible-playbook\n-i inventory/production site.yml"| PLAY
    T1 --> T2 --> T3 --> T4 --> T5 --> T6 --> T7 --> T8 --> T9 --> T10 --> T11

    PLAY --> N1
    PLAY --> N2
    PLAY --> N3
```

---

## 5. Option C — Docker Compose (Development)

### 5.1 Service Dependency Graph

```mermaid
graph TD
    subgraph Infra["Infrastructure (starts first)"]
        PG["🐘 postgres:16-alpine\n:5432\nVolume: pg_data"]
        RD["🔴 redis:7-alpine\n:6379\nNo persistence"]
        ET["etcd:v3.5.12\n:2379\nSingle-node"]
    end

    subgraph Agents["CNF Agents (start after infra healthy)"]
        A1["cnf-agent-1\nopenstack-1 (MASTER candidate)\nREST :8081 → :8080\nMetrics :9091 → :9090"]
        A2["cnf-agent-2\nopenstack-2 (WORKER)\nREST :8082 → :8080\nMetrics :9092 → :9090"]
    end

    subgraph Async["Async Workers"]
        CW["celery-worker\nExecutes migration tasks"]
        CB["celery-beat\nPeriodic scheduler"]
    end

    subgraph Observ["Observability"]
        PR["prometheus:v2.51.0\n:9093\nScrapes agents"]
        GR["grafana:10.4.0\n:3000\nadmin/admin"]
    end

    PG -->|"healthcheck"| A1
    RD -->|"healthcheck"| A1
    ET -->|"healthcheck"| A1
    PG -->|"healthcheck"| A2
    RD -->|"healthcheck"| A2
    ET -->|"healthcheck"| A2
    PG -->|"healthcheck"| CW
    RD -->|"healthcheck"| CW
    PG -->|"healthcheck"| CB
    RD -->|"healthcheck"| CB
    A1 -->|"scrape :9090"| PR
    A2 -->|"scrape :9090"| PR
    PR --> GR
```

### 5.2 Quick Start

```bash
cd openstack-cnf
docker compose up --build

# Verify
curl http://localhost:8081/healthz
curl http://localhost:8082/healthz
curl http://localhost:8081/v1/master
open http://localhost:3000   # Grafana (admin/admin)
```

---

## 6. Database Migration

```mermaid
flowchart LR
    subgraph WHEN["When to run"]
        W1["First deployment"]
        W2["After CNF version upgrade"]
    end

    subgraph HOW["How to run"]
        K8S_MIG["Kubernetes:\nkubectl run cnf-migrate\n--image=cnf:0.1.0 --rm -it\n-- cnf-manage db upgrade head"]
        BM_MIG["Bare metal:\nexport CNF_DATABASE__URL=...\ncnf-manage db upgrade head"]
        DC_MIG["Docker Compose:\ndocker compose run --rm\ncnf-agent-1 cnf-manage db upgrade head"]
    end

    subgraph TABLES["Expected Tables After Migration"]
        T["clusters\ncluster_metrics\nmigrations\nmigration_events\npolicies"]
    end

    W1 --> HOW
    W2 --> HOW
    HOW --> TABLES
```

---

## 7. Rollback Plan

```mermaid
flowchart TD
    DETECT["🚨 Issue detected\nafter deployment"]

    CHECK{{"In-flight\nmigrations?"}}
    WAIT["Wait for DONE or FAILED\nbefore rolling back"]

    subgraph ROLLBACK["Rollback Steps"]
        R1["1 — Helm rollback\nhelm rollback cnf -n openstack"]
        R2["2 — Or Ansible rollback\nansible-playbook rollback.yml\n(installs previous version)"]
        R3["3 — DB rollback if needed\ncnf-manage db downgrade -1"]
    end

    VERIFY["Verify:\ncurl http://node:8080/healthz\ncurl http://node:8080/v1/master"]

    DETECT --> CHECK
    CHECK -->|Yes| WAIT --> ROLLBACK
    CHECK -->|No| ROLLBACK
    ROLLBACK --> VERIFY
    R1 --> R3
    R2 --> R3
```

---

## 8. Post-Deployment Verification Checklist

```mermaid
flowchart TD
    V1{"GET /healthz\n→ 200 on all nodes?"}
    V2{"GET /readyz\n→ 200 on all nodes?"}
    V3{"GET /v1/master\n→ exactly ONE master?"}
    V4{"GET /v1/clusters\n→ all clusters visible?"}
    V5{"Prometheus metrics\nflowing on :9090?"}
    V6{"Grafana dashboards\npopulated?"}
    V7{"Celery workers\nping OK?"}
    V8{"Test migration\non non-prod VM OK?"}
    DONE(["✅ Deployment verified"])
    FAIL(["❌ Investigate issue\ncheck logs + infra"])

    V1 -->|Pass| V2
    V2 -->|Pass| V3
    V3 -->|Pass| V4
    V4 -->|Pass| V5
    V5 -->|Pass| V6
    V6 -->|Pass| V7
    V7 -->|Pass| V8
    V8 -->|Pass| DONE

    V1 -->|Fail| FAIL
    V2 -->|Fail| FAIL
    V3 -->|Fail| FAIL
    V4 -->|Fail| FAIL
    V5 -->|Fail| FAIL
    V6 -->|Fail| FAIL
    V7 -->|Fail| FAIL
    V8 -->|Fail| FAIL
```

---

## 9. Environment Configuration Comparison

| Parameter | Development | Staging | Production |
| --- | --- | --- | --- |
| `log_format` | console | json | json |
| `log_level` | DEBUG | INFO | INFO |
| `grpc.tls_enabled` | false | true | true |
| `bgp.enabled` | false | true | true |
| `raft.etcd_endpoints` | single node | 3-node | 3-node |
| `celery.task_time_limit` | 3600s | 7200s | 7200s |
| Ceph | mock/disabled | real | real |
| OpenStack | simulated | real staging | real production |

---

*For startup order details see [Startup Guide](StartupGuide.md). For architecture diagrams see [Architecture](Architecture.md).*
