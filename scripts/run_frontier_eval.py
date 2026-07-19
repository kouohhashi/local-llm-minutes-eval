#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
フロンティアモデルによる品質評価（フェーズ3の参照軸）

=============================================================================
このスクリプトの位置づけ
=============================================================================
フロンティアモデル（Claude / GPT）は **品質評価にのみ**参加させ、
フェーズ2（Thinking税＝時間・tok/s）には**含めない**。理由:
  - ネットワーク遅延・共有インフラ・非公開のハードウェア構成が混入し、
    ローカル実測（64GB Mac mini）と同じ土俵にならない
  - 「64GB Mac で完結する」という本シリーズの主題が薄まる

得られるもの:
  1. 実務判断の基準点 — 27B がフロンティアの何割まで届くのか
  2. **正解ラベルの妥当性検証**（こちらが重要）
     hard_size / hard_thinking のタグは仮説にすぎない。
     ローカルモデルだけでは、27B が hard_thinking を落としたときに
     「モデルの限界」なのか「問題が曖昧で誰にも解けない」のかを切り分けられない。
     フロンティアが取れていれば前者、フロンティアも落とすならデータセット側の欠陥。

=============================================================================
重要な設計判断（比較可能性のため）
=============================================================================
■ structured outputs（JSON Schema強制）を**使わない**
  Anthropic / OpenAI とも出力を JSON Schema に強制する機能を持つが、これを使うと
  JSON が必ず妥当になり、**「JSON有効率」をローカルモデルと同じ土俵で測れなくなる**。
  フロンティア側も生の出力をパースし、score.py の同一の前処理を通す。

■ Thinking の on/off を**明示指定**する
  既定値がモデルごとに違うため（Opus 4.8 は省略すると思考オフ、
  Sonnet 5 は省略すると adaptive）、必ず明示的に指定して
  Ollama の `think: true/false` と対応を取る。

■ ログを Ollama と同じ形に正規化する
  score.py がそのまま採点できるよう、レスポンスを Ollama の /api/chat と
  同じ形（{"message": {"content": ...}, "done_reason": ..., "eval_count": ...}）に
  変換して保存する。ファイル名も同じ規約に揃える。

■ 時間は記録するが、論文の時間軸には載せない
  参考値として CSV に残すが、ローカルの tok/s と並べて比較しない。

=============================================================================
注意: Python 3.9 互換 / 標準ライブラリのみ
=============================================================================
本プロジェクトの測定スクリプトは全て stdlib のみで書いている（システム同梱の
Python 3.9.6 を汚さないため）。公式SDK（anthropic / openai）を使うのが本来は
推奨だが、ここでは4本の測定スクリプトの実装を揃えることと、パッケージを
一切インストールしないことを優先し、urllib による raw HTTP を用いる。
この判断は notes.md に記録済み。

使い方:
  export ANTHROPIC_API_KEY=...
  export OPENAI_API_KEY=...
  python3 scripts/run_frontier_eval.py --list-models        # 実在するモデルIDを確認
  python3 scripts/run_frontier_eval.py --models claude-opus-4-8 claude-sonnet-5
  python3 scripts/run_frontier_eval.py --trials 5
"""

import argparse
import csv
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATASET_DIR = os.path.join(ROOT, "dataset")
RESULTS_DIR = os.path.join(ROOT, "results")
LOGS_DIR = os.path.join(ROOT, "logs")

ANTHROPIC_VERSION = "2023-06-01"
TIMEOUT_SEC = 15 * 60
# 出力トークン上限。当初16000にしていたが、gpt-5.1 / gpt-5.4-mini の
# reasoning_effort=high で 7/55 が stop_reason=length に到達した。
# これは「モデルの限界」ではなく「こちらが設定した上限」に起因する打ち切りであり、
# 混同すると誤った結論（miniは出力が壊れる等）になるため上限を引き上げる。
# GPT系の reasoning モデルは output_tokens に推論トークンを含むため余裕が要る。
MAX_TOKENS = int(os.environ.get("FRONTIER_MAX_TOKENS", "48000"))

# 既定の候補。実在確認は --list-models / 実行時のエラーで行い、
# 存在しないIDを「測ったこと」には絶対にしない。
DEFAULT_ANTHROPIC = ["claude-opus-4-8", "claude-sonnet-5", "claude-fable-5"]
DEFAULT_OPENAI = ["gpt-5.1", "gpt-5.4"]

# 思考が常時オンで、オフにできないモデル。
# claude-fable-5 / claude-mythos-5 は thinking:{"type":"disabled"} を送ると 400 を返す
# （thinking パラメータ自体を省略しても思考は走る）。
# したがって「Thinkingオフ」は原理的に測定不可能であり、測定不可として記録する。
THINKING_ALWAYS_ON = ("claude-fable-5", "claude-mythos-5")

CSV_FIELDS = [
    "timestamp", "provider", "model", "thinking", "trial",
    "input_tokens", "output_tokens", "elapsed_s",
    "stop_reason", "notes",
]


def log(msg):
    print("[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def build_prompt(task="extract"):
    """フェーズ2と完全に同一のプロンプトを使う（比較可能性のため）。

    task="extract"        … タスクA（現行。導出の指示なし）
    task="extract_derive" … タスクB（Aに導出ルールを1行だけ足したもの）
    """
    sys.path.insert(0, HERE)
    import run_thinking_bench
    return run_thinking_bench.build_prompt(task)


def http_json(url, headers, body=None, method=None):
    """JSON を投げて JSON を受け取る。エラーはそのまま例外にする。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- Anthropic

def anthropic_headers(key):
    return {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def anthropic_list_models(key):
    out = http_json("https://api.anthropic.com/v1/models",
                    anthropic_headers(key), method="GET")
    return [m["id"] for m in out.get("data", [])]


def anthropic_call(key, model, prompt, thinking):
    """Messages API を1回叩く。

    thinking=True  -> {"type": "adaptive"}（思考あり）
    thinking=False -> {"type": "disabled"}（思考なし）
    ※既定値がモデルごとに異なるため必ず明示する。
    ※structured outputs は使わない（JSON有効率を測るため）。
    """
    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    note = None
    if model in THINKING_ALWAYS_ON:
        # thinking パラメータ自体を省略する。{"type":"disabled"} は 400 になる。
        note = "thinking_always_on(param_omitted)"
    else:
        body["thinking"] = {"type": "adaptive"} if thinking else {"type": "disabled"}

    started = time.time()
    out = http_json("https://api.anthropic.com/v1/messages",
                    anthropic_headers(key), body)
    elapsed = time.time() - started

    # content は content block の配列。text ブロックだけを連結する。
    text = "".join(b.get("text", "") for b in out.get("content", [])
                   if b.get("type") == "text")
    usage = out.get("usage") or {}

    notes = [note] if note else []
    if out.get("stop_reason") == "refusal":
        # 安全性分類器による拒否。捏造せず、拒否されたこと自体を記録する。
        sd = out.get("stop_details") or {}
        notes.append("REFUSAL(category=%s)" % sd.get("category"))

    return {
        "text": text,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "stop_reason": out.get("stop_reason"),
        "elapsed": elapsed,
        "raw": out,
        "note": ";".join(notes) if notes else None,
    }


# ---------------------------------------------------------------- OpenAI

def openai_headers(key):
    return {"Authorization": "Bearer " + key, "content-type": "application/json"}


def openai_list_models(key):
    out = http_json("https://api.openai.com/v1/models",
                    openai_headers(key), method="GET")
    return sorted(m["id"] for m in out.get("data", []))


def openai_call(key, model, prompt, thinking):
    """Chat Completions を1回叩く。

    ※GPT系の reasoning 制御パラメータはモデル世代によって異なる。
      まず reasoning_effort を付けて試し、拒否されたら付けずに再試行し、
      **実際に採用した方法を notes 列に記録する**（捏造しないため）。
    """
    base = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": MAX_TOKENS,
    }
    attempts = []
    if thinking:
        attempts.append(("reasoning_effort=high", dict(base, reasoning_effort="high")))
    else:
        attempts.append(("reasoning_effort=minimal", dict(base, reasoning_effort="minimal")))
    attempts.append(("no_reasoning_param", dict(base)))

    last_err = None
    for label, body in attempts:
        started = time.time()
        try:
            out = http_json("https://api.openai.com/v1/chat/completions",
                            openai_headers(key), body)
        except urllib.error.HTTPError as e:
            last_err = "%s -> HTTP %d: %s" % (label, e.code, e.read().decode("utf-8")[:200])
            continue
        elapsed = time.time() - started
        choice = (out.get("choices") or [{}])[0]
        usage = out.get("usage") or {}
        return {
            "text": (choice.get("message") or {}).get("content") or "",
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "stop_reason": choice.get("finish_reason"),
            "elapsed": elapsed,
            "raw": out,
            "note": "param=" + label,
        }
    raise RuntimeError("全ての呼び出し方法が失敗: " + str(last_err))


# ---------------------------------------------------------------- メイン

def save_as_ollama_shape(path, result):
    """score.py がそのまま採点できるよう Ollama の /api/chat と同じ形に正規化する。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "message": {"content": result["text"]},
            "done_reason": ("length" if result.get("stop_reason") in
                            ("max_tokens", "length") else "stop"),
            "eval_count": result.get("output_tokens"),
            "prompt_eval_count": result.get("input_tokens"),
            "_frontier_raw": result.get("raw"),
        }, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=None)
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--task", default="extract",
                    choices=["extract", "extract_derive"],
                    help="extract=タスクA(現行) / extract_derive=タスクB(導出指示あり)")
    ap.add_argument("--list-models", action="store_true",
                    help="APIから実在するモデルID一覧を取得して終了する")
    args = ap.parse_args()

    akey = os.environ.get("ANTHROPIC_API_KEY")
    okey = os.environ.get("OPENAI_API_KEY")

    if args.list_models:
        if akey:
            log("Anthropic の利用可能モデル:")
            for m in anthropic_list_models(akey):
                print("  " + m)
        else:
            log("ANTHROPIC_API_KEY 未設定のため Anthropic は照会できません")
        if okey:
            log("OpenAI の利用可能モデル（gpt- で始まるもののみ）:")
            for m in openai_list_models(okey):
                if m.startswith("gpt-"):
                    print("  " + m)
        else:
            log("OPENAI_API_KEY 未設定のため OpenAI は照会できません")
        return 0

    # 測定対象の決定。キーが無いプロバイダは「未測定」として扱い、捏造しない。
    targets = []
    wanted = args.models
    for m in (wanted if wanted is not None else DEFAULT_ANTHROPIC):
        if m.startswith("claude"):
            if akey:
                targets.append(("anthropic", m))
            else:
                log("[skip] %s: ANTHROPIC_API_KEY 未設定のため未測定" % m)
    for m in (wanted if wanted is not None else DEFAULT_OPENAI):
        if m.startswith("gpt"):
            if okey:
                targets.append(("openai", m))
            else:
                log("[skip] %s: OPENAI_API_KEY 未設定のため未測定" % m)

    if not targets:
        log("測定対象がありません。APIキーを設定してから再実行してください。")
        log("  export ANTHROPIC_API_KEY=...   /   export OPENAI_API_KEY=...")
        return 1

    prompt = build_prompt(args.task)
    log("プロンプト長=%d文字 / 対象=%s / trials=%d"
        % (len(prompt), ",".join(m for _, m in targets), args.trials))

    raw_dir = os.path.join(LOGS_DIR, "frontier_" + args.task)
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    out_csv = os.path.join(RESULTS_DIR, "frontier_eval%s.csv"
                           % ("" if args.task == "extract" else "_" + args.task))
    is_new = not os.path.exists(out_csv)
    csv_f = open(out_csv, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_f, fieldnames=CSV_FIELDS)
    if is_new:
        writer.writeheader()
        csv_f.flush()

    total = len(targets) * 2 * args.trials
    done = 0

    for provider, model in targets:
        for thinking in (False, True):
            # 思考が常時オンのモデルでは「Thinkingオフ」を測定できない。
            # 1行だけ「測定不可」として記録し、試行はスキップする（捏造しないため）。
            if (not thinking) and model in THINKING_ALWAYS_ON:
                log("[skip] %s think=False: このモデルは思考を無効化できないため測定不可" % model)
                writer.writerow({
                    "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                    "provider": provider, "model": model, "thinking": False, "trial": "",
                    "input_tokens": "", "output_tokens": "", "elapsed_s": "",
                    "stop_reason": "",
                    "notes": "NOT_MEASURABLE_thinking_cannot_be_disabled"})
                csv_f.flush()
                done += args.trials
                continue

            for trial in range(1, args.trials + 1):
                done += 1
                log("(%d/%d) %s think=%s trial=%d ..." % (done, total, model, thinking, trial))
                ts = datetime.datetime.now().isoformat(timespec="seconds")
                notes = ["max_tokens=%d" % MAX_TOKENS, "task=" + args.task]
                try:
                    key = akey if provider == "anthropic" else okey
                    fn = anthropic_call if provider == "anthropic" else openai_call
                    r = fn(key, model, prompt, thinking)
                    if r.get("note"):
                        notes.append(r["note"])
                except urllib.error.HTTPError as e:
                    detail = e.read().decode("utf-8")[:300]
                    log("  -> HTTPエラー %d: %s" % (e.code, detail))
                    notes.append("FAILED_HTTP%d:%s" % (e.code, detail.replace("\n", " ")))
                    writer.writerow({
                        "timestamp": ts, "provider": provider, "model": model,
                        "thinking": thinking, "trial": trial,
                        "input_tokens": "", "output_tokens": "", "elapsed_s": "",
                        "stop_reason": "", "notes": ";".join(notes)})
                    csv_f.flush()
                    continue
                except Exception as e:
                    log("  -> 失敗: %s" % e)
                    notes.append("FAILED_%s:%s" % (type(e).__name__, e))
                    writer.writerow({
                        "timestamp": ts, "provider": provider, "model": model,
                        "thinking": thinking, "trial": trial,
                        "input_tokens": "", "output_tokens": "", "elapsed_s": "",
                        "stop_reason": "", "notes": ";".join(notes)})
                    csv_f.flush()
                    continue

                stem = "%s_think-%s_trial%d" % (model.replace(":", "-"), thinking, trial)
                save_as_ollama_shape(os.path.join(raw_dir, stem + ".json"), r)

                log("  -> in=%s out=%s stop=%s (%.1fs)"
                    % (r["input_tokens"], r["output_tokens"], r["stop_reason"], r["elapsed"]))
                writer.writerow({
                    "timestamp": ts, "provider": provider, "model": model,
                    "thinking": thinking, "trial": trial,
                    "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                    "elapsed_s": r["elapsed"], "stop_reason": r["stop_reason"],
                    "notes": ";".join(notes)})
                csv_f.flush()

    csv_f.close()
    log("完了。CSV: %s / 生ログ: %s" % (out_csv, raw_dir))
    log("採点: python3 scripts/score.py --logs %s --out %s"
        % (raw_dir, os.path.join(RESULTS_DIR, "quality_eval_frontier.csv")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
