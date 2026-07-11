#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cleanup_baks.py — verify written .cbz then delete its .bak (post-batch cleanup).

For each "<name>.cbz.bak" under --source-root: re-open the live "<name>.cbz",
confirm zip integrity (testzip), that its <Genre> matches what mapping.json says
it should be, and that PageCount is present (progress-safety sanity). Only if all
pass is the .bak deleted. Anything suspicious keeps its .bak and is reported.

Run it after each --batch-size chunk of cbz_batch_runner so .bak storage on the
share stays bounded. The ZFS snapshot remains the disaster-recovery rollback; the
.bak is the fine-grained in-run rollback, safe to drop once the new file verifies.

    python cleanup_baks.py --source-root Y:/books --mapping out/mapping.json [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import zipfile
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cbz_tag_writer import build_genre_string  # noqa: E402


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--mapping", required=True, help="mapping.json to check <Genre> against")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.source_root)
    with open(args.mapping, "r", encoding="utf-8") as f:
        mapping = {k: build_genre_string(v if isinstance(v, list) else str(v).split(","))
                   for k, v in json.load(f).items()}

    baks = []
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]  # skip .zfs snapshots etc.
        for fn in files:
            if fn.lower().endswith(".cbz.bak"):
                baks.append(os.path.join(dp, fn))

    deleted = kept = 0
    for bak in sorted(baks):
        cbz = bak[:-4]  # strip ".bak"
        rel_key = os.path.relpath(os.path.dirname(cbz), root).replace("\\", "/")
        expected = mapping.get(rel_key)
        name = os.path.basename(cbz)
        try:
            if not os.path.exists(cbz):
                raise RuntimeError("live .cbz missing")
            # cp932 fallback: some .cbz carry Shift-JIS entry names the default
            # reader can't match (see cbz_tag_writer).
            for enc in (None, "cp932"):
                try:
                    with zipfile.ZipFile(cbz, "r", metadata_encoding=enc) as z:
                        if z.testzip() is not None:
                            raise RuntimeError("testzip failed")
                        ci = next((n for n in z.namelist()
                                   if os.path.basename(n).lower() == "comicinfo.xml"), None)
                        if ci is None:
                            raise RuntimeError("no ComicInfo.xml")
                        root_el = ET.fromstring(z.read(ci))
                        genre = (root_el.findtext("Genre") or "")
                        pages = root_el.findtext("PageCount")
                    break
                except zipfile.BadZipFile:
                    if enc == "cp932":
                        raise
                    continue
            if expected is None:
                raise RuntimeError(f"series not in mapping ({rel_key})")
            if genre != expected:
                raise RuntimeError(f"Genre mismatch: {genre!r} != {expected!r}")
            # progress-safety: PageCount must be UNCHANGED vs the backup (both-absent ok).
            pages_bak = None
            for enc in (None, "cp932"):
                try:
                    with zipfile.ZipFile(bak, "r", metadata_encoding=enc) as zb:
                        cib = next((n for n in zb.namelist()
                                    if os.path.basename(n).lower() == "comicinfo.xml"), None)
                        pages_bak = ET.fromstring(zb.read(cib)).findtext("PageCount") if cib else None
                    break
                except zipfile.BadZipFile:
                    if enc == "cp932":
                        raise
                    continue
            if pages != pages_bak:
                raise RuntimeError(f"PageCount changed {pages_bak!r}->{pages!r} (progress risk!)")
            # verified good
            if args.dry_run:
                print(f"  OK (would delete) {name}")
            else:
                os.remove(bak)
                print(f"  deleted .bak  {name}")
            deleted += 1
        except Exception as e:  # noqa: BLE001
            print(f"  KEEP .bak — {name}: {e}")
            kept += 1

    print(f"\n{'would delete' if args.dry_run else 'deleted'}: {deleted}  |  kept (suspicious): {kept}"
          f"  |  total .bak: {len(baks)}")
    return 1 if kept else 0


if __name__ == "__main__":
    raise SystemExit(main())
