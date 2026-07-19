#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
フェーズ3: 品質評価の採点エンジン

=============================================================================
採点基準（指示書 §6 に基づく。この節が採点の全定義であり、実装はこれに従う）
=============================================================================

■ 1. 前処理（全出力に一律適用する。適用内容は quality_eval.csv の preprocess 列に記録）
   モデル出力からJSONを取り出すために、以下を順に試みる:
     (a) 出力全体をそのまま json.loads する
     (b) ```json ... ``` / ``` ... ``` のコードフェンスを除去して json.loads する
     (c) 最初の '{' から最後の '}' までを切り出して json.loads する
     (d) 上記が全て失敗した場合、JSONパース失敗（json_valid=False）とする
   ※ (b)(c) は「モデルがJSON以外を出力した」ことの記録を兼ねる。どの段で成功したかを記録する。
   ※ 出力の途中切れ（コンテキスト溢れ）による失敗は truncated フラグで区別する。
      これは「Thinkingの効果」ではなく「出力が入り切らなかった」ことを意味し、
      混同すると実験の主軸が交絡するため、必ず分けて集計する。

■ 2. 正規化（表記ゆれの吸収）
   - テキスト: NFKC正規化 → 空白・記号（・，、。「」（）等）除去 → 小文字化
   - 氏名: 上記に加え、姓のみ／名のみでの一致も許容する
     （gold「芦田 巧」に対しモデル出力「芦田」「芦田さん」を正解とする。
       敬称「さん」「氏」「様」は除去。ただし別人の姓と衝突しないことを事前に確認済み
       ―― 6名の姓・名はいずれも重複していない）
   - 日付: 「2026-10-15」「2026/10/15」「10月15日」「十月十五日」等を YYYY-MM-DD に正規化。
     年の記載がない場合は会議年（2026）を補う。
     ※日付は正規化後に **厳密一致** を要求する（指示書 §6「日付は正規化後に厳密一致」）。
   - 数値: 「600万円」「6,000,000円」「六百万円」を同一の数値に正規化して比較。
     漢数字・万/億の単位・カンマ・通貨記号を吸収する。
     単位（拠点/人/件/台/分/秒/年/円）は補助的に照合し、数値が一致し単位が異なる場合は
     人手確認フラグ（needs_review）を立てる。

■ 3. マッチング（gold項目 ⇔ モデル出力項目の対応付け）
   gold の id とモデル出力の id は一致しないため、**id ではなく内容で対応付ける**。
   - 各 gold 項目に対し、同カテゴリのモデル出力項目の中から、
     識別テキスト（decisions:content / action_items:task / key_numbers:label）の
     類似度が最大のものを選ぶ。
   - **類似度は3つの尺度の最大値を採る**（日本語対応。下記「■ 3-1」参照）。
   - 類似度が SIM_THRESHOLD 未満なら「対応する出力なし（未検出）」とする。
   - 同カテゴリで見つからない場合、**他カテゴリも探索する**（下記「■ 3-2」参照）。

■ 3-1. 日本語向けの類似度（重要な設計判断）
   当初 difflib.SequenceMatcher の ratio のみを使っていたが、**日本語では機能しなかった**。
   実測で確認した失敗例:
     gold「移行方式はB案（段階移行）を採用する」
     出力「移行方式は拠点を分けて順次切り替える段階移行（B案）で確定する」  → ratio 0.49
     gold「目標応答時間」 vs 出力「主要画面の応答時間目標」                  → ratio 0.47
   どちらも**内容は完全に正解**だが、閾値0.55に届かず「未検出」と誤判定されていた。
   日本語は単語境界が無いため、正しくても語順が違ったり修飾が付くだけで
   文字列類似度が急落する。この尺度をそのまま使うと、
   **全モデルのスコアを系統的に過小評価し、丁寧に書くモデルほど不当に罰する**。

   対策として次の3尺度の最大値を採る:
     (a) SequenceMatcher の ratio      … 全体的な文字列の近さ
     (b) 文字bigramの Dice 係数        … 語順の違いに強い
     (c) 文字bigramの包含率            … 一方が他方を言い換えつつ含む場合に強い
                                        （「目標応答時間」⊂「主要画面の応答時間目標」）
   (c) は短い文字列で暴発しやすいため、**両者が3bigram以上のときのみ**適用する。
   識別テキストのマッチはあくまで「対応付け」であり、正誤は後段のフィールド照合
   （decided_by / assignee / due / value）で決まるため、この緩和で誤って正解が
   増えることはない。

■ 3-2. カテゴリを跨ぐ対応付け
   decisions と action_items は本質的に重なる。実測では
   gold D10「議事録は桐生が当日中に全員へ共有する」(decisions) に対し、
   モデルが同じ内容を action_items 側に出力していた。
   カテゴリ別マッチのみだと、これは **「取りこぼし」と「捏造」の二重減点**になる。
   これはモデルの誤りではなく分類の揺れなので、
   同カテゴリで未検出の gold 項目は他カテゴリも探索し、
   見つかった場合は cross_category フラグを立てた上で対応付ける。
   - 1つのモデル出力項目が複数のgold項目に重複して割り当てられないよう、
     類似度の高い順に貪欲に確定させる（1対1マッチング）。
   - 閾値付近（0.55〜0.70）のマッチは needs_review フラグを立てる。
     ※閾値は緩和マッチであり、機械的判定の限界を認めるための人手確認フラグである。

■ 4. 正解判定（項目が「取れた」とみなす条件）
   マッチングが成立した上で、さらに以下を満たすこと:
   - decisions:     decided_by が gold の decided_by_acceptable のいずれかに氏名一致すること
                    ※会議では「提案した人」と「場を締めた議長」が異なることが多く、どちらを
                      decided_by とするかは一意に決まらない。独立検証でこの基準の不統一を
                      指摘されたため、gold 側に許容リストを持たせて複数正解を認める。
   - action_items:  assignee が氏名一致し、かつ due が日付として厳密一致すること
   - key_numbers:   value が数値として一致すること
   識別テキストのマッチだけでは正解としない。
   これは hard_size / hard_thinking の仕掛け（訂正・上書き、多段算術、相対日付、話者混線）が
   **まさにこれらのフィールドで正誤が分かれる**ように設計されているためである。
   例: A8「移行リハーサル手順書」は task では全モデルがマッチするが、
       assignee が「白鳥 千夏」（訂正前）か「沼田 賢一」（訂正後＝正解）かで割れる。

■ 5. 集計
   - JSON有効率 = json_valid だった試行数 / 全試行数
   - 項目別F1  = decisions / action_items / key_numbers それぞれ、および全体
       precision = 正解した出力項目数 / (モデルが出力した項目数 − 許容項目にマッチした数)
       recall    = 正解した gold 項目数 / gold 項目数
       F1        = 2PR/(P+R)
     ※precision の分母から「許容項目」を除外する。本文中には gold の正解ではないが
       抽出されても妥当な記述が存在し（gold.json の acceptable_items）、これを分母に
       含めると正しい抽出をしたモデルを不当に罰することになる。独立検証での指摘に基づく。
     ※逆に、gold にも許容リストにもマッチしない出力は「捏造」としてカウントし、
       precision を下げる。S4のノイズ区間（定食屋の値段・気温・電車の遅延・子供の年齢）を
       key_numbers として拾った場合が典型例。
   - 難易度タグ別正答率 = そのタグの gold 項目のうち正解した数 / そのタグの gold 項目数
     （easy / medium / hard_size / hard_thinking / hard_implicit 別。ここが考察の核）

■ 6. 実用グレード判定（議事録生成の用途を想定した実務指標）
   F1 だけでは「業務投入の可否」に答えられないため、失敗の種類を区別した判定を併記する。
   議事録生成では、同じ F1 でも「取りこぼし」と「捏造」では深刻さが全く違う。
   - グレードA（自動連携可）  : JSON有効 / easy・medium 全問正解 / 捏造ゼロ / 担当者・期限の誤りゼロ
   - グレードB（人手レビュー前提）: JSON有効 / easy 全問正解 / 捏造ゼロ。取りこぼしは許容
   - グレードC（実務投入不可）  : JSONが壊れる、または easy を落とす、または捏造がある

=============================================================================
注意: Python 3.9 互換で書くこと（システム同梱版が 3.9.6 のため）。
=============================================================================

使い方:
  python3 scripts/score.py --logs logs/thinking_bench_extract --out results/quality_eval.csv
"""

import argparse
import csv
import difflib
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 対応付けの閾値。実測に基づき 0.55 → 0.50 に緩和した。
# 例: gold「議事録は桐生が当日中に全員へ共有する」に対しモデル出力
#     「本日の議事録を作成し全員に共有する」は内容が正解だが類似度 0.51 で、
#     0.55 のままだと「未検出」と誤判定されていた。
# 対応付けを緩めても、正誤は後段のフィールド照合（decided_by / assignee / due / value）
# が決めるため、誤って正解が増えることはない。閾値付近は needs_review で人手確認に回す。
SIM_THRESHOLD = 0.50       # これ未満は「未検出」
REVIEW_BAND = (0.50, 0.72)  # この範囲は人手確認フラグを立てる
ACCEPTABLE_THRESHOLD = 0.50  # 許容項目との照合はやや緩めにする（不当減点を避ける方向に倒す）
MEETING_YEAR = 2026

DIFFICULTIES = ("easy", "medium", "hard_size", "hard_thinking", "hard_implicit")

# 識別に使うフィールドと、正解判定に使うフィールド
CATEGORY_SPEC = {
    "decisions":    {"key": "content", "check": ["decided_by"]},
    "action_items": {"key": "task",    "check": ["assignee", "due"]},
    "key_numbers":  {"key": "label",   "check": ["value"]},
}

HONORIFICS = ("さん", "氏", "様", "君", "くん")

KANJI_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9}


# ---------------------------------------------------------------- 正規化

def norm_text(s):
    """テキストの正規化: NFKC → 記号・空白除去 → 小文字化。"""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = re.sub(r"[\s・,，、。.．「」『』（）()\[\]【】:：;；!！?？\"'`~〜\-—_/\\|]", "", s)
    return s.lower()


def norm_name(s):
    """氏名の正規化: 敬称除去 + 正規化。"""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).strip()
    for h in HONORIFICS:
        if s.endswith(h):
            s = s[: -len(h)]
    return norm_text(s)


def name_match(gold, pred):
    """氏名の一致判定。フルネーム一致、または姓のみ・名のみの一致を許容。"""
    g, p = norm_name(gold), norm_name(pred)
    if not g or not p:
        return False
    if g == p:
        return True
    # gold は「芦田 巧」形式。姓と名に分けて部分一致を許す
    parts = [norm_name(x) for x in str(gold).split() if x.strip()]
    if len(parts) >= 2:
        sei, mei = parts[0], parts[1]
        if p in (sei, mei, sei + mei):
            return True
        # モデルが「芦田巧」と詰めて書く場合も上でカバー済み
    return False


def kanji_to_int(s):
    """簡易な漢数字パーサ。十/百/千/万 に対応（会議録に出る範囲で十分）。"""
    if not s:
        return None
    total, section, current = 0, 0, 0
    for ch in s:
        if ch in KANJI_DIGITS:
            current = KANJI_DIGITS[ch]
        elif ch == "十":
            section += (current or 1) * 10
            current = 0
        elif ch == "百":
            section += (current or 1) * 100
            current = 0
        elif ch == "千":
            section += (current or 1) * 1000
            current = 0
        elif ch == "万":
            total += (section + current) * 10000
            section, current = 0, 0
        elif ch == "億":
            total += (section + current) * 100000000
            section, current = 0, 0
        else:
            return None
    return total + section + current


def parse_date(s):
    """日付を YYYY-MM-DD に正規化する。解釈できなければ None。"""
    if s is None:
        return None
    t = unicodedata.normalize("NFKC", str(s)).strip()

    m = re.search(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})", t)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # 年なし「10月15日」「10/15」
    m = re.search(r"(?<!\d)(\d{1,2})\s*[/月]\s*(\d{1,2})\s*日?", t)
    if m:
        return "%04d-%02d-%02d" % (MEETING_YEAR, int(m.group(1)), int(m.group(2)))

    # 漢数字「十月十五日」
    m = re.search(r"([〇零一二三四五六七八九十百]+)月([〇零一二三四五六七八九十百]+)日", t)
    if m:
        mo, d = kanji_to_int(m.group(1)), kanji_to_int(m.group(2))
        if mo and d:
            return "%04d-%02d-%02d" % (MEETING_YEAR, mo, d)
    return None


def parse_number(s):
    """数値を float に正規化する。「600万円」「6,000,000」「六百万」等に対応。

    戻り値: (数値 or None, 単位文字列)
    """
    if s is None:
        return None, ""
    t = unicodedata.normalize("NFKC", str(s)).replace(",", "").strip()

    unit_m = re.search(r"(拠点|箇所|か所|人|名|件|台|分|秒|時間|年|ヶ月|カ月|か月|月|円|%|パーセント)", t)
    unit = unit_m.group(1) if unit_m else ""

    # 「48万件」「600万円」形式
    m = re.search(r"(\d+(?:\.\d+)?)\s*億", t)
    oku = float(m.group(1)) * 100000000 if m else 0
    m = re.search(r"(\d+(?:\.\d+)?)\s*万", t)
    if m:
        return oku + float(m.group(1)) * 10000, unit

    m = re.search(r"(\d+(?:\.\d+)?)", t)
    if m:
        return oku + float(m.group(1)) if oku else float(m.group(1)), unit

    # 漢数字のみ
    m = re.search(r"[〇零一二三四五六七八九十百千万億]+", t)
    if m:
        v = kanji_to_int(m.group(0))
        if v is not None:
            return float(v), unit
    return None, unit


def value_match(gold, pred):
    """key_numbers の value 一致判定。

    戻り値: (一致したか, 人手確認が必要か)
    """
    # まず日付として解釈できるなら日付として厳密比較
    gd, pd_ = parse_date(gold), parse_date(pred)
    if gd is not None:
        return (gd == pd_), False

    gn, gu = parse_number(gold)
    pn, pu = parse_number(pred)
    if gn is None or pn is None:
        # 数値として読めない場合はテキスト一致にフォールバック
        return (norm_text(gold) == norm_text(pred)), True

    if abs(gn - pn) > 1e-9:
        return False, False
    # 数値は一致。単位が食い違う場合は人手確認フラグ
    if gu and pu and gu != pu:
        return True, True
    return True, False


# ---------------------------------------------------------------- JSON抽出

def extract_json(text):
    """モデル出力からJSONを取り出す。

    戻り値: (dict or None, 前処理の説明文字列)
    """
    if text is None:
        return None, "empty_output"
    t = text.strip()
    if not t:
        return None, "empty_output"

    # (a) そのまま
    try:
        return json.loads(t), "raw"
    except Exception:
        pass

    # (b) コードフェンス除去
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        try:
            return json.loads(fence.group(1).strip()), "strip_code_fence"
        except Exception:
            pass

    # (c) 最初の { から最後の } まで
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(t[i:j + 1]), "brace_slice"
        except Exception:
            pass

    return None, "parse_failed"


# ---------------------------------------------------------------- マッチング

def bigrams(s):
    """文字bigramの集合。1文字以下の場合はその文字自体を返す。"""
    if len(s) < 2:
        return set([s]) if s else set()
    return set(s[i:i + 2] for i in range(len(s) - 1))


def similarity(a, b):
    """日本語向けの類似度。3尺度の最大値を採る（採点基準 ■3-1 参照）。

    (a) SequenceMatcher ratio / (b) bigram Dice / (c) bigram 包含率
    (c) は短文字列での暴発を避けるため、両者が3bigram以上のときのみ適用。
    """
    if not a or not b:
        return 0.0
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    ba, bb = bigrams(a), bigrams(b)
    if not ba or not bb:
        return seq
    inter = len(ba & bb)
    dice = 2.0 * inter / (len(ba) + len(bb))
    best = max(seq, dice)
    if len(ba) >= 3 and len(bb) >= 3:
        containment = inter / min(len(ba), len(bb))
        best = max(best, containment)
    return best


def match_category(gold_items, pred_items, spec):
    """gold項目とモデル出力項目を1対1で貪欲マッチングする。

    戻り値: gold index -> (pred index or None, similarity)
    """
    key = spec["key"]
    pairs = []
    for gi, g in enumerate(gold_items):
        gk = norm_text(g.get(key))
        for pi, p in enumerate(pred_items):
            pk = norm_text(p.get(key))
            if not gk or not pk:
                continue
            sim = similarity(gk, pk)
            if sim >= SIM_THRESHOLD:
                pairs.append((sim, gi, pi))

    pairs.sort(reverse=True)  # 類似度の高い順に確定
    used_g, used_p = set(), set()
    result = {}
    for sim, gi, pi in pairs:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        result[gi] = (pi, sim)
    for gi in range(len(gold_items)):
        result.setdefault(gi, (None, 0.0))
    return result


def judge(gold_item, pred_item, category):
    """1つのgold項目について正解判定を行う。

    戻り値: (正解か, 人手確認が必要か, 理由文字列)
    """
    if pred_item is None:
        return False, False, "not_found"

    spec = CATEGORY_SPEC[category]
    reasons = []
    review = False
    ok = True

    # カテゴリを跨いで対応付いた場合、人物フィールドの名前が食い違う
    # （decisions は decided_by、action_items は assignee）。
    # どちらも「その項目に責任を持つ人物」を指す同義のフィールドなので相互に参照する。
    PERSON_FIELDS = ("decided_by", "assignee")

    def person_of(item):
        for f in PERSON_FIELDS:
            if item.get(f):
                return item.get(f)
        return None

    for field in spec["check"]:
        gv, pv = gold_item.get(field), pred_item.get(field)
        if field in PERSON_FIELDS and not pv:
            pv = person_of(pred_item)
        if field == "decided_by":
            # 「提案者」と「場を締めた議長」のどちらも正解として認める（gold側に許容リスト）
            candidates = gold_item.get("decided_by_acceptable") or [gv]
            if not any(name_match(c, pv) for c in candidates):
                ok = False
                reasons.append("decided_by_mismatch(gold=%s,pred=%s)"
                               % ("|".join(str(c) for c in candidates), pv))
        elif field == "assignee":
            if not name_match(gv, pv):
                ok = False
                reasons.append("%s_mismatch(gold=%s,pred=%s)" % (field, gv, pv))
        elif field == "due":
            gd, pd_ = parse_date(gv), parse_date(pv)
            if gd is None:
                reasons.append("gold_due_unparsable")
                review = True
            elif gd != pd_:
                ok = False
                reasons.append("due_mismatch(gold=%s,pred=%s)" % (gd, pd_))
        elif field == "value":
            m, r = value_match(gv, pv)
            if r:
                review = True
            if not m:
                ok = False
                reasons.append("value_mismatch(gold=%s,pred=%s)" % (gv, pv))

    return ok, review, ";".join(reasons) if reasons else "ok"


# ---------------------------------------------------------------- 1試行の採点

def pred_text(item, category):
    """モデル出力項目を、許容リスト・ノイズリストと照合するための1本の文字列にする。"""
    spec = CATEGORY_SPEC[category]
    parts = [str(item.get(spec["key"]) or "")]
    for f in spec["check"]:
        v = item.get(f)
        if v:
            parts.append(str(v))
    return norm_text(" ".join(parts))


def best_ratio(text, candidates):
    """text と候補リストの最大類似度を返す。"""
    best = 0.0
    for c in candidates:
        r = similarity(text, norm_text(c))
        if r > best:
            best = r
    return best


def value_in_candidates(pred_item, candidates):
    """key_numbers の値が許容リストのいずれかの値と数値一致するかを見る。

    ラベルの言い回しは大きく揺れる（実測例:
      モデル「新人研修にかかる操作習得時間: 2日」
      許容  「新人研修の所要日数: 2日」）ため、ラベルの文字列類似度だけでは
    許容項目とノイズを分離できないことが実測で判明した。
    一方、値（数値＋単位）は揺れが小さく判別力が高い。
    S4のノイズ（850円/28度/12分/5歳）は許容リストのどの値とも一致しないため、
    この照合でノイズを誤って許容してしまうことはない。
    """
    pv = pred_item.get("value")
    if pv is None:
        return False
    pn, pu = parse_number(pv)
    pd_ = parse_date(pv)
    if pn is None and pd_ is None:
        return False
    for c in candidates:
        cn, cu = parse_number(c)
        cd = parse_date(c)
        if pd_ is not None and cd is not None and pd_ == cd:
            return True
        if pn is not None and cn is not None and abs(pn - cn) < 1e-9:
            # 数値が一致し、単位も矛盾しないこと（どちらかが空なら単位は問わない）
            if not pu or not cu or pu == cu:
                return True
    return False


def score_one(gold, pred_obj):
    """1試行分の採点。

    戻り値: (gold項目ごとの判定リスト, カテゴリ別カウント dict)
    カウントには出力総数・許容にマッチした数・捏造数を含む。
    """
    acceptable = gold.get("acceptable_items", {})
    noise = (gold.get("noise_items", {}) or {}).get("values", [])
    cats = ("decisions", "action_items", "key_numbers")

    # モデル出力を category ごとに整える（辞書でない要素は型崩れとして除外）
    preds = {}
    for category in cats:
        pl = pred_obj.get(category, []) if isinstance(pred_obj, dict) else []
        if not isinstance(pl, list):
            pl = []
        preds[category] = [p for p in pl if isinstance(p, dict)]

    used = dict((c, set()) for c in cats)   # 既に gold に割り当てた出力項目
    records = []                             # gold項目ごとの中間結果

    # --- 第1passː 同カテゴリでのマッチング ---
    for category in cats:
        gold_items = gold.get(category, [])
        matches = match_category(gold_items, preds[category], CATEGORY_SPEC[category])
        for gi, g in enumerate(gold_items):
            pi, sim = matches[gi]
            if pi is not None:
                used[category].add(pi)
            records.append({"gold": g, "category": category,
                            "pred_cat": category if pi is not None else None,
                            "pred_idx": pi, "sim": sim, "cross": False})

    # --- 第2pass: 同カテゴリで未検出のものを他カテゴリから探す（採点基準 ■3-2）---
    # decisions と action_items は本質的に重なるため、分類の揺れを減点しない。
    for rec in records:
        if rec["pred_idx"] is not None:
            continue
        g, home = rec["gold"], rec["category"]
        gk = norm_text(g.get(CATEGORY_SPEC[home]["key"]))
        if not gk:
            continue
        best = (0.0, None, None)
        for other in cats:
            if other == home:
                continue
            okey = CATEGORY_SPEC[other]["key"]
            for pi, p in enumerate(preds[other]):
                if pi in used[other]:
                    continue
                s = similarity(gk, norm_text(p.get(okey)))
                if s > best[0]:
                    best = (s, other, pi)
        if best[0] >= SIM_THRESHOLD:
            used[best[1]].add(best[2])
            rec.update({"pred_cat": best[1], "pred_idx": best[2],
                        "sim": best[0], "cross": True})

    # --- 正誤判定 ---
    per_item = []
    for rec in records:
        g, home = rec["gold"], rec["category"]
        p = (preds[rec["pred_cat"]][rec["pred_idx"]]
             if rec["pred_idx"] is not None else None)
        # 判定は gold 側のカテゴリ規則で行う（何を照合すべきかは gold が決める）
        ok, review, reason = judge(g, p, home)
        if rec["pred_idx"] is not None and REVIEW_BAND[0] <= rec["sim"] < REVIEW_BAND[1]:
            review = True
            reason = reason + ";low_similarity(%.2f)" % rec["sim"]
        if rec["cross"]:
            review = True
            reason = reason + ";cross_category(%s->%s)" % (home, rec["pred_cat"])
        per_item.append({
            "category": home,
            "gold_id": g.get("id"),
            "difficulty": g.get("difficulty"),
            "matched": rec["pred_idx"] is not None,
            "cross_category": rec["cross"],
            "similarity": rec["sim"],
            "correct": ok,
            "needs_review": review,
            "reason": reason,
        })

    # --- goldにマッチしなかった出力項目を「許容」と「捏造」に振り分ける ---
    counts = {}
    for category in cats:
        allow = acceptable.get(category, []) or []
        # 許容リストは全カテゴリ分を見る（モデルの分類の揺れで不当に捏造扱いしないため）
        allow_all = []
        for c in cats:
            allow_all.extend(acceptable.get(c, []) or [])
        n_acceptable, n_halluc, n_noise = 0, 0, 0
        for pi, p in enumerate(preds[category]):
            if pi in used[category]:
                continue
            t = pred_text(p, category)
            if not t:
                continue
            is_acceptable = (best_ratio(t, allow) >= ACCEPTABLE_THRESHOLD or
                             best_ratio(t, allow_all) >= ACCEPTABLE_THRESHOLD)
            # key_numbers はラベルの揺れが大きいため、値の数値一致でも許容とする
            if (not is_acceptable) and category == "key_numbers":
                is_acceptable = value_in_candidates(p, allow_all)
            if is_acceptable:
                n_acceptable += 1          # 加点も減点もしない
            else:
                n_halluc += 1              # 捏造としてカウント
                if noise and best_ratio(t, noise) >= ACCEPTABLE_THRESHOLD:
                    n_noise += 1           # うちS4のノイズ区間由来のもの

        counts[category] = {
            "n_pred": len(preds[category]),
            "n_acceptable": n_acceptable,
            "n_hallucination": n_halluc,
            "n_noise_pickup": n_noise,
        }

    return per_item, counts


# ---------------------------------------------------------------- メイン

def load_gold():
    with open(os.path.join(ROOT, "dataset", "gold.json"), encoding="utf-8") as f:
        return json.load(f)


def parse_log_filename(name):
    """`qwen3.5-27b_think-True_trial3.json` から (model, thinking, trial) を取り出す。"""
    m = re.match(r"(.+)_think-(True|False)_trial(\d+)\.json$", name)
    if m:
        return m.group(1).replace("-", ":", 1), m.group(2) == "True", int(m.group(3))
    # 2段構え（run_twostage.py）のファイル名。thinking は第2段の設定を指す。
    m = re.match(r"(.+)_s2think-(True|False)_trial(\d+)\.json$", name)
    if m:
        return m.group(1).replace("-", ":", 1), m.group(2) == "True", int(m.group(3))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=os.path.join(ROOT, "logs", "thinking_bench_extract"))
    ap.add_argument("--out", default=os.path.join(ROOT, "results", "quality_eval.csv"))
    ap.add_argument("--detail", default=os.path.join(ROOT, "results", "quality_eval_items.csv"),
                    help="gold項目ごとの詳細（人手確認用）")
    args = ap.parse_args()

    gold = load_gold()
    gold_by_diff = {}
    for cat in ("decisions", "action_items", "key_numbers"):
        for g in gold.get(cat, []):
            gold_by_diff.setdefault(g.get("difficulty"), 0)
            gold_by_diff[g["difficulty"]] += 1

    if not os.path.isdir(args.logs):
        print("ログディレクトリが見つかりません: %s" % args.logs, file=sys.stderr)
        print("先に run_thinking_bench.py を実行してください。", file=sys.stderr)
        return 1

    files = sorted(f for f in os.listdir(args.logs) if f.endswith(".json"))
    if not files:
        print("採点対象のJSONログがありません: %s" % args.logs, file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    summary_rows = []
    detail_rows = []

    for fn in files:
        meta = parse_log_filename(fn)
        if meta is None:
            print("[skip] ファイル名を解釈できません: %s" % fn)
            continue
        model, thinking, trial = meta

        with open(os.path.join(args.logs, fn), encoding="utf-8") as f:
            resp = json.load(f)

        content = (resp.get("message") or {}).get("content")
        pred_obj, preprocess = extract_json(content)
        json_valid = pred_obj is not None and isinstance(pred_obj, dict)

        # 出力切れ（コンテキスト溢れ）の判定。
        # Thinkingの効果とコンテキスト溢れを混同しないため必ず分けて記録する。
        truncated = resp.get("done_reason") == "length"

        empty_counts = {c: {"n_pred": 0, "n_acceptable": 0,
                            "n_hallucination": 0, "n_noise_pickup": 0}
                        for c in CATEGORY_SPEC}
        if not json_valid:
            per_item, pred_counts = [], empty_counts
        else:
            per_item, pred_counts = score_one(gold, pred_obj)

        for it in per_item:
            row = {"model": model, "thinking": thinking, "trial": trial}
            row.update(it)
            detail_rows.append(row)

        # --- 集計 ---
        n_pred_total = sum(c["n_pred"] for c in pred_counts.values())
        n_acceptable = sum(c["n_acceptable"] for c in pred_counts.values())
        n_halluc = sum(c["n_hallucination"] for c in pred_counts.values())
        n_noise = sum(c["n_noise_pickup"] for c in pred_counts.values())
        n_correct_total = sum(1 for it in per_item if it["correct"])
        n_gold_total = sum(len(gold.get(c, [])) for c in CATEGORY_SPEC)

        def f1(correct, npred, nacc, ngold):
            """precision の分母から許容項目を除外する（不当減点を避けるため）。"""
            denom = npred - nacc
            p = correct / denom if denom > 0 else 0.0
            r = correct / ngold if ngold else 0.0
            return (2 * p * r / (p + r)) if (p + r) else 0.0, p, r

        base = {
            "model": model, "thinking": thinking, "trial": trial,
            "json_valid": json_valid, "preprocess": preprocess,
            "truncated": truncated,
            "done_reason": resp.get("done_reason"),
            "eval_count": resp.get("eval_count"),
            "n_pred_total": n_pred_total, "n_gold_total": n_gold_total,
            "n_correct_total": n_correct_total,
            "n_acceptable": n_acceptable,
            "n_hallucination": n_halluc,
            "n_noise_pickup": n_noise,
            "needs_review_count": sum(1 for it in per_item if it["needs_review"]),
        }
        ov_f1, ov_p, ov_r = f1(n_correct_total, n_pred_total, n_acceptable, n_gold_total)
        base["precision_overall"] = ov_p
        base["recall_overall"] = ov_r
        base["f1_overall"] = ov_f1

        for cat in CATEGORY_SPEC:
            c = sum(1 for it in per_item if it["category"] == cat and it["correct"])
            pc = pred_counts.get(cat, {})
            cf1, cp, cr = f1(c, pc.get("n_pred", 0), pc.get("n_acceptable", 0),
                             len(gold.get(cat, [])))
            base["f1_" + cat] = cf1
            base["precision_" + cat] = cp
            base["recall_" + cat] = cr

        for diff in DIFFICULTIES:
            items = [it for it in per_item if it["difficulty"] == diff]
            n = gold_by_diff.get(diff, 0)
            c = sum(1 for it in items if it["correct"])
            base["acc_" + diff] = (c / n) if n else 0.0
            base["n_correct_" + diff] = c
            base["n_gold_" + diff] = n

        # --- 実用グレード判定（議事録生成の用途を想定）---
        # 失敗の種類を区別する。取りこぼしと捏造では実務上の深刻さが違う。
        easy_perfect = base["n_correct_easy"] == base["n_gold_easy"] and base["n_gold_easy"] > 0
        med_perfect = base["n_correct_medium"] == base["n_gold_medium"]
        # 担当者・期限の誤り（未検出は除く。取りこぼしではなく「誤った値」だけを数える）
        wrong_field = sum(1 for it in per_item
                          if it["matched"] and not it["correct"]
                          and ("assignee_mismatch" in it["reason"]
                               or "due_mismatch" in it["reason"]))
        base["n_wrong_assignee_or_due"] = wrong_field

        if not json_valid:
            grade = "C"
            grade_reason = "JSONが壊れている(%s)" % preprocess
        elif not easy_perfect:
            grade = "C"
            grade_reason = "easyを落とした(%d/%d)" % (base["n_correct_easy"], base["n_gold_easy"])
        elif n_halluc > 0:
            grade = "C"
            grade_reason = "捏造%d件" % n_halluc
        elif med_perfect and wrong_field == 0:
            grade = "A"
            grade_reason = "easy/medium全問正解・捏造なし・担当期限の誤りなし"
        else:
            grade = "B"
            bits = []
            if not med_perfect:
                bits.append("medium取りこぼし%d件" % (base["n_gold_medium"] - base["n_correct_medium"]))
            if wrong_field:
                bits.append("担当/期限の誤り%d件" % wrong_field)
            grade_reason = "、".join(bits) if bits else "取りこぼしあり"
        base["grade"] = grade
        base["grade_reason"] = grade_reason

        summary_rows.append(base)
        print("%-14s think=%-5s t%d json=%-5s trunc=%-5s F1=%.3f 捏造=%-2d [%s] "
              "easy=%.2f med=%.2f size=%.2f think=%.2f impl=%.2f"
              % (model, thinking, trial, json_valid, truncated, ov_f1, n_halluc, grade,
                 base["acc_easy"], base["acc_medium"], base["acc_hard_size"],
                 base["acc_hard_thinking"], base["acc_hard_implicit"]))

    if summary_rows:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
            w.writeheader()
            w.writerows(summary_rows)
        print("\n書き出し: %s (%d行)" % (args.out, len(summary_rows)))

    if detail_rows:
        with open(args.detail, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(detail_rows[0].keys()))
            w.writeheader()
            w.writerows(detail_rows)
        print("書き出し: %s (%d行)" % (args.detail, len(detail_rows)))
        nr = sum(1 for r in detail_rows if r["needs_review"])
        if nr:
            print("※ 人手確認フラグ付き項目が %d 件あります（緩和マッチの限界）。"
                  " quality_eval_items.csv の needs_review 列を確認してください。" % nr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
