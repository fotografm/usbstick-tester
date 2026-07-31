"""Throughput and latency sampling for storage I/O.

The naive approach - count bytes into fixed wall-clock bins - aliases badly
here.  A 1 MiB chunk written to a stick in its slow post-cache state takes
~125 ms, so a 250 ms bin catches two chunks or three depending on phase, a
+-50% ripple that looks exactly like a failing device.  It is the same trap that
made a healthy RTL-SDR link paint a picket fence.

Storage lets us do better than the SDR case ever could, because the worker knows
each operation's start *and* end time.  So instead of crediting bytes to the bin
they landed in, every operation is **spread across the interval it actually
occupied**.  A 4-second stalled write becomes a 4-second low plateau rather than
forty empty bins followed by one impossible spike.

That spreading is retroactive - a long operation only reports once it finishes -
so recent bins are re-sent on every push and the client overwrites them by
sequence number.  A stall is still visible the instant it starts, via the
separate in-flight watchdog below, so nothing about the live view waits on it.
"""

from __future__ import annotations

import collections
import threading
import time

BIN_MS = 100
WINDOW_S = 300

# How far back an operation is allowed to amend the record.  Six seconds is
# comfortably longer than any single-chunk stall worth plotting; past that the
# device is hung, not slow, and the in-flight watchdog is the right signal.
AMEND_BINS = 60

# Rolling window used for the per-phase headline figures.  Long enough that a
# single slow chunk does not dominate, short enough to show the SLC cache
# cliff as it happens rather than averaging across it.
STATS_WINDOW_S = 10.0

# Cap on operations examined per phase when computing those figures.  A random
# 4K phase can retire tens of thousands of operations a second, and neither
# sorting nor walking all of them five times a second is worth the CPU.  Because
# the rate is computed from the span the sampled operations actually cover, a
# truncated sample shortens the effective window without distorting the answer.
MAX_SAMPLES = 6000

# Idle time between consecutive operations of one phase that marks the end of
# that phase's most recent run.  The window can easily span two runs of the same
# phase with other phases in between; without this, the gap between them counts
# as time the phase was running and deflates its rate by the duty cycle.
#
# Measured against idle *between* operations, never within one, so a device that
# stalls for seconds inside a single operation does not get split - that stall
# belongs to the run and must stay in the average.
RUN_GAP_S = 0.25

PHASES = ("seq_write", "seq_read", "rand_read", "rand_write")


def _percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))
    return ordered[idx]


class Metrics:
    def __init__(self, bin_ms: int = BIN_MS, window_s: int = WINDOW_S):
        self.bin_s = bin_ms / 1000.0
        self.max_bins = int(window_s / self.bin_s)

        self._lock = threading.Lock()
        self._bins = {}                       # seq -> bin dict
        self._future = {}                     # seq -> bin the ticker has yet to open
        self._order = collections.deque()     # seqs, oldest first
        self._ops = collections.deque(maxlen=60000)   # (t_end, phase, bytes, latency)
        self.events = collections.deque(maxlen=500)

        self._seq = 0
        self._event_seq = 0
        self._phase = "idle"
        self._state = "starting"
        self._detail = ""
        self._inflight = None                 # (t_start, phase) or None
        # Last computed figures per phase.  The rotation is longer than the
        # stats window - by the time random write is running, sequential write
        # left the window entirely - so without this the results table could
        # never show all four phases at once, which is the whole point of it.
        self._last = {p: None for p in PHASES}
        self._phases = {p: None for p in PHASES}
        self._stop = threading.Event()
        self._t0 = time.monotonic()

        self.bytes_written = 0
        self.bytes_read = 0

        # Bin timestamps are relative to _t0, so a client that reconnects to a
        # restarted server must throw away what it has rather than interleave
        # two time axes onto one chart.
        self.epoch = f"{int(time.time() * 1000):x}"

        self._ticker = threading.Thread(target=self._run, daemon=True, name="metrics")
        self._ticker.start()

    # -- producer side -----------------------------------------------------

    def op_start(self, phase: str) -> float:
        """Mark an operation in flight and return its start time."""
        t = time.monotonic()
        with self._lock:
            self._inflight = (t, phase)
        return t

    def op_done(self, t_start: float, nbytes: int, phase: str) -> None:
        """Record a completed operation, spread over the time it occupied."""
        t_end = time.monotonic()
        with self._lock:
            self._inflight = None
            self._ops.append((t_end - self._t0, phase, nbytes, t_end - t_start))
            if phase.endswith("write"):
                self.bytes_written += nbytes
            else:
                self.bytes_read += nbytes
            self._spread(t_start - self._t0, t_end - self._t0, nbytes)

            # Worst latency in the bin the operation *finished* in, for the
            # latency trace.  Max rather than mean on purpose: one 3-second
            # stall among thirty fast operations is the whole story, and a mean
            # would bury it.
            entry = self._bin_slot(int((t_end - self._t0) // self.bin_s) + 1)
            if entry is not None:
                entry["lat"] = max(entry.get("lat") or 0.0, t_end - t_start)

    def _bin_slot(self, seq: int):
        """The bin for this sequence number, or None if it is gone for good.

        An operation routinely finishes a few microseconds before the tick that
        opens the bin it finished in.  Without a placeholder for the ticker to
        adopt, that final sliver of bytes - and the latency stamp that goes with
        it - is dropped, which is exactly the tail of every slow operation.
        Bins that have already aged out of the window are not resurrected.
        """
        entry = self._bins.get(seq)
        if entry is not None:
            return entry
        if seq <= self._seq:
            return None
        entry = self._future.get(seq)
        if entry is None:
            entry = {"seq": seq, "t": round(seq * self.bin_s, 3), "bytes": 0.0,
                     "bps": 0.0, "lat": None, "phase": self._phase}
            self._future[seq] = entry
        return entry

    def _spread(self, a: float, b: float, nbytes: int) -> None:
        """Credit ``nbytes`` to every bin the interval [a, b) overlaps.

        Caller holds the lock.  Bins that have already aged out are skipped
        rather than resurrected - losing a few bytes off the left edge of a
        300-second window is not worth the bookkeeping.
        """
        if nbytes <= 0:
            return
        if b <= a:  # sub-microsecond op; put it all in one bin
            b = a + 1e-9

        rate = nbytes / (b - a)
        first = int(a // self.bin_s)
        last = int(b // self.bin_s)
        for idx in range(first, last + 1):
            lo = max(a, idx * self.bin_s)
            hi = min(b, (idx + 1) * self.bin_s)
            if hi <= lo:
                continue
            entry = self._bin_slot(idx + 1)   # bin seq is 1-based
            if entry is None:
                continue
            entry["bytes"] += rate * (hi - lo)
            entry["bps"] = entry["bytes"] / self.bin_s

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    def set_state(self, state: str, detail: str | None = None) -> None:
        with self._lock:
            self._state = state
            if detail is not None:
                self._detail = detail

    def event(self, kind: str, detail: str = "") -> None:
        with self._lock:
            self._event_seq += 1
            self.events.append(
                {
                    "seq": self._event_seq,
                    "t": round(time.monotonic() - self._t0, 3),
                    "wall": time.time(),
                    "kind": kind,
                    "detail": detail,
                }
            )

    # -- ticker ------------------------------------------------------------

    def _run(self) -> None:
        """Open a new bin every tick.

        Clock-driven rather than data-driven, for the same reason as the SDR
        tool: when a stick wedges, no operation ever completes, and a binner
        driven by arriving data would simply stop emitting instead of drawing
        the flatline that *is* the fault.
        """
        next_tick = time.monotonic() + self.bin_s
        ticks = 0
        while not self._stop.wait(max(0.0, next_tick - time.monotonic())):
            now = time.monotonic()
            ticks += 1
            with self._lock:
                # Rolled up here rather than in snapshot() because snapshot()
                # only runs while a browser is attached.  Deriving these from
                # the viewer would mean a phase that completed with nobody
                # watching never got recorded at all - and with a rotation
                # longer than the stats window, that is most of them.  It also
                # costs the same whether zero or ten clients are connected.
                if ticks % 5 == 0:
                    self._phases = self._phase_stats(now - self._t0)
                self._seq += 1
                # Adopt whatever an already-completed operation credited to this
                # bin before the tick that opens it.
                entry = self._future.pop(self._seq, None)
                if entry is None:
                    entry = {"seq": self._seq, "bytes": 0.0, "bps": 0.0, "lat": None}
                entry["t"] = round(now - self._t0, 3)
                entry["phase"] = self._phase
                self._bins[self._seq] = entry
                self._order.append(self._seq)
                # A stalled ticker could otherwise let placeholders accumulate.
                for stale in [s for s in self._future if s < self._seq]:
                    self._future.pop(stale, None)
                while len(self._order) > self.max_bins:
                    self._bins.pop(self._order.popleft(), None)
            next_tick += self.bin_s
            if next_tick < now:  # we fell behind; resynchronise
                next_tick = now + self.bin_s

    def stop(self) -> None:
        self._stop.set()

    # -- consumer side -----------------------------------------------------

    def _phase_stats(self, now_rel: float):
        """Headline figures per phase over the recent window.

        Latency percentiles matter as much as the mean rate: a stick that
        averages 20 MB/s with a p99 of 3 seconds behaves nothing like one that
        averages 20 MB/s with a p99 of 30 ms, and the throughput trace alone
        cannot tell them apart.
        """
        cutoff = now_rel - STATS_WINDOW_S
        buckets = {p: {"bytes": 0, "ops": 0, "lat": [], "newest": None,
                       "oldest": None, "oldest_lat": 0.0} for p in PHASES}
        saturated = set()

        for t, phase, nbytes, lat in reversed(self._ops):
            if t < cutoff or len(saturated) == len(PHASES):
                break
            bucket = buckets.get(phase)
            if bucket is None or phase in saturated:
                continue
            # Walking backwards, so the operation seen previously for this phase
            # is the later one; the idle time before it started is the gap.
            if bucket["oldest"] is not None:
                if (bucket["oldest"] - bucket["oldest_lat"]) - t > RUN_GAP_S:
                    saturated.add(phase)   # reached the start of the latest run
                    continue
            bucket["bytes"] += nbytes
            bucket["ops"] += 1
            bucket["lat"].append(lat)
            if bucket["newest"] is None:
                bucket["newest"] = t
            bucket["oldest"] = t
            bucket["oldest_lat"] = lat
            if bucket["ops"] >= MAX_SAMPLES:
                saturated.add(phase)

        out = {}
        for phase, bucket in buckets.items():
            if not bucket["ops"]:
                # Fall back to the last run of this phase, aged so the UI can
                # show it as history rather than as a current reading.
                previous = self._last[phase]
                if previous is not None:
                    previous = dict(previous, age=now_rel - previous["at"])
                out[phase] = previous
                continue
            # Divide by the wall time these operations actually occupied -
            # from the start of the oldest to the end of the newest - not by
            # the nominal window.  A phase that ran for one second out of the
            # last ten did not average its throughput over ten seconds, and
            # dividing by the window would understate it by the same factor.
            span = (bucket["newest"] - bucket["oldest"]) + bucket["oldest_lat"]
            span = max(span, 1e-9)
            out[phase] = {
                "bps": bucket["bytes"] / span,
                "iops": bucket["ops"] / span,
                "ops": bucket["ops"],
                "span": span,
                "p50": _percentile(bucket["lat"], 0.50),
                "p99": _percentile(bucket["lat"], 0.99),
                "max": max(bucket["lat"]),
                "at": bucket["newest"],
                "age": 0.0,
            }
            self._last[phase] = out[phase]
        return out

    def snapshot(self, since_seq: int = 0, since_event: int = 0) -> dict:
        with self._lock:
            now_rel = time.monotonic() - self._t0

            # Always resend the amendable tail: a long operation credits bins
            # that were already pushed, and the client overwrites them by seq.
            floor = max(0, min(since_seq, self._seq - AMEND_BINS))
            bins = [self._bins[s] for s in self._order if s > floor]
            events = [e for e in self.events if e["seq"] > since_event]

            inflight = None
            if self._inflight is not None:
                start, phase = self._inflight
                inflight = {"phase": phase, "elapsed": now_rel - (start - self._t0)}

            return {
                "epoch": self.epoch,
                "bins": bins,
                "events": events,
                "seq": self._seq,
                "event_seq": self._event_seq,
                "state": self._state,
                "detail": self._detail,
                "phase": self._phase,
                "inflight": inflight,
                "bin_ms": int(self.bin_s * 1000),
                # Age is stamped at send time, not when the figures were rolled
                # up, so a reading always reports how old it really is.
                "phases": {
                    p: (None if r is None else dict(r, age=now_rel - r["at"]))
                    for p, r in self._phases.items()
                },
                "bytes_written": self.bytes_written,
                "bytes_read": self.bytes_read,
                "elapsed": now_rel,
            }
