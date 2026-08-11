#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_tags.py — Mechanical tag validator (handoff §8 / v4 self-check).

Checks a 10-tag list against the hard rules that a machine can judge, returning
a list of violation strings (empty == pass). The generation loop retries a work
whose tags fail. Judgment calls that need a model (structural/rare balance,
2冊目テスト for novel rare tags) are intentionally NOT enforced here.

Rules enforced (hard — cause failure/retry):
  * exactly 10 tags, none empty/whitespace
  * no duplicates
  * length <= 6 codepoints, UNLESS the tag is a ledger term (canonical/pending).
    Established genre/critical terms (ダークファンタジー, サイバーパンク, 信頼できない語り手
    …) legitimately exceed 6 chars; the length cap applies only to *novel* tags.
    (ERRATA §A-1)
  * no ASCII/English tag unless it's an accepted canonical token (SF, SNS, ...)
  * no internal spaces
  * not in the discarded blacklist (作中固有名詞・書誌メタ from seed_vocab)
  * not in the bibliographic-meta blacklist (レーベル分類・刊行状態 from phase2 §2)

Two-stage per ERRATA §B-4: exact-match blacklists (above) are stage 1; stage 2 is
best-effort *soft* pattern heuristics (award/event/publication-meta suffixes) that
只 flag for review, NOT fail — because mechanical detection of 作中固有名詞 is
unreliable and would false-positive legit tags (芥川賞, きさらぎ駅). A discarded miss
is NOT proof of safety; the real final defense is pass-2 refine + the human diff gate.

Usage:
    echo '{"tags":[...]}' | python validate_tags.py --stdin
    python validate_tags.py --file result.json
    # exit 0 = valid, 1 = violations (printed)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

_ASCII = re.compile(r"^[\x00-\x7f]+$")

# Bibliographic meta that is never a story-element tag (phase2 §2 + observed).
META_BLACKLIST = {
    "ライトノベル", "ラノベ", "青年漫画", "少年漫画", "少女漫画", "コミックス",
    "アンソロジー", "選集", "受賞作", "連載中", "完結", "解説付き",
    "名作再評価", "古典推理", "書き下ろし", "文庫", "新装版", "既刊",
    "女性向け", "男性向け", "成人向け",   # 客層メタ（レーベル同様、作品要素でない）
    "ノンフィクション",   # 棚カテゴリ（Non-fictionライブラリで分かる）。内容の性質は「実話」を使う
}


def _load_vocab() -> tuple[set[str], set[str], set[str]]:
    # Prefer the confirmed (living) ledger; fall back to the original seed.
    base = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base, "seed_vocab.confirmed.json")
    if not os.path.exists(p):
        p = os.path.join(base, "seed_vocab.json")
    with open(p, "r", encoding="utf-8") as f:
        d = json.load(f)
    canonical = set(d.get("canonical", []))
    pending = set(d.get("pending", []))
    discarded = set(d.get("discarded", []))
    return canonical, pending, discarded


# Stage-2 soft heuristics (ERRATA §B-4): suffixes that often mark award/event/
# publication meta. NOT hard failures — 芥川賞 etc. can be legit rare tags.
_META_SUFFIX = ("賞", "祭", "展", "杯", "編集部")
_META_SUBSTR = ("新刊", "既刊", "発売", "重版", "電子版", "限定版", "特装版")


def validate(tags: list[str], canonical: set[str], pending: set[str],
             discarded: set[str]) -> list[str]:
    ledger = canonical | pending
    issues: list[str] = []

    if len(tags) != 10:
        issues.append(f"count: {len(tags)} tags (must be exactly 10)")

    seen: set[str] = set()
    for i, raw in enumerate(tags):
        tag = (raw or "").strip()
        loc = f"[{i}] {raw!r}"
        if not tag:
            issues.append(f"{loc}: empty/whitespace tag")
            continue
        if tag in seen:
            issues.append(f"{loc}: duplicate")
        seen.add(tag)
        if " " in tag or "　" in tag:
            issues.append(f"{loc}: contains a space")
        # ERRATA §A-1: length cap applies only to novel (non-ledger) tags.
        if len(tag) > 6 and tag not in ledger:
            issues.append(f"{loc}: too long ({len(tag)} chars, max 6 for novel tags)")
        if _ASCII.match(tag) and tag not in canonical:
            issues.append(f"{loc}: ASCII/English tag not in canonical vocab")
        if tag in discarded:
            issues.append(f"{loc}: in discarded blacklist (固有名詞/メタ)")
        if tag in META_BLACKLIST:
            issues.append(f"{loc}: bibliographic meta, not a story element")

    return issues


def heuristic_flags(tags: list[str], canonical: set[str], pending: set[str]) -> list[str]:
    """Stage-2 soft advisories for human/pass-2 review (do NOT fail the tag)."""
    ledger = canonical | pending
    flags: list[str] = []
    for i, raw in enumerate(tags):
        tag = (raw or "").strip()
        if not tag or tag in ledger:
            continue
        if tag.endswith(_META_SUFFIX):
            flags.append(f"[{i}] {tag!r}: award/event-like suffix — verify not 書誌メタ")
        if any(s in tag for s in _META_SUBSTR):
            flags.append(f"[{i}] {tag!r}: publication-status substring — likely 書誌メタ")
    return flags


def warnings(tags: list[str], ledger: set[str]) -> list[str]:
    """Soft flags: novel tags longer than the 2-4 char target (ledger terms exempt)."""
    return [f"[{i}] {t!r}: {len(t.strip())} chars (>4 target)"
            for i, t in enumerate(tags)
            if len(t.strip()) in (5, 6) and t.strip() not in ledger]


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--file")
    args = ap.parse_args(argv)

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            obj = json.load(f)
    elif args.stdin:
        obj = json.load(sys.stdin)
    else:
        ap.error("provide --stdin or --file")

    tags = obj["tags"] if isinstance(obj, dict) else obj
    canonical, pending, discarded = _load_vocab()
    issues = validate(tags, canonical, pending, discarded)
    flags = heuristic_flags(tags, canonical, pending)
    warns = warnings(tags, canonical | pending)

    if issues:
        print("INVALID:")
        for x in issues:
            print(f"  - {x}")
    else:
        print("VALID (10 tags, all hard rules pass)")
    if flags:
        print("heuristic flags (soft — review, not failure):")
        for fl in flags:
            print(f"  ! {fl}")
    if warns:
        print("warnings (soft):")
        for w in warns:
            print(f"  ~ {w}")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
