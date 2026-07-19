#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2段構え（テキスト要約 → JSON化）vs 1段構え（JSON直接出力）の比較

=============================================================================
検証する仮説
=============================================================================
JSON直接出力は「読解・整理・整形」を1回のパスで同時にやらせる二重課題である。
これを2回のリクエストに分けると、品質と書式の両方が安定する（実務者の経験知）。

本実験のデータから、この仮説を支持する状況証拠が既にある:
  GPT-5.4-mini  Thinkingオフ F1=0.677 / easy=0.86 / medium=0.43
                Thinkingオン F1=0.880 / easy=1.00 / medium=0.86
  → Thinkingは「思考領域で整理してから最後にJSONを吐く」という
    内部的な2段構えとして働いていると解釈できる。

だとすれば重要な含意がある:
  ローカルモデルは Thinking がコンテキスト上限で潰れるため内部的な分離ができない。
  **なら外部から明示的に分離すればよい。**

=============================================================================
設計
=============================================================================
■ 第1段: 会議録 → 自由記述のテキスト整理（JSONスキーマの制約なし）
■ 第2段: 第1段の出力のみ → JSON化
  ※ 第2段に元のトランスクリプトは渡さない。渡すと2段構えの意味がなくなる。

■ 第2段はプロンプトが小さい（数千トークン）ため、Thinkingに大きな余裕が生まれる。
  1段構えでは Thinking が上限で潰れていた（生成9,069トークンで打ち切り）が、
  第2段なら3万トークン近い余裕がある。多段算術・相対日付の導出が救われるかを見る。

■ 比較可能性のため、出力JSONの形式・抽出ルールは1段構え（run_thinking_bench.py の
  EXTRACT_INSTRUCTION）と完全に同一にする。差は「1回で書くか2回に分けるか」だけ。

=============================================================================
注意: Python 3.9 互換 / 標準ライブラリのみ
=============================================================================
使い方:
  # ローカル
  python3 scripts/run_twostage.py --backend ollama --models qwen3.6:27b --trials 5
  # フロンティア
  python3 scripts/run_twostage.py --backend frontier --models gpt-5.4-mini claude-sonnet-5 --trials 5
"""

import argparse
import csv
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATASET_DIR = os.path.join(ROOT, "dataset")
RESULTS_DIR = os.path.join(ROOT, "results")
LOGS_DIR = os.path.join(ROOT, "logs")

OLLAMA_HOST = "http://localhost:11434"
NUM_CTX = 32768
TIMEOUT_SEC = int(os.environ.get("BENCH_TIMEOUT_SEC", str(20 * 60)))
MAX_TOKENS = int(os.environ.get("FRONTIER_MAX_TOKENS", "48000"))
ANTHROPIC_VERSION = "2023-06-01"

# ---- 第1段: 自由記述での整理（JSONスキーマの制約を一切かけない）----
STAGE1_INSTRUCTION = """以下の会議トランスクリプトを読み、決定事項・アクションアイテム・重要数値を抽出してください。

抽出する内容:
- 決定事項: 決定内容を1文で / 決定した人物の氏名
- アクションアイテム: 実行すべき内容を1文で / 担当者の氏名 / 期限（YYYY-MM-DD）
- 重要数値: その数値が何を指すか / 数値と単位

抽出のルール:
- 決定事項・アクションアイテム・重要数値が複数ある場合は、該当するものを全て入れる。
- 検討中・保留のものは決定事項に含めない。確定したものだけを含める。
- 会議中に内容が変更・訂正された項目は、最終的な状態を書く。
- 「来週の金曜」のような相対的な期限表現は、会議日を基準に YYYY-MM-DD 形式の絶対日付に変換する。
- 氏名は会議中の表記どおりフルネームで書く。

--- 会議トランスクリプト ここから ---
{TRANSCRIPT}
--- 会議トランスクリプト ここまで ---
"""

# ---- 第2段: 第1段の出力のみをJSON化（元のトランスクリプトは渡さない）----
STAGE2_INSTRUCTION = """以下のメモを、下記のJSON形式に変換してください。

これは形式の変換だけを行う作業です。
内容の判断・追加・削除・要約・並べ替えは一切行わないでください。
メモに書かれている項目は、1つ残らず対応する配列に入れてください。

{
  "decisions": [
    {"id": "D1", "content": "決定内容", "decided_by": "決定した人物の氏名"}
  ],
  "action_items": [
    {"id": "A1", "task": "タスク内容", "assignee": "担当者の氏名", "due": "期限"}
  ],
  "key_numbers": [
    {"id": "N1", "label": "その数値が何を指すか", "value": "数値と単位"}
  ]
}

メモの「決定事項」は decisions、「アクションアイテム」は action_items、
「重要数値」は key_numbers に対応します。
id は各配列の先頭から D1, A1, N1 と順に振ってください。

出力はJSONのみとし、JSON以外の文章・説明・コードフェンスは一切出力しないでください。

--- メモ ここから ---
{MEMO}
--- メモ ここまで ---
"""

CSV_FIELDS = [
    "timestamp", "backend", "model", "stage2_thinking", "trial",
    "s1_prompt_tokens", "s1_output_tokens", "s1_time_s", "s1_stop",
    "s2_prompt_tokens", "s2_output_tokens", "s2_time_s", "s2_stop",
    "total_time_s", "notes",
]


def log(msg):
    print("[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def http_json(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- バックエンド

def call_ollama(model, prompt, thinking):
    started = time.time()
    d = http_json(OLLAMA_HOST + "/api/chat",
                  {"Content-Type": "application/json"},
                  {"model": model,
                   "messages": [{"role": "user", "content": prompt}],
                   "stream": False, "think": thinking,
                   "options": {"num_ctx": NUM_CTX}})
    return {"text": (d.get("message") or {}).get("content") or "",
            "prompt_tokens": d.get("prompt_eval_count"),
            "output_tokens": d.get("eval_count"),
            "stop": d.get("done_reason"),
            "elapsed": time.time() - started}


def call_anthropic(model, prompt, thinking):
    body = {"model": model, "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}]}
    # claude-fable-5 系は思考を無効化できないため thinking を指定しない
    if not model.startswith("claude-fable") and not model.startswith("claude-mythos"):
        body["thinking"] = {"type": "adaptive"} if thinking else {"type": "disabled"}
    started = time.time()
    d = http_json("https://api.anthropic.com/v1/messages",
                  {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                   "anthropic-version": ANTHROPIC_VERSION,
                   "content-type": "application/json"}, body)
    u = d.get("usage") or {}
    return {"text": "".join(b.get("text", "") for b in d.get("content", [])
                            if b.get("type") == "text"),
            "prompt_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
            "stop": d.get("stop_reason"), "elapsed": time.time() - started}


def call_openai(model, prompt, thinking):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": MAX_TOKENS,
            "reasoning_effort": "high" if thinking else "minimal"}
    started = time.time()
    try:
        d = http_json("https://api.openai.com/v1/chat/completions",
                      {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
                       "content-type": "application/json"}, body)
    except urllib.error.HTTPError:
        body.pop("reasoning_effort")
        d = http_json("https://api.openai.com/v1/chat/completions",
                      {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
                       "content-type": "application/json"}, body)
    ch = (d.get("choices") or [{}])[0]
    u = d.get("usage") or {}
    return {"text": (ch.get("message") or {}).get("content") or "",
            "prompt_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens"),
            "stop": ch.get("finish_reason"), "elapsed": time.time() - started}


def dispatch(model):
    if model.startswith("claude"):
        return call_anthropic
    if model.startswith("gpt"):
        return call_openai
    return call_ollama


# ---------------------------------------------------------------- メイン

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--stage2-thinking", default="both",
                    choices=["both", "on", "off"],
                    help="第2段のThinking。第1段は常にオフ（公平性のため）")
    args = ap.parse_args()

    with open(os.path.join(DATASET_DIR, "transcript.txt"), encoding="utf-8") as f:
        transcript = f.read()
    s1_prompt = STAGE1_INSTRUCTION.replace("{TRANSCRIPT}", transcript)

    modes = {"both": [False, True], "on": [True], "off": [False]}[args.stage2_thinking]
    raw_dir = os.path.join(LOGS_DIR, "twostage")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    out_csv = os.path.join(RESULTS_DIR, "twostage.csv")
    is_new = not os.path.exists(out_csv)
    f = open(out_csv, "a", newline="", encoding="utf-8")
    w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
    if is_new:
        w.writeheader(); f.flush()

    total = len(args.models) * len(modes) * args.trials
    done = 0
    for model in args.models:
        call = dispatch(model)
        backend = ("ollama" if call is call_ollama else
                   "anthropic" if call is call_anthropic else "openai")
        if backend == "ollama":
            subprocess.run(["ollama", "stop", model], check=False,
                           capture_output=True, timeout=60)
        for s2think in modes:
            for trial in range(1, args.trials + 1):
                done += 1
                log("(%d/%d) %s 第2段think=%s trial=%d" % (done, total, model, s2think, trial))
                ts = datetime.datetime.now().isoformat(timespec="seconds")
                t0 = time.time()
                try:
                    # 第1段: 常にThinkingオフ（1段構えとの比較を公平にするため）
                    r1 = call(model, s1_prompt, False)
                    log("   第1段: out=%s stop=%s (%.1fs)"
                        % (r1["output_tokens"], r1["stop"], r1["elapsed"]))
                    if not r1["text"].strip():
                        raise RuntimeError("第1段の出力が空")
                    # 第2段: 第1段の出力のみを渡す（元のトランスクリプトは渡さない）
                    s2_prompt = STAGE2_INSTRUCTION.replace("{MEMO}", r1["text"])
                    r2 = call(model, s2_prompt, s2think)
                    log("   第2段: prompt=%s out=%s stop=%s (%.1fs)"
                        % (r2["prompt_tokens"], r2["output_tokens"], r2["stop"], r2["elapsed"]))
                except Exception as e:
                    log("   -> 失敗: %s" % e)
                    w.writerow({"timestamp": ts, "backend": backend, "model": model,
                                "stage2_thinking": s2think, "trial": trial,
                                "notes": "FAILED_%s:%s" % (type(e).__name__, str(e)[:150])})
                    f.flush(); continue

                stem = "%s_s2think-%s_trial%d" % (model.replace(":", "-").replace("/", "-"),
                                                  s2think, trial)
                # score.py がそのまま採点できる形で保存する
                with open(os.path.join(raw_dir, stem + ".json"), "w", encoding="utf-8") as jf:
                    json.dump({"message": {"content": r2["text"]},
                               "done_reason": ("length" if r2["stop"] in ("length", "max_tokens")
                                               else "stop"),
                               "eval_count": r2["output_tokens"],
                               "prompt_eval_count": r2["prompt_tokens"],
                               "_stage1_text": r1["text"]}, jf, ensure_ascii=False, indent=2)

                w.writerow({
                    "timestamp": ts, "backend": backend, "model": model,
                    "stage2_thinking": s2think, "trial": trial,
                    "s1_prompt_tokens": r1["prompt_tokens"], "s1_output_tokens": r1["output_tokens"],
                    "s1_time_s": r1["elapsed"], "s1_stop": r1["stop"],
                    "s2_prompt_tokens": r2["prompt_tokens"], "s2_output_tokens": r2["output_tokens"],
                    "s2_time_s": r2["elapsed"], "s2_stop": r2["stop"],
                    "total_time_s": time.time() - t0, "notes": ""})
                f.flush()
        if backend == "ollama":
            subprocess.run(["ollama", "stop", model], check=False,
                           capture_output=True, timeout=60)
    f.close()
    log("完了。CSV: %s / 生ログ: %s" % (out_csv, raw_dir))
    log("採点: python3 scripts/score.py --logs %s --out %s"
        % (raw_dir, os.path.join(RESULTS_DIR, "quality_eval_twostage.csv")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
