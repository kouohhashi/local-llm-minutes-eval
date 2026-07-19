#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
フェーズ4: MLX vs Ollama の比較（軽い確認）

=============================================================================
このフェーズの目的（指示書 §7）
=============================================================================
Ollama は 0.19 以降 Apple Silicon で内部的に MLX バックエンドを使うとされる。
よって比較の構図は「Ollama経由（MLX内蔵） vs mlx-lm 直叩き」であり、
**精密比較ではなく「差が大きいか小さいか」の確認**が目的。
tok/s とプロンプト処理速度の2点が分かれば十分。

=============================================================================
誠実性ルール
=============================================================================
- 数値は各ツールが報告した生の値をそのまま記録する。
- **モデルが完全一致しない場合は必ず差異を明記する**。
  mlx-community に qwen3.5:27b と完全同一の変換済みモデルが無い場合、
  最も近い量子化・サイズを選び、「何が違うか」を notes 列と notes.md に記録する。
  量子化方式・パラメータ数が違うものを「同じモデルの比較」として提示しない。
- mlx-lm が使えない場合は「未測定」と記録する。推定値で埋めない。
- Ollama が実際に MLX を使っているかは**確認できた場合のみ**記録する。
  確認できなければ「確認できなかった」と書く。憶測を事実として書かない。

=============================================================================
セットアップ（システムPythonを汚さないため venv を使う）
=============================================================================
  cd experiment
  python3 -m venv .venv-mlx
  .venv-mlx/bin/pip install --upgrade pip
  .venv-mlx/bin/pip install mlx-lm

  # 変換済みモデルを探す（例）
  .venv-mlx/bin/python -m mlx_lm.generate --model mlx-community/<モデル名> \
      --prompt "test" --max-tokens 8

注意: 27Bクラスの MLX モデルは 15GB 前後のダウンロードが発生する。

使い方:
  python3 scripts/run_mlx_compare.py --check                      # 環境確認のみ
  python3 scripts/run_mlx_compare.py --mlx-model mlx-community/... \
      --ollama-model qwen3.5:27b --trials 5
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
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS_DIR = os.path.join(ROOT, "results")
LOGS_DIR = os.path.join(ROOT, "logs")
VENV_PY = os.path.join(ROOT, ".venv-mlx", "bin", "python")

OLLAMA_HOST = "http://localhost:11434"
NUM_CTX = 32768
TIMEOUT_SEC = 20 * 60

# 指示書 §7:「同一プロンプト（300字要約の固定タスクでよい）」
PROMPT = "リモートワークの生産性について、300字で要約してください。"

CSV_FIELDS = [
    "timestamp", "backend", "model", "trial",
    "prompt_tokens", "gen_tokens", "prompt_time_s", "gen_time_s",
    "tokens_per_sec", "peak_memory_gb", "notes",
]


def log(msg):
    print("[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def fmt(v):
    return "" if v is None else v


# ---------------------------------------------------------------- 環境確認

def check_environment():
    """測定前に環境と前提を確認し、分かったことだけを報告する。"""
    log("=" * 60)
    log("環境確認")

    # 1. Ollama
    try:
        with urllib.request.urlopen(OLLAMA_HOST + "/api/version", timeout=10) as r:
            ver = json.loads(r.read().decode("utf-8")).get("version")
        log("Ollama: %s" % ver)
    except Exception as e:
        log("Ollama: 到達できません (%s)" % e)

    # 2. mlx-lm（venv）
    if os.path.exists(VENV_PY):
        try:
            out = subprocess.run([VENV_PY, "-c", "import mlx_lm,mlx.core as mx;"
                                  "print(mlx_lm.__version__ if hasattr(mlx_lm,'__version__') else 'unknown')"],
                                 capture_output=True, timeout=120, encoding="utf-8")
            if out.returncode == 0:
                log("mlx-lm: 導入済み (version=%s)" % out.stdout.strip())
            else:
                log("mlx-lm: import に失敗\n%s" % out.stderr[:300])
        except Exception as e:
            log("mlx-lm: 確認に失敗 (%s)" % e)
    else:
        log("mlx-lm: venv が未作成（%s が無い）" % VENV_PY)
        log("  セットアップ: python3 -m venv .venv-mlx && .venv-mlx/bin/pip install mlx-lm")

    # 3. Ollama が MLX を使っているかの確認を試みる
    #    ※確認できたことだけを記録する。憶測は書かない。
    log("-" * 60)
    log("Ollama のバックエンド確認（確認できた事実のみ記録する）:")
    found = False
    for path in ("~/.ollama/logs/server.log", "~/Library/Logs/Ollama/server.log"):
        p = os.path.expanduser(path)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    tail = f.read()[-200000:]
                hits = [l for l in tail.splitlines()
                        if re.search(r"\bmlx\b|metal|ggml|backend", l, re.I)][-8:]
                if hits:
                    found = True
                    log("  %s より:" % path)
                    for h in hits:
                        log("    " + h.strip()[:160])
            except Exception as e:
                log("  %s の読み取りに失敗: %s" % (path, e))
    if not found:
        log("  ログからバックエンドを特定できなかった。")
        log("  → notes.md には「確認できなかった」と記録すること（憶測を書かない）。")
    log("=" * 60)


# ---------------------------------------------------------------- Ollama 側

def ollama_stop(model):
    subprocess.run(["ollama", "stop", model], check=False,
                   capture_output=True, timeout=60)


def run_ollama(model, trial):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
        "think": False,          # 指示書 §7: Thinkingオフで比較
        "options": {"num_ctx": NUM_CTX},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA_HOST + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    started = time.time()
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    elapsed = time.time() - started

    ec = d.get("eval_count")
    ed = d.get("eval_duration")
    pd_ = d.get("prompt_eval_duration")
    gen_time = (ed / 1e9) if ed else None
    return {
        "prompt_tokens": d.get("prompt_eval_count"),
        "gen_tokens": ec,
        "prompt_time_s": (pd_ / 1e9) if pd_ else None,
        "gen_time_s": gen_time,
        "tokens_per_sec": (ec / gen_time) if (ec and gen_time) else None,
        "peak_memory_gb": None,      # Ollama は報告しない → 空欄（捏造しない）
        "notes": "wall_clock=%.2fs" % elapsed,
        "raw": d,
    }


# ---------------------------------------------------------------- mlx-lm 側

# mlx_lm.generate が標準エラーに出す統計行のパターン
RE_PROMPT = re.compile(r"Prompt:\s*([\d.]+)\s*tokens?,\s*([\d.]+)\s*tokens-per-sec", re.I)
RE_GEN = re.compile(r"Generation:\s*([\d.]+)\s*tokens?,\s*([\d.]+)\s*tokens-per-sec", re.I)
RE_PEAK = re.compile(r"Peak memory:\s*([\d.]+)\s*GB", re.I)


def run_mlx(model, trial, max_tokens=400):
    """mlx_lm.generate をサブプロセスで実行し、報告された統計をパースする。

    パースできなかった項目は None のままにする（推定値で埋めない）。
    """
    if not os.path.exists(VENV_PY):
        raise RuntimeError("mlx-lm の venv が未作成: " + VENV_PY)
    cmd = [VENV_PY, "-m", "mlx_lm.generate",
           "--model", model, "--prompt", PROMPT,
           "--max-tokens", str(max_tokens)]
    started = time.time()
    out = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_SEC, encoding="utf-8")
    elapsed = time.time() - started
    blob = (out.stdout or "") + "\n" + (out.stderr or "")
    if out.returncode != 0:
        raise RuntimeError("mlx_lm.generate が異常終了 (rc=%d):\n%s"
                           % (out.returncode, blob[-500:]))

    pm, gm, km = RE_PROMPT.search(blob), RE_GEN.search(blob), RE_PEAK.search(blob)
    notes = ["wall_clock=%.2fs" % elapsed]
    if not gm:
        notes.append("WARN_generation統計をパースできず")

    gen_tokens = float(gm.group(1)) if gm else None
    gen_tps = float(gm.group(2)) if gm else None
    return {
        "prompt_tokens": float(pm.group(1)) if pm else None,
        "gen_tokens": gen_tokens,
        "prompt_time_s": (float(pm.group(1)) / float(pm.group(2))
                          if pm and float(pm.group(2)) else None),
        "gen_time_s": (gen_tokens / gen_tps) if (gen_tokens and gen_tps) else None,
        "tokens_per_sec": gen_tps,
        "peak_memory_gb": float(km.group(1)) if km else None,
        "notes": ";".join(notes),
        "raw": blob[-4000:],
    }


# ---------------------------------------------------------------- メイン

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="環境確認のみ行って終了")
    ap.add_argument("--ollama-model", default="qwen3.5:27b")
    ap.add_argument("--mlx-model", default=None,
                    help="mlx-community 等の変換済みモデル名。未指定なら mlx 側は未測定として記録")
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--skip-ollama", action="store_true",
                    help="Ollama側を測らない（MLXモデルを複数測る際、Ollamaの重複を避ける）")
    args = ap.parse_args()

    if args.check:
        check_environment()
        return 0

    check_environment()
    os.makedirs(RESULTS_DIR, exist_ok=True)
    raw_dir = os.path.join(LOGS_DIR, "mlx_compare")
    os.makedirs(raw_dir, exist_ok=True)

    out_csv = os.path.join(RESULTS_DIR, "mlx_compare.csv")
    is_new = not os.path.exists(out_csv)
    f = open(out_csv, "a", newline="", encoding="utf-8")
    w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
    if is_new:
        w.writeheader()
        f.flush()

    backends = [] if args.skip_ollama else [("ollama", args.ollama_model, run_ollama)]
    if args.mlx_model:
        backends.append(("mlx-lm", args.mlx_model, run_mlx))
    else:
        log("[未測定] mlx-lm: --mlx-model が未指定のため測定しない")
        w.writerow({"timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                    "backend": "mlx-lm", "model": "", "trial": "",
                    "prompt_tokens": "", "gen_tokens": "", "prompt_time_s": "",
                    "gen_time_s": "", "tokens_per_sec": "", "peak_memory_gb": "",
                    "notes": "NOT_MEASURED_no_mlx_model_specified"})
        f.flush()

    for backend, model, fn in backends:
        log("=" * 60)
        log("%s / %s" % (backend, model))
        if backend == "ollama":
            ollama_stop(model)
        for trial in range(1, args.trials + 1):
            log("  trial %d/%d ..." % (trial, args.trials))
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            try:
                r = fn(model, trial)
            except Exception as e:
                log("    -> 失敗: %s" % e)
                w.writerow({"timestamp": ts, "backend": backend, "model": model,
                            "trial": trial, "prompt_tokens": "", "gen_tokens": "",
                            "prompt_time_s": "", "gen_time_s": "", "tokens_per_sec": "",
                            "peak_memory_gb": "",
                            "notes": "FAILED_%s:%s" % (type(e).__name__, str(e)[:200])})
                f.flush()
                continue

            with open(os.path.join(raw_dir, "%s_%s_trial%d.txt"
                                   % (backend, model.replace("/", "-").replace(":", "-"), trial)),
                      "w", encoding="utf-8") as rf:
                rf.write(str(r.pop("raw")))

            log("    -> prompt=%s gen=%s tok/s=%s peak=%sGB"
                % (r["prompt_tokens"], r["gen_tokens"],
                   ("%.2f" % r["tokens_per_sec"]) if r["tokens_per_sec"] else "n/a",
                   r["peak_memory_gb"]))
            row = {"timestamp": ts, "backend": backend, "model": model, "trial": trial}
            for k in ("prompt_tokens", "gen_tokens", "prompt_time_s", "gen_time_s",
                      "tokens_per_sec", "peak_memory_gb", "notes"):
                row[k] = fmt(r.get(k))
            w.writerow(row)
            f.flush()
        if backend == "ollama":
            ollama_stop(model)

    f.close()
    log("完了。CSV: %s" % out_csv)
    log("※ モデルが完全一致でない場合、その差異を notes.md に必ず明記すること。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
