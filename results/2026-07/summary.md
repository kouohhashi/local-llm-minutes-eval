# 実験結果サマリ

本ファイルは `scripts/summarize.py` が results/ 配下のCSVから自動生成する。
代表値は**中央値**、括弧内は **min–max**。欠損は「—」で示し、推定値では埋めていない。
実験環境は `env.txt`、実施中の判断・逸脱・想定外は `notes.md` を参照。


## フェーズ2: Thinking税（主タスク: 会議録からの構造化抽出）

#### 総時間・生成トークン（中央値、括弧内は min–max）

| モデル | Thinking | n | 総時間 (s) | 生成トークン | eval時間 (s) | tok/s | 失敗/打切 |
|---|---|---|---|---|---|---|---|
| qwen3.5:4b | off | 5 | 24.7 <br><span style="font-size:85%">(15.4–71.2)</span> | 1005 <br><span style="font-size:85%">(667–1435)</span> | 22.8 <br><span style="font-size:85%">(15.1–32.7)</span> | 44.06 <br><span style="font-size:85%">(43.92–44.09)</span> | 0 |
| qwen3.5:4b | on | 5 | 215.9 <br><span style="font-size:85%">(215.8–216.1)</span> | 9069 <br><span style="font-size:85%">(9069–9069)</span> | 215.7 <br><span style="font-size:85%">(215.6–215.8)</span> | 42.05 <br><span style="font-size:85%">(42.02–42.06)</span> | 0 |
| qwen3.5:9b | off | 5 | 47.1 <br><span style="font-size:85%">(41.7–138.0)</span> | 1469 <br><span style="font-size:85%">(1299–1692)</span> | 46.7 <br><span style="font-size:85%">(41.3–53.9)</span> | 31.42 <br><span style="font-size:85%">(31.37–31.44)</span> | 0 |
| qwen3.5:9b | on | 5 | 297.9 <br><span style="font-size:85%">(145.2–298.4)</span> | 9069 <br><span style="font-size:85%">(4496–9069)</span> | 297.6 <br><span style="font-size:85%">(144.9–298.1)</span> | 30.47 <br><span style="font-size:85%">(30.42–31.03)</span> | 0 |
| qwen3.5:27b | off | 5 | 151.7 <br><span style="font-size:85%">(108.1–403.7)</span> | 1604 <br><span style="font-size:85%">(1160–1852)</span> | 148.8 <br><span style="font-size:85%">(107.4–171.9)</span> | 10.78 <br><span style="font-size:85%">(10.77–10.80)</span> | 0 |
| qwen3.5:27b | on | 5 | 865.3 <br><span style="font-size:85%">(864.2–869.5)</span> | 9069 <br><span style="font-size:85%">(9069–9069)</span> | 864.9 <br><span style="font-size:85%">(863.8–869.1)</span> | 10.49 <br><span style="font-size:85%">(10.43–10.50)</span> | 0 |
| qwen3.6:27b | off | 5 | 156.9 <br><span style="font-size:85%">(138.7–440.0)</span> | 1673 <br><span style="font-size:85%">(1479–1925)</span> | 156.3 <br><span style="font-size:85%">(138.1–180.0)</span> | 10.70 <br><span style="font-size:85%">(10.69–10.71)</span> | 0 |
| qwen3.6:27b | on | 5 | 733.6 <br><span style="font-size:85%">(690.3–871.6)</span> | 7725 <br><span style="font-size:85%">(7287–9069)</span> | 733.2 <br><span style="font-size:85%">(689.9–871.3)</span> | 10.47 <br><span style="font-size:85%">(10.41–10.56)</span> | 0 |

#### Thinking税率

Thinking税率 = 時間(on)の中央値 ÷ 時間(off)の中央値。生成トークン倍率も併記する。

| モデル | 時間(off) s | 時間(on) s | **時間の税率** | トークン(off) | トークン(on) | トークン倍率 |
|---|---|---|---|---|---|---|
| qwen3.5:4b | 24.7 | 215.9 | **8.73 倍** | 1005 | 9069 | 9.02 倍 |
| qwen3.5:9b | 47.1 | 297.9 | **6.33 倍** | 1469 | 9069 | 6.17 倍 |
| qwen3.5:27b | 151.7 | 865.3 | **5.70 倍** | 1604 | 9069 | 5.65 倍 |
| qwen3.6:27b | 156.9 | 733.6 | **4.68 倍** | 1673 | 7725 | 4.62 倍 |


## フェーズ2補助: Thinking税（短い固定タスク: リモートワークの生産性300字要約）
先行実測（前作の動画・記事）との接続を取るための補助タスク。

（`thinking_bench_summary300.csv` が未生成のため、この節は未測定）


## フェーズ3: 品質評価

#### JSON有効率と全体F1

| モデル | Thinking | n | JSON有効率 | 出力切れ | F1(全体) | F1(decisions) | F1(action_items) | F1(key_numbers) |
|---|---|---|---|---|---|---|---|---|
| qwen3.5:4b | off | 5 | 5/5 | 0 | 0.431 | 0.286 | 0.571 | 0.375 |
| qwen3.5:4b | on | 5 | 0/5 | 5 | 0.000 | 0.000 | 0.000 | 0.000 |
| qwen3.5:9b | off | 5 | 5/5 | 0 | 0.561 | 0.400 | 0.636 | 0.526 |
| qwen3.5:9b | on | 5 | 1/5 | 4 | 0.000 | 0.000 | 0.000 | 0.000 |
| qwen3.5:27b | off | 5 | 4/5 | 0 | 0.635 | 0.588 | 0.783 | 0.667 |
| qwen3.5:27b | on | 5 | 0/5 | 5 | 0.000 | 0.000 | 0.000 | 0.000 |
| qwen3.6:27b | off | 5 | 5/5 | 0 | 0.788 | 0.818 | 0.870 | 0.667 |
| qwen3.6:27b | on | 5 | 4/5 | 1 | 0.781 | 0.737 | 0.870 | 0.700 |

#### 難易度タグ別の正答率（中央値）— 考察の核

| モデル | Thinking | easy | medium | hard_size | hard_thinking |
|---|---|---|---|---|---|
| qwen3.5:4b | off | 0.57 | 0.00 | 0.60 | 0.00 |
| qwen3.5:4b | on | 0.00 | 0.00 | 0.00 | 0.00 |
| qwen3.5:9b | off | 0.71 | 0.14 | 0.60 | 0.14 |
| qwen3.5:9b | on | 0.00 | 0.00 | 0.00 | 0.00 |
| qwen3.5:27b | off | 0.86 | 0.29 | 0.60 | 0.29 |
| qwen3.5:27b | on | 0.00 | 0.00 | 0.00 | 0.00 |
| qwen3.6:27b | off | 0.93 | 0.57 | 0.80 | 0.29 |
| qwen3.6:27b | on | 0.93 | 0.57 | 0.60 | 0.43 |

**検証すべき仮説**（外れた場合も知見として notes.md に記録する）:
- H1: `hard_thinking` は Thinking オンでのみ正答率が上がるか
- H2: `hard_size` は 27B でのみ取れるか
- H3: `easy` は全モデル・全条件でほぼ満点か（満点でなければタスク設計かモデルに根本問題）

> ⚠ 出力切れ（`done_reason=length`）が 15 件あった。これは Thinking の質ではなくコンテキスト溢れを意味するため、Thinking の効果と混同しないこと。


## フェーズ4: MLX vs Ollama
目的は精密比較ではなく「差が大きいか小さいか」の確認。

（`mlx_compare.csv` が未生成のため、この節は未測定）
