# comicinfo-tag-regenerator

Kavita 蔵書のジャンルタグを、**接続性の高い要素タグ**へ作り直すためのパイプライン。
各 `.cbz` 内 `ComicInfo.xml` の `<Genre>` を、ローカルLLM＋自前検索＋人（モデル）の最終判断で再生成する。

> **タグは分類ではなく接続である。**
> 棚に仕舞うラベルではなく、`図書館` で青春とホラーが、`競馬` でラブコメとミステリが繋がる——
> タグクラウドを回遊して「この二作が繋がるのか」という発見が起きる状態を作る。

設計と、その過程で一番面白かったこと（**機械は正規化できるが「何を残すべきか」は判断できない**）については、
エッセイ [**タグは分類ではなく接続である**](https://florilegium.mof.li/comicinfo-tag-regeneration) に書いた。

## 仕組み

```
scan → reviews(SearXNG) → pass1 自由生成 → finalize 台帳 → pass2 台帳照合 → pass3 判断 → diff → 書き戻し
```

- **重い筋肉（検索・生成）はローカルへ**：SearXNG でレビュー取得、trafilatura で本文抽出、Ollama(Qwen3.6) でタグ生成。
- **頭脳（最終判断）だけ賢いモデルへ**：pass2 の正規化が端的な要素語を凡庸な上位語に潰すので、
  pass3 で「pass1 の端的さ＋pass2 の掃除」を選び直す。ここが品質を決める。
- **語彙台帳は生きている**：1回きりの語は保留、2作目で正典に昇格。蔵書が増えるほど賢くなる。
- **書き戻しは無劣化・atomic**：画像は触らず `<Genre>` だけ差し替え、ページ数不変で読書進捗も保つ。

## 使い方

- フル再生成（数百シリーズ）: [`docs/RUNBOOK.md`](./docs/RUNBOOK.md)
- 1冊ずつの増分: [`docs/SINGLE_BOOK.md`](./docs/SINGLE_BOOK.md)
- タグ哲学の原典: [`docs/Tag_Prompt_v4.md`](./docs/Tag_Prompt_v4.md)

### 主なスクリプト（`docs/`）

| スクリプト | 役割 |
|---|---|
| `cbz_library_scan.py` | 蔵書スキャン → inventory |
| `fetch_reviews.py` | SearXNG → trafilatura でレビュー抽出 |
| `generate_tags.py` / `refine_tags.py` / `run_pipeline.py` | pass1 / pass2（resume・phase 分離） |
| `finalize_ledger.py` / `build_tag_ledger.py` | 頻度集計・正規化ルール・台帳マージ |
| `validate_tags.py` | 機械検証（10個/字数/メタ/固有名詞） |
| `pass3_apply.py` | 最終キュレーション結果の反映 |
| `diff_report.py` | 旧→新 diff・書き戻し用 mapping 生成 |
| `cbz_tag_writer.py` / `cbz_batch_runner.py` / `writeback_all.py` / `cleanup_baks.py` | 無劣化・atomic な書き戻し（cp932 対応） |

### 前提

- Python 3.14（`requests`, `trafilatura`）
- [Ollama](https://ollama.com/) + Qwen3.6 系（GPU）
- [SearXNG](https://docs.searxng.org/)（JSON 出力を有効化。無くても HTML フォールバック）
- Kavita 管理下の `.cbz`（ComicInfo.xml 入り）

## 語彙台帳について

`docs/seed_vocab.confirmed.json` は canonical / pending / discarded の語彙台帳。
**個人蔵書の傾向を反映した具体的な語彙リスト**であり、そのまま使うより「seed（出発点）」として各自の蔵書で育てるのが本来の使い方。

## ライセンス

MIT License — [`LICENSE`](./LICENSE)
