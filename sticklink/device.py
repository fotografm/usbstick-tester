"""USB mass-storage discovery and physical port mapping.

Three independent views have to be joined to describe a stick honestly:

* ``/sys/block/<name>`` - the block device: capacity, block sizes, whether it is
  removable.
* the USB *interface* directory a few levels up the same path - which tells us
  whether the kernel bound ``uas`` or ``usb-storage``, a difference worth an
  order of magnitude on random I/O.
* the USB *device* directory above that - vendor/product/serial, negotiated link
  speed, declared power draw, and the physical port chain.

They are joined by walking one sysfs realpath, so there is no guessing and no
matching on strings that might collide between two identical sticks.
"""

from __future__ import annotations

import glob
import os
import re

BLOCK_DEVICES = "/sys/block"
USB_DEVICES = "/sys/bus/usb/devices"

# A USB interface directory looks like "1-4.2:1.0" - bus-port.chain:config.iface.
INTERFACE_RE = re.compile(r"^\d+-[\d.]+:\d+\.\d+$")

# Practical ceilings, not the marketing bit rate.  USB 2.0 high speed is
# 480 Mbit/s on the wire but bulk transfers with protocol overhead top out
# around 40 MB/s; USB 3 SuperSpeed is 5 Gbit/s of 8b/10b, so ~500 MB/s of
# payload, and real controllers land nearer 400.  Shown next to the measured
# rate so you can tell "slow stick" from "slow port" at a glance.
LINK_CEILING = {
    "1.5": (1.5, "USB 1.0 low-speed", 0.15),
    "12": (12, "USB 1.1 full-speed", 1.0),
    "480": (480, "USB 2.0 high-speed", 40.0),
    "5000": (5000, "USB 3.0 SuperSpeed", 400.0),
    "10000": (10000, "USB 3.1 gen 2", 900.0),
    "20000": (20000, "USB 3.2 gen 2x2", 1800.0),
}


def _read(path, name, default=""):
    try:
        with open(os.path.join(path, name)) as fh:
            return fh.read().strip()
    except OSError:
        return default


def _read_int(path, name, default=0):
    raw = _read(path, name)
    try:
        return int(raw)
    except ValueError:
        return default


def _port_label(node: str) -> str:
    """Human-readable location for a sysfs node name like ``1-5.4``."""
    bus, _, rest = node.partition("-")
    if not rest:
        return node
    hops = rest.split(".")
    if len(hops) == 1:
        return f"bus {bus} port {hops[0]}"
    chain = " > ".join(f"hub port {h}" for h in hops[1:])
    return f"bus {bus} port {hops[0]} > {chain}"


def _usb_chain(realpath: str):
    """Find the USB interface and device directories above a block device.

    Returns ``(interface_node, device_node)`` or ``(None, None)`` when the block
    device is not behind USB at all - which is how internal disks get excluded
    without ever needing a blocklist.
    """
    for part in reversed(realpath.split(os.sep)):
        if INTERFACE_RE.match(part):
            return part, part.split(":")[0]
    return None, None


def _driver_of(interface: str) -> str:
    """``uas`` or ``usb-storage``.

    BOT (``usb-storage``) issues one SCSI command at a time, so queue depth is
    effectively 1 no matter what the benchmark asks for.  UAS pipelines them.
    On random 4K this is frequently the entire explanation for a slow stick.
    """
    link = os.path.join(USB_DEVICES, interface, "driver")
    try:
        return os.path.basename(os.readlink(link))
    except OSError:
        return ""


def _mounts_for(name: str):
    """Every mounted filesystem living on this disk or one of its partitions."""
    out = []
    try:
        with open("/proc/mounts") as fh:
            lines = fh.readlines()
    except OSError:
        return out

    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        source, mountpoint, fstype = fields[0], fields[1], fields[2]
        if not source.startswith("/dev/"):
            continue
        base = source[len("/dev/"):]
        # Either the whole disk or one of its partitions: sdb, sdb1, sdb12.
        if base != name and not (base.startswith(name) and base[len(name):].isdigit()):
            continue
        # /proc/mounts octal-escapes exactly four characters.  Decoding the
        # whole string with unicode_escape would also mangle any non-ASCII
        # bytes in the path, so only these are undone.
        for code, char in (("\\040", " "), ("\\011", "\t"),
                           ("\\012", "\n"), ("\\134", "\\")):
            mountpoint = mountpoint.replace(code, char)
        entry = {
            "source": source,
            "mountpoint": mountpoint,
            "fstype": fstype,
            "options": fields[3] if len(fields) > 3 else "",
            "readonly": fields[3].split(",")[0] == "ro" if len(fields) > 3 else False,
        }
        try:
            st = os.statvfs(mountpoint)
            entry["total"] = st.f_blocks * st.f_frsize
            entry["free"] = st.f_bavail * st.f_frsize
        except OSError:
            entry["total"] = entry["free"] = 0
        out.append(entry)
    return out


def describe(name: str):
    """Full description of one block device, or None if it is not USB storage."""
    sysfs = os.path.join(BLOCK_DEVICES, name)
    if not os.path.isdir(sysfs):
        return None

    real = os.path.realpath(sysfs)
    interface, node = _usb_chain(real)
    if node is None:
        return None

    usb = os.path.join(USB_DEVICES, node)
    speed = _read(usb, "speed")
    ceiling = LINK_CEILING.get(speed)

    # /sys/block/*/size is always in 512-byte units regardless of the device's
    # own logical block size.  Getting this wrong is a factor-of-8 error on a 4Kn
    # device, so it is spelled out rather than inferred.
    capacity = _read_int(sysfs, "size") * 512

    mounts = _mounts_for(name)
    writable = [m for m in mounts if not m["readonly"]]

    return {
        "name": name,
        "dev": f"/dev/{name}",
        "capacity": capacity,
        "removable": _read(sysfs, "removable") == "1",
        "readonly": _read(sysfs, "ro") == "1",
        "rotational": _read(sysfs, "queue/rotational") == "1",
        "logical_block": _read_int(sysfs, "queue/logical_block_size", 512),
        "physical_block": _read_int(sysfs, "queue/physical_block_size", 512),
        "max_sectors_kb": _read_int(sysfs, "queue/max_sectors_kb"),
        "scheduler": _read(sysfs, "queue/scheduler"),
        "model": _read(sysfs, "device/model"),
        "vendor": _read(sysfs, "device/vendor"),
        "rev": _read(sysfs, "device/rev"),
        # -- USB view --
        "node": node,
        "port": _port_label(node),
        "interface": interface,
        "driver": _driver_of(interface),
        "vid": _read(usb, "idVendor"),
        "pid": _read(usb, "idProduct"),
        "serial": _read(usb, "serial"),
        "product": _read(usb, "product"),
        "manufacturer": _read(usb, "manufacturer"),
        "usb_version": _read(usb, "version"),
        "max_power": _read(usb, "bMaxPower"),
        "speed": speed,
        "link_name": ceiling[1] if ceiling else f"{speed} Mbit/s",
        "link_ceiling_mbs": ceiling[2] if ceiling else None,
        # -- filesystem view --
        "mounts": mounts,
        "mountpoint": writable[0]["mountpoint"] if writable else None,
        "fstype": writable[0]["fstype"] if writable else None,
        "free": writable[0]["free"] if writable else 0,
    }


def scan():
    """Every USB mass-storage device currently attached, by kernel name."""
    out = []
    for path in sorted(glob.glob(os.path.join(BLOCK_DEVICES, "*"))):
        info = describe(os.path.basename(path))
        if info is not None:
            out.append(info)
    return out


def root_disks():
    """Kernel names backing ``/`` and ``/boot`` - never testable, ever.

    A USB-booted machine really can have its root filesystem on a removable
    stick, so "is it USB" is not on its own a safe guard.
    """
    names = set()
    for target in ("/", "/boot", "/boot/efi"):
        try:
            st = os.stat(target)
        except OSError:
            continue
        major, minor = os.major(st.st_dev), os.minor(st.st_dev)
        link = f"/sys/dev/block/{major}:{minor}"
        try:
            real = os.path.realpath(link)
        except OSError:
            continue
        # Walk up out of a partition directory to the parent disk.
        while real and real != "/sys":
            if os.path.isdir(os.path.join(real, "queue")):
                names.add(os.path.basename(real))
                break
            real = os.path.dirname(real)
    return names


def pick(preferred: str | None = None):
    """Choose the stick to test.

    With no preference, auto-select only when the answer is unambiguous - one
    USB disk that is not backing the root filesystem.  Anything else is the
    user's call, made in the UI.
    """
    disks = scan()
    protected = root_disks()
    candidates = [d for d in disks if d["name"] not in protected]

    if preferred:
        want = preferred.split("/")[-1]
        return next((d for d in disks if d["name"] == want), None)
    if len(candidates) == 1:
        return candidates[0]
    return None
