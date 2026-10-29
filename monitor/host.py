"""Host collectors.

Everything the console knows about the machine comes from the kernel's virtual
filesystems:

- /proc is a window into the kernel's process table and counters (CPU time,
  memory, network bytes, mounts). Nothing in it lives on disk.
- /sys (sysfs) is the kernel's device tree: every disk, network card, sensor,
  battery and GPU appears as a folder of small text files.

Inside the container the host's root filesystem is mounted read-only at /host,
so every path below is joined onto ``root`` ("/host" in the container, "/" when
running straight on the server). Every collector returns None instead of
raising when a file is missing, because laptops, kernels and containers all
differ in what they expose.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import psutil

log = logging.getLogger(__name__)

KEEP_FSTYPES = {"ext4", "ext3", "xfs", "btrfs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "zfs", "f2fs"}
SKIP_MOUNT_PREFIXES = ("/snap", "/boot", "/var/lib/docker")
SKIP_BLOCK_PREFIXES = ("loop", "ram", "dm-", "zram", "sr", "fd", "md")
SKIP_NET_PREFIXES = ("lo", "veth", "docker", "br-")

PCI_VENDORS = {"0x8086": "Intel", "0x10de": "NVIDIA", "0x1002": "AMD"}
# A few devices worth naming even when pci.ids is not installed.
PCI_DEVICES = {
    ("0x8086", "0x3e9b"): "UHD Graphics 630 (Mobile)",
    ("0x8086", "0x3e92"): "UHD Graphics 630",
    ("0x10de", "0x1c20"): "GeForce GTX 1060 Mobile",
    ("0x10de", "0x1c8d"): "GeForce GTX 1050 Mobile",
}


# ---------------------------------------------------------------- file helpers

def read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except (OSError, ValueError):
        return None


def read_int(path: Path) -> int | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


def safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one collector; a failure becomes None and a debug log line."""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - a broken sensor must never stop the loop
        log.debug("collector %s failed", getattr(fn, "__name__", fn), exc_info=True)
        return None


def _unescape_mount(path: str) -> str:
    # /proc/mounts writes spaces and tabs as octal escapes (\040, \011).
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)


def host_path(root: Path, absolute: str) -> Path:
    return root / absolute.lstrip("/")


# ---------------------------------------------------------------------- identity

def read_identity(root: Path) -> dict:
    os_release: dict[str, str] = {}
    for line in (read_text(root / "etc/os-release") or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            os_release[key] = value.strip().strip('"')
    cpu_model = None
    for line in (read_text(root / "proc/cpuinfo") or "").splitlines():
        if line.startswith("model name"):
            cpu_model = line.split(":", 1)[1].strip()
            break
    dmi = root / "sys/class/dmi/id"
    return {
        "hostname": read_text(root / "etc/hostname") or os.uname().nodename,
        "os": os_release.get("PRETTY_NAME"),
        "kernel": read_text(root / "proc/sys/kernel/osrelease") or os.uname().release,
        "vendor": read_text(dmi / "sys_vendor"),
        "product": read_text(dmi / "product_name"),
        "cpu_model": cpu_model,
        "cpu_threads": psutil.cpu_count(logical=True),
        "cpu_cores": psutil.cpu_count(logical=False),
        "boot_time": read_boot_time(root),
    }


def read_uptime(root: Path) -> float | None:
    text = read_text(root / "proc/uptime")
    return float(text.split()[0]) if text else None


def read_boot_time(root: Path) -> float | None:
    up = read_uptime(root)
    return round(time.time() - up) if up is not None else None


# --------------------------------------------------------------------------- cpu

def read_loadavg(root: Path) -> list[float] | None:
    text = read_text(root / "proc/loadavg")
    if not text:
        return None
    return [float(x) for x in text.split()[:3]]


def read_cpu_freqs(root: Path) -> list[float]:
    """Current clock of each logical CPU in MHz (sysfs reports kHz)."""
    out = []
    base = root / "sys/devices/system/cpu"
    for d in sorted(base.glob("cpu[0-9]*"), key=lambda p: int(p.name[3:])):
        khz = read_int(d / "cpufreq/scaling_cur_freq")
        if khz is not None:
            out.append(round(khz / 1000))
    return out


def read_throttle_counts(root: Path) -> dict | None:
    """The CPU counts every time it slowed itself down to stay under its heat limit."""
    base = root / "sys/devices/system/cpu"
    core = 0
    package = None
    found = False
    seen_cores = set()
    for d in base.glob("cpu[0-9]*"):
        tt = d / "thermal_throttle"
        c = read_int(tt / "core_throttle_count")
        core_id = read_text(d / "topology/core_id")
        if c is not None:
            found = True
            # Hyperthread siblings report the same core counter; count each core once.
            if core_id not in seen_cores:
                core += c
                seen_cores.add(core_id)
        p = read_int(tt / "package_throttle_count")
        if p is not None:
            found = True
            package = max(package or 0, p)
    if not found:
        return None
    return {"core": core, "package": package or 0}


# -------------------------------------------------------------------------- net

def parse_net_dev(text: str) -> dict[str, tuple[int, int]]:
    """Return {iface: (rx_bytes, tx_bytes)} from the contents of /proc/net/dev."""
    out = {}
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) < 9:
            continue
        out[name.strip()] = (int(fields[0]), int(fields[8]))
    return out


def net_kind(root: Path, name: str) -> str:
    cls = root / "sys/class/net" / name
    if name.startswith("tailscale"):
        return "tailscale"
    if (cls / "wireless").exists() or (cls / "phy80211").exists():
        return "wifi" if (cls / "device").exists() else "virtual"
    if (cls / "device").exists():
        return "ethernet"
    return "virtual"


# ----------------------------------------------------------------------- mounts

def read_mountinfo_devices(root: Path) -> dict[str, str]:
    """mountpoint -> "major:minor" from /proc/1/mountinfo, the most reliable device id."""
    out = {}
    for line in (read_text(root / "proc/1/mountinfo") or "").splitlines():
        parts = line.split()
        if len(parts) > 4:
            out[_unescape_mount(parts[4])] = parts[2]
    return out


def read_mounts(root: Path) -> list[dict]:
    """Real filesystems the host has mounted, with df-style usage.

    /proc/1/mounts is the mount table seen by PID 1 (systemd) on the host, which
    is the host's view even though we read it from inside a container.
    """
    text = read_text(root / "proc/1/mounts") or read_text(root / "proc/mounts")
    if not text:
        return []
    majmin = read_mountinfo_devices(root)
    seen = set()
    mounts = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        device, mountpoint, fstype, opts = parts[0], _unescape_mount(parts[1]), parts[2], parts[3]
        if fstype not in KEEP_FSTYPES:
            continue
        if any(mountpoint == p or mountpoint.startswith(p + "/") for p in SKIP_MOUNT_PREFIXES):
            continue
        if mountpoint in seen:
            continue
        seen.add(mountpoint)
        entry: dict[str, Any] = {
            "mountpoint": mountpoint,
            "device": device,
            "fstype": fstype,
            "readonly": "ro" in opts.split(","),
            "block": resolve_block_name(root, device, majmin.get(mountpoint)),
        }
        entry.update(statvfs_usage(host_path(root, mountpoint)) or {})
        mounts.append(entry)
    return mounts


def statvfs_usage(path: Path) -> dict | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bfree * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    used = total - free
    # Same math as df: space reserved for root is neither "used" nor "available".
    denom = used + avail
    return {
        "total": total,
        "used": used,
        "free": avail,
        "percent": round(used / denom * 100, 1) if denom else 0.0,
    }


def resolve_block_name(root: Path, device: str, majmin: str | None = None) -> str | None:
    """Turn a mount source into a kernel block device name such as "sda2" or "dm-0"."""
    if majmin:
        link = root / "sys/dev/block" / majmin
        if link.exists():
            return Path(os.path.realpath(link)).name
    if device.startswith("/dev/"):
        dev = host_path(root, device)
        if dev.exists():
            name = Path(os.path.realpath(dev)).name
            if (root / "sys/class/block" / name).exists():
                return name
        name = Path(device).name
        if (root / "sys/class/block" / name).exists():
            return name
    return None


def parent_disks(root: Path, name: str, depth: int = 0) -> set[str]:
    """Follow partitions and LVM/dm devices down to the physical disk(s) underneath.

    LVM (Logical Volume Manager) lets several disks or partitions act as one
    pool. The kernel shows each logical volume as a "dm-N" device whose
    ``slaves`` folder lists what it is built from.
    """
    if depth > 8 or not name:
        return set()
    cls = root / "sys/class/block" / name
    if not cls.exists():
        return set()
    slaves = root / "sys/block" / name / "slaves"
    if name.startswith("dm-") or (slaves.is_dir() and any(slaves.iterdir())):
        out: set[str] = set()
        for s in slaves.iterdir() if slaves.is_dir() else []:
            out |= parent_disks(root, s.name, depth + 1)
        return out
    if (cls / "partition").exists():
        return {Path(os.path.realpath(cls)).parent.name}
    return {name}


# ------------------------------------------------------------------------ disks

def _udev_props(root: Path, block_dir: Path) -> dict[str, str]:
    dev = read_text(block_dir / "dev")
    props = {}
    if dev:
        for line in (read_text(root / f"run/udev/data/b{dev}") or "").splitlines():
            if line.startswith("E:") and "=" in line:
                k, _, v = line[2:].partition("=")
                props[k] = v
    return props


def read_disks(root: Path, mounts: list[dict]) -> list[dict]:
    """Physical disks, whether or not anything on them is mounted."""
    base = root / "sys/block"
    if not base.is_dir():
        return []
    by_disk: dict[str, list[str]] = {}
    for m in mounts:
        for disk in parent_disks(root, m.get("block") or ""):
            by_disk.setdefault(disk, []).append(m["mountpoint"])

    disks = []
    for d in sorted(base.iterdir()):
        name = d.name
        if name.startswith(SKIP_BLOCK_PREFIXES):
            continue
        sectors = read_int(d / "size") or 0
        if sectors == 0:
            continue  # empty card reader slots and the like
        real = os.path.realpath(d)
        partitions = sorted(p.name for p in d.iterdir() if (p / "partition").exists())
        props = _udev_props(root, d)
        fstypes = sorted({_udev_props(root, d / p).get("ID_FS_TYPE", "") for p in partitions} - {""})
        if not partitions and props.get("ID_FS_TYPE"):
            fstypes = [props["ID_FS_TYPE"]]
        model = read_text(d / "device/model") or props.get("ID_MODEL", "").replace("_", " ") or None
        vendor = read_text(d / "device/vendor")
        if vendor and model and vendor.lower() not in ("ata", "") and not model.lower().startswith(vendor.lower()):
            model = f"{vendor} {model}"
        rotational = read_int(d / "queue/rotational")
        mountpoints = sorted(set(by_disk.get(name, [])))
        disks.append({
            "name": name,
            "model": model,
            "size": sectors * 512,  # sysfs always counts 512-byte sectors
            "kind": "hdd" if rotational == 1 else "ssd",
            "usb": "/usb" in real,
            "removable": read_int(d / "removable") == 1,
            "partitions": partitions,
            "filesystems": fstypes,
            "mounts": mountpoints,
            "state": "mounted" if mountpoints else "not mounted",
        })
    return disks


# ------------------------------------------------------------- temps and fans

def read_hwmon(root: Path) -> dict:
    """Temperatures and fan speeds from every hwmon (hardware monitor) driver.

    On this laptop that is coretemp (CPU), the chipset (pch_cannonlake) and
    ACPI. Fans only appear once the dell-smm-hwmon driver is loaded, which the
    G7 doesn't do by itself (see the README).
    """
    temps = []
    fans = []
    cpu_temp = None
    for hw in sorted((root / "sys/class/hwmon").glob("hwmon*")):
        chip = read_text(hw / "name") or hw.name
        for t in sorted(hw.glob("temp*_input")):
            prefix = t.name[: -len("_input")]
            raw = read_int(t)
            if raw is None:
                continue
            label = read_text(hw / f"{prefix}_label") or prefix
            crit = read_int(hw / f"{prefix}_crit")
            entry = {
                "chip": chip,
                "label": label,
                "c": round(raw / 1000, 1),
                "crit": round(crit / 1000) if crit else None,
            }
            temps.append(entry)
            if chip == "coretemp" and label == "Package id 0":
                cpu_temp = entry["c"]
        for f in sorted(hw.glob("fan*_input")):
            prefix = f.name[: -len("_input")]
            rpm = read_int(f)
            if rpm is None:
                continue
            fans.append({"chip": chip, "label": read_text(hw / f"{prefix}_label") or prefix, "rpm": rpm})
    if cpu_temp is None:
        # Fallbacks for AMD (k10temp Tctl) or a coretemp that labels differently.
        for t in temps:
            if t["chip"] in ("coretemp", "k10temp", "zenpower") and t["label"] in ("Tctl", "Tdie", "Package id 0", "temp1"):
                cpu_temp = t["c"]
                break
    return {"cpu_temp": cpu_temp, "temps": temps, "fans": fans}


# ------------------------------------------------------------------ power supply

def read_power_supply(root: Path) -> dict | None:
    base = root / "sys/class/power_supply"
    if not base.is_dir():
        return None
    ac_online = None
    battery = None
    for ps in sorted(base.iterdir()):
        kind = read_text(ps / "type")
        if kind == "Mains":
            online = read_int(ps / "online")
            if online is not None:
                ac_online = bool(online) or bool(ac_online)
        elif kind == "Battery" and read_int(ps / "present") != 0 and battery is None:
            full = read_int(ps / "energy_full") or read_int(ps / "charge_full")
            design = read_int(ps / "energy_full_design") or read_int(ps / "charge_full_design")
            power_uw = read_int(ps / "power_now")
            if power_uw is None:
                cur = read_int(ps / "current_now")
                volt = read_int(ps / "voltage_now")
                if cur is not None and volt is not None:
                    power_uw = cur * volt // 1_000_000
            start = read_int(ps / "charge_control_start_threshold")
            end = read_int(ps / "charge_control_end_threshold")
            battery = {
                "name": ps.name,
                "capacity": read_int(ps / "capacity"),
                "status": read_text(ps / "status"),
                "health": round(full / design * 100, 1) if full and design else None,
                # Dell firmware reports 0 when it doesn't count cycles at all.
                "cycle_count": read_int(ps / "cycle_count") or None,
                "power_w": round(power_uw / 1_000_000, 2) if power_uw is not None else None,
                "threshold_start": start,
                "threshold_end": end,
                "model": read_text(ps / "model_name"),
            }
    if ac_online is None and battery is None:
        return None
    if ac_online is None and battery is not None:
        ac_online = battery["status"] != "Discharging"
    return {"ac_online": ac_online, "battery": battery}


# ------------------------------------------------------------------------- RAPL

class RaplMeter:
    """CPU package power from Intel RAPL (Running Average Power Limit).

    The CPU keeps a running total of the energy it has used, in microjoules.
    Two readings a few seconds apart give watts. The counter wraps back to zero
    at max_energy_range_uj, which we correct for.
    """

    def __init__(self, root: Path):
        self.dir = root / "sys/class/powercap/intel-rapl:0"
        self.last: tuple[int, float] | None = None
        self.max_range = read_int(self.dir / "max_energy_range_uj")

    @property
    def available(self) -> bool:
        return (self.dir / "energy_uj").exists()

    def read(self, now: float | None = None) -> float | None:
        energy = read_int(self.dir / "energy_uj")
        now = time.monotonic() if now is None else now
        if energy is None:
            return None
        prev, self.last = self.last, (energy, now)
        if prev is None:
            return None
        delta = energy - prev[0]
        if delta < 0:
            if not self.max_range:
                return None
            delta += self.max_range
        dt = now - prev[1]
        if dt <= 0:
            return None
        return round(delta / dt / 1_000_000, 2)


def summarize_power(cpu_w: float | None, supply: dict | None, rate: float) -> dict:
    battery = (supply or {}).get("battery") or {}
    on_battery = supply is not None and supply.get("ac_online") is False
    system_w = battery.get("power_w") if on_battery else None
    if system_w:
        watts, source, label = system_w, "battery", "Whole system, measured by the battery"
    elif cpu_w is not None:
        watts, source, label = cpu_w, "rapl", "CPU package only (RAPL); the whole laptop draws more"
    else:
        watts, source, label = None, None, "No power sensor available"
    return {
        "cpu_package_w": cpu_w,
        "system_w": system_w,
        "watts": watts,
        "source": source,
        "label": label,
        "rate": rate,
        "monthly_kwh": round(watts * 730 / 1000, 2) if watts is not None else None,
        "monthly_cost": round(watts * 730 / 1000 * rate, 2) if watts is not None else None,
    }


# ------------------------------------------------------------------------- GPUs

_pci_ids_cache: dict[tuple[str, str], str] | None = None


def _pci_name(root: Path, vendor: str, device: str) -> str | None:
    """Look a device up in the host's pci.ids database (pciutils), if installed."""
    global _pci_ids_cache
    key = (vendor, device)
    if key in PCI_DEVICES:
        return PCI_DEVICES[key]
    if _pci_ids_cache is None:
        _pci_ids_cache = {}
        for candidate in ("usr/share/misc/pci.ids", "usr/share/hwdata/pci.ids"):
            path = root / candidate
            if path.is_file():
                _pci_ids_cache = _parse_pci_ids(path)
                break
    return _pci_ids_cache.get(key)


def _parse_pci_ids(path: Path) -> dict[tuple[str, str], str]:
    wanted = set(PCI_VENDORS)
    out = {}
    vendor = None
    try:
        with path.open(errors="replace") as fh:
            for line in fh:
                if not line.strip() or line.startswith("#"):
                    continue
                if not line.startswith("\t"):
                    if line.startswith("C "):
                        break  # device classes start; vendors are done
                    vid = "0x" + line[:4].lower()
                    vendor = vid if vid in wanted else None
                elif vendor and not line.startswith("\t\t"):
                    did, _, name = line.strip().partition("  ")
                    out[(vendor, "0x" + did.lower())] = name.strip()
    except OSError:
        pass
    return out


def read_gpus(root: Path) -> list[dict]:
    """Every display controller on the PCI bus (PCI class 0x03xxxx).

    ``power/runtime_status`` says whether the kernel has put the device to sleep
    ("suspended") or it is awake ("active"). The GTX 1060 should sit in
    "suspended" until something uses it.
    """
    out = []
    for dev in sorted((root / "sys/bus/pci/devices").glob("*")):
        cls = read_text(dev / "class")
        if not cls or not cls.startswith("0x03"):
            continue
        vendor = read_text(dev / "vendor") or ""
        device = read_text(dev / "device") or ""
        driver_link = dev / "driver"
        driver = Path(os.path.realpath(driver_link)).name if driver_link.exists() else None
        render = sorted(p.name for p in (dev / "drm").glob("renderD*")) if (dev / "drm").is_dir() else []
        out.append({
            "slot": dev.name,
            "vendor": PCI_VENDORS.get(vendor, vendor),
            "name": _pci_name(root, vendor, device),
            "driver": driver,
            "runtime_status": read_text(dev / "power/runtime_status"),
            "boot_vga": read_int(dev / "boot_vga") == 1,
            "render_node": render[0] if render else None,
        })
    return out


def quick_sync_node(root: Path, gpus: list[dict]) -> str | None:
    """The Intel GPU's render node, which Jellyfin needs for Quick Sync transcoding.

    The number is not fixed. On this laptop nouveau registers the GTX 1060
    first, so it gets renderD128 and the Intel GPU gets renderD129.
    """
    for g in gpus:
        node = g.get("render_node")
        if g.get("vendor") == "Intel" and node and (root / "dev/dri" / node).exists():
            return node
    return None


# -------------------------------------------------------------------------- lid

def read_lid(root: Path) -> str | None:
    for state in (root / "proc/acpi/button/lid").glob("*/state"):
        text = read_text(state)
        if text:
            return text.split(":", 1)[-1].strip()
    return None


# -------------------------------------------------------------------- processes

def read_passwd(root: Path) -> dict[int, str]:
    out = {}
    for line in (read_text(root / "etc/passwd") or "").splitlines():
        parts = line.split(":")
        if len(parts) > 2 and parts[2].isdigit():
            out[int(parts[2])] = parts[0]
    return out


PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


def read_thread_runtimes(pid_dir: Path) -> dict[int, int]:
    """Nanoseconds each thread of a process has spent on a CPU (task/*/schedstat)."""
    out = {}
    try:
        tids = os.listdir(pid_dir / "task")
    except OSError:
        return out
    for tid in tids:
        text = read_text(pid_dir / "task" / tid / "schedstat")
        if text and tid.isdigit():
            try:
                out[int(tid)] = int(text.split()[0])
            except (ValueError, IndexError):
                continue
    return out


def read_process(proc: Path, pid: int) -> dict | None:
    """One process from /proc/<pid>, without opening /proc/<pid>/stat.

    Reading another process's stat file runs a ptrace access check. Docker's
    AppArmor profile denies it for host processes and the kernel logs each
    denial to the journal, which buried every other line on the Logs page.
    comm, status, statm and schedstat hold what we need and are not checked.
    """
    d = proc / str(pid)
    name = read_text(d / "comm")
    status = read_text(d / "status")
    statm = read_text(d / "statm")
    if name is None or status is None or statm is None:
        return None
    if len(name) >= 15:  # comm is cut to 15 characters; the command line has the full name
        full = os.path.basename((read_text(d / "cmdline") or "").split("\0", 1)[0])
        if full.startswith(name):
            name = full
    uid = None
    for line in status.splitlines():
        if line.startswith("Uid:"):
            uid = int(line.split()[1])
            break
    pages = statm.split()
    return {
        "pid": pid,
        "name": name,
        "uid": uid,
        "rss": int(pages[1]) * PAGE_SIZE if len(pages) > 1 else 0,
        "threads": read_thread_runtimes(d),
    }


_CONTAINER_RE = re.compile(r"(?:docker[-/]|containerd[-/]|/)([0-9a-f]{64})")


def container_of(pid: int) -> str | None:
    """Short container id a process belongs to, read from its cgroup path."""
    text = read_text(Path(f"/proc/{pid}/cgroup"))
    if not text:
        return None
    m = _CONTAINER_RE.search(text)
    return m.group(1)[:12] if m else None


# ------------------------------------------------------------------ the collector

class HostCollector:
    """Holds the previous sample of every counter so it can report rates."""

    def __init__(self, root: Path, power_rate: float = 0.15, proc: Path = Path("/proc")):
        self.root = root
        self.power_rate = power_rate
        # The container shares the host's process list (pid: host), so its own
        # /proc lists every host process.
        self.proc = proc
        self.rapl = RaplMeter(root)
        self._net_prev: tuple[dict[str, tuple[int, int]], float] | None = None
        self._io_prev: tuple[dict[str, tuple[int, int]], float] | None = None
        self._throttle_prev: dict | None = None
        self._throttle_events = 0
        self._passwd: dict[int, str] = {}
        self._passwd_at = 0.0
        self._proc_prev: tuple[dict[int, dict], float] | None = None
        self.identity = safe(read_identity, root) or {}
        psutil.cpu_percent(percpu=True)  # prime the counters; the first call returns zeros

    # Each sample is a plain dict so it can go straight to JSON.
    def sample(self) -> dict:
        now = time.time()
        mono = time.monotonic()
        mounts = safe(read_mounts, self.root) or []
        supply = safe(read_power_supply, self.root)
        hw = safe(read_hwmon, self.root) or {"cpu_temp": None, "temps": [], "fans": []}
        cpu_w = safe(self.rapl.read, mono)
        gpus = safe(read_gpus, self.root) or []
        return {
            "time": now,
            "uptime_s": safe(read_uptime, self.root),
            "cpu": safe(self._cpu),
            "memory": safe(self._memory),
            "network": safe(self._network, mono),
            "mounts": mounts,
            "disks": safe(read_disks, self.root, mounts) or [],
            "disk_io": safe(self._disk_io, mono),
            "cpu_temp": hw["cpu_temp"],
            "temps": hw["temps"],
            "fans": hw["fans"],
            "power_supply": supply,
            "power": summarize_power(cpu_w, supply, self.power_rate),
            "gpus": gpus,
            "quick_sync": safe(quick_sync_node, self.root, gpus),
            "lid": safe(read_lid, self.root),
            "processes": safe(self._processes, mono),
        }

    def _cpu(self) -> dict:
        per_core = psutil.cpu_percent(percpu=True)
        freqs = read_cpu_freqs(self.root)
        throttle = read_throttle_counts(self.root)
        throttling = False
        if throttle and self._throttle_prev:
            grew = (throttle["core"] - self._throttle_prev["core"]) + (throttle["package"] - self._throttle_prev["package"])
            throttling = grew > 0
        self._throttle_prev = throttle
        return {
            "percent": round(sum(per_core) / len(per_core), 1) if per_core else None,
            "per_core": per_core,
            "freq_mhz": round(sum(freqs) / len(freqs)) if freqs else None,
            "freq_max_mhz": max(freqs) if freqs else None,
            "load": read_loadavg(self.root),
            "throttle": throttle,
            "throttling": throttling,
        }

    def _memory(self) -> dict:
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        used = vm.total - vm.available  # what apps really hold; cache can be dropped any time
        return {
            "total": vm.total,
            "used": used,
            "available": vm.available,
            "cached": getattr(vm, "cached", 0) + getattr(vm, "buffers", 0),
            "percent": round(used / vm.total * 100, 1) if vm.total else None,
            "swap_total": sw.total,
            "swap_used": sw.used,
            "swap_percent": sw.percent,
        }

    def _network(self, mono: float) -> list[dict]:
        text = read_text(self.root / "proc/1/net/dev") or read_text(self.root / "proc/net/dev")
        if not text:
            return []
        counters = parse_net_dev(text)
        prev = self._net_prev
        self._net_prev = (counters, mono)
        try:
            addrs = psutil.net_if_addrs()
        except Exception:  # noqa: BLE001
            addrs = {}
        out = []
        for name, (rx, tx) in counters.items():
            if name.startswith(SKIP_NET_PREFIXES):
                continue
            kind = net_kind(self.root, name)
            if kind == "virtual":
                continue
            rx_rate = tx_rate = None
            if prev and name in prev[0] and mono > prev[1]:
                dt = mono - prev[1]
                rx_rate = max(0, rx - prev[0][name][0]) / dt
                tx_rate = max(0, tx - prev[0][name][1]) / dt
            cls = self.root / "sys/class/net" / name
            speed = read_int(cls / "speed")
            ipv4 = [a.address for a in addrs.get(name, []) if a.family.name == "AF_INET"]
            out.append({
                "name": name,
                "kind": kind,
                "physical": kind in ("ethernet", "wifi"),
                "state": read_text(cls / "operstate"),
                "speed_mbps": speed if speed and speed > 0 else None,
                "ipv4": ipv4[0] if ipv4 else None,
                "rx_total": rx,
                "tx_total": tx,
                "rx_rate": round(rx_rate) if rx_rate is not None else None,
                "tx_rate": round(tx_rate) if tx_rate is not None else None,
            })
        out.sort(key=lambda n: (not n["physical"], n["name"]))
        return out

    def _disk_io(self, mono: float) -> dict[str, dict]:
        raw = psutil.disk_io_counters(perdisk=True) or {}
        counters = {k: (v.read_bytes, v.write_bytes) for k, v in raw.items() if not k.startswith(SKIP_BLOCK_PREFIXES)}
        counters = {k: v for k, v in counters.items() if (self.root / "sys/block" / k).exists()} or counters
        prev = self._io_prev
        self._io_prev = (counters, mono)
        out = {}
        for name, (r, w) in counters.items():
            if prev and name in prev[0] and mono > prev[1]:
                dt = mono - prev[1]
                out[name] = {
                    "read_rate": round(max(0, r - prev[0][name][0]) / dt),
                    "write_rate": round(max(0, w - prev[0][name][1]) / dt),
                }
            else:
                out[name] = {"read_rate": None, "write_rate": None}
        return out

    def _user(self, uid: int | None) -> str | None:
        if uid is None:
            return None
        if time.monotonic() - self._passwd_at > 300:
            self._passwd = read_passwd(self.root)
            self._passwd_at = time.monotonic()
        return self._passwd.get(uid, str(uid))

    def _processes(self, mono: float) -> dict:
        pids = [int(p) for p in os.listdir(self.proc) if p.isdigit()]
        prev = self._proc_prev
        seen: dict[int, dict] = {}
        rows = []
        for pid in pids:
            p = read_process(self.proc, pid)
            if p is None:
                continue  # it exited while we looked
            seen[pid] = p
            before = prev[0].get(pid) if prev else None
            if before is None or before["name"] != p["name"]:
                continue  # new process (or a reused pid): the next sample has its CPU use
            # A thread that started since the last sample did all its work in between.
            ns = sum(max(0, t - before["threads"].get(tid, 0)) for tid, t in p["threads"].items())
            rows.append({**p, "cpu_ns": ns})
        self._proc_prev = (seen, mono)

        threads = psutil.cpu_count() or 1
        dt = mono - prev[1] if prev else 0.0

        def finish(r: dict) -> dict:
            return {
                "pid": r["pid"],
                "name": r["name"],
                "user": self._user(r["uid"]),
                # % of the whole machine, like the CPU gauge
                "cpu": round(r["cpu_ns"] / (dt * 1e9 * threads) * 100, 1) if dt > 0 else 0.0,
                "rss": r["rss"],
                "container": container_of(r["pid"]),
            }

        by_cpu = sorted(rows, key=lambda r: r["cpu_ns"], reverse=True)[:8]
        by_mem = sorted(rows, key=lambda r: r["rss"], reverse=True)[:8]
        return {
            "count": len(pids),
            "by_cpu": [finish(r) for r in by_cpu],
            "by_mem": [finish(r) for r in by_mem],
        }
