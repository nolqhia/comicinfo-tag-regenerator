#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_ledger.py — apply a finished 10-tag set to the living vocabulary ledger.

Step 4 of the single-book workflow (see AGENTS.md). The ledger has three areas:
    canonical — proven by >= 2 works. Reuse these first; they carry the connections.
    pending   — seen once. Promoted to canonical the moment a 2nd work uses it.
    discarded — 作中固有名詞 / 書誌メタ. Never emit these.

Promotion rules (mirrors what the full regeneration did):
  * already canonical            -> no-op
  * in pending                   -> promote (this work is the 2nd sighting)
  * new, but >=1 work in mapping.json already carries it
                                 -> promote (the ledger lagged; mapping.json is
                                    ground truth for what the library actually has)
  * new, unused elsewhere        -> add to pending
  * in discarded                 -> refuse (the tag should not have been chosen)

Applying the same book twice would wrongly promote its own new tags (the pending
entry this book just created would read as a 2nd sighting), so pass --isbn to
record the book; a repeat run for that ISBN is refused.

Usage:
    python update_ledger.py --check "SF,宇宙,バディ"                  # preview only
    python update_ledger.py --tags  "SF,宇宙,バディ" --isbn 978...    # apply
    python update_ledger.py --demote 溺愛                # canonical -> pending
                                                          # (when a promotion's 2nd
                                                          #  witness is later removed)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "seed_vocab.confirmed.json")
MAPPING = os.path.join(os.path.dirname(HERE), "out", "mapping.json")


def load_mapping_counts() -> dict[str, int]:
    """How many already-written works carry each tag (ground truth)."""
    if not os.path.exists(MAPPING):
        return {}
    with open(MAPPING, "r", encoding="utf-8") as f:
        mapping = json.load(f)
    counts: dict[str, int] = {}
    for tags in mapping.values():
        for t in tags:
            counts[t] = counts.get(t, 0) + 1
    return counts


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tags", help="comma-separated final tags, applied to the ledger")
    g.add_argument("--check", help="comma-separated tags, report status only")
    g.add_argument("--demote", help="move this canonical tag back to pending")
    ap.add_argument("--isbn", help="book being applied; guards against a double run")
    args = ap.parse_args(argv)

    with open(LEDGER, "r", encoding="utf-8") as f:
        led = json.load(f)
    canon, pend, disc = set(led["canonical"]), set(led["pending"]), set(led["discarded"])
    before = (len(canon), len(pend))
    applied = led.setdefault("_meta", {}).setdefault("applied_isbn", [])

    if args.tags and args.isbn and args.isbn in applied:
        print(f"{args.isbn} は適用済み。二重適用すると自分が作った pending を"
              f"2冊目と誤認して昇格させるため中止。")
        return 1

    if args.demote:
        t = args.demote.strip()
        if t not in canon:
            print(f"{t!r} is not canonical — nothing to do")
            return 1
        canon.discard(t)
        pend.add(t)
        print(f"demoted: {t} canonical -> pending")
    else:
        raw = args.check or args.tags
        tags = [t.strip() for t in raw.split(",") if t.strip()]
        counts = load_mapping_counts()
        promote, add, noop, refuse = [], [], [], []
        for t in tags:
            if t in disc:
                refuse.append(t)
            elif t in canon:
                noop.append(t)
            elif t in pend:
                promote.append(t)
            elif counts.get(t, 0) >= 1:
                promote.append(f"{t} (mapping実績 {counts[t]}作)")
            else:
                add.append(t)

        for t in refuse:
            print(f"  REFUSE   {t} — discarded（固有名詞/メタ）。別の語に差し替えること")
        for t in promote:
            print(f"  promote  {t} -> canonical")
        for t in add:
            print(f"  add      {t} -> pending")
        if noop:
            print(f"  (already canonical: {', '.join(noop)})")
        if refuse:
            print("\n中止: discarded 語が含まれている。タグを直してから再実行。")
            return 1
        if args.check:
            print(f"\n[check only] canonical {before[0]} / pending {before[1]} — 変更なし")
            return 0
        for t in tags:
            if t in canon or t in disc:
                continue
            if t in pend or counts.get(t, 0) >= 1:
                pend.discard(t)
                canon.add(t)
            else:
                pend.add(t)
        if args.isbn:
            applied.append(args.isbn)

    led["canonical"], led["pending"], led["discarded"] = sorted(canon), sorted(pend), sorted(disc)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(led, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LEDGER)
    print(f"\n台帳: canonical {before[0]} -> {len(canon)} / pending {before[1]} -> {len(pend)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
