"""Markdown -> 스타일 HTML -> Edge 헤드리스 인쇄 -> PDF.

한글(맑은 고딕) + 박스 다이어그램/코드(굴림체 = 한글 모노스페이스로 정렬 보존) +
표 테두리를 갖춘 기획서용 PDF를 생성한다.

사용:
    python scripts/md_to_pdf.py docs/종합기획서_v2.1.md exports/종합기획서_v2.1.pdf
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import markdown

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

CSS = """
@page { size: A4; margin: 17mm 15mm 16mm 15mm; }
* { box-sizing: border-box; }
body {
  font-family: 'Malgun Gothic','맑은 고딕', sans-serif;
  font-size: 10.3pt; line-height: 1.55; color: #1f2328;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
h1 { font-size: 20pt; color: #0b3d66; border-bottom: 3px solid #0b3d66;
     padding-bottom: 6px; margin: 0 0 14px; }
h2 { font-size: 14.5pt; color: #0b3d66; border-bottom: 1px solid #c9d6e2;
     padding-bottom: 4px; margin: 22px 0 10px; page-break-after: avoid; }
h3 { font-size: 12pt; color: #14507f; margin: 16px 0 7px; page-break-after: avoid; }
p, li { margin: 5px 0; }
blockquote { margin: 8px 0; padding: 7px 12px; background: #eef3f8;
  border-left: 4px solid #5b8db8; color: #41505e; font-size: 9.6pt; }
strong { color: #0b3d66; }
table { border-collapse: collapse; width: 100%; margin: 9px 0;
  font-size: 9.3pt; page-break-inside: avoid; }
th, td { border: 1px solid #c2ccd6; padding: 4px 7px; text-align: left;
  vertical-align: top; }
th { background: #e8eef5; color: #0b3d66; font-weight: 600; }
tr:nth-child(even) td { background: #f7f9fb; }
code { font-family: 'D2Coding','GulimChe','굴림체', monospace;
  background: #eef1f4; color: #b1361e; padding: 0.5px 4px; border-radius: 3px;
  font-size: 9pt; }
pre { font-family: 'GulimChe','굴림체','D2Coding', monospace;
  background: #f6f8fa; border: 1px solid #d7dee5; border-radius: 6px;
  padding: 10px 12px; font-size: 8.3pt; line-height: 1.28;
  white-space: pre; overflow: hidden; page-break-inside: avoid; }
pre code { background: none; color: #1f2328; padding: 0; font-size: inherit; }
hr { border: none; border-top: 1px solid #d0d7de; margin: 18px 0; }
em { color: #50607a; }
"""


def build_html(md_path: Path) -> str:
    text = md_path.read_text(encoding="utf-8")
    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "toc", "nl2br"],
    )
    return (
        "<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


def main() -> None:
    md_path = Path(sys.argv[1]).resolve()
    pdf_path = Path(sys.argv[2]).resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    html_path = pdf_path.with_suffix(".html")
    html_path.write_text(build_html(md_path), encoding="utf-8")

    subprocess.run(
        [
            EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=10000",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ],
        check=True,
        timeout=120,
    )
    html_path.unlink(missing_ok=True)
    size_kb = pdf_path.stat().st_size / 1024
    print(f"PDF 생성 완료: {pdf_path} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
