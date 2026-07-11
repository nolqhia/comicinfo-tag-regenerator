# Kavita ジャンルタグ再生成 — ランブック / RUNBOOK

自炊蔵書（Kavita 管理）の各 .cbz 内 `ComicInfo.xml` の `<Genre>` を、接続性の高い要素タグへ全再生成する運用手順。
2026-07 に 300 シリーズ / 619 巻へ適用完了。**次回（新ローカルモデル・新GPU・蔵書増）も同じ手順で回せるように**まとめる。

設計思想は `Tag_Prompt_v4.md`（タグ哲学：接続＞想起、全タグを「他作にも付きうる要素語」に）、
経緯は `HANDOFF_claude_code.md` ＋ `ERRATA_HANDOFF_v1.md`。本書は**操作手順**に絞る。

---

## 0. 前提環境

| 要素 | 内容 | 確認 |
|---|---|---|
| 蔵書 | SMB マウント（前回 `Y:\books`）。構造 `<Library>/<著者キー>/<[年]作品(レーベル)>/*.cbz`。「シリーズ = .cbz を直接含むディレクトリ」 | `ls` |
| Ollama | thinking モデル可。前回 `qwen3.6:35b`(a3b, pass1) / `qwen3.6:27b`(dense, pass2)。**新モデルが出たらここを差し替える** | `curl localhost:11434/api/tags` |
| SearXNG | レビュー検索。前回 `http://SEARXNG:PORT`。**JSON出力を有効化**（settings.yml `search: {formats: [html, json]}`）。無くても HTML フォールバックで動く | `?q=test&format=json` が 200 |
| Python | 3.14。`requests`, `trafilatura`（`pip install trafilatura`）。標準 `zipfile` は `metadata_encoding` 対応版 | — |
| 台帳 | **前回の確定台帳 `docs/seed_vocab.confirmed.json` を次回の seed にする**（canonical を引き継ぐ＝語彙の一貫性を保つ） | — |

**ローカルLLMに逃がす設計理由:** タグ付けは「大量×定型×要約」。検索(SearXNG)＋生成(Ollama)をローカルに置くことで、
重い反復を Claude トークンから切り離す。Claude(Opus) は設計・デバッグ・**pass3 の判断**に専念する。

---

## 1. パイプライン全体（フル再生成）

```
scan → (reviews) → pass1自由生成 → finalize台帳 → pass2台帳照合 → pass3 Opus選定 → diff承認 → 書き戻し
```
**統一台帳の原則:** 全作 pass1 → finalize で台帳を1本に確定 → 全作 pass2。半分ずつ別台帳にしない（語彙が割れ接続が壊れる）。
処理・レビュー・書き戻しの**分割**は可（負荷とリスク限定）。台帳は**1本**。

### 手順とコマンド（プロジェクトルート `D:\CLAUDEWORK\kavita-genres` で実行）

**① 台帳スキャン（read-only）**
```bash
python docs/cbz_library_scan.py --root Y:/books --out docs/inventory.json
# → シリーズ数・巻数・現Genre・異常(ComicInfo欠損/Tags誤格納)を出力
python -c "import json;print('\n'.join(json.load(open('docs/inventory.json',encoding='utf-8'))['inventory']))" > all_keys.txt
```

**② pass1（全作・台帳なし自由生成＋レビュー）** resume可・数時間
```bash
python docs/run_pipeline.py --root Y:/books --keys-file all_keys.txt \
  --searxng http://SEARXNG:PORT --out-dir out --phase pass1 > out/pass1.log 2>&1
# 監視: 別端末で  python docs/progress.py --out-dir out --keys-file all_keys.txt --phase pass1
```

**③ 台帳確定（seed とマージ）★人が昇格語を一瞥して刈る★**
```bash
python docs/finalize_ledger.py --pass1-dir out/pass1 \
  --seed docs/seed_vocab.confirmed.json --out docs/seed_vocab.next.json --report out/ledger_diff.txt
# out/ledger_diff.txt の「昇格 canonical」を一瞥し、メタ(アニメ化/客層/賞)や同義ドリフトを
# build_tag_ledger.py の META_REMOVE / ALIAS に足して再 finalize。確定版を seed_vocab.confirmed.json に。
```

**④ pass2（全作・確定台帳で照合推敲）** resume可・数時間（27b dense が主コスト）
```bash
python docs/run_pipeline.py --root Y:/books --keys-file all_keys.txt \
  --seed docs/seed_vocab.confirmed.json --out-dir out --phase pass2 > out/pass2.log 2>&1
```

**⑤ pass3（Opus キュレーション）★最終品質の決め手★**
- `out/pass2/*.json` を素材に、Opus(＝このセッションの Claude)が **旧/pass1/pass2 の3タグ列＋タイトル**から最良10個を選定。
- 入力生成: `python -c "..."` で `out/pass3_input.txt`（各作 4行: hash/タイトル/旧/p1/p2）を作る（前回スクリプトは会話ログ参照）。
- Opus が 40作ずつ JSON `{hash:[10tags]}` を出し、`python docs/pass3_apply.py --batch batch.json` で `out/pass3/*.json` に反映（検証付き）。
- 方針: **pass1 の端的な実在要素（お好み焼き/競馬/純文学/ヒモ）を復活、pass2 の掃除（著者名/作中造語/メタ/英語 除去）を活用**。レビュー由来のハルシネーション（別作混入）は旧Genre＋タイトルで補正。詳細は [[pass3-opus-curation]]。

**⑥ diff レポート → 承認 → mapping.json**
```bash
python docs/diff_report.py --inventory docs/inventory.json --pass2-dir out/pass3 \
  --seed docs/seed_vocab.confirmed.json --out-report out/diff_report.txt --out-mapping out/mapping.json
```

**⑦ 書き戻し前ゲート（必須）**
- **ZFSスナップショット**（災害復旧）: `zfs snapshot <pool>/<dataset>@pre-tag-regen-YYYYMMDD`
- **Kavitaフィールドロック検証**（1作カナリア）: 読みかけの1作だけ⑧で書換→Kavita再スキャン→**Genre反映＋読書進捗維持**を確認。
  反映されなければ手編集ロック → `POST /api/Series/metadata` で `genresLocked:false`（DTO名は /swagger 確認）。

**⑧ 書き戻し（30作ごと書換→検証→.bak削除）** resume可・数時間
```bash
python docs/writeback_all.py --source-root Y:/books --staging D:/wb-staging \
  --mapping out/mapping.json --journal out/writeback.journal
# 完了後 Kavita を1回スキャン → 全反映
```

---

## 2. 単発（1冊）自炊の増分フロー

新規に1冊自炊したら（Opus が対話内で実行）:
1. 書誌特定（タイトル/著者/レーベル。Amazon URL でも可）
2. `fetch_reviews.py` で SearXNG からレビュー抽出
3. Opus が確定台帳＋2冊目テストで**10タグを直接キュレーション**（pass1〜pass3 を一手で）
4. `cbz_tag_writer.py` で当該 .cbz の `<Genre>` を書換（無劣化・atomic・cp932安全）
5. **台帳更新**: 新語は pending 追加、pending の2件目観測は canonical 昇格 → `seed_vocab.confirmed.json` 保存

---

## 3. ハマりどころ（hard-won、次回も踏む）

- **区切りは カンマ＋スペースなし**（`SF,宇宙,バディ`）。既存コーパスに合わせ無駄 diff を防ぐ。[[genre-separator-no-space]]
- **qwen3.6 は thinking モデル**: `think:false` にしないと dense 27b は数分かかりタイムアウト。`call_ollama` に既定化済み。
- **num_ctx=12288**: Ollama 既定4096は狭い。canonical注入(575語)＋レビューで ~7900 tokens → 出力が切れ不正JSON。広げる。
- **レビュー総量 6000字 cap**: 芥川賞級の膨大レビューでプロンプト溢れ→JSON切れ。`cap_reviews`。
- **cp932 ファイル名 .cbz**: 内部エントリ名が Shift-JIS で UTF-8フラグ無し→標準 zipfile が BadZipFile。`cbz_tag_writer`/`cleanup_baks` は `metadata_encoding='cp932'` フォールバック実装済み。
- **文字数上限6は新規タグのみ**（台帳収載語は免除）。iconic な長尺実在語（宇宙エレベーター/テラフォーミング/ヤングケアラー等）は台帳 pending に足して活かす。[[errata-v1-applied]]
- **discarded は完全でない**: 芥川賞/卑語当て字/虚体 等が入っている一方、実在文化語も落ちる。pass3 で個別補正、最終防衛は人の diff。
- **PageCount 不変＝進捗保護**: 画像とPageCountを触らない限り Kavita の ChapterId が保たれ ProgressSync は無事。cleanup_baks が .bak と比較確認。
- **finalize は seed とマージ**（ゼロ再構築しない）。確定台帳を次回 seed に引き継ぐ。
- **Windows/cp932 コンソール**: 全ツール stdout を UTF-8 に reconfigure 済み。`open(...,encoding='utf-8')` 必須。

---

## 4. ファイル / 成果物

| ファイル | 役割 |
|---|---|
| `docs/seed_vocab.confirmed.json` | **確定台帳**（canonical~575＋長尺実在語）。次回の seed |
| `docs/build_tag_ledger.py` | 正規化規則（ALIAS/DECOMPOSE/META_REMOVE）。`normalize_one` |
| `docs/cbz_library_scan.py` | スキャン→inventory.json |
| `docs/fetch_reviews.py` | SearXNG→trafilatura レビュー抽出 |
| `docs/prompt_pass1.txt` / `prompt_pass2.txt` | 蒸留プロンプト |
| `docs/generate_tags.py` / `refine_tags.py` / `run_pipeline.py` | pass1/pass2 実行（resume・phase分離） |
| `docs/finalize_ledger.py` | 頻度集計→台帳マージ |
| `docs/validate_tags.py` | 機械検証（10個/長さ/メタ/discarded） |
| `docs/pass3_apply.py` | Opus pass3 を反映 |
| `docs/diff_report.py` | 旧→新 diff・mapping.json 生成 |
| `docs/cbz_tag_writer.py` | 単一 .cbz の Genre 無劣化書換（atomic・cp932安全） |
| `docs/cbz_batch_runner.py` / `writeback_all.py` / `cleanup_baks.py` | SMB バッチ書き戻し＋.bak掃除 |

---

## 5. 公開（GitHub / florilegium）前チェックリスト

- [x] **`comicinfo-gen/` はこのリポジトリに含めない**（別リポ `github.com/nolqhia/comicinfo-gen` があり、タグ生成パイプラインでも未使用）。`.gitignore` で除外。楽天API鍵はローカル原本にのみ残る。
- [x] SearXNG 内部IPは docs からプレースホルダ `http://SEARXNG:PORT` に置換済み（実IPは memory のみ）。SMBパス `Y:/books` は環境依存の例示。
- [ ] `seed_vocab.confirmed.json` は個人蔵書の傾向を反映（公開してもよいが、そういう語彙リストと明記）
- [ ] LICENSE 選定（MIT 等）
- [ ] README = 本 RUNBOOK の §0-§3 を流用
