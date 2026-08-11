#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_tag_ledger.py — 既存 inventory.json から正規語彙台帳(tag_ledger.json)を生成する

phase2_normalization_plan.md の §2(削除) §3(正規化) を機械適用し、
頻度2以上を確定正規語(canonical)、頻度1を保留(pending)に仕分ける。
「2冊目テスト」の機械実装: pending は2件目が触れて初めて canonical へ昇格する。

usage:
  python build_tag_ledger.py --inventory inventory.json --outdir ./ledger [--dry-run]

出力:
  tag_ledger.json     台帳（canonical / pending / alias / meta_removed の4区画）
  ledger_report.txt   人間確認用サマリ（何がどう変わったか）
"""
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path


# ── §2 削除対象（書誌メタ・非要素） ─────────────────────────
META_REMOVE = {
    "ライトノベル", "青年漫画", "少年漫画", "少女漫画", "コミックス",
    "アンソロジー", "選集", "受賞作", "連載中", "完結", "解説付き",
    "名作再評価", "古典推理",
    # finalize 一瞥で検出したメディアミックス状態・形式メタ（作品要素でない）
    "ラノベ", "アニメ化", "コミカライズ", "漫画化", "実写化", "映画化",
    "ドラマ化", "ゲーム化", "メディアミックス",
    "女性向け", "男性向け", "成人向け",   # 客層メタ
}

# ── §3-1 単純表記揺れ（別名→正規形） ───────────────────────
# 注: Kavita取り込みが英字略語を Title-Case 化する癖があるため復元する
ALIAS = {
    "ミステリー": "ミステリ",
    "Sf": "SF",
    "ｓｆ": "SF",
    "ハードsf": "SF",
    "ラブコメディ": "ラブコメ",
    "幼なじみ": "幼馴染",
    "名探偵": "探偵",
    "大魔王": "魔王",
    "ホラー要素": "ホラー",
    "ホラー風味": "ホラー",
    "ファンタジー要素": "ファンタジー",
    "恋愛要素": "恋愛",
    "セクシー要素": "お色気",
    "幻想的": "幻想",
    "総選挙": "選挙",
    "Ai": "AI",
    "Dna": "DNA",
    "It": "IT",
    "Jis": "JIS",
    "Vr": "VR",
    "Sns": "SNS",
    "Vtuber": "VTuber",
    # ERRATA §B-3: TS と 性転換 は同義対 → 性転換 に統合（Kavita の Title-Case 化 "Ts" も同様）。
    # pending の「トランスジェンダー」は別概念（アイデンティティ）なので統合しない。
    "TS": "性転換",
    "Ts": "性転換",
    # ERRATA §B-1: いとこ ≠ 姉妹。従姉/従姉妹/従兄弟 は「いとこ」に正規化（姉妹へ潰さない）。
    "従姉": "いとこ",
    "従姉妹": "いとこ",
    "従兄弟": "いとこ",
    # finalize 一瞥で検出した同義ドリフト（両表記が canonical 昇格していた）を統合。
    "第一人称": "一人称",
    "神様": "神",
    "日常系": "日常",
    "復讐劇": "復讐",
    "閉鎖": "閉鎖空間",
    "癒やし": "癒し",   # 送り仮名揺れ（オーロラとサーモン処理時に検出）
}

# ── §3-2 複合語→原子分解（1語→複数語） ─────────────────────
# 確立ジャンル語は分解しない（保持リスト）
KEEP_COMPOUND = {
    "ダークファンタジー", "ハイファンタジー", "本格ミステリ", "新本格",
    "異世界転生", "異世界転移", "セカイ系", "電波系",
}
DECOMPOSE = {
    "青春ミステリ": ["青春", "ミステリ"],
    "医療ミステリ": ["医療", "ミステリ"],
    "学園ファンタジー": ["学園", "ファンタジー"],
    "恋愛ファンタジー": ["恋愛", "ファンタジー"],
    "歴史ファンタジー": ["歴史", "ファンタジー"],
    "現代ファンタジー": ["現代", "ファンタジー"],
    "学園ラブコメ": ["学園", "ラブコメ"],
    "学園バトル": ["学園", "バトル"],
    "能力バトル": ["異能", "バトル"],
    "すれ違い恋愛": ["すれ違い"],
    "恋愛すれ違い": ["すれ違い"],
    "人生やり直し": ["やり直し"],
    "青春SF": ["青春", "SF"],
    "Sf融合": ["SF"],
    # ERRATA §B-2: 複合ジャンルを ALIAS で潰すと片側成分が消える → DECOMPOSE で両成分を残す。
    "Sfミステリー": ["SF", "ミステリ"],
    "ホラーミステリ": ["ホラー", "ミステリ"],
}


def normalize_one(term: str) -> list[str]:
    """1タグを正規化。削除なら[]、別名なら正規形、分解なら複数語を返す。"""
    t = term.strip()
    if not t:
        return []
    if t in META_REMOVE:
        return []
    if t in KEEP_COMPOUND:
        return [t]
    if t in DECOMPOSE:
        return DECOMPOSE[t]
    if t in ALIAS:
        return [ALIAS[t]]
    return [t]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--outdir", default="./ledger")
    ap.add_argument("--dry-run", action="store_true",
                    help="台帳を書かず統計のみ表示")
    args = ap.parse_args()

    inv = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    # 正規化後の 語→作品集合 を再構築
    norm_map: dict[str, set] = defaultdict(set)
    removed_hits = 0
    decomposed_hits = 0
    alias_hits = 0
    per_series_after: dict[str, list] = {}

    for s in inv:
        name = s["name"]
        after = set()
        for g in s.get("genres", []):
            res = normalize_one(g)
            if not res:
                removed_hits += 1
                continue
            if g in DECOMPOSE:
                decomposed_hits += 1
            if g in ALIAS:
                alias_hits += 1
            for r in res:
                after.add(r)
                norm_map[r].add(name)
        per_series_after[name] = sorted(after)

    # 頻度で canonical / pending に仕分け
    canonical, pending = {}, {}
    for term, series_set in norm_map.items():
        entry = {"count": len(series_set), "series": sorted(series_set)}
        if len(series_set) >= 2:
            canonical[term] = entry
        else:
            pending[term] = entry

    # 破綻作品（正規化後3タグ未満）
    thin = {n: tags for n, tags in per_series_after.items() if len(tags) < 3}

    ledger = {
        "_meta": {
            "source": args.inventory,
            "series_count": len(inv),
            "canonical_count": len(canonical),
            "pending_count": len(pending),
            "policy": "count>=2 => canonical, count==1 => pending(2冊目テスト待ち)",
        },
        "canonical": dict(sorted(canonical.items(), key=lambda kv: -kv[1]["count"])),
        "pending": dict(sorted(pending.items())),
        "alias": ALIAS,
        "meta_removed": sorted(META_REMOVE),
        "decompose": DECOMPOSE,
        "keep_compound": sorted(KEEP_COMPOUND),
    }

    # レポート
    lines = []
    lines.append(f"総作品数: {len(inv)}")
    lines.append(f"正規化後の語彙総数: {len(norm_map)} "
                 f"(canonical {len(canonical)} / pending {len(pending)})")
    lines.append(f"削除されたタグ延べ: {removed_hits}")
    lines.append(f"別名で正規化された延べ: {alias_hits}")
    lines.append(f"複合語分解された延べ: {decomposed_hits}")
    lines.append(f"正規化後3タグ未満の破綻作品: {len(thin)}")
    lines.append("")
    lines.append("=== canonical 上位30 ===")
    for term, e in list(ledger["canonical"].items())[:30]:
        lines.append(f"  {term}  {e['count']}")
    lines.append("")
    lines.append("=== pending のうち『2冊目が来そうな良い種』候補（手動確認用・先頭40） ===")
    # 固有名詞っぽいもの(4文字超 or カタカナ列)を除外して、要素語っぽいものを優先表示
    def looks_element(t: str) -> bool:
        if len(t) > 5:
            return False
        return True
    good_pending = [t for t in ledger["pending"] if looks_element(t)]
    for t in good_pending[:40]:
        lines.append(f"  {t}")
    lines.append("")
    lines.append(f"=== 破綻作品（要再調査） {len(thin)}件 ===")
    for n, tags in sorted(thin.items()):
        lines.append(f"  [{len(tags)}] {n}  ← {', '.join(tags) if tags else '（全滅）'}")

    report = "\n".join(lines)
    print(report)

    if not args.dry_run:
        (outdir / "tag_ledger.json").write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (outdir / "ledger_report.txt").write_text(report, encoding="utf-8")
        (outdir / "series_normalized.json").write_text(
            json.dumps(per_series_after, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[*] 出力: {outdir}/tag_ledger.json, ledger_report.txt, series_normalized.json")


if __name__ == "__main__":
    main()
