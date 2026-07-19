#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
フェーズ2: Thinking税の測定

対象: 4モデル x Thinking{on, off} x 5試行 = 40実行
Ollama HTTP API (/api/chat) を使用。num_ctx は 32768 固定。

誠実性ルール:
  - 数値は Ollama API が返した生の値をそのまま記録する。丸め・整形・補完は行わない。
  - 取得できなかった値は空欄にし、notes 列に理由を書く。
  - タイムアウトした実行も「打ち切り」として1行記録する（打ち切り自体がデータ）。
  - 全実行の生レスポンスを logs/ に JSON で保存する。

注意: Python 3.9 互換で書くこと（システム同梱版が 3.9.6 のため）。
      match 文・`X | Y` 型注釈・`list[str]` 記法・外部ライブラリは使わない。

使い方:
  python3 scripts/run_thinking_bench.py --task extract
  python3 scripts/run_thinking_bench.py --task summary300
  python3 scripts/run_thinking_bench.py --task extract --models qwen3.5:4b --trials 1   # 疎通確認用
"""

import argparse
import csv
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 設定

OLLAMA_HOST = "http://localhost:11434"
NUM_CTX = 32768
# 指示書 §5 は「1実行20分で打ち切り」。ただし num_ctx を 65536 に上げる追加測定では、
# モデルが上限まで生成し続けた場合の所要時間が 20分を超えることが事前計算で分かっている
# （27B: 41,837トークン ÷ 10.5 tok/s ≒ 66分）。20分で切ると「打ち切られた」ことしか
# 分からず、実際に何トークン使ったのかという肝心の情報が失われる。
# そのため環境変数で延長できるようにした。既定は指示書どおり20分。
TIMEOUT_SEC = int(os.environ.get("BENCH_TIMEOUT_SEC", str(20 * 60)))

MODELS = ["qwen3.5:4b", "qwen3.5:9b", "qwen3.5:27b", "qwen3.6:27b"]
THINKING_MODES = [False, True]
DEFAULT_TRIALS = 5

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATASET_DIR = os.path.join(ROOT, "dataset")
RESULTS_DIR = os.path.join(ROOT, "results")
LOGS_DIR = os.path.join(ROOT, "logs")

CSV_FIELDS = [
    "timestamp", "model", "thinking", "task", "trial",
    "prompt_eval_count", "eval_count",
    "eval_duration_s", "total_duration_s", "tokens_per_sec",
    "num_ctx", "ollama_ps_size_gb", "notes",
]

# ---------------------------------------------------------------- プロンプト

# タスクA: 会議録からの構造化抽出（主タスク）
EXTRACT_INSTRUCTION = """以下の会議トランスクリプトを読み、決定事項・アクションアイテム・重要数値を抽出してください。

出力は次の形式のJSONのみとし、JSON以外の文章・説明・コードフェンスは一切出力しないでください。

{
  "decisions": [
    {"id": "D1", "content": "決定内容を1文で", "decided_by": "決定した人物の氏名"}
  ],
  "action_items": [
    {"id": "A1", "task": "実行すべき内容を1文で", "assignee": "担当者の氏名", "due": "YYYY-MM-DD"}
  ],
  "key_numbers": [
    {"id": "N1", "label": "その数値が何を指すか", "value": "数値と単位"}
  ]
}

抽出のルール:
- 検討中・保留のものは decisions に含めない。確定したものだけを含める。
- 会議中に内容が変更・訂正された項目は、最終的な状態を書く。
- 「来週の金曜」のような相対的な期限表現は、会議日を基準に YYYY-MM-DD 形式の絶対日付に変換する。
- 氏名は会議中の表記どおりフルネームで書く。

--- 会議トランスクリプト ここから ---
{TRANSCRIPT}
--- 会議トランスクリプト ここまで ---
"""


# タスクA' : タスクAに「導出せよ」という指示を1行だけ加えたもの。
# 目的: 「本文に literal に書かれていない値を導出させるには、明示的な指示が要るのか」を測る。
# 背景（実測に基づく）: 現行プロンプトは相対日付についてのみ導出を明示している。
#   その結果、hard_thinking 内で以下のように綺麗に割れた（フロンティア45試行）:
#     相対日付→絶対日付（指示あり）: A6=96% / A7=89%
#     多段算術（指示なし）        : N7=20% / N6=2% / N8=0%
#   どちらも「本文に無い値を導出する」同種の作業で、違いは指示の有無だけだった。
#   そこで導出ルールを1行だけ足したB版を作り、A/B比較する。
# 注意: 特定の問題を名指しする例（「1.2倍を掛けよ」等）は入れない。
#       それは答えを教えることになり、測定として無意味になるため。
EXTRACT_DERIVE_INSTRUCTION = EXTRACT_INSTRUCTION.replace(
    "- 氏名は会議中の表記どおりフルネームで書く。",
    "- 氏名は会議中の表記どおりフルネームで書く。\n"
    "- 重要数値は、最終的な値が本文中にそのまま述べられていない場合でも、本文中の情報から計算で確定できるなら計算した結果を書く。計算できない場合のみ、述べられたとおりに書く。")


# タスクA'' : タスクAに「複数ある場合は全て入れる」という網羅指示を1行加えたもの。
# 2段構え（run_twostage.py）の第1段にも同じ行を入れているため、
# 「1段 vs 2段」の比較で差分が『形式指示の有無』だけになるよう揃えるためのもの。
EXTRACT_ALL_INSTRUCTION = EXTRACT_INSTRUCTION.replace(
    "- 検討中・保留のものは decisions に含めない。確定したものだけを含める。",
    "- 決定事項・アクションアイテム・重要数値が複数ある場合は、該当するものを全て入れる。\n"
    "- 検討中・保留のものは decisions に含めない。確定したものだけを含める。")

# タスクB: 短い固定タスク（先行実測との接続用。指示書 §5 の補助タスク）
SUMMARY300_INSTRUCTION = "リモートワークの生産性について、300字で要約してください。"


def build_prompt(task):
    """タスク名からプロンプト本文を組み立てて返す。"""
    if task in ("extract", "extract_derive", "extract_all"):
        path = os.path.join(DATASET_DIR, "transcript.txt")
        with open(path, encoding="utf-8") as f:
            transcript = f.read()
        base = {"extract": EXTRACT_INSTRUCTION,
                "extract_derive": EXTRACT_DERIVE_INSTRUCTION,
                "extract_all": EXTRACT_ALL_INSTRUCTION}[task]
        # str.format() は本文中の { } を壊すので使わない
        return base.replace("{TRANSCRIPT}", transcript)
    if task == "summary300":
        return SUMMARY300_INSTRUCTION
    raise ValueError("unknown task: " + task)


# ---------------------------------------------------------------- Ollama 操作

def ollama_stop(model):
    """モデルをアンロードしてメモリ状態をリセットする（指示書 §2）。"""
    try:
        subprocess.run(["ollama", "stop", model], check=False,
                       capture_output=True, timeout=60)
    except Exception as e:
        log("  [warn] ollama stop %s に失敗: %s" % (model, e))


def ollama_ps_size_gb(model):
    """`ollama ps` の SIZE 列を GB の float で返す。取得できなければ None。

    捏造を避けるため、パースできなかった場合は推定値を入れずに None を返す。
    """
    try:
        out = subprocess.run(["ollama", "ps"], capture_output=True,
                             timeout=60, encoding="utf-8").stdout
    except Exception as e:
        log("  [warn] ollama ps に失敗: %s" % e)
        return None, None
    for line in out.splitlines():
        if line.startswith(model):
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(GB|MB)", line)
            if not m:
                return None, line.strip()
            val = float(m.group(1))
            if m.group(2) == "MB":
                val = val / 1024.0
            return val, line.strip()
    return None, None


def chat(model, prompt, think, num_ctx):
    """/api/chat を1回叩き、(レスポンスdict, 経過秒, エラー文字列) を返す。"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": think,
        "options": {"num_ctx": num_ctx},
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_HOST + "/api/chat", data=body,
        headers={"Content-Type": "application/json"})

    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data, time.time() - started, None
    except Exception as e:
        elapsed = time.time() - started
        # socket.timeout / urllib の timeout はここに落ちる
        kind = "timeout" if elapsed >= TIMEOUT_SEC - 5 else type(e).__name__
        return None, elapsed, "%s: %s" % (kind, e)


# ---------------------------------------------------------------- ユーティリティ

def log(msg):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    print("[%s] %s" % (stamp, msg), flush=True)


def ns_to_s(v):
    """ナノ秒を秒に変換。None はそのまま返す（捏造しない）。"""
    if v is None:
        return None
    return v / 1e9


def fmt(v):
    """CSV 用。None は空欄（未取得であることを空欄で表す）。丸めは行わない。"""
    return "" if v is None else v


# ---------------------------------------------------------------- メイン

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="extract", choices=["extract", "extract_derive", "extract_all", "summary300"])
    ap.add_argument("--models", nargs="*", default=MODELS)
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--num-ctx", type=int, default=NUM_CTX,
                    help="コンテキスト長。既定は指示書指定の32768。"
                         "Thinkingオン時に出力が入り切らない問題の検証で65536も測る。")
    ap.add_argument("--thinking", default="both", choices=["both", "on", "off"],
                    help="測定するThinkingモード。65536の追加測定ではonのみ測る想定。")
    ap.add_argument("--out", default=None, help="CSV出力先（既定: results/thinking_bench_<task>.csv）")
    args = ap.parse_args()

    modes = {"both": [False, True], "on": [True], "off": [False]}[args.thinking]

    prompt = build_prompt(args.task)
    log("task=%s / プロンプト長=%d文字 / models=%s / trials=%d / num_ctx=%d / thinking=%s"
        % (args.task, len(prompt), ",".join(args.models), args.trials,
           args.num_ctx, args.thinking))

    # num_ctx ごとにログと CSV を分ける（32768 と 65536 の結果を混ぜないため）
    suffix = args.task if args.num_ctx == NUM_CTX else "%s_ctx%d" % (args.task, args.num_ctx)
    raw_dir = os.path.join(LOGS_DIR, "thinking_bench_" + suffix)
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    out_csv = args.out or os.path.join(RESULTS_DIR, "thinking_bench_%s.csv" % suffix)
    # 追記モード: 途中で落ちても再開できるようにする
    is_new = not os.path.exists(out_csv)
    csv_f = open(out_csv, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_f, fieldnames=CSV_FIELDS)
    if is_new:
        writer.writeheader()
        csv_f.flush()

    total = len(args.models) * len(modes) * args.trials
    done = 0

    for model in args.models:
        log("=" * 60)
        log("モデル: %s" % model)
        ollama_stop(model)  # 測定前にメモリ状態をリセット

        for think in modes:
            ps_size, ps_line = None, None

            for trial in range(1, args.trials + 1):
                done += 1
                label = "%s think=%s trial=%d" % (model, think, trial)
                log("(%d/%d) %s ..." % (done, total, label))

                ts = datetime.datetime.now().isoformat(timespec="seconds")
                data, elapsed, err = chat(model, prompt, think, args.num_ctx)

                # 各条件で1回だけ ollama ps を記録（指示書 §2）
                if ps_size is None and ps_line is None:
                    ps_size, ps_line = ollama_ps_size_gb(model)
                    if ps_line:
                        log("  ollama ps: %s" % ps_line)

                stem = "%s_think-%s_trial%d" % (model.replace(":", "-"), think, trial)
                notes = []

                if data is None:
                    # 打ち切り・エラーも1行記録する（測れなかったことを記録する）
                    notes.append("FAILED_" + (err or "unknown"))
                    log("  -> 失敗: %s (%.1fs)" % (err, elapsed))
                    row = {
                        "timestamp": ts, "model": model, "thinking": think,
                        "task": args.task, "trial": trial,
                        "prompt_eval_count": "", "eval_count": "",
                        "eval_duration_s": "", "total_duration_s": elapsed,
                        "tokens_per_sec": "", "num_ctx": args.num_ctx,
                        "ollama_ps_size_gb": fmt(ps_size),
                        "notes": ";".join(notes),
                    }
                    with open(os.path.join(raw_dir, stem + ".error.txt"), "w",
                              encoding="utf-8") as f:
                        f.write("%s\nelapsed=%.3f\n" % (err, elapsed))
                else:
                    # 生レスポンスを丸ごと保存（後の監査・フェーズ3の採点に使う）
                    with open(os.path.join(raw_dir, stem + ".json"), "w",
                              encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)

                    pe = data.get("prompt_eval_count")
                    ec = data.get("eval_count")
                    ed = ns_to_s(data.get("eval_duration"))
                    td = ns_to_s(data.get("total_duration"))

                    # tok/s は eval_count / eval_duration。どちらか欠けたら算出しない
                    tps = None
                    if ec is not None and ed:
                        tps = ec / ed

                    if data.get("done_reason") not in (None, "stop"):
                        notes.append("done_reason=" + str(data.get("done_reason")))
                    msg = data.get("message") or {}
                    has_thinking = bool(msg.get("thinking"))
                    if think and not has_thinking:
                        notes.append("WARN_think-on-but-no-thinking-field")
                    if (not think) and has_thinking:
                        notes.append("WARN_think-off-but-thinking-field-present")
                    if pe is not None and pe >= args.num_ctx:
                        notes.append("WARN_prompt_eval_count>=num_ctx")

                    log("  -> prompt=%s eval=%s eval_time=%.2fs total=%.2fs tok/s=%s"
                        % (pe, ec, ed or -1, td or -1,
                           ("%.2f" % tps) if tps is not None else "n/a"))

                    row = {
                        "timestamp": ts, "model": model, "thinking": think,
                        "task": args.task, "trial": trial,
                        "prompt_eval_count": fmt(pe), "eval_count": fmt(ec),
                        "eval_duration_s": fmt(ed), "total_duration_s": fmt(td),
                        "tokens_per_sec": fmt(tps), "num_ctx": args.num_ctx,
                        "ollama_ps_size_gb": fmt(ps_size),
                        "notes": ";".join(notes),
                    }

                writer.writerow(row)
                csv_f.flush()

        ollama_stop(model)  # 測定後にもアンロード
        log("モデル %s 完了、アンロード済み" % model)

    csv_f.close()
    log("=" * 60)
    log("全 %d 実行が完了。CSV: %s" % (total, out_csv))
    log("生ログ: %s" % raw_dir)


if __name__ == "__main__":
    sys.exit(main())
