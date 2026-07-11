#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pass3_apply.py — write Opus's pass-3 curation into out/pass3/<hash>.json.

Reads a JSON file mapping {hash: [10 tags]} (Opus's curated output for a batch)
and, for each, writes the final result carrying metadata from the pass-2 file
(so diff_report / write-back can key by it). Validates against the confirmed
ledger and reports anything that breaks the rules.

    python pass3_apply.py --batch batch.json
"""
from __future__ import annotations
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate_tags import _load_vocab, validate  # noqa: E402


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True, help="JSON {hash: [tags...]}")
    ap.add_argument("--seed", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "seed_vocab.confirmed.json"))
    args = ap.parse_args(argv)

    with open(args.seed, "r", encoding="utf-8") as f:
        sd = json.load(f)
    canon, pend, disc = set(sd["canonical"]), set(sd.get("pending", [])), set(sd["discarded"])

    with open(args.batch, "r", encoding="utf-8") as f:
        batch = json.load(f)

    os.makedirs("out/pass3", exist_ok=True)
    ok = bad = 0
    for h, tags in batch.items():
        p2path = os.path.join("out/pass2", h + ".json")
        if not os.path.exists(p2path):
            print(f"  ?? {h}: no pass2 file"); bad += 1; continue
        p2 = json.load(open(p2path, encoding="utf-8"))
        issues = validate(tags, canon, pend, disc)
        rec = {"tags": tags, "_key": p2["_key"], "_pass1": p2.get("_pass1", []),
               "_pass2": p2["tags"], "_pass3": True, "_valid": not issues, "_issues": issues}
        with open(os.path.join("out/pass3", h + ".json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        if issues:
            bad += 1
            print(f"  INVALID {p2['_key'].split('/')[-1][:40]}: {issues}")
        else:
            ok += 1
    print(f"applied: ok={ok} invalid={bad} | pass3 total={len(os.listdir('out/pass3'))}/300")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
