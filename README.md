# ローカルLLMで議事録は作れるか — Local LLM Minutes Eval

64GB Mac mini 上のローカルLLMが、議事録生成・構造化データ抽出の実務にどこまで使えるかを
定点観測するプロジェクトです。半年ごとを目安に、同一のデータセット・同一の採点系で測り直します。

**Can local LLMs take the minutes?** A recurring, fixed-benchmark evaluation of local LLMs
(27B-class and below) for Japanese meeting-minutes generation and structured extraction,
measured on a 64GB Mac mini. Re-measured periodically with the same dataset and scoring code.

---

## 最新版 / Latest Edition

### 📄 2026年7月版（v2026.07）

**結論の要約**: ローカル最良は qwen3.6:27b（F1 0.788、フロンティア最上位の約86%）。
「人手レビュー前提なら使える、自動連携（グレードA）はまだ不可」が2026年7月時点の現在地。
Thinkingモードは時間を4.7〜8.7倍にし、num_ctx 32768 では qwen3.5系の成果物がゼロになった。

- 📕 テクニカルペーパー: [`paper/`](paper/)（PDF）
- 📊 測定結果: [`results/2026-07/`](results/2026-07/)
- 📝 解説ブログ / 🎥 動画: https://shabelab.com （リンクを追記）

| 測定対象 | F1中央値 | 実用グレード |
|---|---|---|
| qwen3.6:27b（ローカル最良） | **0.788** | C（B×1）— 人手レビュー必須 |
| Claude Fable 5（フロンティア最上位・参照） | 0.932 | B×5 |
| GPT-5.4-mini（Thinkingオフ・参照） | 0.677 | C×5 — **ローカルが上回る** |

---

## リポジトリ構成

```
dataset/            評価データセット（ものさし）… CC BY 4.0
  transcript.txt      合成会議トランスクリプト（33,401字 / 23,403トークン・完全フィクション）
  gold.json           正解37項目＋許容53件＋ノイズ4件（難易度タグ付き）
  schema.json         抽出JSONの形式定義
  dataset_notes.md    設計メモ（仕掛けと根拠位置）
  LICENSE             CC BY 4.0

scripts/            測定・採点スクリプト … MIT（ルートのLICENSE）
  run_thinking_bench.py   Thinking税測定（Ollama HTTP API）
  score.py                採点（JSON有効率・F1・難易度タグ別・捏造検出）
  run_frontier_eval.py    フロンティアモデル測定
  summarize.py            集計

results/            版ごとの測定結果
  2026-07/            CSV / summary.md / 生ログ / env.txt / notes.md

paper/              テクニカルペーパー（PDF・図）
```

Python 3.9 互換・標準ライブラリのみで動作します。

## データセットの特徴

実在企業の会議データは機密のため使えません。そこで**先に正解を設計し、それが埋まるように
トランスクリプトを逆算して書く**方式で、機密ゼロ・正解ラベル付きの評価データを構築しました。

- 架空企業の6時間相当の会議（話者6名・全て架空）
- 正解37項目（decisions 12 / action_items 12 / key_numbers 13）＋許容リスト53件
- 難易度タグ: `easy` / `medium` / `hard_size` / `hard_thinking` / `hard_implicit`
- 仕掛けの例: 複数ステップの計算（正解の数字が本文に一度も現れない）、離れた箇所への照応、
  会議中の訂正・上書き、暗黙の合意、昼休憩のノイズ数値（捏造検出用）
- 独立検証3回（うち2回 NO-GO）を経て修正済み。経緯は `dataset/dataset_notes.md` 参照

## 自分のモデルを評価する

```bash
# 1. Ollama でモデルを用意し、測定を実行
python3 scripts/run_thinking_bench.py --task extract

# 2. 採点
python3 scripts/score.py --logs logs/thinking_bench_extract

# 3. 集計
python3 scripts/summarize.py
```

採点基準（緩和マッチの定義・許容リストの扱い）は `scripts/score.py` 冒頭のコメントを参照。

## 版の方針 / Versioning

- **測定の版**: リリースタグ `v2026.07`, `v2027.01`, … で固定。論文・ブログからは
  タグ付きURLにリンクし、後のコミットで参照が壊れないようにする
- **データセットの版**: `gold.json` 内にバージョンを記載（現在 v1.2）。
  ものさしを変えた場合は必ず版を上げ、版をまたぐ比較はしない
- **誠実性ルール**: 数値の捏造・補完はしない。測れなかったものは「測れなかった」と記録する。
  各版の全判断・逸脱・想定外は `results/<版>/notes.md` に残す

## ライセンス

- `dataset/` … **CC BY 4.0**（`dataset/LICENSE`）。出典表示の上、自由に利用できます
- 上記以外（スクリプト等）… **MIT**（ルートの `LICENSE`）

## 引用 / Citation

```bibtex
@techreport{ohashi2026localminutes07,
  title       = {ローカルLLMで議事録は作れるか［2026年7月版］：64GB Mac mini・27Bモデルの実用性評価},
  author      = {大橋 功},
  institution = {株式会社喋ラボ},
  year        = {2026},
  url         = {https://github.com/kouohhashi/local-llm-minutes-eval}
}
```

## 関連

- 前作: [llm-quantization-benchmark](https://github.com/kouohhashi/llm-quantization-benchmark)
  — 日本語ビジネス文書タスクにおけるLLM量子化方式の比較評価（8GB VRAM環境）
- 株式会社喋ラボ: https://shabelab.com
