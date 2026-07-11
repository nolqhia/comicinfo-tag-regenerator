#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_pipeline.py — Drive the generation pipeline over a set of series, with resume.

Phases (handoff §4: a unified ledger requires pass-1 over ALL works BEFORE the
ledger is finalized, THEN pass-2 over all works against that one ledger):

  --phase pass1   fetch reviews + generate pass-1 tags -> <out>/pass1/<key>.json
                  (the fetched review TEXT is saved in the file so pass-2 need not
                  re-fetch). Between pass1 and pass2 you run finalize_ledger.py.
  --phase pass2   read each pass-1 file + the confirmed ledger, refine -> <out>/pass2/
  --phase both    pass1+pass2 inline per work (for small samples; ledger = seed as-is)

Resume: output filename is derived from a hash of the inventory key, so a stopped
run is continued simply by re-invoking — existing outputs are skipped. Safe to run,
kill, and re-run (long 300-work sweeps outlive a single session this way).

Usage:
    # real run, unified ledger:
    python run_pipeline.py --root Y:/books --keys-file all_keys.txt \
        --searxng http://SEARXNG:PORT --out-dir out --phase pass1
    python finalize_ledger.py --pass1-dir out/pass1 --seed seed_vocab.json --out seed_vocab.confirmed.json
    python run_pipeline.py --root Y:/books --keys-file all_keys.txt \
        --seed seed_vocab.confirmed.json --out-dir out --phase pass2
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate_tags import generate, read_context_from_cbz          # noqa: E402
from refine_tags import refine, load_ledger                        # noqa: E402
from validate_tags import _load_vocab, validate                    # noqa: E402


def key_name(key: str) -> str:
    """Stable, collision-free filename for an inventory key (order-independent)."""
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:10]
    return f"{h}.json"


def _write(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def do_pass1(root: str, key: str, searxng: str | None, model: str, host: str) -> dict:
    ctx = read_context_from_cbz(root, key)
    n_rev, rev_chars = 0, 0
    if searxng:
        from fetch_reviews import gather
        rv = gather(ctx.get("title", ""), ctx.get("author", ""), searxng)
        ctx["reviews"] = rv["reviews"]
        n_rev, rev_chars = len(rv["sources"]), rv["chars"]
    p1 = generate(ctx, model=model, host=host)
    p1["_reviews_text"] = ctx.get("reviews", [])
    p1["_reviews"] = {"sources": n_rev, "chars": rev_chars}
    return p1


VALIDATE_RETRIES = 3   # regenerate pass-2 up to this many times if tags break rules


def do_pass2(root: str, key: str, p1: dict, canonical, discarded,
             canon_set, pend_set, disc_set, model: str, host: str) -> dict:
    ctx = read_context_from_cbz(root, key)
    ctx["reviews"] = p1.get("_reviews_text", [])   # reuse pass-1's reviews, no re-fetch
    best: dict | None = None
    best_issues: list[str] | None = None
    for attempt in range(VALIDATE_RETRIES):
        # nudge temperature up each retry to escape a repeated rule-breaking tag
        p2 = refine(ctx, p1["tags"], canonical, discarded, model=model, host=host,
                    temperature=0.3 + 0.15 * attempt)
        issues = validate(p2["tags"], canon_set, pend_set, disc_set)
        if not issues:
            best, best_issues = p2, issues
            break
        if best is None or len(issues) < len(best_issues):
            best, best_issues = p2, issues   # keep the least-bad if none is clean
    p2 = best
    p2["_pass1"] = p1["tags"]
    p2["_reviews"] = p1.get("_reviews", {})
    p2["_valid"] = not best_issues
    p2["_issues"] = best_issues
    p2["_attempts"] = attempt + 1
    return p2


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--keys-file", required=True, help="one inventory key per line")
    ap.add_argument("--phase", choices=["pass1", "pass2", "both"], default="both")
    ap.add_argument("--seed", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "seed_vocab.json"))
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--searxng")
    ap.add_argument("--pass1-model", default="qwen3.6:35b")
    ap.add_argument("--pass2-model", default="qwen3.6:27b")
    ap.add_argument("--host", default="http://localhost:11434")
    args = ap.parse_args(argv)

    with open(args.keys_file, "r", encoding="utf-8") as f:
        keys = [ln.strip() for ln in f if ln.strip()]

    p1_dir = os.path.join(args.out_dir, "pass1")
    p2_dir = os.path.join(args.out_dir, "pass2")
    os.makedirs(p1_dir, exist_ok=True)
    os.makedirs(p2_dir, exist_ok=True)

    need_ledger = args.phase in ("pass2", "both")
    if need_ledger:
        # one ledger source for BOTH refine (canonical/discarded) and validate
        # (canon/pend/disc sets) — the --seed file (confirmed ledger), not the raw seed.
        with open(args.seed, "r", encoding="utf-8") as f:
            _sd = json.load(f)
        canonical, discarded = _sd["canonical"], _sd["discarded"]
        canon_set, pend_set, disc_set = set(canonical), set(_sd.get("pending", [])), set(discarded)

    done = skipped = failed = 0
    for i, key in enumerate(keys, 1):
        fn = key_name(key)
        p1_path, p2_path = os.path.join(p1_dir, fn), os.path.join(p2_dir, fn)
        t = time.time()
        try:
            if args.phase == "pass1":
                if os.path.exists(p1_path):
                    skipped += 1; print(f"[{i}/{len(keys)}] SKIP (done) {key[:60]}"); continue
                p1 = do_pass1(args.root, key, args.searxng, args.pass1_model, args.host)
                _write(p1_path, p1)
                print(f"[{i}/{len(keys)}] pass1 OK {time.time()-t:.0f}s rev={p1['_reviews']['sources']}/{p1['_reviews']['chars']}c  {key[:50]}")
                print(f"    {', '.join(p1['tags'])}")

            elif args.phase == "pass2":
                if os.path.exists(p2_path):
                    skipped += 1; print(f"[{i}/{len(keys)}] SKIP (done) {key[:60]}"); continue
                if not os.path.exists(p1_path):
                    failed += 1; print(f"[{i}/{len(keys)}] MISSING pass1 for {key[:55]}"); continue
                with open(p1_path, encoding="utf-8") as f:
                    p1 = json.load(f)
                p2 = do_pass2(args.root, key, p1, canonical, discarded,
                              canon_set, pend_set, disc_set, args.pass2_model, args.host)
                _write(p2_path, p2)
                flag = "OK " if p2["_valid"] else "INVALID"
                print(f"[{i}/{len(keys)}] pass2 {flag} {time.time()-t:.0f}s  {key[:50]}")
                print(f"    p1: {', '.join(p2['_pass1'])}")
                print(f"    p2: {', '.join(p2['tags'])}")
                if p2["_issues"]:
                    print(f"    issues: {p2['_issues']}")

            else:  # both
                if os.path.exists(p2_path):
                    skipped += 1; print(f"[{i}/{len(keys)}] SKIP (done) {key[:60]}"); continue
                p1 = do_pass1(args.root, key, args.searxng, args.pass1_model, args.host)
                p2 = do_pass2(args.root, key, p1, canonical, discarded,
                              canon_set, pend_set, disc_set, args.pass2_model, args.host)
                _write(p2_path, p2)
                flag = "OK " if p2["_valid"] else "INVALID"
                print(f"[{i}/{len(keys)}] both {flag} {time.time()-t:.0f}s rev={p1['_reviews']['sources']}  {key[:45]}")
                print(f"    p1: {', '.join(p1['tags'])}")
                print(f"    p2: {', '.join(p2['tags'])}")
            done += 1
        except Exception as e:  # noqa: BLE001 — keep the sweep going, resume covers it
            failed += 1
            print(f"[{i}/{len(keys)}] FAIL {time.time()-t:.0f}s {key[:55]}\n    {e}")
        sys.stdout.flush()

    # \a = terminal bell so an unattended run is audible when it finishes.
    print(f"\n\a===== {args.phase} FINISHED: done={done} skipped={skipped} "
          f"failed={failed} / {len(keys)} =====")
    if failed:
        print("(re-run the same command to retry the failed ones — resume skips the rest)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
