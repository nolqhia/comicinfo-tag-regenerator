#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resolve_book.py — ISBN (or Amazon URL) -> bibliographic record + official synopsis.

Step 1 of the single-book workflow (see AGENTS.md). Resolves, in order:
  1. 楽天ブックス API — richest synopsis, needs RAKUTEN_APP_ID in the environment.
     New light novels are usually here months before OpenBD has them.
  2. OpenBD — free, no key. Good for established/older titles.
  3. NDL Search (国会図書館) — no key; title/author/publisher only, no synopsis.
     Last resort for niche imprints (KAエスマ文庫 etc.) that the other two miss.

Usage:
    python resolve_book.py 9784094533057
    python resolve_book.py "https://www.amazon.co.jp/.../dp/4824204690/..."
    python resolve_book.py 9784094533057 --json      # machine-readable

Rakuten key (optional but recommended) — borrow the local comicinfo-gen setting:
    export RAKUTEN_APP_ID=$(grep -oP 'RAKUTEN_APP_ID\\s*=\\s*"\\K[^"]+' comicinfo-gen/comicinfo_gen.py)
    export RAKUTEN_ACCESS_KEY=$(grep -oP 'RAKUTEN_ACCESS_KEY\\s*=\\s*"\\K[^"]+' comicinfo-gen/comicinfo_gen.py)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse

import requests

UA = "Mozilla/5.0 (compatible; kavita-tagger/1.0)"


def extract_isbn(text: str) -> str | None:
    """Accept a bare ISBN or an Amazon URL (dp/<ASIN> is the ISBN-10 for books)."""
    text = (text or "").strip()
    m = re.search(r"/dp/([0-9X]{10})", text, re.I) or re.search(r"/dp/(97[89][0-9]{10})", text)
    if m:
        return m.group(1).upper()
    digits = re.sub(r"[^0-9X]", "", text.upper())
    return digits if len(digits) in (10, 13) else None


def isbn13(isbn: str) -> str:
    """ISBN-10 -> ISBN-13 (OpenBD/NDL prefer 13)."""
    isbn = re.sub(r"[^0-9X]", "", isbn.upper())
    if len(isbn) != 10:
        return isbn
    base = "978" + isbn[:9]
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(base))
    return base + str((10 - total % 10) % 10)


def from_rakuten(isbn: str, timeout: float = 15) -> dict | None:
    app_id = os.environ.get("RAKUTEN_APP_ID")
    if not app_id:
        return None
    try:
        r = requests.get(
            "https://openapi.rakuten.co.jp/services/api/BooksBook/Search/20170404",
            params={"applicationId": app_id,
                    "accessKey": os.environ.get("RAKUTEN_ACCESS_KEY"), "isbn": isbn},
            headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code != 200 or not r.json().get("count"):
            return None
        it = r.json()["Items"][0]["Item"]
    except Exception:
        return None
    return {"source": "rakuten", "title": it.get("title", ""), "author": it.get("author", ""),
            "label": it.get("seriesName", ""), "publisher": it.get("publisherName", ""),
            "pubdate": it.get("salesDate", ""), "synopsis": (it.get("itemCaption") or "").strip()}


def from_openbd(isbn: str, timeout: float = 15) -> dict | None:
    try:
        r = requests.get(f"https://api.openbd.jp/v1/get?isbn={isbn13(isbn)}",
                         headers={"User-Agent": UA}, timeout=timeout)
        data = r.json() if r.status_code == 200 else None
        if not data or not data[0]:
            return None
        rec = data[0]
    except Exception:
        return None
    s = rec.get("summary") or {}
    syn = ""
    tcs = ((rec.get("onix") or {}).get("CollateralDetail") or {}).get("TextContent") or []
    if isinstance(tcs, dict):
        tcs = [tcs]
    for want in ("03", "02"):          # 03=内容紹介, 02=短い内容紹介
        for tc in tcs:
            if tc.get("TextType") == want and (tc.get("Text") or "").strip():
                syn = re.sub(r"<[^>]+>", "", tc["Text"]).strip()
                break
        if syn:
            break
    return {"source": "openbd", "title": s.get("title", ""), "author": s.get("author", ""),
            "label": s.get("series", ""), "publisher": s.get("publisher", ""),
            "pubdate": s.get("pubdate", ""), "synopsis": syn}


def from_ndl(isbn: str, timeout: float = 40) -> dict | None:
    """国会図書館サーチ. No synopsis, but catches niche imprints the others miss.
    NDL is slow (often 15-30s), hence the generous timeout."""
    try:
        r = requests.get("https://ndlsearch.ndl.go.jp/api/opensearch",
                         params={"isbn": isbn13(isbn)}, headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code != 200:
            return None
        x = r.text
    except Exception as e:  # noqa: BLE001 — surface *why*, a timeout is not a miss
        print(f"[ndl] {type(e).__name__}: {e}", file=sys.stderr)
        return None

    def pick(tag: str) -> str:
        vals = re.findall(rf"<{tag}>([^<]+)</{tag}>", x)
        # the first <title> is the feed title ("... OpenSearch"), so skip it
        vals = [v for v in vals if "OpenSearch" not in v]
        return vals[0].strip() if vals else ""

    title = pick("title")
    if not title:
        return None
    return {"source": "ndl", "title": title, "author": pick("dc:creator"), "label": "",
            "publisher": pick("dc:publisher"), "pubdate": pick("dcterms:issued"), "synopsis": ""}


def resolve(isbn: str) -> dict:
    for fn in (from_rakuten, from_openbd, from_ndl):
        rec = fn(isbn)
        if rec and rec.get("title"):
            rec["isbn"] = isbn
            return rec
    return {"source": None, "isbn": isbn, "title": "", "author": "", "label": "",
            "publisher": "", "pubdate": "", "synopsis": ""}


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("isbn_or_url", help="ISBN-10/13, or an Amazon product URL")
    ap.add_argument("--json", action="store_true", help="print the raw record as JSON")
    args = ap.parse_args(argv)

    isbn = extract_isbn(args.isbn_or_url)
    if not isbn:
        print("could not extract an ISBN from the argument", file=sys.stderr)
        return 1

    rec = resolve(isbn)
    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=1))
        return 0 if rec["title"] else 1

    if not rec["title"]:
        print(f"NOT FOUND in 楽天/OpenBD/NDL: {isbn}")
        print("→ fall back to a web search for the title, then run fetch_reviews.py by title.")
        return 1
    print(f"source   : {rec['source']}")
    print(f"title    : {rec['title']}")
    print(f"author   : {rec['author']}")
    print(f"label    : {rec['label']}  ({rec['publisher']})")
    print(f"pubdate  : {rec['pubdate']}")
    print(f"synopsis : {rec['synopsis'] or '(なし — SearXNG のレビュー取得に頼る)'}")
    if not os.environ.get("RAKUTEN_APP_ID") and rec["source"] != "rakuten":
        print("\n※ RAKUTEN_APP_ID 未設定。新刊ラノベはこれが無いとあらすじを取り逃しやすい。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
