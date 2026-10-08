"""Mounted volumes and how full they are, read from the operating system."""

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

_PSEUDO_FILESYSTEMS = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "securityfs",
    "pstore", "debugfs", "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs",
    "binfmt_misc", "autofs", "rpc_pipefs", "overlay", "squashfs", "nsfs", "bpf",
}  # fmt: skip
_WINDOWS_DRIVE_KINDS = {2: "removable", 3: "fixed", 4: "network", 5: "optical", 6: "ramdisk"}


@dataclass(frozen=True)
class Volume:
    mount: str
    label: str | None
    filesystem: str | None
    kind: str
    total_bytes: int
    used_bytes: int
    free_bytes: int


def _usage(mount: str) -> tuple[int, int, int] | None:
    try:
        usage = shutil.disk_usage(mount)
    except OSError:
        return None
    return usage.total, usage.used, usage.free


def _windows_volumes() -> list[Volume]:
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    mask = kernel32.GetLogicalDrives()
    volumes: list[Volume] = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        mount = f"{chr(65 + index)}:\\"
        kind = _WINDOWS_DRIVE_KINDS.get(kernel32.GetDriveTypeW(mount), "other")
        label = ctypes.create_unicode_buffer(261)
        filesystem = ctypes.create_unicode_buffer(261)
        has_info = kernel32.GetVolumeInformationW(
            mount, label, 261, None, None, None, filesystem, 261
        )
        usage = _usage(mount)
        if usage is None:
            continue
        volumes.append(
            Volume(
                mount=mount,
                label=(label.value or None) if has_info else None,
                filesystem=(filesystem.value or None) if has_info else None,
                kind=kind,
                total_bytes=usage[0],
                used_bytes=usage[1],
                free_bytes=usage[2],
            )
        )
    return volumes


def _posix_volumes() -> list[Volume]:
    mounts: list[tuple[str, str | None]] = []
    if os.path.exists("/proc/mounts"):
        with open("/proc/mounts", encoding="utf-8") as table:
            for line in table:
                parts = line.split()
                if len(parts) >= 3 and parts[2] not in _PSEUDO_FILESYSTEMS:
                    mounts.append((parts[1].replace("\\040", " "), parts[2]))
    else:
        mounts.append(("/", None))
        if os.path.isdir("/Volumes"):
            mounts += [(os.path.join("/Volumes", name), None) for name in os.listdir("/Volumes")]
    volumes: list[Volume] = []
    seen: set[int] = set()
    for mount, filesystem in mounts:
        try:
            device = os.stat(mount).st_dev
        except OSError:
            continue
        usage = _usage(mount)
        if usage is None or device in seen or usage[0] == 0:
            continue
        seen.add(device)
        volumes.append(
            Volume(
                mount=mount,
                label=os.path.basename(mount) or None,
                filesystem=filesystem,
                kind="external" if mount.startswith(("/media", "/mnt", "/Volumes")) else "fixed",
                total_bytes=usage[0],
                used_bytes=usage[1],
                free_bytes=usage[2],
            )
        )
    return volumes


def list_volumes() -> list[Volume]:
    return _windows_volumes() if sys.platform == "win32" else _posix_volumes()


def volume_for(path: Path) -> Volume | None:
    """The volume a path lives on: the mounted volume with the longest mount
    path that contains it."""
    target = os.path.normcase(os.path.realpath(path))
    best: Volume | None = None
    for volume in list_volumes():
        mount = os.path.normcase(volume.mount)
        inside = target == mount.rstrip(os.sep) or target.startswith(mount.rstrip(os.sep) + os.sep)
        if inside and (best is None or len(volume.mount) > len(best.mount)):
            best = volume
    return best
