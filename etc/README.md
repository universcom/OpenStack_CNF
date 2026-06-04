# `etc/` — Reference Configuration Tree

This directory is a **canonical reference** for every config file the CNF agent
expects to find under `/etc/` at runtime. Files here are committed templates,
not live runtime config — copy/template them into the real `/etc/` on a host
(or mount them into containers) before starting the agent.

## Layout

```
etc/
├── cnf/                          # Agent-owned configuration
│   ├── cnf.yaml                  # Main agent config (Settings schema)
│   └── tls/                      # gRPC mTLS material (NOT committed)
│       └── README.md
├── ceph/                         # Ceph cluster connection (read by the rbd CLI)
│   ├── ceph.conf.example
│   └── ceph.client.cnf.keyring.example
├── frr/                          # FRR / BGP daemon configuration
│   ├── daemons.example
│   └── frr.conf.example
├── systemd/                      # systemd unit for bare-metal installs
│   └── cnf-agent.service
└── logrotate.d/                  # log rotation policy
    └── cnf-agent
```

## How files map to runtime paths

| Repo path                                       | Runtime path                          | Consumed by                        |
| ----------------------------------------------- | ------------------------------------- | ---------------------------------- |
| `etc/cnf/cnf.yaml`                              | `/etc/cnf/cnf.yaml`                   | `cnf.config.get_settings`          |
| `etc/cnf/tls/{server.crt,server.key,ca.crt}`    | `/etc/cnf/tls/...`                    | gRPC server (`cnf.grpc.server`)    |
| `etc/ceph/ceph.conf.example`                    | `/etc/ceph/ceph.conf`                 | `rbd` CLI from `cnf.storage.ceph`  |
| `etc/ceph/ceph.client.cnf.keyring.example`      | `/etc/ceph/ceph.client.cnf.keyring`   | `rbd` CLI auth                     |
| `etc/frr/frr.conf.example`                      | `/etc/frr/frr.conf`                   | `vtysh` from `cnf.network.bgp`     |
| `etc/frr/daemons.example`                       | `/etc/frr/daemons`                    | FRR systemd unit                   |
| `etc/systemd/cnf-agent.service`                 | `/etc/systemd/system/cnf-agent.service` | systemd                          |
| `etc/logrotate.d/cnf-agent`                     | `/etc/logrotate.d/cnf-agent`          | logrotate                          |

## Selecting which file the agent loads

The agent honours the `CNF_CONFIG` environment variable; defaults to
`/etc/cnf/cnf.yaml`. Any field can also be overridden via env vars using the
prefixes declared in [cnf/config.py](../cnf/config.py) (`CNF_`, `CNF_CEPH_`,
`CNF_BGP_`, `CNF_GRPC_`, `CNF_RAFT_`, `CNF_API_`, `CNF_DB_`, `CNF_CELERY_`,
`CNF_METRICS_`, `OS_`).

## Production deployment

For Ansible-managed hosts the templates under
[deploy/ansible/roles/cnf/templates/](../deploy/ansible/roles/cnf/templates/)
are rendered into `/etc/cnf/cnf.yaml`. For Helm/Kubernetes the equivalent
values come from a ConfigMap; see
[deploy/helm/cnf/values.yaml](../deploy/helm/cnf/values.yaml). Local-dev configs
live under [deploy/dev/](../deploy/dev/) and are bind-mounted by
[docker-compose.yml](../docker-compose.yml).

## Secrets

Real TLS keys, Ceph keyrings, and JWT secrets must NOT be committed. The
`*.example` suffix marks placeholders. Repo `.gitignore` already excludes
`*.pem`, `*.key`, `*.crt`, `*.p12`, `*.pfx`, and `secrets/` to prevent
accidental commits.
