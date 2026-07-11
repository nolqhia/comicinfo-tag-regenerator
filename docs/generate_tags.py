#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_tags.py — Pass-1 free tag generation via a local Ollama model.

Per handoff §4/§8: pass 1 generates 10 tags for a work from its あらすじ (+ reviews
when available) WITHOUT showing the model any vocabulary ledger — anchoring on an
existing vocab shrinks invention. The confirmed ledger is applied later in pass 2
(refine_tags.py). Output is strict JSON so it feeds the aggregator/validator.

Context sources:
  * あらすじ (Summary) is read straight from each work's ComicInfo.xml — already
    present for the whole corpus, so no fetch is needed for the synopsis.
  * reviews: optional list of extracted review texts (from the SearXNG layer,
    wired separately once its endpoint is known). Works without them, weaker.

Usage:
    # single work from the library, identified by its inventory key
    python generate_tags.py --root Y:/books --key "Fiction/AGA_東崎惟子/[2024] 少女星間漂流記 (電撃文庫)"
    # or feed context JSON on stdin: {"title","author","imprint","summary","reviews":[]}
    echo '{...}' | python generate_tags.py --stdin

    # options: --model qwen3.6:35b (pass1 default) --temperature 0.7 --host http://localhost:11434
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from xml.etree import ElementTree as ET

import urllib.request

PASS1_MODEL = "qwen3.6:35b"      # a3b MoE — fast, for volume pass 1
OLLAMA_HOST = "http://localhost:11434"

# ERRATA §C-2: the pipeline system prompt is distilled into prompt_pass1.txt
# (rules only, NO vocab list — anti-anchoring). Single source of truth.
_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_pass1.txt")
with open(_PROMPT_PATH, "r", encoding="utf-8") as _f:
    SYSTEM_PROMPT = _f.read()

# Ollama structured-output schema: forces {tags:[...], rationale:{...}}
FORMAT_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 10, "maxItems": 10},
        "rationale": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["tags"],
}


def read_context_from_cbz(root: str, key: str) -> dict:
    """Read title/author/imprint/summary from the first volume's ComicInfo.xml."""
    inv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory.json")
    with open(inv_path, "r", encoding="utf-8") as f:
        inv = json.load(f)["inventory"]
    if key not in inv:
        raise SystemExit(f"key not in inventory.json: {key}")
    vol0 = os.path.join(root, inv[key]["volumes"][0])
    with zipfile.ZipFile(vol0, "r") as z:
        ci = next(n for n in z.namelist() if os.path.basename(n).lower() == "comicinfo.xml")
        r = ET.fromstring(z.read(ci))

    def t(tag):
        el = r.find(tag)
        return (el.text or "").strip() if el is not None else ""

    return {
        "title": t("Title") or t("Series"),
        "author": t("Writer"),
        "imprint": t("Imprint"),
        "summary": t("Summary"),
        "old_genre": t("Genre"),
        "reviews": [],
        "_key": key,
        "_library": inv[key]["library"],
    }


# Cap total review text fed to the model. Heavily-reviewed works (芥川賞 winners,
# bestsellers) can otherwise supply ~16k chars, overflowing num_ctx=8192 so the
# generated JSON gets truncated -> invalid even after retries. ~6000 chars leaves
# room for system rules + (pass2) the canonical list + output.
MAX_REVIEW_CHARS = 6000


def cap_reviews(reviews: list[str], max_chars: int = MAX_REVIEW_CHARS) -> list[str]:
    out: list[str] = []
    total = 0
    for rv in reviews:
        rv = (rv or "").strip()
        if not rv:
            continue
        if total + len(rv) > max_chars:
            head = rv[: max_chars - total]
            if head:
                out.append(head)
            break
        out.append(rv)
        total += len(rv)
    return out


def build_user_message(ctx: dict) -> str:
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
    parts += ["", "上記だけを根拠に、規則に従いタグを10個生成し JSON で返しなさい。"]
    return "\n".join(parts)


def call_ollama(host: str, model: str, system: str, user: str, temperature: float,
                timeout: float = 600, think: bool = False, retries: int = 3,
                num_ctx: int = 12288) -> dict:
    """Call Ollama chat with structured output. Even with format schema the model
    occasionally emits invalid JSON (truncation, stray token) — retry a few times,
    nudging temperature up slightly, before giving up.

    num_ctx: context window. Ollama's default is only 4096. The pass-2 prompt injects
    the full canonical list (575 words after finalize) + up to 6000 chars of reviews,
    measuring ~7900 tokens for review-heavy works. At num_ctx=8192 that left only ~290
    tokens for output, so the rationale got truncated -> invalid JSON on every retry
    (looks like a stall: the work never produces a file). 12288 gives ~4000 tokens of
    output headroom over the worst-case prompt."""
    last_err: Exception | None = None
    for attempt in range(retries):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": FORMAT_SCHEMA,
            "options": {"temperature": temperature + 0.1 * attempt, "num_ctx": num_ctx},
            "keep_alive": "10m",  # keep the model resident across a batch run
            # qwen3.6 is a THINKING model: left on, dense 27b spends minutes on hidden
            # reasoning and times out. The task is constrained JSON tagging — no visible
            # chain-of-thought needed. Disabling it took pass-2 from >600s to ~53s.
            "think": think,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"{host}/api/chat", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return json.loads(body["message"]["content"])
        except json.JSONDecodeError as e:
            last_err = e  # bad model output — retry with a nudged temperature
            continue
    raise ValueError(f"Ollama returned invalid JSON after {retries} attempts: {last_err}")


def generate(ctx: dict, *, model: str = PASS1_MODEL, host: str = OLLAMA_HOST,
             temperature: float = 0.7) -> dict:
    user = build_user_message(ctx)
    result = call_ollama(host, model, SYSTEM_PROMPT, user, temperature)
    result["_key"] = ctx.get("_key")
    result["_model"] = model
    result["_old_genre"] = ctx.get("old_genre")
    return result


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="library root (for --key mode)")
    ap.add_argument("--key", help="inventory.json key of the work")
    ap.add_argument("--stdin", action="store_true", help="read context JSON from stdin")
    ap.add_argument("--model", default=PASS1_MODEL)
    ap.add_argument("--host", default=OLLAMA_HOST)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--searxng", help="SearXNG URL; if set, fetch reviews to enrich context")
    args = ap.parse_args(argv)

    if args.stdin:
        ctx = json.load(sys.stdin)
    elif args.key:
        if not args.root:
            ap.error("--key requires --root")
        ctx = read_context_from_cbz(args.root, args.key)
    else:
        ap.error("provide --key (with --root) or --stdin")

    # ERRATA/handoff §4: reviews are the quality lever. Fetch them when a SearXNG
    # endpoint is provided (falls back gracefully to summary-only if none found).
    if args.searxng and not ctx.get("reviews"):
        from fetch_reviews import gather
        rv = gather(ctx.get("title", ""), ctx.get("author", ""), args.searxng)
        ctx["reviews"] = rv["reviews"]
        print(f"# reviews: {len(rv['sources'])} sources, {rv['chars']} chars",
              file=sys.stderr)

    result = generate(ctx, model=args.model, host=args.host, temperature=args.temperature)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
