#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cbz_library_scan.py — READ-ONLY inventory of the .cbz library.

Defines a "series" as *the directory that directly contains .cbz files*
(Kavita's notion: the folder grouping a work's volumes). This is layout-
agnostic: it does not care how deep <library>/<author>/<work> nesting goes.

For each series it records: relative dir key (from --root), volume list,
current <Genre> (read from the first volume's ComicInfo.xml), and flags
anomalies (no ComicInfo, tags in <Tags> instead of <Genre>, etc.).

Output: inventory.json keyed by the series dir relative to --root — the same
key space cbz_batch_runner.py consumes as mapping.json keys.

Usage:
    python cbz_library_scan.py --root Y:/books --out inventory.json [--read-all-vols]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from collections import defaultdict
from xml.etree import ElementTree as ET


def _read_comicinfo_genre(cbz_path: str):
    """Return (genre_text_or_None, source) where source in {'Genre','Tags',None,'ERR:..'}."""
    try:
        with zipfile.ZipFile(cbz_path, "r") as z:
            ci = next((n for n in z.namelist()
                       if os.path.basename(n).lower() == "comicinfo.xml"), None)
            if ci is None:
                return None, None
            root = ET.fromstring(z.read(ci))
            g = root.find("Genre")
            if g is not None and (g.text or "").strip():
                return g.text, "Genre"
            t = root.find("Tags")
            if t is not None and (t.text or "").strip():
                return t.text, "Tags"
            return None, "Genre"  # ComicInfo present but empty genre
    except Exception as e:  # noqa: BLE001
        return None, f"ERR:{e}"


def scan(root: str, read_all: bool) -> dict:
    root = os.path.abspath(root)
    # Group every .cbz by the directory that directly contains it.
    by_dir: dict[str, list[str]] = defaultdict(list)
    for dp, _dirs, files in os.walk(root):
        if os.sep + "." in os.sep + os.path.relpath(dp, root):  # skip .zfs etc.
            continue
        for fn in files:
            if fn.lower().endswith(".cbz"):
                by_dir[dp].append(os.path.join(dp, fn))

    inventory: dict = {}
    anomalies: list[str] = []
    n = 0
    for series_dir in sorted(by_dir):
        vols = sorted(by_dir[series_dir])
        rel = os.path.relpath(series_dir, root).replace("\\", "/")
        parts = rel.split("/")
        library = parts[0] if parts else ""
        author = parts[1] if len(parts) > 1 else ""
        work = parts[-1]

        genre, source = _read_comicinfo_genre(vols[0])
        # Optionally check every volume for consistency / missing ComicInfo.
        vol_issues = []
        if read_all:
            for v in vols:
                g, s = _read_comicinfo_genre(v)
                if s is None:
                    vol_issues.append(f"no-ComicInfo:{os.path.basename(v)}")
                elif isinstance(s, str) and s.startswith("ERR:"):
                    vol_issues.append(f"{s}:{os.path.basename(v)}")

        entry = {
            "library": library,
            "author": author,
            "work": work,
            "vol_count": len(vols),
            "volumes": [os.path.relpath(v, root).replace("\\", "/") for v in vols],
            "genre_current": genre,
            "genre_source": source,
        }
        if vol_issues:
            entry["vol_issues"] = vol_issues
        inventory[rel] = entry

        if source is None:
            anomalies.append(f"NO_COMICINFO: {rel}")
        elif isinstance(source, str) and source.startswith("ERR:"):
            anomalies.append(f"READ_ERROR: {rel} -> {source}")
        elif source == "Tags":
            anomalies.append(f"TAGS_NOT_GENRE: {rel}")
        n += 1

    return {"root": root, "series_count": n, "anomalies": anomalies, "inventory": inventory}


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--read-all-vols", action="store_true",
                    help="open every volume (slower) to flag per-volume ComicInfo issues")
    args = ap.parse_args(argv)

    result = scan(args.root, args.read_all_vols)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    inv = result["inventory"]
    libs: dict[str, int] = defaultdict(int)
    vols = 0
    with_genre = 0
    for e in inv.values():
        libs[e["library"]] += 1
        vols += e["vol_count"]
        if e["genre_source"] == "Genre" and e["genre_current"]:
            with_genre += 1
    print(f"series (cbz-containing dirs): {result['series_count']}")
    print(f"total volumes (.cbz):        {vols}")
    print(f"by library: {dict(libs)}")
    print(f"series with existing <Genre>: {with_genre}")
    print(f"anomalies: {len(result['anomalies'])}")
    for a in result["anomalies"][:40]:
        print(f"  - {a}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
