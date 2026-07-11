#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
progress.py — check a run_pipeline sweep's progress and whether it has stalled.

Each completed work writes one JSON file, so progress = file count and liveness =
age of the newest file. Run this anytime (in another terminal) during a long sweep.

    python progress.py --out-dir out --keys-file all_keys.txt --phase pass1
    # loop it:  (git bash)  while true; do clear; python docs/progress.py ...; sleep 60; done

Exit code: 0 = progressing or complete, 2 = looks STALLED (no new output for a while).
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--keys-file", required=True)
    ap.add_argument("--phase", choices=["pass1", "pass2"], default="pass1")
    ap.add_argument("--stall-min", type=float, default=15,
                    help="minutes without a new file before calling it STALLED")
    args = ap.parse_args(argv)

    with open(args.keys_file, "r", encoding="utf-8") as f:
        total = sum(1 for ln in f if ln.strip())

    d = os.path.join(args.out_dir, args.phase)
    files = glob.glob(os.path.join(d, "*.json"))
    done = len(files)
    now = time.time()

    if files:
        newest = max(os.path.getmtime(p) for p in files)
        age_min = (now - newest) / 60
        last = time.strftime("%H:%M:%S", time.localtime(newest))
    else:
        age_min, last = None, "—"

    pct = 100 * done / total if total else 0
    bar_n = int(pct / 5)
    bar = "#" * bar_n + "." * (20 - bar_n)

    print(f"phase {args.phase}:  {done}/{total}  [{bar}] {pct:.0f}%")
    print(f"last completed: {last}" + (f"  ({age_min:.1f} min ago)" if age_min is not None else ""))

    if done >= total:
        print("status: COMPLETE ✓")
        return 0
    if age_min is not None and age_min > args.stall_min:
        print(f"status: ⚠ STALLED — no new output for {age_min:.0f} min "
              f"(> {args.stall_min:.0f}). Check the run/Ollama; safe to re-run to resume.")
        return 2
    print("status: running…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
