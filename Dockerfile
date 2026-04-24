# ── Stage 1: build dependencies ──────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps for Ceph, libvirt, and gRPC native extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ make \
    libffi-dev libssl-dev \
    librados-dev librbd-dev \
    libvirt-dev \
    protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel \
 && pip install --prefix=/install --no-warn-script-location .

# Compile protobuf stubs
COPY proto/ proto/
COPY cnf/grpc/ cnf/grpc/
RUN pip install grpcio-tools \
 && python -m grpc_tools.protoc \
      -I proto \
      --python_out=cnf/grpc \
      --grpc_python_out=cnf/grpc \
      proto/cnf.proto

# ── Stage 2: runtime image ────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="CNF Agent" \
      org.opencontainers.image.description="Cluster Nova Federation — OpenStack cross-cluster VM migration" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN apt-get update && apt-get install -y --no-install-recommends \
    librados2 librbd1 \
    libvirt-clients libvirt0 \
    frr \
    iproute2 \
    iputils-ping \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN groupadd -r cnf && useradd -r -g cnf -s /bin/bash cnf

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy app source (with compiled protos)
WORKDIR /app
COPY --from=builder /build/cnf/grpc/cnf_pb2*.py cnf/grpc/
COPY cnf/ cnf/
COPY proto/ proto/

# Config and TLS dirs (mounted at runtime via Kubernetes secret / volume)
RUN mkdir -p /etc/cnf/tls && chown -R cnf:cnf /etc/cnf /app

USER cnf

# Ports: 8080=REST API, 50051=gRPC, 9090=Prometheus metrics
EXPOSE 8080 50051 9090

# Health check via REST API
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8080/healthz || exit 1

ENV CNF_CONFIG=/etc/cnf/cnf.yaml \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["cnf-agent"]
