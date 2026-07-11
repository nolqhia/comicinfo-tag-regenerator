#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cbz_tag_writer.py — Safely rewrite the <Genre> field inside a .cbz's ComicInfo.xml.

Design (per phase-2 safety policy, revised for ComicInfo direct-write):
  * NEVER edit the source .cbz in place. Build a brand-new zip that copies every
    entry losslessly (byte-identical, same compress_type), replacing ONLY the
    text of the single <Genre> element in ComicInfo.xml.
  * Verify the new zip before it is allowed to replace the original:
      - zip integrity (testzip / CRC of every entry)
      - entry set identical to the source
      - every non-ComicInfo entry byte-lossless (CRC + size match source)
      - ComicInfo parses and its Genre text == the exact string we intended
  * Only after verification passes: atomic os.replace() into place
    (temp file lives in the same directory -> same filesystem -> atomic).

The <Genre> value is written as a single comma-separated element (no spaces),
matching the existing corpus convention:  <Genre>SF,宇宙,バディ</Genre>

Usage:
    # single file, explicit genres
    python cbz_tag_writer.py --cbz "少女星間漂流記.cbz" --genres "SF,宇宙,バディ" [--dry-run] [--backup]

    # batch: JSON mapping { "<cbz path>": ["genre1","genre2", ...], ... }
    python cbz_tag_writer.py --batch mapping.json [--dry-run] [--backup]

Exit code 0 = all requested files succeeded (or dry-run clean); non-zero = at least one failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass
from typing import Iterable
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape

COMICINFO_NAME = "ComicInfo.xml"
# Match a single <Genre>...</Genre> element (DOTALL: values never contain '<', but be safe).
_GENRE_RE = re.compile(r"<Genre\b[^>]*>.*?</Genre>", re.DOTALL)
# Insertion anchors (first that matches wins), used only when no <Genre> exists yet.
_INSERT_AFTER = [re.compile(r"</Title>"), re.compile(r"</Series>")]


class WriteError(Exception):
    pass


def _normalize_genre_list(genres: Iterable[str]) -> list[str]:
    """Trim, drop empties, de-dup (preserve order)."""
    seen: set[str] = set()
    out: list[str] = []
    for g in genres:
        g = (g or "").strip()
        if not g or g in seen:
            continue
        seen.add(g)
        out.append(g)
    return out


def build_genre_string(genres: Iterable[str]) -> str:
    """The exact text that goes inside <Genre>...</Genre> — comma-joined, no spaces."""
    return ",".join(_normalize_genre_list(genres))


def _splice_genre(xml_bytes: bytes, genre_str: str) -> bytes:
    """Return ComicInfo bytes with ONLY the <Genre> element's text replaced.

    Everything else (XML declaration, <Pages> table, whitespace, element order)
    is preserved byte-for-byte. If no <Genre> element exists, one is inserted
    after </Title> (or </Series>), or as the last child of <ComicInfo>.
    """
    text = xml_bytes.decode("utf-8")
    new_el = f"<Genre>{_xml_escape(genre_str)}</Genre>"

    matches = _GENRE_RE.findall(text)
    if len(matches) > 1:
        raise WriteError(f"multiple <Genre> elements found ({len(matches)}); refusing to guess")
    if len(matches) == 1:
        return _GENRE_RE.sub(lambda _m: new_el, text, count=1).encode("utf-8")

    # No <Genre> — insert one, matching sibling indentation if detectable.
    indent = "  "
    m_ind = re.search(r"\n([ \t]+)<Title>", text) or re.search(r"\n([ \t]+)<Series>", text)
    if m_ind:
        indent = m_ind.group(1)
    for anchor in _INSERT_AFTER:
        m = anchor.search(text)
        if m:
            ins = text[: m.end()] + "\n" + indent + new_el + text[m.end():]
            return ins.encode("utf-8")
    # Fallback: before </ComicInfo>
    m = re.search(r"</ComicInfo>", text)
    if not m:
        raise WriteError("not a ComicInfo.xml (no </ComicInfo>)")
    ins = text[: m.start()] + indent + new_el + "\n" + text[m.start():]
    return ins.encode("utf-8")


def _find_comicinfo(zin: zipfile.ZipFile) -> str:
    """Return the archive name of the ComicInfo.xml (case-insensitive, root preferred)."""
    hits = [n for n in zin.namelist() if os.path.basename(n).lower() == COMICINFO_NAME.lower()]
    if not hits:
        raise WriteError("no ComicInfo.xml in archive")
    # Prefer a root-level one (Kavita reads root).
    root_hits = [n for n in hits if "/" not in n and "\\" not in n]
    if len(root_hits) == 1:
        return root_hits[0]
    if len(hits) == 1:
        return hits[0]
    raise WriteError(f"ambiguous ComicInfo.xml location: {hits}")


@dataclass
class Result:
    path: str
    ok: bool
    old_genre: str | None
    new_genre: str
    changed: bool
    message: str


def rewrite_cbz(cbz_path: str, genres: Iterable[str], *, dry_run: bool, backup: bool) -> Result:
    """Rewrite a .cbz's <Genre>. Some archives store Japanese entry names in cp932
    without the UTF-8 flag; the default reader rejects them (central-dir vs local-
    header name mismatch → BadZipFile). Try the normal reader, then fall back to
    decoding entry names as cp932."""
    try:
        return _rewrite_cbz_enc(cbz_path, genres, dry_run=dry_run, backup=backup,
                                metadata_encoding=None)
    except zipfile.BadZipFile:
        try:
            r = _rewrite_cbz_enc(cbz_path, genres, dry_run=dry_run, backup=backup,
                                 metadata_encoding="cp932")
            r.message += " [cp932]"
            return r
        except Exception as e:  # noqa: BLE001
            return Result(cbz_path, False, None, build_genre_string(genres), False,
                          f"ERROR (cp932 fallback failed): {e}")


def _rewrite_cbz_enc(cbz_path: str, genres: Iterable[str], *, dry_run: bool, backup: bool,
                     metadata_encoding: str | None) -> Result:
    genre_str = build_genre_string(genres)
    if not genre_str:
        return Result(cbz_path, False, None, genre_str, False, "empty genre list — refusing to write")

    tmp_path = f"{cbz_path}.tmp-{os.getpid()}"
    old_genre: str | None = None
    try:
        with zipfile.ZipFile(cbz_path, "r", metadata_encoding=metadata_encoding) as zin:
            ci_name = _find_comicinfo(zin)
            src_infos = zin.infolist()
            # Snapshot source CRCs/sizes for the lossless verification later.
            src_meta = {i.filename: (i.CRC, i.file_size) for i in src_infos}

            old_ci_bytes = zin.read(ci_name)
            m_old = _GENRE_RE.search(old_ci_bytes.decode("utf-8"))
            if m_old:
                inner = re.sub(r"</?Genre\b[^>]*>", "", m_old.group(0))
                old_genre = inner
            new_ci_bytes = _splice_genre(old_ci_bytes, genre_str)
            changed = new_ci_bytes != old_ci_bytes

            if dry_run:
                return Result(cbz_path, True, old_genre, genre_str, changed,
                              "dry-run: would " + ("update" if changed else "leave unchanged"))

            # Build the replacement archive, copying every entry losslessly.
            with zipfile.ZipFile(tmp_path, "w", allowZip64=True) as zout:
                for info in src_infos:
                    if info.filename == ci_name:
                        # Keep original ZipInfo metadata (date_time, compress_type,
                        # external_attr, ...), only the payload changes.
                        zout.writestr(info, new_ci_bytes)
                    else:
                        data = zin.read(info.filename)  # STORED -> raw bytes, no recompress
                        zout.writestr(info, data)

        # ---- verification pass on the freshly written temp archive ----
        _verify(tmp_path, ci_name, src_meta, genre_str)

        if backup:
            bak = f"{cbz_path}.bak"
            if not os.path.exists(bak):
                # Reflink/rename-safe backup: copy original bytes aside.
                import shutil
                shutil.copy2(cbz_path, bak)

        os.replace(tmp_path, cbz_path)  # atomic on same filesystem
        return Result(cbz_path, True, old_genre, genre_str, changed,
                      "updated" if changed else "rewritten (genre already equal)")
    except zipfile.BadZipFile:
        # cp932-named entries etc. — let rewrite_cbz retry with metadata_encoding.
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    except Exception as e:  # noqa: BLE001 — report per-file, keep batch going
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return Result(cbz_path, False, old_genre, genre_str, False, f"ERROR: {e}")


def _verify(new_path: str, ci_name: str, src_meta: dict, expected_genre: str) -> None:
    with zipfile.ZipFile(new_path, "r") as z:
        bad = z.testzip()
        if bad is not None:
            raise WriteError(f"integrity check failed on entry: {bad}")
        new_names = set(z.namelist())
        old_names = set(src_meta.keys())
        if new_names != old_names:
            missing = old_names - new_names
            extra = new_names - old_names
            raise WriteError(f"entry set changed (missing={missing}, extra={extra})")
        # Lossless check for every non-ComicInfo entry.
        for info in z.infolist():
            if info.filename == ci_name:
                continue
            old_crc, old_size = src_meta[info.filename]
            if info.CRC != old_crc or info.file_size != old_size:
                raise WriteError(
                    f"lossless check FAILED for {info.filename}: "
                    f"crc {old_crc}->{info.CRC}, size {old_size}->{info.file_size}"
                )
        # ComicInfo must parse and carry exactly the genre we intended.
        ci_bytes = z.read(ci_name)
        root = ET.fromstring(ci_bytes)
        el = root.find("Genre")
        got = (el.text or "") if el is not None else None
        if got != expected_genre:
            raise WriteError(f"Genre verify mismatch: expected {expected_genre!r}, got {got!r}")


def _print_result(r: Result) -> None:
    status = "OK " if r.ok else "FAIL"
    arrow = ""
    if r.old_genre is not None and r.old_genre != r.new_genre:
        arrow = f"\n       old: {r.old_genre}\n       new: {r.new_genre}"
    print(f"[{status}] {r.path} — {r.message}{arrow}")


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 stdout so Japanese genres / em-dashes print on Windows (cp932) consoles.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cbz", help="path to a single .cbz")
    ap.add_argument("--genres", help="comma-separated genre string for --cbz mode")
    ap.add_argument("--batch", help="JSON file: { '<cbz path>': [genres...], ... }")
    ap.add_argument("--dry-run", action="store_true", help="report intended change, write nothing")
    ap.add_argument("--backup", action="store_true", help="keep <cbz>.bak of the original before swap")
    args = ap.parse_args(argv)

    jobs: list[tuple[str, list[str]]] = []
    if args.batch:
        with open(args.batch, "r", encoding="utf-8") as f:
            mapping = json.load(f)
        for path, genres in mapping.items():
            if isinstance(genres, str):
                genres = genres.split(",")
            jobs.append((path, list(genres)))
    elif args.cbz:
        if args.genres is None:
            ap.error("--cbz requires --genres")
        jobs.append((args.cbz, args.genres.split(",")))
    else:
        ap.error("provide --cbz/--genres or --batch")

    failures = 0
    for path, genres in jobs:
        if not os.path.isfile(path):
            print(f"[FAIL] {path} — file not found")
            failures += 1
            continue
        r = rewrite_cbz(path, genres, dry_run=args.dry_run, backup=args.backup)
        _print_result(r)
        if not r.ok:
            failures += 1

    print(f"\n{len(jobs) - failures}/{len(jobs)} succeeded"
          + (" (dry-run)" if args.dry_run else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
