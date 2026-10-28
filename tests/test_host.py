"""Host collectors against fake /proc and /sys trees built in tmp_path."""

import os
from pathlib import Path

import pytest

from monitor import host


def write(root: Path, rel: str, text: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def link(root: Path, rel: str, target: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, p)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A laptop with an SSD holding LVM, an unmounted HDD and a USB SSD."""
    r = tmp_path
    pci = "sys/devices/pci0000:00/0000:00:17.0/ata1/host0/target0:0:0/0:0:0:0/block"
    # sda: the OS SSD, partitions sda1 (EFI) and sda2 (LVM physical volume)
    write(r, f"{pci}/sda/size", "234441648")
    write(r, f"{pci}/sda/queue/rotational", "0")
    write(r, f"{pci}/sda/device/model", "SanDisk SD9SN8W1")
    write(r, f"{pci}/sda/device/vendor", "ATA")
    write(r, f"{pci}/sda/dev", "8:0")
    for n, part in ((1, "sda1"), (2, "sda2")):
        write(r, f"{pci}/sda/{part}/partition", str(n))
        write(r, f"{pci}/sda/{part}/dev", f"8:{n}")
    # sdb: internal HDD, never formatted
    pci2 = "sys/devices/pci0000:00/0000:00:17.0/ata2/host1/target1:0:0/1:0:0:0/block"
    write(r, f"{pci2}/sdb/size", "1953525168")
    write(r, f"{pci2}/sdb/queue/rotational", "1")
    write(r, f"{pci2}/sdb/device/model", "ST1000LM035-1RK1")
    write(r, f"{pci2}/sdb/dev", "8:16")
    # sdc: USB SSD with one exfat partition, mounted at /mnt/media library
    usb = "sys/devices/pci0000:00/0000:00:14.0/usb2/2-1/2-1:1.0/host2/target2:0:0/2:0:0:0/block"
    write(r, f"{usb}/sdc/size", "1953525168")
    write(r, f"{usb}/sdc/queue/rotational", "0")
    write(r, f"{usb}/sdc/device/model", "Portable SSD")
    write(r, f"{usb}/sdc/device/vendor", "Samsung")
    write(r, f"{usb}/sdc/dev", "8:32")
    write(r, f"{usb}/sdc/sdc1/partition", "1")
    write(r, f"{usb}/sdc/sdc1/dev", "8:33")
    write(r, "run/udev/data/b8:33", "E:ID_FS_TYPE=exfat\n")
    # dm-0: the LVM logical volume on sda2
    write(r, "sys/devices/virtual/block/dm-0/size", "200000000")
    write(r, "sys/devices/virtual/block/dm-0/dev", "253:0")
    link(r, "sys/devices/virtual/block/dm-0/slaves/sda2", f"../../../../../../{pci}/sda/sda2")
    # loop0 must be ignored
    write(r, "sys/devices/virtual/block/loop0/size", "100")

    for name, dev in (("sda", f"{pci}/sda"), ("sdb", f"{pci2}/sdb"), ("sdc", f"{usb}/sdc"),
                      ("dm-0", "sys/devices/virtual/block/dm-0"), ("loop0", "sys/devices/virtual/block/loop0")):
        link(r, f"sys/block/{name}", f"../../{dev}")
        link(r, f"sys/class/block/{name}", f"../../../{dev}")
    for part, dev in (("sda1", f"{pci}/sda/sda1"), ("sda2", f"{pci}/sda/sda2"), ("sdc1", f"{usb}/sdc/sdc1")):
        link(r, f"sys/class/block/{part}", f"../../../{dev}")
    link(r, "sys/dev/block/253:0", "../../devices/virtual/block/dm-0")
    link(r, "dev/mapper/ubuntu--vg-ubuntu--lv", "../dm-0")
    write(r, "dev/dm-0")

    (r / "mnt/media library").mkdir(parents=True)
    (r / "boot/efi").mkdir(parents=True)
    write(r, "proc/1/mounts", "\n".join([
        "/dev/mapper/ubuntu--vg-ubuntu--lv / ext4 rw,relatime 0 0",
        "/dev/sda1 /boot/efi vfat rw 0 0",
        "/dev/sdc1 /mnt/media\\040library exfat rw 0 0",
        "overlay /var/lib/docker/overlay2/abc/merged overlay rw 0 0",
        "/dev/loop3 /snap/core22/1 squashfs ro 0 0",
        "tmpfs /run tmpfs rw 0 0",
    ]) + "\n")
    return r


def test_mounts_filters_and_unescapes(tree):
    mounts = host.read_mounts(tree)
    points = [m["mountpoint"] for m in mounts]
    assert points == ["/", "/mnt/media library"]
    root = mounts[0]
    assert root["block"] == "dm-0"
    assert 0 <= root["percent"] <= 100
    assert root["total"] > 0
    assert mounts[1]["block"] == "sdc1"


def test_mountinfo_majmin_wins(tree):
    write(tree, "proc/1/mountinfo", "29 1 253:0 / / rw,relatime shared:1 - ext4 /dev/root rw\n")
    (tree / "proc/1/mounts").write_text("/dev/root / ext4 rw 0 0\n")
    assert host.read_mounts(tree)[0]["block"] == "dm-0"


def test_lvm_and_partitions_resolve_to_parent_disk(tree):
    assert host.parent_disks(tree, "dm-0") == {"sda"}
    assert host.parent_disks(tree, "sdc1") == {"sdc"}
    assert host.parent_disks(tree, "sdb") == {"sdb"}
    assert host.parent_disks(tree, "nope") == set()


def test_disks(tree):
    disks = {d["name"]: d for d in host.read_disks(tree, host.read_mounts(tree))}
    assert set(disks) == {"sda", "sdb", "sdc"}
    assert disks["sda"]["kind"] == "ssd"
    assert disks["sda"]["mounts"] == ["/"]
    assert disks["sda"]["model"] == "SanDisk SD9SN8W1"
    assert disks["sdb"]["kind"] == "hdd"
    assert disks["sdb"]["state"] == "not mounted"
    assert disks["sdb"]["partitions"] == []
    assert disks["sdb"]["size"] == 1953525168 * 512
    assert disks["sdc"]["usb"] is True
    assert disks["sdc"]["model"] == "Samsung Portable SSD"
    assert disks["sdc"]["filesystems"] == ["exfat"]
    assert disks["sdc"]["mounts"] == ["/mnt/media library"]


def test_hwmon(tmp_path):
    write(tmp_path, "sys/class/hwmon/hwmon2/name", "coretemp")
    write(tmp_path, "sys/class/hwmon/hwmon2/temp1_input", "52000")
    write(tmp_path, "sys/class/hwmon/hwmon2/temp1_label", "Package id 0")
    write(tmp_path, "sys/class/hwmon/hwmon2/temp1_crit", "100000")
    write(tmp_path, "sys/class/hwmon/hwmon2/temp2_input", "49000")
    write(tmp_path, "sys/class/hwmon/hwmon2/temp2_label", "Core 0")
    write(tmp_path, "sys/class/hwmon/hwmon5/name", "dell_smm")
    write(tmp_path, "sys/class/hwmon/hwmon5/fan1_input", "2100")
    write(tmp_path, "sys/class/hwmon/hwmon5/fan1_label", "Processor Fan")
    write(tmp_path, "sys/class/hwmon/hwmon5/fan2_input", "0")
    write(tmp_path, "sys/class/hwmon/hwmon5/temp1_input", "41000")
    hw = host.read_hwmon(tmp_path)
    assert hw["cpu_temp"] == 52.0
    assert {"chip": "coretemp", "label": "Package id 0", "c": 52.0, "crit": 100} in hw["temps"]
    assert hw["fans"][0] == {"chip": "dell_smm", "label": "Processor Fan", "rpm": 2100}
    assert hw["fans"][1]["label"] == "fan2"


def test_hwmon_missing(tmp_path):
    assert host.read_hwmon(tmp_path) == {"cpu_temp": None, "temps": [], "fans": []}


def _battery(root, status="Discharging", ac="0"):
    write(root, "sys/class/power_supply/AC/type", "Mains")
    write(root, "sys/class/power_supply/AC/online", ac)
    b = "sys/class/power_supply/BAT0"
    write(root, f"{b}/type", "Battery")
    write(root, f"{b}/present", "1")
    write(root, f"{b}/capacity", "78")
    write(root, f"{b}/status", status)
    write(root, f"{b}/energy_full", "51000000")
    write(root, f"{b}/energy_full_design", "56000000")
    write(root, f"{b}/cycle_count", "214")
    write(root, f"{b}/power_now", "18500000")
    write(root, f"{b}/charge_control_start_threshold", "50")
    write(root, f"{b}/charge_control_end_threshold", "80")


def test_power_supply_on_battery(tmp_path):
    _battery(tmp_path)
    ps = host.read_power_supply(tmp_path)
    assert ps["ac_online"] is False
    bat = ps["battery"]
    assert bat["capacity"] == 78
    assert bat["health"] == 91.1
    assert bat["cycle_count"] == 214
    assert bat["power_w"] == 18.5
    assert (bat["threshold_start"], bat["threshold_end"]) == (50, 80)
    power = host.summarize_power(9.0, ps, 0.15)
    assert power["source"] == "battery"
    assert power["watts"] == 18.5
    assert power["monthly_cost"] == round(18.5 * 730 / 1000 * 0.15, 2)


def test_power_on_wall_uses_rapl(tmp_path):
    _battery(tmp_path, status="Not charging", ac="1")
    power = host.summarize_power(9.0, host.read_power_supply(tmp_path), 0.15)
    assert power["source"] == "rapl"
    assert power["system_w"] is None
    assert "CPU package" in power["label"]


def test_power_supply_missing(tmp_path):
    assert host.read_power_supply(tmp_path) is None
    assert host.summarize_power(None, None, 0.15)["watts"] is None


def test_rapl_wraparound(tmp_path):
    d = "sys/class/powercap/intel-rapl:0"
    write(tmp_path, f"{d}/max_energy_range_uj", "1000000000")
    write(tmp_path, f"{d}/energy_uj", "999000000")
    meter = host.RaplMeter(tmp_path)
    assert meter.read(now=100.0) is None  # needs two readings
    (tmp_path / d / "energy_uj").write_text("29000000")  # wrapped past the max
    assert meter.read(now=103.0) == 10.0  # 30 J over 3 s
    (tmp_path / d / "energy_uj").write_text("59000000")
    assert meter.read(now=106.0) == 10.0


NET_DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 1000      10    0    0    0     0          0         0     1000      10    0    0    0     0       0          0
enp3s0: 5000000   4000  0    0    0     0          0         0   200000     1500    0    0    0     0       0          0
tailscale0: 700  7    0    0    0     0          0         0      900       9    0    0    0     0       0          0
"""


def test_parse_net_dev():
    parsed = host.parse_net_dev(NET_DEV)
    assert parsed["enp3s0"] == (5000000, 200000)
    assert parsed["tailscale0"] == (700, 900)
    assert parsed["lo"] == (1000, 1000)


def test_network_rates_and_kinds(tmp_path):
    write(tmp_path, "proc/1/net/dev", NET_DEV)
    write(tmp_path, "sys/class/net/enp3s0/device/vendor", "0x10ec")
    write(tmp_path, "sys/class/net/enp3s0/speed", "1000")
    write(tmp_path, "sys/class/net/enp3s0/operstate", "up")
    write(tmp_path, "sys/class/net/tailscale0/operstate", "unknown")
    c = host.HostCollector(tmp_path)
    first = c._network(10.0)
    assert [n["name"] for n in first] == ["enp3s0", "tailscale0"]
    assert first[0]["rx_rate"] is None
    (tmp_path / "proc/1/net/dev").write_text(NET_DEV.replace("5000000", "5600000"))
    second = c._network(12.0)
    eth = second[0]
    assert eth["physical"] is True and eth["speed_mbps"] == 1000
    assert eth["rx_rate"] == 300000
    assert eth["tx_rate"] == 0
    assert second[1]["kind"] == "tailscale" and second[1]["physical"] is False


def test_gpus_and_lid(tmp_path):
    igpu = "sys/bus/pci/devices/0000:00:02.0"
    write(tmp_path, f"{igpu}/class", "0x030000")
    write(tmp_path, f"{igpu}/vendor", "0x8086")
    write(tmp_path, f"{igpu}/device", "0x3e9b")
    write(tmp_path, f"{igpu}/power/runtime_status", "active")
    write(tmp_path, f"{igpu}/boot_vga", "1")
    (tmp_path / igpu / "drm/renderD128").mkdir(parents=True)
    write(tmp_path, "sys/bus/pci/drivers/i915/bind")
    link(tmp_path, f"{igpu}/driver", "../../drivers/i915")
    dgpu = "sys/bus/pci/devices/0000:01:00.0"
    write(tmp_path, f"{dgpu}/class", "0x030200")
    write(tmp_path, f"{dgpu}/vendor", "0x10de")
    write(tmp_path, f"{dgpu}/device", "0x1c20")
    write(tmp_path, f"{dgpu}/power/runtime_status", "suspended")
    write(tmp_path, "sys/bus/pci/devices/0000:00:1f.3/class", "0x040300")  # audio, ignored
    write(tmp_path, "dev/dri/renderD128")
    write(tmp_path, "proc/acpi/button/lid/LID0/state", "state:      closed")

    gpus = {g["vendor"]: g for g in host.read_gpus(tmp_path)}
    assert set(gpus) == {"Intel", "NVIDIA"}
    assert gpus["Intel"]["driver"] == "i915"
    assert gpus["Intel"]["render_node"] == "renderD128"
    assert gpus["Intel"]["name"] == "UHD Graphics 630 (Mobile)"
    assert gpus["NVIDIA"]["runtime_status"] == "suspended"
    assert gpus["NVIDIA"]["driver"] is None
    assert host.render_node_ready(tmp_path) is True
    assert host.read_lid(tmp_path) == "closed"


def test_throttle_counts_once_per_core(tmp_path):
    for cpu, core in ((0, 0), (1, 1), (6, 0)):  # cpu6 is cpu0's hyperthread sibling
        base = f"sys/devices/system/cpu/cpu{cpu}"
        write(tmp_path, f"{base}/topology/core_id", str(core))
        write(tmp_path, f"{base}/thermal_throttle/core_throttle_count", "3")
        write(tmp_path, f"{base}/thermal_throttle/package_throttle_count", "5")
        write(tmp_path, f"{base}/cpufreq/scaling_cur_freq", "2200000")
    assert host.read_throttle_counts(tmp_path) == {"core": 6, "package": 5}
    assert host.read_cpu_freqs(tmp_path) == [2200, 2200, 2200]


def test_identity_and_passwd(tmp_path):
    write(tmp_path, "etc/hostname", "stx-server\n")
    write(tmp_path, "etc/os-release", 'PRETTY_NAME="Ubuntu 26.04.1 LTS"\nID=ubuntu\n')
    write(tmp_path, "sys/class/dmi/id/sys_vendor", "Dell Inc.")
    write(tmp_path, "sys/class/dmi/id/product_name", "G7 7588")
    write(tmp_path, "etc/passwd", "root:x:0:0::/root:/bin/bash\nisaac:x:1000:1000::/home/isaac:/bin/bash\n")
    ident = host.read_identity(tmp_path)
    assert ident["hostname"] == "stx-server"
    assert ident["os"] == "Ubuntu 26.04.1 LTS"
    assert ident["product"] == "G7 7588"
    assert host.read_passwd(tmp_path) == {0: "root", 1000: "isaac"}


def test_full_sample_never_raises(tmp_path):
    sample = host.HostCollector(tmp_path).sample()
    assert sample["mounts"] == [] and sample["disks"] == []
    assert sample["power_supply"] is None
    assert sample["memory"]["total"] > 0


def test_full_sample_on_this_machine():
    c = host.HostCollector(Path("/"))
    c.sample()
    s = c.sample()
    assert s["cpu"]["percent"] is not None
    assert s["processes"]["count"] > 0
