"""
cnf.storage.ceph — Ceph RBD operations for cross-cluster VM disk management.

Handles:
  - RBD mirror image promotion / demotion
  - Mirror sync lag checks
  - Image snapshot management
  - Pre-migration readiness checks
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from typing import Any

from cnf.config import CephConfig, get_settings
from cnf.utils.logging import get_logger, RBD_PROMOTIONS

logger = get_logger(__name__)


@dataclass
class MirrorStatus:
    image: str
    pool: str
    state: str          # "up+replaying" | "up+stopped" | "down"
    description: str
    lag_bytes: int
    lag_seconds: int
    synced: bool


@dataclass
class RBDImageInfo:
    name: str
    pool: str
    size_bytes: int
    features: list[str]
    mirroring_enabled: bool
    mirroring_state: str


class CephRBDClient:
    """
    Async wrapper around the rbd CLI and librbd.

    We shell out to `rbd` for mirror operations because the Python
    bindings do not yet expose the full mirroring API. Direct RADOS
    calls are used for performance-sensitive paths.
    """

    def __init__(self, config: CephConfig | None = None) -> None:
        self.cfg = config or get_settings().ceph
        self._base_cmd = [
            "rbd",
            f"--conf={self.cfg.conf_path}",
            f"--keyring={self.cfg.keyring_path}",
            f"--id={self.cfg.client_name.removeprefix('client.')}",
            "--format=json",
        ]

    # ── internal helpers ──────────────────────────────────────────────

    async def _run(self, args: list[str]) -> dict[str, Any]:
        cmd = self._base_cmd + args
        logger.debug("rbd_cmd", cmd=" ".join(cmd))
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=60),
        )
        if result.returncode != 0:
            raise CephError(
                f"rbd command failed: {result.stderr.strip()}",
                cmd=cmd,
                returncode=result.returncode,
            )
        if result.stdout.strip():
            return json.loads(result.stdout)
        return {}

    async def _run_plain(self, args: list[str]) -> str:
        """Run rbd without --format=json, return raw stdout."""
        cmd = [
            "rbd",
            f"--conf={self.cfg.conf_path}",
            f"--keyring={self.cfg.keyring_path}",
            f"--id={self.cfg.client_name.removeprefix('client.')}",
        ] + args
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=60),
        )
        if result.returncode != 0:
            raise CephError(result.stderr.strip(), cmd=cmd, returncode=result.returncode)
        return result.stdout

    # ── image info ────────────────────────────────────────────────────

    async def get_image_info(self, pool: str, image: str) -> RBDImageInfo:
        data = await self._run(["info", f"{pool}/{image}"])
        return RBDImageInfo(
            name=data["name"],
            pool=pool,
            size_bytes=data.get("size", 0),
            features=data.get("features", []),
            mirroring_enabled="journaling" in data.get("features", []),
            mirroring_state=data.get("mirroring", {}).get("state", "disabled"),
        )

    # ── mirror operations ─────────────────────────────────────────────

    async def enable_mirror(self, pool: str, image: str) -> None:
        """Enable journaling-based mirroring on an image."""
        # Enable journaling feature first
        await self._run_plain(["feature", "enable", f"{pool}/{image}", "journaling"])
        await self._run_plain(["mirror", "image", "enable", f"{pool}/{image}", "journaling"])
        logger.info("rbd_mirror_enabled", pool=pool, image=image)

    async def get_mirror_status(self, pool: str, image: str) -> MirrorStatus:
        data = await self._run(["mirror", "image", "status", f"{pool}/{image}"])
        desc = data.get("description", "")
        lag_bytes = 0
        lag_seconds = 0

        # Parse lag from description: "replaying, master_position=..."
        if "entries_behind_master" in desc:
            try:
                part = desc.split("entries_behind_master=")[1].split(",")[0]
                lag_bytes = int(part) * 4096  # approx 4KB per entry
            except (IndexError, ValueError):
                pass

        state = data.get("state", "unknown")
        synced = state == "up+replaying" and lag_bytes == 0

        return MirrorStatus(
            image=image,
            pool=pool,
            state=state,
            description=desc,
            lag_bytes=lag_bytes,
            lag_seconds=lag_seconds,
            synced=synced,
        )

    async def wait_for_sync(
        self,
        pool: str,
        image: str,
        max_lag_bytes: int | None = None,
        timeout_seconds: int = 300,
        poll_interval: int = 5,
    ) -> MirrorStatus:
        """
        Poll mirror status until lag falls below threshold or timeout.
        Raises CephSyncTimeout if timeout is exceeded.
        """
        threshold = max_lag_bytes or self.cfg.mirror_lag_threshold_bytes
        elapsed = 0
        while elapsed < timeout_seconds:
            status = await self.get_mirror_status(pool, image)
            logger.info(
                "rbd_sync_check",
                pool=pool,
                image=image,
                state=status.state,
                lag_bytes=status.lag_bytes,
            )
            if status.lag_bytes <= threshold:
                return status
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        raise CephSyncTimeout(
            f"Image {pool}/{image} did not sync within {timeout_seconds}s. "
            f"Last lag: {status.lag_bytes} bytes"
        )

    async def demote(self, pool: str, image: str) -> None:
        """
        Demote primary image to secondary (source cluster, before cutover).
        This makes the image read-only and allows promotion on the destination.
        """
        logger.info("rbd_demote_start", pool=pool, image=image)
        await self._run_plain(["mirror", "image", "demote", f"{pool}/{image}"])
        logger.info("rbd_demote_done", pool=pool, image=image)

    async def promote(self, pool: str, image: str, force: bool = False) -> None:
        """
        Promote secondary image to primary (destination cluster, after cutover).
        force=True skips journal sync check — use only in emergency failover.
        """
        logger.info("rbd_promote_start", pool=pool, image=image, force=force)
        args = ["mirror", "image", "promote", f"{pool}/{image}"]
        if force:
            args.append("--force")
        await self._run_plain(args)
        logger.info("rbd_promote_done", pool=pool, image=image)
        RBD_PROMOTIONS.labels(cluster_id=get_settings().cluster_id, status="success").inc()

    async def snapshot(self, pool: str, image: str, snap_name: str) -> str:
        """Create a named snapshot. Returns full snapshot spec."""
        spec = f"{pool}/{image}@{snap_name}"
        await self._run_plain(["snap", "create", spec])
        logger.info("rbd_snapshot_created", spec=spec)
        return spec

    async def remove_snapshot(self, pool: str, image: str, snap_name: str) -> None:
        spec = f"{pool}/{image}@{snap_name}"
        await self._run_plain(["snap", "rm", spec])
        logger.info("rbd_snapshot_removed", spec=spec)

    async def list_images(self, pool: str) -> list[str]:
        data = await self._run(["ls", pool])
        return data if isinstance(data, list) else []

    # ── migration readiness ───────────────────────────────────────────

    async def preflight_check(
        self, pool: str, image: str, max_lag_bytes: int | None = None
    ) -> tuple[bool, str]:
        """
        Verify the mirror image is in a state safe to migrate.
        Returns (ok: bool, reason: str).
        """
        try:
            info = await self.get_image_info(pool, image)
        except CephError as e:
            return False, f"Image not found: {e}"

        if not info.mirroring_enabled:
            return False, f"Mirroring not enabled on {pool}/{image}"

        try:
            status = await self.get_mirror_status(pool, image)
        except CephError as e:
            return False, f"Cannot get mirror status: {e}"

        threshold = max_lag_bytes or self.cfg.mirror_lag_threshold_bytes
        if status.lag_bytes > threshold:
            return (
                False,
                f"Mirror lag too high: {status.lag_bytes} bytes "
                f"(threshold: {threshold} bytes)",
            )

        return True, "ok"

    async def get_vm_rbd_images(self, pool: str, vm_id: str) -> list[str]:
        """
        Return all RBD image names associated with a VM (by volume ID convention).
        In a standard OpenStack/Ceph setup, volume images are named
        'volume-<cinder-volume-uuid>'.
        """
        all_images = await self.list_images(pool)
        return [img for img in all_images if vm_id in img or f"volume-" in img]


# ── Exceptions ────────────────────────────────────────────────────────

class CephError(Exception):
    def __init__(self, message: str, cmd: list[str] | None = None, returncode: int = -1):
        super().__init__(message)
        self.cmd = cmd
        self.returncode = returncode


class CephSyncTimeout(Exception):
    pass
