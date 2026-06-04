# `etc/cnf/tls/` — gRPC mTLS material

The CNF agent's gRPC server (port 50051) uses mutual TLS to authenticate
peers in the federation. Three files are expected here:

| File         | Purpose                                          |
| ------------ | ------------------------------------------------ |
| `ca.crt`     | CA bundle that signs both server and client certs |
| `server.crt` | This node's server cert (SAN must include the gRPC hostname) |
| `server.key` | Private key for `server.crt` (mode 0600)         |

These paths are configurable via `grpc.tls_cert` / `grpc.tls_key` /
`grpc.tls_ca` in `cnf.yaml` (or the env vars `CNF_GRPC_TLS_CERT`,
`CNF_GRPC_TLS_KEY`, `CNF_GRPC_TLS_CA`).

## Generating a dev CA + cert

```bash
# 1. Self-signed CA
openssl req -x509 -newkey rsa:4096 -nodes -days 365 \
  -keyout ca.key -out ca.crt \
  -subj "/CN=cnf-dev-ca"

# 2. Server key + CSR
openssl req -newkey rsa:4096 -nodes \
  -keyout server.key -out server.csr \
  -subj "/CN=cnf-agent-1"

# 3. Sign with the CA, embedding SANs
cat > san.cnf <<EOF
subjectAltName = DNS:cnf-agent-1,DNS:localhost,IP:127.0.0.1
EOF
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days 365 -extfile san.cnf

chmod 0600 server.key
rm server.csr san.cnf ca.srl
```

## Production

Use cert-manager (Kubernetes) or your existing PKI. Rotate before expiry —
the agent re-reads files on SIGHUP.

## Why these files are NOT committed

The repo `.gitignore` excludes `*.crt`, `*.key`, `*.pem`, `*.p12`, `*.pfx`.
This README is the only file that should live in this directory in git.
