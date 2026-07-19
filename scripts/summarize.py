#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
集計スクリプト: 各CSVから summary.md（Markdown表）を生成する。

誠実性ルール:
  - 代表値は中央値。ばらつきは min/max を併記する（指示書 §1）。
  - 見栄えのための丸め・整形はしない。表示上の桁数は落とすが、CSVの生値は一切変更しない。
  - 欠損（測れなかった値）は「—」と表示し、0や推定値で埋めない。
  - 打ち切り（タイムアウト）や出力切れの件数を明示する。

注意: Python 3.9 互換で書くこと。

使い方:
  python3 scripts/summarize.py
"""

import csv
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(ROOT, "results")

MODEL_ORDER = ["qwen3.5:4b", "qwen3.5:9b", "qwen3.5:27b", "qwen3.6:27b"]


def read_csv(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fnum(row, key):
    """CSVのセルを float にする。空欄・解釈不能は None（捏造しない）。"""
    v = (row.get(key) or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def stats(values):
    """(中央値, min, max, n) を返す。値がなければ全て None。"""
    vs = [v for v in values if v is not None]
    if not vs:
        return None, None, None, 0
    return statistics.median(vs), min(vs), max(vs), len(vs)


def cell(med, lo, hi, fmt="%.1f"):
    if med is None:
        return "—"
    return ("%s <br><span style=\"font-size:85%%\">(%s–%s)</span>"
            % (fmt % med, fmt % lo, fmt % hi))


def plain(med, fmt="%.1f"):
    return "—" if med is None else (fmt % med)


# ---------------------------------------------------------------- フェーズ2

def section_thinking(task):
    path = os.path.join(RESULTS, "thinking_bench_%s.csv" % task)
    rows = read_csv(path)
    if not rows:
        return "\n（`%s` が未生成のため、この節は未測定）\n" % os.path.basename(path)

    out = []
    models = [m for m in MODEL_ORDER if any(r["model"] == m for r in rows)]
    models += sorted(set(r["model"] for r in rows) - set(models))

    out.append("\n#### 総時間・生成トークン（中央値、括弧内は min–max）\n")
    out.append("| モデル | Thinking | n | 総時間 (s) | 生成トークン | eval時間 (s) | tok/s | 失敗/打切 |")
    out.append("|---|---|---|---|---|---|---|---|")

    tax = {}
    for m in models:
        for th in ("False", "True"):
            sub = [r for r in rows if r["model"] == m and r["thinking"] == th]
            if not sub:
                continue
            failed = [r for r in sub if (r.get("notes") or "").startswith("FAILED")]
            okrows = [r for r in sub if r not in failed]

            td, td_lo, td_hi, n = stats([fnum(r, "total_duration_s") for r in okrows])
            ec, ec_lo, ec_hi, _ = stats([fnum(r, "eval_count") for r in okrows])
            ed, ed_lo, ed_hi, _ = stats([fnum(r, "eval_duration_s") for r in okrows])
            tp, tp_lo, tp_hi, _ = stats([fnum(r, "tokens_per_sec") for r in okrows])

            tax.setdefault(m, {})[th] = {"total": td, "eval_count": ec}

            out.append("| %s | %s | %d | %s | %s | %s | %s | %d |" % (
                m, "on" if th == "True" else "off", n,
                cell(td, td_lo, td_hi, "%.1f"),
                cell(ec, ec_lo, ec_hi, "%.0f"),
                cell(ed, ed_lo, ed_hi, "%.1f"),
                cell(tp, tp_lo, tp_hi, "%.2f"),
                len(failed)))

    out.append("\n#### Thinking税率\n")
    out.append("Thinking税率 = 時間(on)の中央値 ÷ 時間(off)の中央値。生成トークン倍率も併記する。\n")
    out.append("| モデル | 時間(off) s | 時間(on) s | **時間の税率** | トークン(off) | トークン(on) | トークン倍率 |")
    out.append("|---|---|---|---|---|---|---|")
    for m in models:
        d = tax.get(m, {})
        off, on = d.get("False", {}), d.get("True", {})
        t_off, t_on = off.get("total"), on.get("total")
        e_off, e_on = off.get("eval_count"), on.get("eval_count")
        ratio = ("%.2f 倍" % (t_on / t_off)) if (t_off and t_on) else "—"
        eratio = ("%.2f 倍" % (e_on / e_off)) if (e_off and e_on) else "—"
        out.append("| %s | %s | %s | **%s** | %s | %s | %s |" % (
            m, plain(t_off), plain(t_on), ratio,
            plain(e_off, "%.0f"), plain(e_on, "%.0f"), eratio))

    nfail = sum(1 for r in rows if (r.get("notes") or "").startswith("FAILED"))
    if nfail:
        out.append("\n> 失敗・打ち切りが %d 件あった。詳細は CSV の notes 列と logs/ を参照。"
                   "打ち切り自体がデータであるため除外せず記録している。" % nfail)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- フェーズ3

def section_quality():
    rows = read_csv(os.path.join(RESULTS, "quality_eval.csv"))
    if not rows:
        return "\n（`quality_eval.csv` が未生成のため、この節は未測定）\n"

    out = []
    models = [m for m in MODEL_ORDER if any(r["model"] == m for r in rows)]
    models += sorted(set(r["model"] for r in rows) - set(models))

    out.append("\n#### JSON有効率と全体F1\n")
    out.append("| モデル | Thinking | n | JSON有効率 | 出力切れ | F1(全体) | F1(decisions) | F1(action_items) | F1(key_numbers) |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for m in models:
        for th in ("False", "True"):
            sub = [r for r in rows if r["model"] == m and r["thinking"] == th]
            if not sub:
                continue
            n = len(sub)
            valid = sum(1 for r in sub if r["json_valid"] == "True")
            trunc = sum(1 for r in sub if r.get("truncated") == "True")
            f1, _, _, _ = stats([fnum(r, "f1_overall") for r in sub])
            f1d, _, _, _ = stats([fnum(r, "f1_decisions") for r in sub])
            f1a, _, _, _ = stats([fnum(r, "f1_action_items") for r in sub])
            f1n, _, _, _ = stats([fnum(r, "f1_key_numbers") for r in sub])
            out.append("| %s | %s | %d | %d/%d | %d | %s | %s | %s | %s |" % (
                m, "on" if th == "True" else "off", n, valid, n, trunc,
                plain(f1, "%.3f"), plain(f1d, "%.3f"),
                plain(f1a, "%.3f"), plain(f1n, "%.3f")))

    out.append("\n#### 難易度タグ別の正答率（中央値）— 考察の核\n")
    out.append("| モデル | Thinking | easy | medium | hard_size | hard_thinking |")
    out.append("|---|---|---|---|---|---|")
    for m in models:
        for th in ("False", "True"):
            sub = [r for r in rows if r["model"] == m and r["thinking"] == th]
            if not sub:
                continue
            vals = []
            for d in ("easy", "medium", "hard_size", "hard_thinking"):
                v, _, _, _ = stats([fnum(r, "acc_" + d) for r in sub])
                vals.append(plain(v, "%.2f"))
            out.append("| %s | %s | %s | %s | %s | %s |" % (
                m, "on" if th == "True" else "off",
                vals[0], vals[1], vals[2], vals[3]))

    out.append("\n**検証すべき仮説**（外れた場合も知見として notes.md に記録する）:")
    out.append("- H1: `hard_thinking` は Thinking オンでのみ正答率が上がるか")
    out.append("- H2: `hard_size` は 27B でのみ取れるか")
    out.append("- H3: `easy` は全モデル・全条件でほぼ満点か（満点でなければタスク設計かモデルに根本問題）")

    ntrunc = sum(1 for r in rows if r.get("truncated") == "True")
    if ntrunc:
        out.append("\n> ⚠ 出力切れ（`done_reason=length`）が %d 件あった。"
                   "これは Thinking の質ではなくコンテキスト溢れを意味するため、"
                   "Thinking の効果と混同しないこと。" % ntrunc)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- フェーズ4

def section_mlx():
    rows = read_csv(os.path.join(RESULTS, "mlx_compare.csv"))
    if not rows:
        return "\n（`mlx_compare.csv` が未生成のため、この節は未測定）\n"

    out = []
    out.append("\n| バックエンド | モデル | n | 生成トークン | 生成時間 (s) | tok/s | ピークメモリ (GB) |")
    out.append("|---|---|---|---|---|---|---|")
    keys = []
    for r in rows:
        k = (r.get("backend"), r.get("model"))
        if k not in keys:
            keys.append(k)
    for backend, model in keys:
        sub = [r for r in rows if r.get("backend") == backend and r.get("model") == model]
        gt, gt_lo, gt_hi, n = stats([fnum(r, "gen_tokens") for r in sub])
        gs, gs_lo, gs_hi, _ = stats([fnum(r, "gen_time_s") for r in sub])
        tp, tp_lo, tp_hi, _ = stats([fnum(r, "tokens_per_sec") for r in sub])
        pm, pm_lo, pm_hi, _ = stats([fnum(r, "peak_memory_gb") for r in sub])
        out.append("| %s | %s | %d | %s | %s | %s | %s |" % (
            backend, model, n,
            cell(gt, gt_lo, gt_hi, "%.0f"), cell(gs, gs_lo, gs_hi, "%.1f"),
            cell(tp, tp_lo, tp_hi, "%.2f"), cell(pm, pm_lo, pm_hi, "%.1f")))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- メイン

def main():
    parts = []
    parts.append("# 実験結果サマリ\n")
    parts.append("本ファイルは `scripts/summarize.py` が results/ 配下のCSVから自動生成する。")
    parts.append("代表値は**中央値**、括弧内は **min–max**。欠損は「—」で示し、推定値では埋めていない。")
    parts.append("実験環境は `env.txt`、実施中の判断・逸脱・想定外は `notes.md` を参照。\n")

    parts.append("\n## フェーズ2: Thinking税（主タスク: 会議録からの構造化抽出）")
    parts.append(section_thinking("extract"))

    parts.append("\n## フェーズ2補助: Thinking税（短い固定タスク: リモートワークの生産性300字要約）")
    parts.append("先行実測（前作の動画・記事）との接続を取るための補助タスク。")
    parts.append(section_thinking("summary300"))

    parts.append("\n## フェーズ3: 品質評価")
    parts.append(section_quality())

    parts.append("\n## フェーズ4: MLX vs Ollama")
    parts.append("目的は精密比較ではなく「差が大きいか小さいか」の確認。")
    parts.append(section_mlx())

    text = "\n".join(parts)
    path = os.path.join(RESULTS, "summary.md")
    os.makedirs(RESULTS, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print("書き出し: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
