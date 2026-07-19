#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Markdown をテクニカルペーパー体裁の PDF に変換する。

Markdown → HTML（日本語組版用CSS込み）→ Chrome のヘッドレスモードで PDF、という流れ。
追加のパッケージは不要で、macOS 標準の Chrome とヒラギノフォントだけを使う。

注意: Python 3.9 互換 / 標準ライブラリのみ。

使い方:
  python3 scripts/md2pdf.py ../docs/paper.md
  python3 scripts/md2pdf.py ../docs/paper.md --html-only   # HTMLだけ作って確認
"""

import argparse
import html as htmllib
import os
import re
import subprocess
import sys

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

CSS = """
/* 前作『日本語ビジネス文書タスクにおけるLLM量子化方式の比較評価』の体裁を踏襲 */
@page { size: A4; margin: 22mm 20mm 24mm 20mm; }
body {
  font-family: "Noto Sans CJK JP", "Hiragino Kaku Gothic ProN", "Hiragino Sans", sans-serif;
  font-size: 9.6pt; line-height: 1.85; color: #222;
  -webkit-font-feature-settings: "palt"; font-feature-settings: "palt";
  text-align: justify; word-wrap: break-word;
}

/* --- 表題まわり --- */
.eyebrow {
  font-size: 8pt; font-weight: 700; letter-spacing: 0.14em;
  color: #1f4e79; margin: 0 0 5mm;
}
h1 {
  font-size: 17pt; line-height: 1.45; font-weight: 700;
  color: #1f4e79; margin: 0 0 3mm; letter-spacing: -0.005em;
}
.subtitle { font-size: 8.6pt; line-height: 1.6; color: #666; margin: 0 0 5mm; }
.byline { font-size: 8.8pt; color: #333; margin: 0 0 4mm; }
.rule { border-top: 1.6pt solid #1f4e79; margin: 0 0 7mm; }

/* --- 要旨ボックス --- */
.abstract {
  background: #f4f6f8; border-left: 2.5pt solid #1f4e79;
  padding: 5mm 6mm; margin: 0 0 9mm;
}
.abstract .label {
  font-size: 8pt; font-weight: 700; letter-spacing: 0.12em;
  color: #1f4e79; margin-bottom: 3mm;
}
.abstract p { margin: 0 0 3mm; font-size: 9.3pt; }
.abstract p:last-child { margin-bottom: 0; }

/* --- 見出し --- */
h2 {
  font-size: 12.5pt; font-weight: 700; color: #1f4e79;
  margin: 10mm 0 3.5mm; padding-bottom: 1.8mm;
  border-bottom: 0.8pt solid #1f4e79; page-break-after: avoid;
}
h3 {
  font-size: 10.2pt; font-weight: 700; color: #1f4e79;
  margin: 6mm 0 2.5mm; page-break-after: avoid;
}
h4 { font-size: 9.8pt; font-weight: 700; margin: 4.5mm 0 2mm; page-break-after: avoid; }

p { margin: 0 0 3.5mm; }
ul, ol { margin: 0 0 4mm; padding-left: 6mm; }
li { margin-bottom: 1.6mm; }
strong { font-weight: 700; }

/* --- 表 --- */
table {
  border-collapse: collapse; width: 100%; margin: 3mm 0 5.5mm;
  font-size: 8.3pt; line-height: 1.55; page-break-inside: avoid;
}
th, td { border: 0.4pt solid #c5ccd3; padding: 1.5mm 2.2mm; vertical-align: top; }
th { background: #eaeff4; color: #1f4e79; font-weight: 700; white-space: nowrap; }
tr:nth-child(even) td { background: #fafbfc; }

/* --- コード・引用 --- */
code {
  font-family: "DejaVu Sans Mono", Menlo, monospace; font-size: 8.3pt;
  background: #f0f2f4; padding: 0.3mm 1mm; border-radius: 1.5pt;
}
pre {
  background: #f4f6f8; border-left: 2.5pt solid #8fa6bd; padding: 3mm 4mm;
  margin: 3mm 0 5mm; font-size: 8pt; line-height: 1.6;
  page-break-inside: avoid; white-space: pre-wrap; word-break: break-all;
}
pre code { background: none; padding: 0; font-size: inherit; }
blockquote {
  margin: 3mm 0 5mm; padding: 2.5mm 4mm; background: #f4f6f8;
  border-left: 2.5pt solid #8fa6bd; font-size: 9pt;
}
blockquote p:last-child { margin-bottom: 0; }
hr { border: none; border-top: 0.5pt solid #d5dae0; margin: 7mm 0; }
a { color: #1f4e79; text-decoration: underline; }
"""


def esc(t):
    return htmllib.escape(t, quote=False)


def inline(t):
    """行内記法（コード・強調・リンク）を変換する。コードを先に退避する。"""
    codes = []

    def stash(m):
        codes.append(m.group(1))
        return "\x00%d\x00" % (len(codes) - 1)

    t = re.sub(r"`([^`]+)`", stash, t)
    t = esc(t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', t)
    for i, c in enumerate(codes):
        t = t.replace("\x00%d\x00" % i, "<code>%s</code>" % esc(c))
    return t


def convert(md):
    """必要な記法だけを扱う軽量コンバータ。外部ライブラリを使わないため。"""
    out = []
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        l = lines[i]

        # コードブロック
        if l.startswith("```"):
            i += 1
            body = []
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(lines[i]); i += 1
            i += 1
            out.append("<pre><code>%s</code></pre>" % esc("\n".join(body)))
            continue

        # 表
        if l.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i]); i += 1
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            # 2行目が区切り行なら見出しあり
            hdr = len(cells) > 1 and all(set(c) <= set("-: ") for c in cells[1])
            out.append("<table>")
            for j, row in enumerate(cells):
                if hdr and j == 1:
                    continue
                tag = "th" if (hdr and j == 0) else "td"
                out.append("<tr>" + "".join(
                    "<%s>%s</%s>" % (tag, inline(c), tag) for c in row) + "</tr>")
            out.append("</table>")
            continue

        # 引用
        if l.startswith(">"):
            body = []
            while i < len(lines) and lines[i].startswith(">"):
                body.append(lines[i].lstrip(">").strip()); i += 1
            out.append("<blockquote><p>%s</p></blockquote>" % inline(" ".join(body)))
            continue

        # 箇条書き
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)", l)
        if m:
            ordered = not m.group(2) in ("-", "*")
            tag = "ol" if ordered else "ul"
            out.append("<%s>" % tag)
            while i < len(lines):
                mm = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)", lines[i])
                if not mm:
                    # 継続行（インデントされた続き）
                    if lines[i].startswith("  ") and lines[i].strip():
                        out[-1] = out[-1][:-5] + " " + inline(lines[i].strip()) + "</li>"
                        i += 1
                        continue
                    break
                out.append("<li>%s</li>" % inline(mm.group(3)))
                i += 1
            out.append("</%s>" % tag)
            continue

        # 見出し
        m = re.match(r"^(#{1,4})\s+(.*)", l)
        if m:
            lv = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lv, inline(m.group(2)), lv))
            i += 1
            continue

        if l.strip() == "---":
            out.append("<hr>"); i += 1; continue

        if not l.strip():
            i += 1; continue

        # 段落（空行まで結合）
        para = []
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^(#|\||>|```|---|\s*([-*]|\d+\.)\s)", lines[i]):
            para.append(lines[i].strip()); i += 1
        if para:
            # 原則は結合する（日本語の禁則処理を効かせるため）。
            # ただし「ラベル: 値」形式の短い行が続く場合（表題直後の書誌情報など）は
            # 改行を保持する。本文が誤って分割されないよう条件を厳しくしている。
            meta = (len(para) > 1 and all(len(x) < 60 for x in para)
                    and sum(1 for x in para if re.match(r"^[^。]{1,12}[:：]", x))
                        >= len(para) - 1)
            if meta:
                out.append("<p>%s</p>" % "<br>".join(inline(x) for x in para))
            else:
                out.append("<p>%s</p>" % inline("".join(para)))
    return "\n".join(out)


FRONT_RE = re.compile(
    r"^#\s+(?P<title>.+?)\n+"
    r"(?P<meta>(?:\*\*.+?\*\*\n+|.+?:\s*.+?\n+)+)"
    r"(?:---\n+)?"
    r"##\s*要旨\n+(?P<abst>.*?)(?=\n---\n|\n## )", re.S)


def build_front(md):
    """表題・書誌情報・要旨を、前作の体裁に合わせた専用HTMLに組み替える。

    前作は「アイブロウ → 表題 → 著者情報 → 太罫 → 要旨ボックス」という構成。
    Markdown側は普通の見出しのまま書けるよう、ここで変換する。
    """
    m = FRONT_RE.search(md)
    if not m:
        return None, md
    title = m.group("title").strip()
    meta = [l.strip() for l in m.group("meta").strip().split("\n") if l.strip()]
    abst = m.group("abst").strip()

    html = ['<div class="eyebrow">TECHNICAL PAPER — 株式会社喋ラボ</div>']
    html.append("<h1>%s</h1>" % inline(title))
    if meta:
        html.append('<div class="byline">%s</div>'
                    % "<br>".join(inline(x) for x in meta))
    html.append('<div class="rule"></div>')
    html.append('<div class="abstract"><div class="label">ABSTRACT</div>')
    for para in re.split(r"\n\s*\n", abst):
        para = para.strip()
        if not para:
            continue
        if para.startswith(("-", "*", "1.")):
            html.append(convert(para))
        else:
            html.append("<p>%s</p>" % inline("".join(
                l.strip() for l in para.split("\n"))))
    html.append("</div>")
    return "\n".join(html), md[m.end():]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("md")
    ap.add_argument("--html-only", action="store_true")
    args = ap.parse_args()

    src = os.path.abspath(args.md)
    with open(src, encoding="utf-8") as f:
        md = f.read()

    title = md.split("\n")[0].lstrip("# ").strip()
    front, rest = build_front(md)
    body = (front + "\n" + convert(rest)) if front else convert(md)
    doc = ("<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
           "<title>%s</title><style>%s</style></head><body>%s</body></html>"
           % (esc(title), CSS, body))

    htmlp = os.path.splitext(src)[0] + ".html"
    with open(htmlp, "w", encoding="utf-8") as f:
        f.write(doc)
    print("HTML: %s" % htmlp)
    if args.html_only:
        return 0

    pdfp = os.path.splitext(src)[0] + ".pdf"
    if not os.path.exists(CHROME):
        print("Chrome が見つかりません: %s" % CHROME, file=sys.stderr)
        print("HTMLをブラウザで開き、Cmd+P →「PDFとして保存」でも出力できます。")
        return 1
    r = subprocess.run([CHROME, "--headless", "--disable-gpu",
                        "--no-pdf-header-footer", "--print-to-pdf=" + pdfp,
                        "file://" + htmlp],
                       capture_output=True, timeout=300, encoding="utf-8")
    if not os.path.exists(pdfp):
        print("PDF生成に失敗:\n%s" % (r.stderr or "")[-500:], file=sys.stderr)
        return 1
    print("PDF : %s (%.1f KB)" % (pdfp, os.path.getsize(pdfp) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
