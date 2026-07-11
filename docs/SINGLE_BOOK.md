# 単発自炊タグ付け — 実行カード（Opus 用）

**用途:** 新規に1冊（1シリーズ）自炊したとき、Opus がこの手順を上から実行して
ComicInfo の `<Genre>` を付け、確定台帳を更新する。フル再生成は別（`RUNBOOK.md`）。

**お嬢様が渡すもの（どれか）:** 対象 .cbz パス / タイトル＋著者 / Amazon URL。
**確定台帳:** `docs/seed_vocab.confirmed.json`（canonical=優先再利用, pending=保留, discarded=禁止見本）。
**タグ哲学の原典:** `Tag_Prompt_v4.md`。区切りは**カンマ＋スペースなし**。

---

## 手順

### 1. 特定・在処
- タイトル/著者/レーベルを確定（URLなら日本語デコードして宣言）。
- 対象シリーズのディレクトリと**全巻の .cbz パス**を掴む（例 `Y:/books/<Lib>/<著者>/<[年]作品(レーベル)>/*.cbz`）。
  ```bash
  python -c "import json;i=json.load(open('docs/inventory.json',encoding='utf-8'))['inventory']; \
  print([k for k in i if 'キーワード' in k])"   # inventory があれば。無ければ ls で探す
  ```

### 2. あらすじ＋レビュー取得
- Amazon URL の `dp/XXXXXXXXXX` が ISBN。`--isbn` を渡すと**公式あらすじ**を確実に引く（楽天→OpenBD）。
- **楽天キーを env に設定**すると楽天優先（新刊ラノベは OpenBD に無いことが多い＝楽天が効く）。無ければ OpenBD だけで動く。
  ```bash
  # 楽天キーは comicinfo-gen のローカル設定を借りる（公開コードにはハードコードしない）
  export RAKUTEN_APP_ID=$(grep -oP 'RAKUTEN_APP_ID\s*=\s*"\K[^"]+' comicinfo-gen/comicinfo_gen.py)
  export RAKUTEN_ACCESS_KEY=$(grep -oP 'RAKUTEN_ACCESS_KEY\s*=\s*"\K[^"]+' comicinfo-gen/comicinfo_gen.py)
  python docs/fetch_reviews.py --searxng http://SEARXNG:PORT \
    --title "タイトル" --author "著者" --isbn 4824204690 \
    --verbose --json out/_book_reviews.json
  ```
- 結果の先頭に `[あらすじ]`（公式）、続いて `[検索スニペット]`/本文（レビュー）が入る。あらすじが取れなくても感想だけで進める。

### 3. キュレーション（Opus が pass1〜3 相当を一手で）
あらすじ＋レビュー＋（あれば）旧Genre を根拠に、**最終10タグ**を選ぶ。ルール:
- **10個ちょうど**、日本語のみ、**カンマ＋スペースなし**。新規タグは2〜4文字目安・上限6文字（**台帳収載語は上限免除**）。
- **構造タグ約5＋希少要素タグ約5**。構造は canonical から優先再利用（揺れを作らない）。
- 希少要素は **2冊目テスト**を通す具体語（お好み焼き/競馬/触手/実在地名…）。**凡庸な上位語に丸めない**（心理ドラマ/成長/家族 等でお茶を濁さない）。
- **禁止**: 作中固有名詞（人名・作中地名・作中造語・技名）、著者名、書誌メタ（レーベル/巻数/刊行状態/賞/客層[女性向け等]/メディアミックス[アニメ化等]）、英語（台帳の SF/AI/JK/OL/VTuber 等の canonical は可）、スペース、重複、根拠のない捏造。
- 作中の架空地名は**実在の上位地名へ持ち上げ**（遠見市→島根）。実在文化語（きさらぎ駅等）は可。
- **discarded は完全でない**: ヒットしなくても上記パターンなら出さない。逆に discarded に実在語が混じることもある（芥川賞/卑語当て字/虚体 は登録済み＝出せない）。
- レビューが**別作を拾うハルシネーション**に注意（スピンオフ/アンソロ混入）。旧Genre＋タイトルと矛盾したら旧側を信じる。

### 4. 機械検証
```bash
printf '{"tags":["t1","t2",...,"t10"]}' > out/_tmp_tags.json
python docs/validate_tags.py --file out/_tmp_tags.json
# INVALID が出たら直す。長尺の実在語で弾かれたら → 手順6でその語を pending に足せば以後 OK
```

### 5. ComicInfo 書換（全巻・無劣化・atomic・cp932安全）
```bash
python docs/cbz_tag_writer.py --cbz "Y:/books/.../作品/vol1.cbz" --genres "t1,t2,...,t10" --backup
python docs/cbz_tag_writer.py --cbz "Y:/books/.../作品/vol2.cbz" --genres "t1,t2,...,t10" --backup
# シリーズ全巻に同一Genre。--backup で .bak を残し、確認後に削除
```
- PageCount・画像は不変＝Kavita の読書進捗は保たれる。

### 6. 台帳更新（`docs/seed_vocab.confirmed.json`）
最終10タグについて、下記を実行（新語→pending, pending の2件目→canonical 昇格, 長尺実在語→pending）:
```bash
python - <<'PY'
import json
p="docs/seed_vocab.confirmed.json"; d=json.load(open(p,encoding="utf-8"))
canon=set(d["canonical"]); pend=set(d["pending"]); disc=set(d["discarded"])
final=["t1","t2","...","t10"]   # ← 最終タグに置換
promoted=[]; added=[]
for t in final:
    if t in disc:            continue          # 禁止語は台帳に入れない（そもそもタグに出さない）
    if t in canon:           continue          # 既 canonical
    if t in pend:            canon.add(t); pend.discard(t); promoted.append(t)  # 2件目→昇格
    else:                    pend.add(t); added.append(t)                       # 新語→pending
d["canonical"]=sorted(canon); d["pending"]=sorted(pend)
json.dump(d,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
print("promoted→canonical:",promoted); print("added→pending:",added)
PY
```

### 7. 反映・後片付け
- Kavita でライブラリを1回スキャン → 反映。
- 確認できたら .bak 削除: `python docs/cleanup_baks.py --source-root Y:/books --mapping <この作の1件mapping.json>`
  （単発なら手動 `rm *.cbz.bak` でも可）。
- `out/_tmp_*.json` を掃除。

---

## 例（実績）
`魔女に首輪は付けられない`（電撃文庫, 全2巻）:
- 旧: 絆,抵抗,葛藤,契約,魔女,呪術,ライトノベル,恋愛,自由,ファンタジー
- 新: **ファンタジー,アクション,ミステリ,バディ,魔女,首輪,監禁,刑事,捜査官,呪術**
  （メタ「ライトノベル」除去、`首輪/監禁/捜査官/刑事` の具体要素を復活、`呪術/魔女` 維持）

関連: [[single-book-workflow]] / [[cbz-tag-tooling]] / [[pass3-opus-curation]] / [[searxng-endpoint]] / [[genre-separator-no-space]]
