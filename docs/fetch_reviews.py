#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_reviews.py — Gather review/synopsis text for a work via SearXNG + trafilatura.

Per handoff §4: the search layer is the quality ceiling. For a work we run a few
queries against a self-hosted SearXNG, fetch the top *fetchable* result pages, and
extract main text with trafilatura. If the accumulated text is under a threshold we
try the next query (max 3). Amazon is skipped (robots-blocked, proven).

SearXNG output: JSON is preferred (/search?q=...&format=json) but is disabled by
default — enable it in settings.yml:
    search:
      formats: [html, json]
Until then we fall back to parsing the HTML results page (url_header anchors).

Usage:
    python fetch_reviews.py --searxng http://SEARXNG:PORT \
        --title "少女星間漂流記" --author "東崎惟子" [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse

import requests

try:
    import trafilatura
except ImportError:  # extraction degrades but module still imports
    trafilatura = None

UA = "Mozilla/5.0 (compatible; kavita-tagger/1.0)"
THRESHOLD_CHARS = 1500      # stop early once we have this much review text
MAX_QUERIES = 3
MAX_FETCH_PER_QUERY = 4
MIN_DOC_CHARS = 200         # ignore near-empty extractions
MAX_DOC_CHARS = 4000        # cap one page's contribution (avoid feeding huge blobs)
MIN_SNIPPET_CHARS = 80      # search snippets shorter than this aren't worth it
# Domains we never *fetch* (robots-blocked / JS walls). Amazon page scraping is
# blocked & proven unreliable — but its SearXNG JSON snippet is search-mediated
# and allowed (handoff §4 "スニペット経由のみ"), used only as thin-coverage top-up.
DENY_DOMAINS = ("amazon.", "youtube.", "youtu.be", "x.com", "twitter.",
                "pixiv.", "instagram.", "facebook.", "tiktok.")

_URL_HEADER = re.compile(r'href="(https?://[^"]+)"[^>]*class="url_header"')


def fetch_synopsis(isbn: str, timeout: float = 12) -> str | None:
    """Official あらすじ by ISBN — the *reliable* synopsis source (SearXNG handles
    reviews). 楽天Books is richer but needs a key: set RAKUTEN_APP_ID (and
    RAKUTEN_ACCESS_KEY) in the environment to enable it. OpenBD is free / no key and
    used as the fallback. Returns None if neither has it."""
    isbn = re.sub(r"[^0-9X]", "", (isbn or "").upper())
    if not isbn:
        return None
    # 楽天Books — only if a key is configured in the environment (never hardcoded).
    app_id = os.environ.get("RAKUTEN_APP_ID")
    if app_id:
        try:
            r = requests.get(
                "https://openapi.rakuten.co.jp/services/api/BooksBook/Search/20170404",
                params={"applicationId": app_id,
                        "accessKey": os.environ.get("RAKUTEN_ACCESS_KEY"), "isbn": isbn},
                headers={"User-Agent": UA}, timeout=timeout)
            if r.status_code == 200:
                items = r.json().get("Items", [])
                cap = (items[0]["Item"].get("itemCaption") or "").strip() if items else ""
                if cap:
                    return cap
        except Exception:
            pass
    # OpenBD — free, no key. 内容紹介 is CollateralDetail/TextContent, TextType 03.
    try:
        r = requests.get(f"https://api.openbd.jp/v1/get?isbn={isbn}",
                         headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            if data and data[0]:
                tcs = (((data[0].get("onix") or {}).get("CollateralDetail") or {})
                       .get("TextContent") or [])
                if isinstance(tcs, dict):
                    tcs = [tcs]
                for want in ("03", "02"):  # 03=内容紹介, 02=短い内容紹介
                    for tc in tcs:
                        if tc.get("TextType") == want and (tc.get("Text") or "").strip():
                            return re.sub(r"<[^>]+>", "", tc["Text"]).strip()
    except Exception:
        pass
    return None


def _queries(title: str, author: str) -> list[str]:
    a = f" {author}" if author else ""
    return [
        f"{title}{a} あらすじ",
        f"{title} 感想 レビュー",
        f"{title} ネタバレ 考察",
    ]


def _domain(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def _denied(url: str) -> bool:
    d = _domain(url)
    return any(bad in d for bad in DENY_DOMAINS)


def searxng_results(searxng: str, query: str, timeout: float = 12) -> list[dict]:
    """Return [{url, content}] for a query. JSON (with snippets) if enabled,
    else HTML fallback (urls only, no snippet)."""
    base = searxng.rstrip("/") + "/search"
    params = {"q": query, "format": "json", "language": "ja"}
    try:
        r = requests.get(base, params=params, headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code == 200 and "application/json" in r.headers.get("content-type", ""):
            data = r.json()
            return [{"url": x["url"], "content": (x.get("content") or "").strip()}
                    for x in data.get("results", []) if x.get("url")]
    except Exception:
        pass
    # HTML fallback: no snippets available
    try:
        r = requests.get(base, params={"q": query, "language": "ja"},
                         headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code == 200:
            return [{"url": u, "content": ""} for u in _URL_HEADER.findall(r.text)]
    except Exception:
        pass
    return []


def extract(url: str, timeout: float = 12) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code != 200 or not r.text:
            return None
    except Exception:
        return None
    if trafilatura is not None:
        txt = trafilatura.extract(r.text, include_comments=True, favor_recall=True,
                                  target_language="ja")
        if txt and len(txt) >= MIN_DOC_CHARS:
            return txt.strip()[:MAX_DOC_CHARS]
    return None


def gather(title: str, author: str, searxng: str, *,
           threshold: int = THRESHOLD_CHARS, max_queries: int = MAX_QUERIES,
           isbn: str | None = None, verbose: bool = False) -> dict:
    reviews: list[str] = []
    sources: list[str] = []
    used_domains: set[str] = set()
    used_queries: list[str] = []
    snippet_pool: list[tuple[str, str]] = []   # (url, snippet) — top-up fallback
    total = 0

    # Reliable official synopsis first (楽天/OpenBD by ISBN), then SearXNG reviews.
    if isbn:
        syn = fetch_synopsis(isbn)
        if syn:
            reviews.append(f"[あらすじ] {syn}")
            sources.append(f"synopsis:ISBN {isbn}")
            total += len(syn)
            if verbose:
                print(f"  [syn] ISBN {isbn} ({len(syn)}c)", file=sys.stderr)

    for q in _queries(title, author)[:max_queries]:
        used_queries.append(q)
        fetched = 0
        for res in searxng_results(searxng, q):
            url, snippet = res["url"], res["content"]
            dom = _domain(url)
            # Harvest the search snippet regardless (used only if we fall short).
            if snippet and len(snippet) >= MIN_SNIPPET_CHARS and dom not in used_domains:
                snippet_pool.append((url, snippet))
            if fetched >= MAX_FETCH_PER_QUERY or _denied(url) or dom in used_domains:
                continue
            txt = extract(url)
            if verbose:
                print(f"  [{'ok ' if txt else 'skip'}] {url[:70]}", file=sys.stderr)
            if not txt:
                continue
            used_domains.add(dom)
            reviews.append(txt)
            sources.append(url)
            total += len(txt)
            fetched += 1
            if total >= threshold:   # stop as soon as we have enough — don't pile on
                break
        if total >= threshold:
            break

    # Thin coverage: top up with search snippets (incl. Amazon — search-mediated,
    # not scraped). Deduped by domain, clearly labelled so the model weights them low.
    if total < threshold:
        for url, snippet in snippet_pool:
            dom = _domain(url)
            if dom in used_domains:
                continue
            used_domains.add(dom)
            reviews.append(f"[検索スニペット] {snippet}")
            sources.append(url + " (snippet)")
            total += len(snippet)
            if verbose:
                print(f"  [snip] {url[:70]}", file=sys.stderr)
            if total >= threshold:
                break

    return {"title": title, "author": author, "reviews": reviews,
            "sources": sources, "chars": total, "queries_used": used_queries}


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--searxng", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--author", default="")
    ap.add_argument("--isbn", help="ISBN → official あらすじ via 楽天(env key)/OpenBD(free)")
    ap.add_argument("--threshold", type=int, default=THRESHOLD_CHARS)
    ap.add_argument("--json", help="write full result JSON here")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    res = gather(args.title, args.author, args.searxng,
                 threshold=args.threshold, isbn=args.isbn, verbose=args.verbose)
    print(f"sources: {len(res['sources'])}  chars: {res['chars']}  "
          f"queries: {res['queries_used']}")
    for s in res["sources"]:
        print(f"  - {s}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
