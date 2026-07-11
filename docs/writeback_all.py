#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
writeback_all.py — drive the full write-back in 30-series chunks with .bak cleanup.

Each round: cbz_batch_runner (--backup, default 30 series) writes back a chunk, then
cleanup_baks verifies the new files (testzip + <Genre> match + PageCount) and deletes
their .bak. Repeats until every series in mapping.json is done. This keeps .bak
storage on the share bounded to ~one chunk at a time while the ZFS snapshot remains
the disaster-recovery net.

Resumable: stop it anytime (Ctrl-C / reboot); re-run the same command and the runner's
journal skips finished volumes. Safe to run unattended.

    python writeback_all.py --source-root Y:/books --staging D:/wb-staging \
        --mapping out/mapping.json --journal out/writeback.journal
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def series_done(journal_path: str) -> int:
    if not os.path.exists(journal_path):
        return 0
    with open(journal_path, "r", encoding="utf-8") as f:
        j = json.load(f)
    return sum(1 for s in j.get("series", {}).values() if s.get("state") == "done")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--staging", required=True)
    ap.add_argument("--mapping", required=True)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--batch-size", type=int, default=30)
    args = ap.parse_args(argv)

    total = len(json.load(open(args.mapping, encoding="utf-8")))
    py = sys.executable
    last = -1
    rnd = 0
    while True:
        rnd += 1
        done = series_done(args.journal)
        if done >= total:
            print(f"\n===== ALL DONE: {done}/{total} series written back =====")
            return 0
        if done == last:
            print(f"\n[stop] no progress this round ({done}/{total}); check the log above "
                  f"for failures, then re-run.")
            return 1
        last = done
        print(f"\n########## round {rnd}: {done}/{total} done — writing next {args.batch_size} ##########")
        r = subprocess.run([py, os.path.join(HERE, "cbz_batch_runner.py"),
                            "--source-root", args.source_root, "--staging", args.staging,
                            "--mapping", args.mapping, "--journal", args.journal,
                            "--batch-size", str(args.batch_size), "--backup"])
        if r.returncode not in (0, 1):   # 1 = some volume failed (kept going); other = fatal
            print(f"[fatal] runner exited {r.returncode}; stopping.")
            return r.returncode
        print(f"---------- round {rnd}: verifying + deleting .bak ----------")
        subprocess.run([py, os.path.join(HERE, "cleanup_baks.py"),
                        "--source-root", args.source_root, "--mapping", args.mapping])


if __name__ == "__main__":
    raise SystemExit(main())
