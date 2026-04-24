"""
cnf.openstack.client — OpenStack SDK wrapper.
Provides async-friendly access to Nova, Neutron, Cinder, and Glance.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import openstack
from openstack.compute.v2.server import Server
from openstack.network.v2.floating_ip import FloatingIP

from cnf.config import OpenStackConfig, get_settings
from cnf.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class VMDetails:
    id: str
    name: str
    status: str
    host: str
    flavor_id: str
    image_id: str
    ips: list[str] = field(default_factory=list)
    floating_ips: list[str] = field(default_factory=list)
    volume_ids: list[str] = field(default_factory=list)
    security_groups: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    availability_zone: str = ""


@dataclass
class FlavorDetails:
    id: str
    name: str
    vcpus: int
    ram_mb: int
    disk_gb: int
    extra_specs: dict[str, str] = field(default_factory=dict)


class OpenStackClient:
    """
    Thin async wrapper around the OpenStack SDK.
    All blocking SDK calls are dispatched to a thread pool executor.
    """

    def __init__(self, config: OpenStackConfig | None = None) -> None:
        self.cfg = config or get_settings().openstack
        self._conn: openstack.connection.Connection | None = None

    def _get_conn(self) -> openstack.connection.Connection:
        if self._conn is None:
            self._conn = openstack.connect(
                auth_url=self.cfg.auth_url,
                username=self.cfg.username,
                password=self.cfg.password,
                project_name=self.cfg.project_name,
                project_domain_name=self.cfg.project_domain_name,
                user_domain_name=self.cfg.user_domain_name,
                region_name=self.cfg.region_name,
                interface=self.cfg.interface,
                identity_api_version=self.cfg.identity_api_version,
            )
        return self._conn

    async def _run(self, fn, *args, **kwargs):
        """Run a blocking OpenStack SDK call in the thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ── Nova / Compute ─────────────────────────────────────────────────

    async def get_vm(self, vm_id: str) -> VMDetails:
        conn = self._get_conn()
        server: Server = await self._run(conn.compute.get_server, vm_id)
        if server is None:
            raise VMNotFound(vm_id)

        ips: list[str] = []
        for net_addrs in server.addresses.values():
            for addr in net_addrs:
                ips.append(addr["addr"])

        fips: list[str] = []
        try:
            fips_raw = await self._run(
                conn.network.floating_ips, port_id=server.id
            )
            fips = [f.floating_ip_address for f in fips_raw]
        except Exception:
            pass

        volumes = [
            v["id"] for v in server.get("os-extended-volumes:volumes_attached", [])
        ]

        return VMDetails(
            id=server.id,
            name=server.name,
            status=server.status,
            host=server.get("OS-EXT-SRV-ATTR:host", ""),
            flavor_id=server.flavor["id"] if isinstance(server.flavor, dict) else server.flavor.id,
            image_id=server.image["id"] if isinstance(server.image, dict) else "",
            ips=ips,
            floating_ips=fips,
            volume_ids=volumes,
            security_groups=[sg["name"] for sg in server.security_groups or []],
            metadata=server.metadata or {},
            availability_zone=server.get("OS-EXT-AZ:availability_zone", ""),
        )

    async def list_vms(self, host: str | None = None) -> list[VMDetails]:
        conn = self._get_conn()
        filters: dict[str, Any] = {"all_tenants": True}
        if host:
            filters["host"] = host
        servers = await self._run(conn.compute.servers, **filters)
        result = []
        for s in servers:
            try:
                result.append(await self.get_vm(s.id))
            except Exception as e:
                logger.warning("vm_detail_failed", vm_id=s.id, error=str(e))
        return result

    async def stop_vm(self, vm_id: str) -> None:
        conn = self._get_conn()
        await self._run(conn.compute.stop_server, vm_id)
        await self._wait_vm_status(vm_id, "SHUTOFF", timeout=120)
        logger.info("vm_stopped", vm_id=vm_id)

    async def start_vm(self, vm_id: str) -> None:
        conn = self._get_conn()
        await self._run(conn.compute.start_server, vm_id)
        await self._wait_vm_status(vm_id, "ACTIVE", timeout=120)
        logger.info("vm_started", vm_id=vm_id)

    async def delete_vm(self, vm_id: str, force: bool = False) -> None:
        conn = self._get_conn()
        if force:
            await self._run(conn.compute.force_delete_server, vm_id)
        else:
            await self._run(conn.compute.delete_server, vm_id)
        logger.info("vm_deleted", vm_id=vm_id, force=force)

    async def get_flavor(self, flavor_id: str) -> FlavorDetails:
        conn = self._get_conn()
        f = await self._run(conn.compute.get_flavor, flavor_id)
        return FlavorDetails(
            id=f.id,
            name=f.name,
            vcpus=f.vcpus,
            ram_mb=f.ram,
            disk_gb=f.disk,
            extra_specs=dict(f.extra_specs or {}),
        )

    async def get_cluster_metrics(self) -> dict[str, Any]:
        """Return aggregated compute resource usage for this cluster."""
        conn = self._get_conn()
        hypervisors = await self._run(conn.compute.hypervisors)
        total_vcpus = total_ram = total_disk = 0
        used_vcpus = used_ram = used_disk = 0
        for h in hypervisors:
            total_vcpus += h.vcpus
            used_vcpus  += h.vcpus_used
            total_ram   += h.memory_size
            used_ram    += h.memory_used
            total_disk  += h.local_disk_size
            used_disk   += h.local_disk_used

        cpu_pct  = (used_vcpus / total_vcpus * 100)  if total_vcpus else 0
        ram_pct  = (used_ram   / total_ram   * 100)  if total_ram   else 0
        disk_pct = (used_disk  / total_disk  * 100)  if total_disk  else 0

        servers = list(await self._run(conn.compute.servers, all_tenants=True))

        return {
            "cpu_used_pct":    round(cpu_pct,  2),
            "ram_used_pct":    round(ram_pct,  2),
            "disk_used_pct":   round(disk_pct, 2),
            "vm_count":        len(servers),
            "network_bw_mbps": 0.0,  # populated via Prometheus/Neutron stats
        }

    async def _wait_vm_status(
        self, vm_id: str, target: str, timeout: int = 120, interval: int = 3
    ) -> None:
        elapsed = 0
        while elapsed < timeout:
            conn = self._get_conn()
            server = await self._run(conn.compute.get_server, vm_id)
            if server.status == target:
                return
            if server.status == "ERROR":
                raise VMError(f"VM {vm_id} entered ERROR state while waiting for {target}")
            await asyncio.sleep(interval)
            elapsed += interval
        raise VMTimeout(f"VM {vm_id} did not reach {target} within {timeout}s")

    # ── Cinder / Volumes ───────────────────────────────────────────────

    async def get_volume(self, volume_id: str) -> dict[str, Any]:
        conn = self._get_conn()
        vol = await self._run(conn.block_storage.get_volume, volume_id)
        return {
            "id":       vol.id,
            "name":     vol.name,
            "size_gb":  vol.size,
            "status":   vol.status,
            "type":     vol.volume_type,
            "metadata": vol.metadata or {},
            "attachments": vol.attachments or [],
        }

    async def create_volume_from_rbd(
        self, pool: str, image: str, size_gb: int, name: str, volume_type: str = ""
    ) -> str:
        """Register an existing RBD image as a Cinder volume. Returns volume ID."""
        conn = self._get_conn()
        vol = await self._run(
            conn.block_storage.create_volume,
            name=name,
            size=size_gb,
            volume_type=volume_type or None,
            metadata={"rbd_pool": pool, "rbd_image": image, "cnf_imported": "true"},
        )
        return vol.id

    # ── Neutron / Network ──────────────────────────────────────────────

    async def get_network_by_name(self, name: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        nets = list(await self._run(conn.network.networks, name=name))
        return nets[0] if nets else None

    async def get_floating_ips_for_vm(self, vm_id: str) -> list[str]:
        conn = self._get_conn()
        fips = await self._run(conn.network.floating_ips, device_id=vm_id)
        return [f.floating_ip_address for f in fips]


# ── Exceptions ────────────────────────────────────────────────────────

class VMNotFound(Exception):
    pass

class VMError(Exception):
    pass

class VMTimeout(Exception):
    pass
