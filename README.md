# usbstick-tester

Plug in a USB memory stick and watch what it actually does — sequential and
random read/write throughput, per-operation latency, and the SLC cache cliff,
live in a browser, alongside the physical port, negotiated link speed and
driver it came up on.

A companion to [rtlsdr-usb-tester](https://github.com/fotografm/rtlsdr-usb-tester),
which does the same job for USB *link quality* using an SDR dongle as the load.

**Your data is safe.** Writes go to a single temp file on the stick's own mounted
filesystem, and nothing is ever written to the raw device. The temp file is
deleted when the program exits.

---

## What it measures

Four phases rotate continuously — 20 s, 20 s, 10 s, 10 s by default — until you
stop it. You can pin one phase in the UI and hold it indefinitely.

Pinning takes effect **immediately** — the phase in flight is abandoned rather
than allowed to run out its slot. The one thing that is never skipped is the
`fsync` at the end of a write phase, so switching away from writing takes as
long as the stick needs to commit what it already has: instant on a good stick,
a second or two on a slow one. The phase indicator shows `writing → reading…`
while that is happening, and the do-not-unplug warning stays lit until it is
genuinely done.

| Phase | What it does | Why it matters |
|---|---|---|
| Sequential write | 1 MiB `O_DIRECT` writes, queue depth 1 | The headline number, and the one that exposes the write cliff |
| Sequential read | 1 MiB `O_DIRECT` reads | Usually 2–5× the write speed on a cheap stick |
| Random read | 4 KiB reads across a thread pool | Reads scattered across the medium, as real use produces |
| Random write | 4 KiB writes across a thread pool | The number that decides whether the stick *feels* slow |

Every phase reports throughput, IOPS (for the random phases) and latency
percentiles. **The latency figures matter as much as the rate.** A stick
averaging 20 MB/s with a p99 of 30 ms behaves nothing like one averaging
20 MB/s with a p99 of 3 seconds, and the throughput trace alone cannot tell them
apart — which is why there is a separate latency chart.

---

## Install

Nothing is compiled. Two pure-Python packages, no build toolchain, no `-dev`
packages, and **no root required** for the default configuration.

### 1. Plug the stick in and check the system sees it

```bash
lsblk -o NAME,SIZE,TRAN,TYPE,MOUNTPOINT,MODEL
```

You want a line with `TRAN` = `usb` and `TYPE` = `disk`, plus a partition under
it with a mountpoint:

```
sdb           28.9G usb    disk            Ultra Fit
└─sdb1        28.9G usb    part /media/you/MYSTICK
```

If the stick appears but has **no mountpoint**, it is not mounted. Desktops
usually automount on insert; headless machines do not. Mount it:

```bash
udisksctl mount -b /dev/sdb1
```

`udisksctl` comes from the `udisks2` package. Failing that, mount it by hand:

```bash
sudo mkdir -p /mnt/stick
sudo mount /dev/sdb1 /mnt/stick
sudo chown "$USER" /mnt/stick
```

That `chown` matters — the tool writes as your user, not as root.

If the stick does not appear in `lsblk` at all, it is a hardware or enumeration
problem, not a filesystem one. Check `lsusb` and `dmesg | tail -20`.

**Filesystem drivers.** Most sticks ship formatted exFAT or FAT32. FAT32 is
built into every kernel; exFAT needs the `exfatprogs` package on Debian 13,
Ubuntu 22.04+ and Mint 22.x (older releases used `exfat-fuse`, which is slower
and does not support direct I/O well). NTFS sticks need `ntfs-3g`. If you have a
choice, **exFAT or ext4 gives the most honest numbers**; FUSE-based drivers add
their own overhead to everything measured here.

### 2. Get the code

```bash
git clone https://github.com/fotografm/usbstick-tester.git
cd usbstick-tester
```

Or without git installed:

```bash
mkdir usbstick-tester
wget -qO- https://github.com/fotografm/usbstick-tester/archive/refs/heads/main.tar.gz \
  | tar xz -C usbstick-tester --strip-components=1
cd usbstick-tester
```

### 3. Create the virtualenv

```bash
python3 -m venv .venv
```

If that fails with **`ensurepip is not available`**, Debian-family distros split
`venv` into a separate package:

| Distro | Package |
|---|---|
| Ubuntu 22.04 | `python3-venv` |
| Linux Mint 22.x (Ubuntu 24.04 base) | `python3-venv` |
| Debian 13 | `python3.13-venv` |
| Debian 12 | `python3.11-venv` |

If your distro isn't listed, this derives the right name from the interpreter
you actually have:

```bash
sudo apt install -y "python$(python3 -V | cut -d' ' -f2 | cut -d. -f1,2)-venv"
```

> **Delete the broken `.venv` before retrying.** A failed `python3 -m venv` still
> leaves a partial directory behind, and a second `python3 -m venv .venv` over
> the top of it will *not* repair it — you get an env with no `pip`. Always:
>
> ```bash
> rm -rf .venv
> python3 -m venv .venv
> ```

### 4. Install the Python dependencies

```bash
.venv/bin/pip install -r requirements.txt
```

`aiohttp` serves the web UI. `pyudev` is optional — with it, insertion and
removal are noticed instantly; without it the tool polls twice a second and
everything still works.

### 5. Run it

```bash
.venv/bin/python run.py
```

It prints the URL and then stays quiet. Open `http://<host>:8080/` in a browser
— on the same machine that is <http://localhost:8080/>.

Testing starts by itself as soon as a stick is detected. Unplug the stick and it
returns to waiting; plug one back in and it picks up again. Stop it with
**Ctrl+C**, which deletes the temp file on the way out.

If you are running it on a remote machine, `--host 0.0.0.0` is already the
default, so you only need the box's firewall to allow port 8080.

### 6. Optional — enable raw reads

By default the read phases read the test file back. Given permission to open
the block device, they instead read `/dev/sdX` directly, which bypasses the
filesystem and sweeps the whole medium rather than just the file's extent.

Either run it with `sudo`:

```bash
sudo .venv/bin/python run.py
```

Or add yourself to the `disk` group once, which is tidier:

```bash
sudo usermod -aG disk "$USER"
```

**Log out and back in** for a group change to take effect — `id` should then
list `disk`. The device panel will show `Reads from: /dev/sdb raw (O_DIRECT)`
instead of `test file`.

Nothing is ever written to the raw device; it is opened read-only. `--no-raw-read`
forces the test-file path even when you have permission.

### Read-only sticks

A stick with no writable filesystem — a bootable installer, a write-protected
card, anything mounted `ro` — cannot host a test file, so the **write phases are
skipped and the read phases still run**. The UI says so plainly and greys out
the write rows and pin buttons. Nothing is written, ever.

Reads then come from, in order of preference:

1. **The raw device**, if you have permission. This is much the better option:
   it covers the whole stick and bypasses the filesystem entirely. See step 6.
2. **The largest existing file on the mount**, otherwise. This works without any
   privileges, but only exercises however much of the medium that one file
   occupies, and inherits whatever the filesystem does to the numbers.

Route 2 is a real caveat on ISO-formatted installer sticks: `iso9660` refuses
`O_DIRECT`, so the tool falls back to buffered reads with cache eviction, and the
device panel will say `buffered + fadvise` rather than `O_DIRECT`. Kernel
readahead can then serve some reads from RAM. **Add yourself to the `disk` group
and the numbers become trustworthy**, because the raw path is `O_DIRECT`.

To test writes, reformat the stick with a writable filesystem — which destroys
whatever is on it, so this tool will never do it for you.

### What happens with each kind of stick

Nothing here is destructive, and nothing is written except into the test file on
a writable filesystem. What varies is how much of the job can be done.

| Stick | Without raw access | With raw access (`disk` group) |
|---|---|---|
| **Unformatted**, no partition table | Cannot test — nothing to read or write. Says so and waits. | **Read tests run** on the raw device. No writes. |
| **Partitioned but not mounted** | Cannot test. Mount it, or use raw access. | **Read tests run** on the raw device. |
| **Filesystem straight on the device**, no partition table (`mkfs.ext4 /dev/sdb`) | Works normally — the whole device is treated as one mount. | Same, and reads come from the raw device. |
| **FAT32** (`vfat`) | Full read + write. `O_DIRECT` supported. | Same, reads sweep the whole stick. |
| **exFAT** | Full read + write. `O_DIRECT` supported. Best choice for large sticks. | Same. |
| **ext4 / xfs** | Full read + write **once you own the mountpoint** — see below. | Same. |
| **NTFS** | Works. Via FUSE `ntfs-3g` it falls back to buffered I/O; the kernel `ntfs3` driver does better. | Same. |
| **Mounted read-only** (installer image, write-protect switch) | Read tests only, from the largest file present. | Read tests on the raw device — better. |

Two formatting-specific gotchas the tool handles for you:

**ext4 and xfs sticks are owned by root.** Those filesystems carry their own
permissions, so a freshly created one mounts as root-owned and a normal user
cannot write to it. FAT and exFAT have no ownership concept and are mounted as
you, so they never hit this. The tool detects it and tells you the fix:

```bash
sudo chown "$USER" /media/you/YOURSTICK
```

**FAT32 cannot hold a file of 4 GiB or more**, because it stores file length in
32 bits. `--file-size` is capped at just under 4 GiB on `vfat` with a note in the
UI, rather than failing part-way through the first write pass. If that ceiling is
below your stick's SLC cache you will not see the write cliff — reformat as
exFAT to get a bigger test file.

A stick that cannot be tested at all is reported once and then retried quietly
with a backoff, so leaving an unusable one plugged in does not flood the event
log.

### If it won't start

| Symptom | Cause | Fix |
|---|---|---|
| `ensurepip is not available` | `venv` is a separate package | step 3 — and `rm -rf .venv` before retrying |
| `.venv/bin/pip: No such file` | a failed venv left a partial directory | `rm -rf .venv`, then recreate |
| UI says *"waiting for a USB stick"*, stick is plugged in | not mounted, or not a USB device | step 1 — `lsblk` and mount it |
| Write rows say *"skipped — device is read-only"* | nothing writable on the stick | expected — see [Read-only sticks](#read-only-sticks) |
| UI says *"backs the root filesystem"* | you selected the system disk | pick the right device; this refusal is deliberate |
| UI says *"only 0 MiB usable"* | the stick is full | free space, or lower `--file-size` |
| Device panel says `buffered + fadvise` | filesystem refused `O_DIRECT` | usually a FUSE driver — see step 1 |
| `Address already in use` | port 8080 taken | `--port 8081` |
| Page loads but stays blank / "disconnected" | websocket blocked | check any reverse proxy passes `Upgrade` headers |

---

## Run

```bash
.venv/bin/python run.py
```

| Flag | Default | Meaning |
|---|---|---|
| `-d`, `--device` | the only USB disk present | stick to test, e.g. `/dev/sdb` |
| `--file-size` | `2G` | test file size; must exceed the SLC cache |
| `--chunk` | `1M` | sequential transfer size |
| `--rand-block` | `4k` | random transfer size |
| `--queue-depth` | `8` | concurrent requests during random phases |
| `--phase-seconds` | `20,20,10,10` | seq write, seq read, rand read, rand write |
| `--no-raw-read` | off | never open `/dev/sdX` |
| `--host` | `0.0.0.0` | web UI bind address |
| `--port` | `8080` | web UI port |

Sizes accept `4k`, `512M`, `2G`, `1.5G` or a plain byte count.

With more than one stick attached, nothing is auto-selected — pick one from the
dropdown in the UI, or name it with `-d`. Devices that fail the safety checks
appear in the list but are greyed out and cannot be chosen.

---

## Reading the results

### The write cliff

Nearly every stick writes the first 1–4 GB fast into pseudo-SLC, then collapses
to direct TLC/QLC speed — 150 MB/s dropping to 8 MB/s is entirely normal, often
with multi-second stalls while the controller folds cache into main storage. A
short benchmark measures only the cache and reports a number the stick cannot
sustain.

The default 2 GiB test file is sized to blow past that cache on most sticks. If
the trace never drops, the stick's cache is bigger than the test — raise it with
`--file-size 8G`. **The sustained rate after the cliff is the stick's real write
speed**, and it is the number worth comparing between sticks.

### The device panel

- **Driver** — `uas` pipelines SCSI commands; `usb-storage` (BOT) issues one at a
  time. On random 4K this is frequently the entire explanation for a slow stick,
  and it is a property of the enclosure and kernel quirks, not the flash.
- **Link speed** — the *negotiated* rate with its practical ceiling, drawn as a
  dashed line on the chart. If a USB 3 stick shows `USB 2.0 high-speed`, it is in
  a USB 2 port or on a USB 2 cable. Note that such a device enumerates on the
  USB 2 root hub entirely, so "which socket is it in" and "what did it negotiate"
  are not the same question.
- **Physical port** — the sysfs port chain, e.g. `bus 1 port 4 > hub port 2`, so
  you can tell two identical sticks apart and compare sockets.

### Telling problems apart

| What you see | Likely cause |
|---|---|
| Write starts fast, drops to a fraction, stays there | Normal SLC cache exhaustion — the low number is the truth |
| Throughput fine, p99 latency in seconds | Controller stalling on garbage collection; the stick will feel awful in use |
| Random 4K far worse than sequential, driver is `usb-storage` | BOT serialisation, not the flash |
| Everything ~40 MB/s on a USB 3 stick | It negotiated USB 2 — wrong port or wrong cable |
| Rate collapses and recovers repeatedly | Marginal connector or cable, thermal throttling, or a dying stick |
| Headline reads `stalled` | An operation has been outstanding over 0.75 s and has not returned yet |

---

## Safety

Writes only ever go to `<mountpoint>/.usbstick-tester.dat`. Before testing
anything, the tool refuses:

- any disk backing `/`, `/boot` or `/boot/efi` — a machine really can be booted
  from a removable stick, so "is it USB" is not on its own a sufficient guard;
- any device that is not USB mass storage, checked by walking sysfs rather than
  by matching device names;
- read-only devices, and devices with no writable mounted filesystem.

Devices that fail these checks appear in the picker but cannot be selected.

### Is it safe to unplug mid-test?

**During a read phase, yes.** Nothing is in flight that can be lost, and USB is
designed for hot-plug — the tool notices the removal, says so, and waits for the
stick to come back.

**During a write phase, no.** Two things can go wrong, and neither is about the
data we wrote (that is a scratch file nobody cares about):

- **Filesystem corruption.** `O_DIRECT` keeps *our* data out of the page cache,
  but the filesystem's own metadata — allocation tables, directory entries —
  still goes through it. FAT32 and exFAT have no journal, so losing power
  part-way through a metadata update can damage the filesystem and take
  unrelated files with it. This is the realistic risk.
- **A confused controller.** Interrupting a stick while it is updating its
  internal flash translation tables can, on a cheap controller, lose a block or
  in rare cases brick the device. Uncommon, but it is how sticks die.

There is also a mundane consequence: yank it mid-test and the ~2 GiB
`.usbstick-tester.dat` is left behind, because the tool never gets the chance to
delete it. Delete it by hand, or just re-run and eject properly.

So the UI tells you which case you are in at all times, next to the device
picker:

| Indicator | Meaning |
|---|---|
| 🔴 **Writing — do not unplug** | A write is in flight |
| 🟢 **Reading — safe to unplug** | Read phase; nothing to lose |
| 🟢 **Ejected — safe to unplug** | Unmounted and powered off |
| 🔴 **Still mounted — do not unplug** | Eject was attempted and failed |

### The eject button

**⏏ Eject** does the whole safe-removal sequence: finishes the operation in
flight (within one operation — typically well under a second), stops testing,
deletes the test file, `sync`s, unmounts every filesystem on the device via
`udisksctl`, and powers the port down. Then it says whether it worked.

Testing stops either way and does **not** resume on its own, so a failed unmount
can never quietly put the stick back under load while you are reaching for it.
Plug the stick back in, or pick a device from the dropdown, to start again.

If the unmount fails as *busy*, something else is holding the filesystem — a
second copy of this tool, a file manager, or a shell whose working directory is
on the stick. Close it and eject again. Without `udisks2` installed nothing can
be unmounted automatically, and the tool says so rather than implying success.

### ⚠️ Continuous mode wears the stick

Rewriting a 2 GiB file at 20 MB/s is roughly 86 GB/hour. On a cheap TLC stick
that is a few full-drive writes per hour against maybe 300–1000 P/E cycles —
fine for a benchmark run of a few minutes, genuinely damaging if left running
for days. The UI keeps a running odometer in full-drive writes so the cost is
never invisible. Wear-levelling spreads it; it does not make it free.

---

## Design

### The page cache lies about everything

An ordinary `write()` of 500 MB to a stick returns at RAM speed, because that is
where it went; reading the file back never touches USB at all. Every path here
opens `O_DIRECT` with page-aligned buffers (anonymous `mmap`, which is
page-aligned by construction). If a filesystem refuses it, the fallback is
buffered I/O plus `POSIX_FADV_DONTNEED`, which is weaker — and the UI says so
rather than quietly reporting RAM speeds.

### Operations are spread over the time they occupied

At 8 MB/s a 1 MiB chunk takes ~125 ms, so a 250 ms bin catches two chunks or
three depending on phase — a ±50% ripple that looks exactly like a failing stick.
The same trap made a perfectly healthy RTL-SDR link paint a picket fence in the
companion project.

Storage lets us do better than that project could, because the worker knows each
operation's start *and* end time. Rather than crediting bytes to the bin they
landed in, every operation is spread across the interval it actually occupied. A
4-second stalled write draws as a 4-second low plateau, not forty empty bins
followed by one impossible spike.

That spreading is retroactive, so recent bins are re-sent on every push and the
client overwrites them by sequence number. Bytes are conserved exactly, including
the sliver belonging to a bin the clock has not opened yet.

### Stalls are reported before they finish

A long operation is invisible in the throughput trace until it returns, so a
separate in-flight watchdog reports an outstanding operation immediately — the
headline reads `stalled` while the trace still shows the last completed rate.

### Bins are closed by a clock, not by data

When a stick wedges, no operation ever completes. A binner driven by arriving
data would simply stop emitting, instead of drawing the flatline that *is* the
fault.

### Rolled-up figures come from the producer, not the viewer

The per-phase results are computed on the metrics thread, not when a browser
asks for them. Deriving them from the viewer meant a phase that completed with
nobody watching was never recorded — and with a rotation longer than the stats
window, that is most of them. It also costs the same whether zero or ten
browsers are connected.

### Queue depth is part of the answer, not a detail

One synchronous requester doing 4K random I/O measures round-trip latency, not
the device — the difference against a real queue is easily 10×. Sequential runs
at depth 1, since USB mass storage is largely serial anyway; random runs across a
thread pool, and the depth used is shown next to the number. Compare figures only
at equal depth.

### Rates are divided by the time the work actually took

Not by the nominal window. A phase that ran for one second out of the last ten
did not average its throughput over ten seconds, and dividing by the window would
understate it by the same factor.

---

## Layout

```
run.py                      argument parsing and wiring
sticklink/device.py         USB mass-storage discovery, sysfs port mapping
sticklink/bench.py          the I/O engine: phases, O_DIRECT, thread pool
sticklink/metrics.py        time-spread binning, latency percentiles
sticklink/server.py         aiohttp app and websocket push
sticklink/static/index.html the entire UI, no build step and no dependencies
```

---

## Limitations

- **Linux only.** It depends on sysfs, `O_DIRECT` and `posix_fadvise`.
- **Never validated against real hardware yet.** The build was tested against a
  simulated device and synthetic sysfs paths. The sysfs walk, `uas`/`usb-storage`
  detection, exFAT `O_DIRECT` behaviour and hotplug handling are all unproven on
  a physical stick. On the first real run, check the device panel says
  `O_DIRECT` rather than `buffered + fadvise`.
- Writes measure the filesystem plus the device, not the bare device. That is the
  price of not destroying your data, and it is a few percent on a sane filesystem.
- The temp file occupies at most 80% of free space, so a nearly full stick
  gets a small test file and may not reach its write cliff.
- `O_DIRECT` bypasses the kernel's page cache but not the *stick's* own buffer.
  The sustained portion of a run past the cliff is unaffected; the first second
  is not.

## Not yet built

- Capacity verification (h2testw/f3-style counterfeit detection). It is feasible
  non-destructively by filling free space with verifiable patterns.
- Saved per-stick score cards for comparing devices over time.
- usbmon correlation for CRC/retry counts.
- Thermal tracking across a long run.

## Licence

MIT
