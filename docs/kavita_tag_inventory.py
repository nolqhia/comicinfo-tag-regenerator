#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kavita_tag_inventory.py — Kavita 全シリーズのタグ/ジャンル棚卸し（read-only）

usage:
  pip install requests
  python kavita_tag_inventory.py --base http://<kavita-host>:5000 --api-key <KEY> \
      [--libraries Fiction Manga] [--outdir ./inventory]

API キーは Kavita の ユーザー設定 > 3rd Party Clients で取得。

出力:
  inventory.json   シリーズごとの tags/genres 全量
  tag_freq.csv     タグ頻度表（付与シリーズ一覧付き）
  genre_freq.csv   ジャンル頻度表
  near_pairs.csv   表記揺れ候補ペア（部分一致 or 類似度 0.6 以上）
"""
import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import requests


def authenticate(base: str, api_key: str) -> str:
    r = requests.post(
        f"{base}/api/Plugin/authenticate",
        params={"apiKey": api_key, "pluginName": "tag-inventory"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["token"]


def get_libraries(base: str, headers: dict) -> dict:
    for path in ("/api/Library/libraries", "/api/Library"):
        r = requests.get(base + path, headers=headers, timeout=30)
        if r.ok:
            return {lib["id"]: lib["name"] for lib in r.json()}
    r.raise_for_status()


def get_all_series(base: str, headers: dict) -> list:
    """Kavita 0.8 系: POST /api/Series/all-v2 + FilterV2Dto。ページングは Pagination ヘッダ。"""
    filter_v2 = {
        "id": 0,
        "name": "",
        "statements": [],
        "combination": 1,
        "sortOptions": {"sortField": 1, "isAscending": True},
        "limitTo": 0,
    }
    page, size, out = 1, 200, []
    while True:
        r = requests.post(
            f"{base}/api/Series/all-v2",
            params={"PageNumber": page, "PageSize": size},
            headers=headers,
            json=filter_v2,
            timeout=60,
        )
        if not r.ok:
            sys.exit(
                f"[!] /api/Series/all-v2 が {r.status_code} を返しました。"
                f" Kavita のバージョン差異の可能性があるため {base}/swagger で"
                f" エンドポイントを確認してください。\n{r.text[:300]}"
            )
        batch = r.json()
        out.extend(batch)
        pag = r.headers.get("Pagination")
        if pag:
            meta = json.loads(pag)
            if meta.get("currentPage", page) >= meta.get("totalPages", page):
                break
        elif len(batch) < size:
            break
        page += 1
    return out


def get_series_metadata(base: str, headers: dict, series_id: int) -> dict:
    r = requests.get(
        f"{base}/api/Series/metadata",
        params={"seriesId": series_id},
        headers=headers,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def near_pairs(vocab: set) -> list:
    pairs = []
    v = sorted(vocab)
    for i in range(len(v)):
        for j in range(i + 1, len(v)):
            a, b = v[i], v[j]
            ratio = SequenceMatcher(None, a, b).ratio()
            if a in b or b in a or ratio >= 0.6:
                pairs.append((a, b, round(ratio, 3)))
    return sorted(pairs, key=lambda x: -x[2])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="例: http://192.168.x.x:5000")
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--libraries", nargs="*", default=None,
                    help="対象ライブラリ名（省略時は全ライブラリ）")
    ap.add_argument("--outdir", default="./inventory")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    token = authenticate(base, args.api_key)
    headers = {"Authorization": f"Bearer {token}"}

    libs = get_libraries(base, headers)
    print(f"[*] ライブラリ: {libs}")

    series = get_all_series(base, headers)
    if args.libraries:
        keep = {lid for lid, name in libs.items() if name in set(args.libraries)}
        series = [s for s in series if s.get("libraryId") in keep]
    print(f"[*] 対象シリーズ数: {len(series)}")

    inventory = []
    tag_map = defaultdict(list)
    genre_map = defaultdict(list)

    for n, s in enumerate(series, 1):
        sid = s["id"]
        name = s.get("name", f"id={sid}")
        md = get_series_metadata(base, headers, sid)
        tags = sorted({t["title"].strip() for t in md.get("tags") or []})
        genres = sorted({g["title"].strip() for g in md.get("genres") or []})
        inventory.append({
            "seriesId": sid,
            "name": name,
            "library": libs.get(s.get("libraryId"), "?"),
            "tags": tags,
            "genres": genres,
        })
        for t in tags:
            tag_map[t].append(name)
        for g in genres:
            genre_map[g].append(name)
        if n % 20 == 0:
            print(f"    ... {n}/{len(series)}")
        time.sleep(0.05)

    (outdir / "inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def write_freq(path: Path, mapping: dict) -> None:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["term", "count", "series"])
            for term, names in sorted(mapping.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                w.writerow([term, len(names), " / ".join(names)])

    write_freq(outdir / "tag_freq.csv", tag_map)
    write_freq(outdir / "genre_freq.csv", genre_map)

    vocab = set(tag_map) | set(genre_map)
    with open(outdir / "near_pairs.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["term_a", "term_b", "similarity"])
        for a, b, ratio in near_pairs(vocab):
            w.writerow([a, b, ratio])

    print(f"[*] 完了。タグ語彙 {len(tag_map)} 種 / ジャンル語彙 {len(genre_map)} 種")
    print(f"[*] 出力: {outdir}/inventory.json, tag_freq.csv, genre_freq.csv, near_pairs.csv")


if __name__ == "__main__":
    main()
