#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cbz_batch_runner.py — Batch write-back orchestrator for Genre tags.

Pipeline per volume (handoff §4 / §6.4):

    SMB master ──pull──> local staging ──rewrite(<Genre>)──> verify ──push──> SMB (atomic)

Safety model:
  * The SMB master is only ever touched by the final atomic push: we copy the
    finished file to "<name>.tmp-<pid>" *in the same SMB directory* and then
    os.replace() it onto the original (atomic on one volume/share).
  * Rewriting itself happens on the LOCAL staging copy via cbz_tag_writer, which
    already builds a fresh lossless zip + verifies + atomic-swaps in place.
  * A JSON journal records per-volume state so a crash/stop resumes without
    redoing finished work and without leaving a master half-written.
  * Processing is series-at-a-time; staging files are deleted right after a
    successful push, so free-space need stays ~1 volume, not the whole batch.
  * --batch-size N stops after N not-yet-done series per run (default 30), giving
    a natural checkpoint to review logs between chunks. --all ignores the cap.

Operational prerequisites (ERRATA §A-2/A-3, §C-4):
  * ZFS snapshot FIRST (disaster recovery, orthogonal to per-batch resume):
        zfs snapshot <pool>/<dataset>@pre-tag-regen-YYYYMMDD
    The runner prints a reminder on a real (non-dry-run) start.
  * Kavita field-lock: if a series' Genres were ever hand-edited in Kavita's UI,
    that field is LOCKED and a ComicInfo rewrite + rescan will NOT show up. Verify
    on one series before a mass run; if locked, POST /api/Series/metadata with
    genresLocked=false (confirm DTO field names via /swagger).
  * Series→file mapping: this pipeline keys everything by real filesystem paths
    (scan → inventory → mapping → runner), so there is NO fragile series-name↔folder
    matching to go wrong. A Kavita-API cross-check (series→volumes→files.filePath)
    is an optional extra safety net once the API endpoint is configured.

Layout: a "series" is any directory containing volume files; its mapping key is
that directory's path relative to --source-root. Real library layout is
    <source-root>/<Library>/<author-dir>/<work-dir>/<volume>.cbz
  so keys look like "Fiction/AGA_東崎惟子/[2024] 少女星間漂流記 (電撃文庫)".
  Volume discovery uses os.walk (NOT glob) because real dir names contain
  [ ] ? ( ) ~ ! which glob would misread as metacharacters.
  Every .cbz under the key dir gets that series' genres (handoff §6.2).

Usage:
    python cbz_batch_runner.py \
        --source-root //NAS/manga \
        --staging     D:/kavita-staging \
        --mapping     mapping.json \
        --journal     run_journal.json \
        [--batch-size 30] [--all] [--backup] [--dry-run]

Exit code 0 = no failures this run; non-zero = at least one volume failed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cbz_tag_writer import build_genre_string, rewrite_cbz  # noqa: E402

DONE = "done"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(msg: str) -> None:
    print(f"{_now()}  {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #
class Journal:
    """Crash-safe run journal. Written atomically after every volume."""

    def __init__(self, path: str):
        self.path = path
        self.data: dict = {"version": 1, "created": _now(), "series": {}}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            self.data.setdefault("series", {})

    def vol_state(self, series: str, rel: str) -> str | None:
        return self.data["series"].get(series, {}).get("volumes", {}).get(rel, {}).get("state")

    def set_vol(self, series: str, rel: str, **fields) -> None:
        s = self.data["series"].setdefault(series, {"volumes": {}})
        s.setdefault("volumes", {})
        v = s["volumes"].setdefault(rel, {})
        v.update(fields)
        v["ts"] = _now()
        self._flush()

    def set_series_state(self, series: str, state: str) -> None:
        s = self.data["series"].setdefault(series, {"volumes": {}})
        s["state"] = state
        self._flush()

    def series_done(self, series: str) -> bool:
        return self.data["series"].get(series, {}).get("state") == DONE

    def _flush(self) -> None:
        tmp = f"{self.path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# Per-volume steps
# --------------------------------------------------------------------------- #
@dataclass
class Stats:
    pushed: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)


def _free_bytes(path: str) -> int:
    return shutil.disk_usage(os.path.dirname(os.path.abspath(path)) or ".").free


def _verify_staging(path: str, expected_genre: str) -> None:
    with zipfile.ZipFile(path, "r") as z:
        bad = z.testzip()
        if bad is not None:
            raise RuntimeError(f"staging integrity failed on {bad}")
        ci = next((n for n in z.namelist()
                   if os.path.basename(n).lower() == "comicinfo.xml"), None)
        if ci is None:
            raise RuntimeError("no ComicInfo.xml after rewrite")
        el = ET.fromstring(z.read(ci)).find("Genre")
        got = (el.text or "") if el is not None else None
        if got != expected_genre:
            raise RuntimeError(f"Genre mismatch: expected {expected_genre!r} got {got!r}")


def _atomic_push(staging_file: str, master_file: str, backup: bool) -> None:
    """Copy staging_file onto master_file atomically (temp in master's dir + replace)."""
    dst_dir = os.path.dirname(master_file)
    tmp = os.path.join(dst_dir, f".{os.path.basename(master_file)}.tmp-{os.getpid()}")
    shutil.copy2(staging_file, tmp)
    try:
        if backup:
            bak = master_file + ".bak"
            if not os.path.exists(bak):
                shutil.copy2(master_file, bak)
        os.replace(tmp, master_file)  # atomic on the share/volume
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def process_volume(src: str, rel: str, series: str, genres: list[str],
                   staging_root: str, jr: Journal, *, dry_run: bool, backup: bool,
                   stats: Stats) -> None:
    genre_str = build_genre_string(genres)
    if jr.vol_state(series, rel) == DONE:
        stats.skipped += 1
        _log(f"  SKIP (done)  {rel}")
        return

    staging_file = os.path.join(staging_root, rel)
    os.makedirs(os.path.dirname(staging_file), exist_ok=True)

    if dry_run:
        # Peek at current master Genre without copying the whole file.
        old = None
        try:
            with zipfile.ZipFile(src, "r") as z:
                ci = next((n for n in z.namelist()
                           if os.path.basename(n).lower() == "comicinfo.xml"), None)
                if ci:
                    el = ET.fromstring(z.read(ci)).find("Genre")
                    old = el.text if el is not None else None
        except Exception as e:  # noqa: BLE001
            _log(f"  DRY  {rel} — cannot read master: {e}")
            return
        change = "update" if old != genre_str else "unchanged"
        _log(f"  DRY  {rel} — would {change}  [{old} -> {genre_str}]")
        return

    try:
        need = os.path.getsize(src)
        if _free_bytes(staging_file) < need * 2:
            raise RuntimeError("insufficient staging free space (need ~2x volume size)")

        # 1. pull
        shutil.copy2(src, staging_file)
        jr.set_vol(series, rel, state="pulled", genre=genre_str)
        # 2. rewrite (lossless, verified, atomic swap in staging)
        r = rewrite_cbz(staging_file, genres, dry_run=False, backup=False)
        if not r.ok:
            raise RuntimeError(r.message)
        jr.set_vol(series, rel, state="written", old=r.old_genre, new=r.new_genre)
        # 3. verify staging copy independently
        _verify_staging(staging_file, genre_str)
        jr.set_vol(series, rel, state="verified")
        # 4. push back to SMB atomically
        _atomic_push(staging_file, src, backup)
        jr.set_vol(series, rel, state=DONE)
        stats.pushed += 1
        _log(f"  OK   {rel}  [{r.old_genre} -> {r.new_genre}]")
    except Exception as e:  # noqa: BLE001
        jr.set_vol(series, rel, state="failed", error=str(e))
        stats.failed += 1
        stats.failures.append(f"{rel}: {e}")
        _log(f"  FAIL {rel} — {e}")
    finally:
        # Free staging space regardless of outcome (master already safe).
        if not dry_run and os.path.exists(staging_file):
            try:
                os.remove(staging_file)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Discovery + batch loop
# --------------------------------------------------------------------------- #
def find_volumes(series_dir: str, ext: str = ".cbz") -> list[str]:
    # os.walk, NOT glob: real series dir names contain [ ] ? ( ) ~ ! which glob
    # would (mis)interpret as metacharacters. Recursive to catch any sub-nesting.
    hits: list[str] = []
    for dp, _dirs, files in os.walk(series_dir):
        for fn in files:
            if fn.lower().endswith(ext):
                hits.append(os.path.join(dp, fn))
    return sorted(hits)


def run(args) -> int:
    with open(args.mapping, "r", encoding="utf-8") as f:
        mapping: dict = json.load(f)

    if not args.dry_run:
        _log("REMINDER (ERRATA §A-3): take a ZFS snapshot before mass write-back "
             "-> zfs snapshot <pool>/<dataset>@pre-tag-regen-$(date +%Y%m%d)")
        _log("REMINDER (ERRATA §A-2): verify Kavita hasn't LOCKED Genres on a test "
             "series (hand-edited fields ignore ComicInfo rewrites until unlocked).")

    jr = Journal(args.journal)
    stats = Stats()

    all_series = sorted(mapping.keys())
    processed_series = 0

    for series in all_series:
        if jr.series_done(series):
            continue
        if not args.all and processed_series >= args.batch_size:
            _log(f"batch cap reached ({args.batch_size} series); stopping. "
                 f"re-run to continue (resumes from journal).")
            break

        genres = mapping[series]
        if isinstance(genres, str):
            genres = genres.split(",")
        series_dir = os.path.join(args.source_root, series)
        if not os.path.isdir(series_dir):
            _log(f"SERIES MISSING on source: {series}  (skipped)")
            jr.set_series_state(series, "missing")
            continue

        vols = find_volumes(series_dir, args.ext)
        if not vols:
            _log(f"SERIES has no .cbz: {series}  (skipped)")
            jr.set_series_state(series, "empty")
            continue

        _log(f"SERIES {series}  ({len(vols)} vol)  genres={build_genre_string(genres)}")
        processed_series += 1
        series_failed = False
        for v in vols:
            rel = os.path.relpath(v, args.source_root)
            before = stats.failed
            process_volume(v, rel, series, list(genres), args.staging, jr,
                           dry_run=args.dry_run, backup=args.backup, stats=stats)
            if stats.failed > before:
                series_failed = True
        if not args.dry_run:
            jr.set_series_state(series, "failed" if series_failed else DONE)

    _log(f"DONE  pushed={stats.pushed} skipped={stats.skipped} failed={stats.failed}"
         + ("  (dry-run)" if args.dry_run else ""))
    if stats.failures:
        _log("failures:")
        for fl in stats.failures:
            _log(f"  - {fl}")
    return 1 if stats.failed else 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-root", required=True, help="SMB/master root containing series dirs")
    ap.add_argument("--staging", required=True, help="local staging directory (fast disk, space for ~1 vol)")
    ap.add_argument("--mapping", required=True, help="JSON: { '<series-dir>': [genres...] }")
    ap.add_argument("--journal", required=True, help="run journal JSON (created/updated; enables resume)")
    ap.add_argument("--ext", default=".cbz", help="volume file extension under each series dir")
    ap.add_argument("--batch-size", type=int, default=30, help="max not-done series per run (default 30)")
    ap.add_argument("--all", action="store_true", help="ignore batch-size cap; process everything")
    ap.add_argument("--backup", action="store_true", help="keep <master>.bak before first push")
    ap.add_argument("--dry-run", action="store_true", help="report intended changes, touch nothing")
    args = ap.parse_args(argv)

    os.makedirs(args.staging, exist_ok=True)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
