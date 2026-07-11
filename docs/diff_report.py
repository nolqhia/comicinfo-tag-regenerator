#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diff_report.py — old→new Genre diff with auto-classification (the approval gate).

Per handoff §4: after pass 2, diff each series' old <Genre> against the new tags.
Differences explainable by the cleanup rules (discarded/meta removal, alias/decompose
normalization) are auto-classed 想定内 (expected); anything else is 要レビュー and put
in front of the human. Approving the report is the ★gate★ before write-back.

On success it also emits mapping.json ({inventory-key: [new tags]}) — exactly what
cbz_batch_runner.py consumes — so an approved diff flows straight into write-back.

Inputs:
  * inventory.json (from cbz_library_scan.py): old genres per series key.
  * --pass2-dir: directory of pass-2 result JSONs; each carries "_key" (inventory
    key) and "tags" (new 10). Files without _key are matched by filename stem.

Usage:
    python diff_report.py --inventory inventory.json --pass2-dir out/pass2 \
        --seed seed_vocab.json --out-report diff_report.txt --out-mapping mapping.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_tag_ledger import normalize_one, META_REMOVE  # noqa: E402
from validate_tags import META_BLACKLIST  # noqa: E402


def load_pass2(pass2_dir: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in sorted(glob.glob(os.path.join(pass2_dir, "*.json"))):
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
        key = obj.get("_key")
        if not key:
            continue
        out[key] = list(obj.get("tags", []))
    return out


def classify(old: list[str], new: list[str], discarded: set[str]) -> dict:
    """Split old→new differences into explained vs unexplained."""
    old_set, new_set = list(dict.fromkeys(old)), list(dict.fromkeys(new))
    new_lookup = set(new_set)
    kept = [t for t in old_set if t in new_lookup]

    explained_removals: list[str] = []   # (reason) old tag whose loss is expected
    unexplained_removals: list[str] = []
    for t in old_set:
        if t in new_lookup:
            continue
        if t in discarded:
            explained_removals.append(f"{t} (discarded 固有名詞/メタ)")
        elif t in META_REMOVE or t in META_BLACKLIST:
            explained_removals.append(f"{t} (書誌メタ)")
        else:
            norm = [x for x in normalize_one(t) if x != t]
            if norm and all(x in new_lookup for x in norm):
                explained_removals.append(f"{t} → {'+'.join(norm)} (正規化)")
            else:
                unexplained_removals.append(t)

    # additions that are just the normalized form of a removed old tag are expected
    removed = [t for t in old_set if t not in new_lookup]
    norm_targets: set[str] = set()
    for t in removed:
        norm_targets.update(x for x in normalize_one(t) if x != t)
    additions = [t for t in new_set if t not in old_set]
    expected_adds = [t for t in additions if t in norm_targets]
    fresh_adds = [t for t in additions if t not in norm_targets]

    # Word-order drift: a lost old tag whose characters are a permutation of a fresh
    # add is the same concept reordered (短編連作 ↔ 連作短編) — treat as expected.
    def _key(s: str) -> str:
        return "".join(sorted(s))
    add_perm = {_key(a): a for a in fresh_adds}
    still_unexplained = []
    for t in unexplained_removals:
        match = add_perm.get(_key(t))
        if match and len(t) >= 3:
            explained_removals.append(f"{t} → {match} (語順ゆれ)")
            if match in fresh_adds:
                fresh_adds.remove(match)
        else:
            still_unexplained.append(t)
    unexplained_removals = still_unexplained

    verdict = "想定内" if not unexplained_removals else "要レビュー"
    return {
        "kept": kept,
        "explained_removals": explained_removals,
        "unexplained_removals": unexplained_removals,
        "expected_adds": expected_adds,
        "fresh_adds": fresh_adds,
        "verdict": verdict,
        "divergence": len(unexplained_removals),
    }


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--pass2-dir", required=True)
    ap.add_argument("--seed", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "seed_vocab.json"))
    ap.add_argument("--out-report", required=True)
    ap.add_argument("--out-mapping", required=True)
    args = ap.parse_args(argv)

    with open(args.inventory, "r", encoding="utf-8") as f:
        inv = json.load(f)["inventory"]
    with open(args.seed, "r", encoding="utf-8") as f:
        discarded = set(json.load(f).get("discarded", []))
    new_by_key = load_pass2(args.pass2_dir)

    rows = []
    mapping: dict[str, list[str]] = {}
    for key, new in new_by_key.items():
        if key not in inv:
            rows.append((key, None, "KEY-NOT-IN-INVENTORY", 999))
            continue
        old = [t.strip() for t in (inv[key].get("genre_current") or "").split(",") if t.strip()]
        c = classify(old, new, discarded)
        rows.append((key, {"old": old, "new": new, **c}, c["verdict"], c["divergence"]))
        mapping[key] = new

    # write mapping (all generated series; approval is manual via the report)
    with open(args.out_mapping, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=1)

    # report: 要レビュー first, most divergent first
    order = {"要レビュー": 0, "KEY-NOT-IN-INVENTORY": 0, "想定内": 1}
    rows.sort(key=lambda r: (order.get(r[2], 0), -r[3]))

    n_total = len(new_by_key)
    n_review = sum(1 for r in rows if r[2] != "想定内")
    lines = [
        f"# Genre 再生成 diff レポート",
        f"対象シリーズ: {n_total}  |  想定内: {n_total - n_review}  |  要レビュー: {n_review}",
        f"（要レビュー = 正規化・メタ/固有名詞除去で説明できない旧タグの喪失があるもの）",
        "",
    ]
    for key, d, verdict, _div in rows:
        if d is None:
            lines.append(f"[{verdict}] {key}")
            continue
        lines.append(f"[{verdict}] {key}")
        lines.append(f"    旧: {', '.join(d['old'])}")
        lines.append(f"    新: {', '.join(d['new'])}")
        if d["unexplained_removals"]:
            lines.append(f"    ⚠ 説明不能な喪失: {', '.join(d['unexplained_removals'])}")
        if d["explained_removals"]:
            lines.append(f"    ✓ 想定内の除去: {'; '.join(d['explained_removals'])}")
        if d["fresh_adds"]:
            lines.append(f"    + 新規追加: {', '.join(d['fresh_adds'])}")
        lines.append("")

    text = "\n".join(lines)
    with open(args.out_report, "w", encoding="utf-8") as f:
        f.write(text + "\n")

    print(f"series: {n_total}  想定内: {n_total - n_review}  要レビュー: {n_review}")
    print(f"wrote {args.out_report} and {args.out_mapping}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
