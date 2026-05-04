# ── Stage 1: build dependencies ──────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Build deps for native Python extensions:
#   - libvirt-dev: needed to compile libvirt-python (Linux only)
#   - libffi-dev / libssl-dev: cryptography
#   - gcc/g++/make: grpcio, asyncpg, etc.
# We do NOT install librados-dev/librbd-dev — there are no `rados`/`rbd`
# packages on PyPI; the agent shells out to the `rbd` CLI from ceph-common
# at runtime instead.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ make \
    libffi-dev libssl-dev \
    libvirt-dev pkg-config \
    protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

# Copy the full source tree BEFORE pip install — otherwise pip generates the
# cnf-agent entry-point script but installs an empty package (the cnf/ dir
# has to be present for setuptools.packages.find to discover it).
COPY pyproject.toml ./
COPY proto/ proto/
COPY cnf/ cnf/

# Build everything inside a self-contained venv at /install. Using a venv
# (instead of pip --prefix) avoids a subtle bug: when grpcio-tools is
# installed to system site-packages first, a later `pip install --prefix`
# sees protobuf/grpcio as "already satisfied" and skips them — so the
# resulting prefix tree is missing transitive deps. A venv isolates this.
RUN python -m venv /install
ENV PATH=/install/bin:$PATH

# grpcio-tools is needed at build time for protoc, and protobuf/grpcio are
# also runtime deps — installing once into the venv covers both.
RUN pip install --upgrade pip setuptools wheel grpcio-tools

# Compile protobuf stubs into cnf/grpc/ so they're packaged by pip install.
RUN python -m grpc_tools.protoc \
      -I proto \
      --python_out=cnf/grpc \
      --grpc_python_out=cnf/grpc \
      proto/cnf.proto

# Install the cnf package + all remaining runtime deps into the venv.
RUN pip install --no-warn-script-location .

# ── Stage 2: runtime image ────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="CNF Agent" \
      org.opencontainers.image.description="Cluster Nova Federation — OpenStack cross-cluster VM migration" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.licenses="Apache-2.0"

# Runtime deps:
#   - ceph-common: provides the `rbd` CLI + librados2/librbd1 (what the
#     storage layer shells out to)
#   - libvirt-clients/libvirt0: libvirt-python's runtime + virsh
#   - frr: vtysh CLI used by the BGP module
RUN apt-get update && apt-get install -y --no-install-recommends \
    ceph-common \
    libvirt-clients libvirt0 \
    frr \
    iproute2 \
    iputils-ping \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN groupadd -r cnf && useradd -r -g cnf -s /bin/bash cnf

# Copy the self-contained venv from the builder. cnf-agent lives at
# /install/bin/cnf-agent and uses /install/bin/python — fully self-contained.
COPY --from=builder /install /install
ENV PATH=/install/bin:$PATH

# Config and TLS dirs (mounted at runtime via Kubernetes secret / volume)
RUN mkdir -p /etc/cnf/tls && chown -R cnf:cnf /etc/cnf /install

USER cnf

# Ports: 8080=REST API, 50051=gRPC, 9090=Prometheus metrics
EXPOSE 8080 50051 9090

# Health check via REST API
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8080/healthz || exit 1

ENV CNF_CONFIG=/etc/cnf/cnf.yaml \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # The `etcd3` package (last released 2020) ships protobuf stubs generated
    # with an old protoc that the C++ protobuf 4+ runtime rejects. Switching
    # to the pure-Python parser lets those stubs load. Negligible perf cost
    # for our usage (etcd lease heartbeats, not data plane).
    PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

ENTRYPOINT ["cnf-agent"]
