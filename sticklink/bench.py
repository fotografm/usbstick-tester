"""The I/O engine: keep the stick busy, and measure what it actually does.

Design constraints that drove almost every decision here:

**Writes never destroy data.**  All writing goes to one temp file inside the
stick's own mounted filesystem.  Nothing is ever written to ``/dev/sdX``.

**Reads prefer the raw device.**  Reading ``/dev/sdX`` directly with ``O_DIRECT``
sidesteps the filesystem entirely and can sweep the whole medium rather than
just the file we happened to write.  It needs root or ``disk`` group membership,
so it degrades to reading the temp file back, and the UI says which happened.

**The page cache will lie about everything.**  An ordinary ``write()`` of 500 MB
to a stick returns at RAM speed because that is where it went, and reading the
file back never touches USB at all.  Every path here opens ``O_DIRECT``; if a
filesystem refuses it we fall back to buffered plus ``POSIX_FADV_DONTNEED``,
which is weaker, and again the UI says so.

**Queue depth is part of the measurement, not an implementation detail.**
Sequential transfers at 1 MiB run at depth 1 because USB mass storage is
substantially serial anyway.  Random 4K runs across a small thread pool, because
a single synchronous requester measures round-trip latency rather than the
device - and reports the depth it used alongside the number.
"""

from __future__ import annotations

import errno
import os
import random
import shutil
import subprocess
import threading
import time

from . import device

O_DIRECT = getattr(os, "O_DIRECT", 0o40000)

# Errno values that mean "the stick went away", as opposed to an ordinary I/O
# refusal we could retry.  A yanked USB device produces several of these
# depending on where in the stack the transfer was when it vanished.
GONE = {errno.EIO, errno.ENODEV, errno.ENXIO, errno.EREMOTEIO,
        errno.ESHUTDOWN, errno.ENOMEDIUM, errno.EPIPE}

TMP_NAME = ".usbstick-tester.dat"

# Leave the filesystem room to breathe; filling a stick to the last byte makes
# the allocator itself the bottleneck and measures the wrong thing.
FREE_FRACTION = 0.80
MIN_FILE_BYTES = 64 << 20

# FAT32 keeps file length in a 32-bit field, so 4 GiB is unreachable. Backed off
# by a megabyte so the cap itself is comfortably legal.
FAT32_MAX = (4 << 30) - (1 << 20)


class DeviceGone(Exception):
    """The stick disappeared mid-transfer."""


def _explain(exc: OSError, target) -> str:
    """Turn a setup failure into something the user can act on.

    A bare "Permission denied: /media/.../.usbstick-tester.dat" is the single
    most likely thing to go wrong - a freshly created ext4 or xfs filesystem is
    owned by root, and udisks mounts it with its own permissions - and on its own
    it does not tell anyone what to do about it.
    """
    mount = (target or {}).get("mountpoint")
    if exc.errno in (errno.EACCES, errno.EPERM) and mount:
        return (f"No permission to write to {mount}. A freshly formatted ext4 or "
                f"xfs stick is owned by root; FAT and exFAT are not affected. "
                f"Fix it with: sudo chown $USER {mount}")
    if exc.errno == errno.EROFS and mount:
        return (f"{mount} is mounted read-only, so there is nowhere to put a "
                "test file. Remount it writable to test writes.")
    return exc.strerror or str(exc)


def _aligned(size: int):
    """A page-aligned buffer, which ``O_DIRECT`` requires.

    An anonymous mmap is page-aligned by construction, which is the cheapest way
    to satisfy the alignment rule without reaching for ``posix_memalign``
    through ctypes.
    """
    import mmap

    return mmap.mmap(-1, size)


def _open(path: str, flags: int):
    """Open preferring ``O_DIRECT``; report whether we got it."""
    try:
        return os.open(path, flags | O_DIRECT), True
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
        return os.open(path, flags), False


def _uncache(fd: int, offset: int, length: int) -> None:
    """Evict a range from the page cache - the buffered-mode consolation prize."""
    try:
        os.posix_fadvise(fd, offset, length, os.POSIX_FADV_DONTNEED)
    except (OSError, AttributeError):
        pass


class UdevWatcher:
    """Wakes the reconnect loop the moment the kernel sees a block/USB change.

    Optional; without pyudev we poll, which costs little because recovery is
    dominated by re-enumeration and filesystem mounting anyway.
    """

    def __init__(self):
        self.changed = threading.Event()
        self.available = False
        try:
            import pyudev  # noqa: F401
        except ImportError:
            return
        self.available = True
        threading.Thread(target=self._run, daemon=True, name="udev").start()

    def _run(self):
        import pyudev

        ctx = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(ctx)
        monitor.filter_by(subsystem="block")
        monitor.filter_by(subsystem="usb")
        for _ in iter(lambda: monitor.poll(), None):
            self.changed.set()


class Bench:
    def __init__(self, metrics, preferred: str | None = None, file_size: int = 2 << 30,
                 chunk: int = 1 << 20, rand_block: int = 4096, queue_depth: int = 8,
                 phase_seconds=(20, 20, 10, 10), allow_raw_read: bool = True):
        self.metrics = metrics
        self.preferred = preferred
        self.want_file_size = file_size
        self.chunk = chunk
        self.rand_block = rand_block
        self.queue_depth = queue_depth
        self.phase_seconds = dict(zip(
            ("seq_write", "seq_read", "rand_read", "rand_write"), phase_seconds))
        self.allow_raw_read = allow_raw_read

        self.target = None
        self.pinned = None            # phase name to hold, or None to rotate
        self.read_only = False        # nothing writable here; read phases only
        self.read_source = ""         # human-readable description of what reads hit
        self.writing = False          # a write is in flight - unplugging risks the filesystem
        self.direct_file = False
        self.direct_raw = False
        self.raw_read = False
        self.file_size = 0
        self.tmp_path = None
        self.notes = []

        self._fd_file = -1
        self._fd_raw = -1
        self._written_high = 0        # contiguous bytes of the temp file laid down
        self._seq_offset = 0
        self._stop_current = False    # abandon this stick and re-pick
        self._eject = threading.Event()
        self._ejected_name = None     # do not re-acquire this until it is unplugged
        self._last_error = None       # suppress identical repeats while retrying
        self._retry_delay = 2.0
        self._stop = threading.Event()
        self._udev = UdevWatcher()
        self._thread = threading.Thread(target=self._run, daemon=True, name="bench")

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._teardown()

    def _running(self) -> bool:
        """False as soon as anything wants the current device released.

        Checked inside every I/O loop, not just between phases, so an eject or a
        device change lands within one operation rather than after the rest of a
        twenty-second phase.
        """
        return not self._stop.is_set() and not self._stop_current

    def select(self, name: str | None):
        """Point the bench at a different stick; takes effect next cycle."""
        self.preferred = name
        self._ejected_name = None
        self._stop_current = True
        self.metrics.event("select", name or "auto")

    def eject(self):
        """Finish the current operation, clean up, unmount, and power off.

        The point is to reach a state where unplugging cannot corrupt anything:
        the test file is deleted, metadata is flushed, and the filesystem is
        unmounted before the user pulls the stick out.
        """
        self._eject.set()
        self._stop_current = True
        self.metrics.event("eject", "finishing the current operation")

    def _unmount(self, target):
        """Unmount every filesystem on the device, then power it down."""
        os.sync()
        fresh = device.describe(target["name"]) or target
        if not shutil.which("udisksctl"):
            return (False, "test file removed and data flushed, but udisks2 is not "
                       "installed so nothing was unmounted - unmount by hand "
                       "before unplugging")

        for mount in fresh.get("mounts", []):
            # Retried because a descriptor closed moments ago, or writeback
            # still draining, can make the first attempt fail spuriously.
            for attempt in range(3):
                done = subprocess.run(
                    ["udisksctl", "unmount", "-b", mount["source"]],
                    capture_output=True, text=True, timeout=30)
                if done.returncode == 0:
                    break
                if attempt < 2:
                    self._stop.wait(0.7)
                    os.sync()
            else:
                detail = done.stderr.strip() or "unknown error"
                if "busy" in detail.lower():
                    return (False, f"{mount['source']} is still in use by something "
                                   "else, so it was NOT unmounted - do not unplug yet. "
                                   "A second copy of this tool, a file manager or a "
                                   f"shell sitting in {mount['mountpoint']} will do "
                                   "it. Close it, then eject again.")
                return False, f"could not unmount {mount['source']}: {detail}"

        done = subprocess.run(
            ["udisksctl", "power-off", "-b", target["dev"]],
            capture_output=True, text=True, timeout=30)
        if done.returncode == 0:
            return True, f"{target['dev']} unmounted and powered off - safe to unplug"
        return (True, f"{target['dev']} unmounted - safe to unplug "
                      f"(power-off declined: {done.stderr.strip() or 'not supported'})")

    def pin(self, phase: str | None):
        self.pinned = phase
        self.metrics.event("pin", phase or "rotate all phases")

    # -- setup -------------------------------------------------------------

    def _await_device(self):
        """Block until a testable stick is present.  None only on shutdown."""
        announced = False
        while not self._stop.is_set():
            target = device.pick(self.preferred)

            # An ejected stick must not be picked straight back up, or the
            # eject would silently undo itself and start writing again.
            if self._ejected_name:
                if not any(d["name"] == self._ejected_name for d in device.scan()):
                    self._ejected_name = None   # physically gone; forget it
                    announced = False
                elif target is not None and target["name"] == self._ejected_name:
                    target = None

            if target is not None:
                # Only the root filesystem is grounds for refusal here.  A
                # device with nothing writable on it is still worth testing -
                # reads work fine - so that decision belongs to _prepare, which
                # is the code that knows whether a read source exists.
                if target["name"] in device.root_disks():
                    self.metrics.set_state(
                        "refused", f"{target['dev']} backs the root filesystem "
                                   "and will never be tested")
                else:
                    return target
            elif not announced and not self._ejected_name:
                self.metrics.set_state("searching", "waiting for a USB stick")
                announced = True
            self._udev.changed.clear()
            self._udev.changed.wait(0.5)
        return None

    def _open_raw(self, target):
        """Open the block device read-only, if we are allowed to."""
        self._fd_raw = -1
        self.raw_read = False
        if not self.allow_raw_read:
            return
        try:
            self._fd_raw, self.direct_raw = _open(target["dev"], os.O_RDONLY)
            self.raw_read = True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                self.notes.append(
                    f"No permission to read {target['dev']} directly, so reads "
                    "come from the filesystem instead. For raw reads across the "
                    "whole device, run as root or: sudo usermod -aG disk $USER "
                    "(then log out and back in).")
            else:
                self.notes.append(f"Raw read unavailable: {exc.strerror}")

    def _largest_file(self, mount: str):
        """Biggest readable file on a mount, for benchmarking a read-only stick.

        Bounded because a full walk of a large filesystem would stall startup
        for no benefit - the biggest file in the first few thousand entries is
        invariably big enough to read against.
        """
        best = None
        scanned = 0
        for root, _dirs, files in os.walk(mount, onerror=lambda e: None):
            for name in files:
                scanned += 1
                path = os.path.join(root, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if best is None or size > best[1]:
                    best = (path, size)
            if scanned > 20000:
                break
        return best if best and best[1] >= self.rand_block else None

    def _prepare_read_only(self, target):
        """Set up read-only testing for a stick we cannot write to.

        A write-protected stick, an installer image, anything mounted ``ro`` -
        none of it can host a test file, but all of it can still be read, and
        read speed is half of what this tool exists to measure.  Refusing
        outright would throw that away.
        """
        self.read_only = True
        self.file_size = 0
        self._written_high = 0

        why = ("mounted read-only" if target["mounts"] else "not mounted")
        if self.raw_read:
            self.read_source = f"{target['dev']} (raw device)"
            self.notes.append(
                f"Read-only mode: {target['dev']} is {why}, so there is nowhere "
                "to put a test file and the write tests are skipped. Reading the "
                "raw device directly. To test writes, use a stick with a "
                "writable filesystem.")
            return

        mount = target["mountpoint"] or next(
            (m["mountpoint"] for m in target["mounts"]), None)
        found = self._largest_file(mount) if mount else None
        if found is None:
            raise OSError(errno.EROFS,
                          f"{target['dev']} is {why} and has no readable file to "
                          "test against - mount it writable, or grant raw read "
                          "access, to test this device")

        path, size = found
        self._fd_file, self.direct_file = _open(path, os.O_RDONLY)
        # Doubles as the read limit; see _read_source.
        self._written_high = size
        self.read_source = os.path.basename(path)
        self.notes.append(
            f"Read-only mode: {target['dev']} is {why}, so the write tests are "
            f"skipped. Reading back an existing file ({self.read_source}, "
            f"{size >> 20} MiB). Raw device access would cover the whole stick.")

    def _prepare(self, target):
        """Open the temp file and, if permitted, the raw device."""
        self.notes = []
        self.read_only = False
        self.read_source = ""
        self.tmp_path = None
        self._open_raw(target)

        mount = target["mountpoint"]
        if mount is None or target["readonly"]:
            self._prepare_read_only(target)
            for note in self.notes:
                self.metrics.event("note", note)
            return

        self.tmp_path = os.path.join(mount, TMP_NAME)
        self.read_source = (f"{target['dev']} (raw device)" if self.raw_read
                            else "the test file")

        existing = 0
        try:
            existing = os.path.getsize(self.tmp_path)
        except OSError:
            pass

        budget = int((target["free"] + existing) * FREE_FRACTION)
        self.file_size = max(self.chunk, min(self.want_file_size, budget))

        # FAT32 stores file length in 32 bits, so nothing on it can reach 4 GiB.
        # Without this, --file-size 4G or more fails partway through the first
        # write pass with EFBIG rather than at setup.
        if target.get("fstype") in ("vfat", "msdos") and self.file_size > FAT32_MAX:
            self.file_size = FAT32_MAX
            self.notes.append(
                "FAT32 cannot hold a file of 4 GiB or more, so the test file was "
                f"capped at {FAT32_MAX >> 20} MiB. If that is smaller than this "
                "stick's SLC cache, reformat as exFAT to see the write cliff.")

        self.file_size -= self.file_size % self.chunk
        if self.file_size < MIN_FILE_BYTES:
            raise OSError(errno.ENOSPC,
                          f"only {budget >> 20} MiB usable on {mount}")

        self._fd_file, self.direct_file = _open(
            self.tmp_path, os.O_RDWR | os.O_CREAT)
        if not self.direct_file:
            self.notes.append(
                "O_DIRECT refused on this filesystem - using buffered I/O with "
                "cache eviction, which is less exact")

        # Reusing an already-full file lets a restarted run read immediately
        # instead of waiting a whole write pass for the extent to exist.
        self._written_high = min(existing, self.file_size)
        self._seq_offset = 0
        try:
            os.ftruncate(self._fd_file, self.file_size)
        except OSError:
            pass  # the write pass extends it naturally; not worth failing over

        for note in self.notes:
            self.metrics.event("note", note)

    def _teardown(self):
        for attr in ("_fd_file", "_fd_raw"):
            fd = getattr(self, attr, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attr, -1)
        if self.tmp_path:
            try:
                os.unlink(self.tmp_path)
            except OSError:
                pass

    # -- main loop ---------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            target = self._await_device()
            if target is None:
                return

            self._stop_current = False
            try:
                self._prepare(target)
            except OSError as exc:
                text = _explain(exc, target)
                self.metrics.set_state("error", text)
                # An unusable stick left plugged in fails identically forever.
                # Logging it every couple of seconds would bury the event that
                # actually mattered, so log the first and back off the retries.
                if text != self._last_error:
                    self.metrics.event("error", text)
                    self._last_error = text
                    self._retry_delay = 2.0
                else:
                    self._retry_delay = min(self._retry_delay * 2, 30.0)
                self._teardown()
                self._stop.wait(self._retry_delay)
                continue

            self._last_error = None

            self.target = target
            detail = f"{target['dev']} on {target['port']} @ {target['link_name']}"
            if self.read_only:
                detail += " - read-only, write tests skipped"
            self.metrics.set_state("running", detail)
            self.metrics.event("start", detail)

            try:
                self._cycle()
            except DeviceGone as exc:
                self.metrics.set_state("gone", str(exc))
                self.metrics.event("gone", str(exc))
            finally:
                # Deletes the test file and closes the descriptors; must happen
                # before the unmount below or the filesystem is still busy.
                self._teardown()
                self.target = None
                self.writing = False
                self.metrics.set_phase("idle")

            if self._eject.is_set():
                self._eject.clear()
                self._ejected_name = target["name"]
                self.metrics.set_state("ejecting", f"unmounting {target['dev']}")
                ok, message = self._unmount(target)
                # Only claim it is safe to remove when it genuinely is.
                self.metrics.set_state("ejected" if ok else "eject_failed", message)
                self.metrics.event("ejected" if ok else "eject_failed", message)
                # Stay released either way. A failed unmount must not send us
                # straight back to writing to a stick the user is about to pull
                # out - eject means stop, and only a replug or an explicit
                # device selection resumes.

    def _cycle(self):
        """Rotate through the phases forever, or hold one if pinned."""
        order = [p for p in self.phase_seconds
                 if not (self.read_only and p.endswith("write"))]
        index = 0
        skipped = 0
        while not self._stop.is_set() and not self._stop_current:
            # A pin for a write phase cannot be honoured on a read-only device.
            pinned = self.pinned
            if pinned not in order:
                pinned = None
            phase = pinned or order[index % len(order)]
            index += 1

            # A read phase before anything has been written would either read
            # holes (which never reach the device) or nothing at all.
            if phase in ("seq_read", "rand_read") and not self.raw_read \
                    and self._written_high < self.rand_block:
                skipped += 1
            elif phase == "rand_write" and self._written_high < self.rand_block:
                skipped += 1
            else:
                skipped = 0

            if skipped:
                # Every phase in the rotation was skipped, so without this the
                # loop would spin at full tilt achieving nothing.
                if skipped >= len(order):
                    self._stop.wait(0.5)
                continue

            self.metrics.set_phase(phase)
            deadline = time.monotonic() + self.phase_seconds[phase]
            # Drives the "do not unplug" indicator. Cleared even if the phase
            # raises, so a failure never leaves it stuck on.
            self.writing = phase.endswith("write")
            try:
                if phase == "seq_write":
                    self._seq_write(deadline)
                elif phase == "seq_read":
                    self._seq_read(deadline)
                else:
                    self._random(phase, deadline)
            finally:
                self.writing = False

            # The phase label is deliberately left set until the next phase
            # begins: blanking it here would paint a one-bin idle stripe
            # between every phase on a chart that is otherwise continuous.

    # -- phases ------------------------------------------------------------

    def _guard(self, exc: OSError):
        if exc.errno in GONE or not os.path.isdir(
                os.path.join(device.BLOCK_DEVICES, self.target["name"])):
            raise DeviceGone(exc.strerror or str(exc))

    def _seq_write(self, deadline: float):
        # Incompressible payload: some controllers detect and elide runs of
        # zeroes, which would measure the firmware's cleverness, not the flash.
        buf = _aligned(self.chunk)
        buf.write(os.urandom(self.chunk))

        while time.monotonic() < deadline and self._running():
            if self._seq_offset + self.chunk > self.file_size:
                self._seq_offset = 0
            offset = self._seq_offset
            t0 = self.metrics.op_start("seq_write")
            try:
                n = os.pwrite(self._fd_file, buf, offset)
            except OSError as exc:
                self._guard(exc)
                raise
            self.metrics.op_done(t0, n, "seq_write")
            if not self.direct_file:
                _uncache(self._fd_file, offset, n)
            self._seq_offset = offset + n
            self._written_high = max(self._written_high, self._seq_offset)

        # Force the device to commit before we claim the bytes are on flash.
        # Timed as an event rather than folded into throughput: a multi-second
        # flush is a finding in itself, and averaging it into the trace would
        # hide both it and the write rate.
        t0 = time.monotonic()
        try:
            os.fsync(self._fd_file)
        except OSError as exc:
            self._guard(exc)
        flush = time.monotonic() - t0
        if flush > 0.25:
            self.metrics.event("flush", f"fsync held for {flush:.2f}s after writing")

    def _read_source(self):
        """(fd, limit, direct) for reads - raw device if we have it."""
        if self.raw_read and self._fd_raw >= 0:
            return self._fd_raw, self.target["capacity"], self.direct_raw
        return self._fd_file, self._written_high, self.direct_file

    def _seq_read(self, deadline: float):
        fd, limit, direct = self._read_source()
        buf = _aligned(self.chunk)
        offset = 0

        while time.monotonic() < deadline and self._running():
            if offset + self.chunk > limit:
                offset = 0
                if limit < self.chunk:
                    return
            t0 = self.metrics.op_start("seq_read")
            try:
                n = os.preadv(fd, [buf], offset)
            except OSError as exc:
                self._guard(exc)
                raise
            if n <= 0:
                return
            self.metrics.op_done(t0, n, "seq_read")
            if not direct:
                _uncache(fd, offset, n)
            offset += n

    def _random(self, phase: str, deadline: float):
        """Random small-block I/O across a thread pool.

        The pool is the point.  One synchronous requester measures a round trip
        - submit, wait, repeat - and reports a number bounded by latency rather
        than by the device.  Several in flight is what a real workload looks
        like, and what UAS can actually pipeline.  BOT will serialise it anyway,
        which is itself worth seeing.
        """
        reading = phase == "rand_read"
        if reading:
            fd, limit, direct = self._read_source()
        else:
            fd, limit, direct = self._fd_file, self._written_high, self.direct_file

        span = limit - self.rand_block
        if span <= 0:
            return
        slots = span // self.rand_block
        failure = []

        def worker(seed: int):
            rng = random.Random(seed)
            buf = _aligned(self.rand_block)
            if not reading:
                buf.write(os.urandom(self.rand_block))
            while time.monotonic() < deadline and self._running():
                offset = rng.randrange(slots) * self.rand_block
                t0 = self.metrics.op_start(phase)
                try:
                    if reading:
                        n = os.preadv(fd, [buf], offset)
                    else:
                        n = os.pwrite(fd, buf, offset)
                except OSError as exc:
                    failure.append(exc)
                    return
                if n <= 0:
                    return
                self.metrics.op_done(t0, n, phase)
                if not direct:
                    _uncache(fd, offset, n)

        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True,
                             name=f"{phase}-{i}")
            for i in range(self.queue_depth)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if failure:
            self._guard(failure[0])
            raise failure[0]

    # -- reporting ---------------------------------------------------------

    def status(self):
        target = self.target
        return {
            "target": target,
            "pinned": self.pinned,
            "read_only": self.read_only,
            "read_source": self.read_source,
            "writing": self.writing,
            "ejected": self._ejected_name,
            "chunk": self.chunk,
            "rand_block": self.rand_block,
            "queue_depth": self.queue_depth,
            "file_size": self.file_size,
            "tmp_path": self.tmp_path,
            "direct_io": self.direct_file,
            "raw_read": self.raw_read,
            "raw_direct": self.direct_raw,
            "written_high": self._written_high,
            "udev": self._udev.available,
            "notes": self.notes,
            "phase_seconds": self.phase_seconds,
        }
