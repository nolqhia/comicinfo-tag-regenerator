#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refine_tags.py — Pass-2 tag refinement against the confirmed ledger.

Per handoff §4 + ERRATA §C-2: pass 2 takes pass-1's free-generated tags and
refines them with the *finalized* vocab ledger injected — normalizing drift
(相棒→バディ, 連作短編→短編連作), lifting 作中固有名詞 to real-world hypernyms,
and decomposing over-specific compounds. Post-anchoring is fine here; the goal is
consistency and connection. Uses the quality model (qwen3.6:27b dense) and the
distilled prompt_pass2.txt. Output is the same strict JSON as pass 1.

Usage:
    # take pass-1 tags from a file (generate_tags output) and refine
    python refine_tags.py --root Y:/books --key "<inventory key>" \
        --pass1 out/pass1/<work>.json --seed seed_vocab.json [--searxng URL]
    # or pass the raw tags inline
    python refine_tags.py --root Y:/books --key "<key>" --pass1-tags "連作短編,双主人公,..."
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate_tags import call_ollama, read_context_from_cbz, cap_reviews  # noqa: E402

PASS2_MODEL = "qwen3.6:27b"      # dense — quality pass
OLLAMA_HOST = "http://localhost:11434"

_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_pass2.txt")
with open(_PROMPT_PATH, "r", encoding="utf-8") as _f:
    SYSTEM_PROMPT = _f.read()

# Same structured-output contract as pass 1.
FORMAT_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 10, "maxItems": 10},
        "rationale": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["tags"],
}


def load_ledger(seed_path: str) -> tuple[list[str], list[str]]:
    with open(seed_path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return d.get("canonical", []), d.get("discarded", [])


def build_pass2_message(ctx: dict, pass1_tags: list[str],
                        canonical: list[str], discarded: list[str]) -> str:
    parts = [
        f"タイトル: {ctx.get('title','')}",
        f"著者: {ctx.get('author','')}",
        f"レーベル: {ctx.get('imprint','')}",
        "",
        "## あらすじ",
        ctx.get("summary", "") or "(なし)",
    ]
    reviews = cap_reviews(ctx.get("reviews") or [])
    if reviews:
        parts += ["", "## レビュー・感想（抜粋）"]
        for i, rv in enumerate(reviews, 1):
            parts += [f"--- レビュー{i} ---", rv.strip()]
    parts += [
        "",
        "## パス1の生タグ（これを推敲する）",
        ", ".join(pass1_tags),
        "",
        "## 正規語彙リスト canonical（同概念はこの語へ寄せ、優先再利用する）",
        ", ".join(canonical),
        "",
        "## discarded（出してはならない固有名詞・書誌メタの見本。同種パターンも出さない）",
        ", ".join(discarded),
        "",
        "推敲タスク（揺れ矯正→固有名詞の持ち上げ→複合語分解→不足補完）を適用し、"
        "最終タグを10個ちょうど、JSON で返しなさい。",
    ]
    return "\n".join(parts)


def refine(ctx: dict, pass1_tags: list[str], canonical: list[str], discarded: list[str],
           *, model: str = PASS2_MODEL, host: str = OLLAMA_HOST,
           temperature: float = 0.3) -> dict:
    user = build_pass2_message(ctx, pass1_tags, canonical, discarded)
    result = call_ollama(host, model, SYSTEM_PROMPT, user, temperature)
    result["_key"] = ctx.get("_key")
    result["_model"] = model
    result["_pass1"] = pass1_tags
    result["_old_genre"] = ctx.get("old_genre")
    return result


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--pass1", help="pass-1 result JSON file (with a 'tags' list)")
    ap.add_argument("--pass1-tags", help="comma-separated pass-1 tags (instead of --pass1)")
    ap.add_argument("--seed", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "seed_vocab.json"))
    ap.add_argument("--model", default=PASS2_MODEL)
    ap.add_argument("--host", default=OLLAMA_HOST)
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--searxng", help="SearXNG URL; if set, include reviews in context")
    args = ap.parse_args(argv)

    if args.pass1:
        with open(args.pass1, "r", encoding="utf-8") as f:
            pass1_tags = json.load(f)["tags"]
    elif args.pass1_tags:
        pass1_tags = [t.strip() for t in args.pass1_tags.split(",") if t.strip()]
    else:
        ap.error("provide --pass1 or --pass1-tags")

    ctx = read_context_from_cbz(args.root, args.key)
    if args.searxng and not ctx.get("reviews"):
        from fetch_reviews import gather
        rv = gather(ctx.get("title", ""), ctx.get("author", ""), args.searxng)
        ctx["reviews"] = rv["reviews"]
        print(f"# reviews: {len(rv['sources'])} sources, {rv['chars']} chars", file=sys.stderr)

    canonical, discarded = load_ledger(args.seed)
    result = refine(ctx, pass1_tags, canonical, discarded,
                    model=args.model, host=args.host, temperature=args.temperature)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
