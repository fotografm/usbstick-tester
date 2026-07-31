#!/usr/bin/env python3
"""usbstick-tester - live read/write speed monitor for USB memory sticks.

Plug a stick in and watch sequential and random throughput, per-operation
latency, and the SLC cache cliff in real time, with the physical port, link
speed and driver it negotiated.

Writes are non-destructive: they go to one temp file on the stick's own mounted
filesystem, never to the raw device.  Reads use the raw device when permissions
allow, because that bypasses the filesystem and can sweep the whole medium.
"""

from __future__ import annotations

import argparse
import signal
import sys

from aiohttp import web

from sticklink.bench import Bench
from sticklink.metrics import Metrics
from sticklink.server import build_app


def _size(text: str) -> int:
    """Accept 512M / 2G / 4GiB as well as a plain byte count."""
    units = {"k": 10, "m": 20, "g": 30, "t": 40}
    cleaned = text.strip().lower().rstrip("ib")
    if cleaned and cleaned[-1] in units:
        return int(float(cleaned[:-1]) * (1 << units[cleaned[-1]]))
    return int(cleaned)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-d", "--device", default=None,
                    help="stick to test, e.g. /dev/sdb (default: the only USB "
                         "disk present, else pick one in the UI)")
    # argparse only runs `type` over strings that came off the command line, so
    # the defaults are given already-converted rather than as "2G".
    ap.add_argument("--file-size", type=_size, default=2 << 30,
                    help="size of the test file; must exceed the stick's SLC "
                         "cache to expose the write cliff (default 2G)")
    ap.add_argument("--chunk", type=_size, default=1 << 20,
                    help="sequential transfer size (default 1M)")
    ap.add_argument("--rand-block", type=_size, default=4096,
                    help="random transfer size (default 4k)")
    ap.add_argument("--queue-depth", type=int, default=8,
                    help="concurrent requests during random phases (default 8)")
    ap.add_argument("--phase-seconds", default="20,20,10,10",
                    help="seconds per phase: seq write, seq read, rand read, "
                         "rand write (default 20,20,10,10)")
    ap.add_argument("--no-raw-read", action="store_true",
                    help="never open /dev/sdX; read the test file back instead")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080, help="web UI port")
    args = ap.parse_args()

    try:
        phases = tuple(float(x) for x in args.phase_seconds.split(","))
        if len(phases) != 4:
            raise ValueError
    except ValueError:
        ap.error("--phase-seconds needs four comma-separated numbers")

    metrics = Metrics()
    bench = Bench(
        metrics,
        preferred=args.device,
        file_size=args.file_size,
        chunk=args.chunk,
        rand_block=args.rand_block,
        queue_depth=args.queue_depth,
        phase_seconds=phases,
        allow_raw_read=not args.no_raw_read,
    )
    bench.start()

    print(f"usbstick-tester: web UI on http://{args.host}:{args.port}/")
    print("usbstick-tester: writes go to a temp file only - the stick's data is safe")

    # Without this the temp file survives a SIGTERM, leaving gigabytes of
    # scratch data on a stick the user is about to walk away with.
    def _term(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)

    try:
        web.run_app(
            build_app(metrics, bench),
            host=args.host,
            port=args.port,
            print=None,
        )
    except KeyboardInterrupt:
        pass
    finally:
        bench.stop()
        metrics.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
