#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finalize_ledger.py — Aggregate pass-1 tags into the confirmed vocab ledger.

Per handoff §4 + ERRATA §C-1: after pass 1 over the whole corpus, count tag
frequency and merge into the seed ledger — do NOT rebuild canonical/pending from
zero. Rules:
  * seed canonical is kept (never demoted).
  * a tag observed in >= 2 works (pass-1 frequency) becomes canonical
    (this promotes seed-pending terms and brand-new terms alike).
  * seed pending is preserved; a pass-1 tag seen exactly once is pending.
  * discarded stays discarded; any tag that normalizes into discarded is dropped.

Before counting, each raw pass-1 tag is run through the mechanical normalizer
(build_tag_ledger.normalize_one: ALIAS / DECOMPOSE / META_REMOVE) so obvious drift
(SF/Sf, ミステリー/ミステリ, 従姉→いとこ, TS→性転換) collapses instead of splitting
frequencies. Pass-2 then does the model-side refine with this ledger.

Usage:
    python finalize_ledger.py --pass1-dir out/pass1 --seed seed_vocab.json \
        --out seed_vocab.next.json [--report ledger_diff.txt]
Each file in --pass1-dir is a JSON object with a "tags" list (generate_tags output).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_tag_ledger import normalize_one  # noqa: E402


def load_pass1(pass1_dir: str) -> list[tuple[str, list[str]]]:
    works = []
    for p in sorted(glob.glob(os.path.join(pass1_dir, "*.json"))):
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
        tags = obj.get("tags", obj) if isinstance(obj, dict) else obj
        works.append((os.path.basename(p), list(tags)))
    return works


def normalize_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for t in tags:
        out.extend(normalize_one(t))  # [] (removed), [x], or [x, y] (decomposed)
    # de-dup within a work, preserve order
    seen, uniq = set(), []
    for t in out:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def finalize(seed: dict, works: list[tuple[str, list[str]]]) -> tuple[dict, dict]:
    seed_canon = set(seed.get("canonical", []))
    seed_pending = set(seed.get("pending", []))
    discarded = set(seed.get("discarded", []))

    freq: Counter = Counter()
    for _name, tags in works:
        for t in set(normalize_tags(tags)):
            if t in discarded:
                continue
            freq[t] += 1

    freq2 = {t for t, c in freq.items() if c >= 2}
    freq1 = {t for t, c in freq.items() if c == 1}

    canonical = seed_canon | freq2                      # never demote seed canonical
    pending = (seed_pending | freq1) - canonical - discarded

    new_ledger = dict(seed)  # keep _meta and any extra keys
    new_ledger["canonical"] = sorted(canonical)
    new_ledger["pending"] = sorted(pending)
    new_ledger["discarded"] = sorted(discarded)

    report = {
        "works_counted": len(works),
        "distinct_normalized_tags": len(freq),
        "promoted_to_canonical": sorted(freq2 - seed_canon),   # seed-pending + brand-new that hit >=2
        "new_pending": sorted(pending - seed_pending),         # brand-new single-sighting terms
        "canonical_count": (len(seed_canon), len(canonical)),
        "pending_count": (len(seed_pending), len(pending)),
        "top20": freq.most_common(20),
    }
    return new_ledger, report


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pass1-dir", required=True)
    ap.add_argument("--seed", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report")
    args = ap.parse_args(argv)

    with open(args.seed, "r", encoding="utf-8") as f:
        seed = json.load(f)
    works = load_pass1(args.pass1_dir)
    if not works:
        print(f"no pass-1 JSON files in {args.pass1_dir}")
        return 1

    new_ledger, report = finalize(seed, works)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(new_ledger, f, ensure_ascii=False, indent=2)

    lines = [
        f"works counted:            {report['works_counted']}",
        f"distinct normalized tags: {report['distinct_normalized_tags']}",
        f"canonical: {report['canonical_count'][0]} -> {report['canonical_count'][1]}",
        f"pending:   {report['pending_count'][0]} -> {report['pending_count'][1]}",
        f"promoted to canonical ({len(report['promoted_to_canonical'])}): {report['promoted_to_canonical'][:40]}",
        f"top20 freq: {report['top20']}",
    ]
    text = "\n".join(lines)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
