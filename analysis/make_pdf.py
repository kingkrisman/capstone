"""
Render the PDF report (report/Logistics_Operations_Review.pdf) from analysis/output/data.json.

Usage (from the repo root, after running analysis.py):
    pip install playwright && playwright install chromium
    python analysis/make_pdf.py            # uses Google Fonts
    python analysis/make_pdf.py FONT_DIR   # or a local folder holding geist-sans/ and geist-mono/
"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
tpl = (ROOT / "analysis" / "report_template.html").read_text(encoding="utf-8")
data = (ROOT / "analysis" / "output" / "data.json").read_text(encoding="utf-8")
html = tpl.replace("__DATA__", data)

if len(sys.argv) > 1:
    html = html.replace("__FONTDIR__", Path(sys.argv[1]).resolve().as_uri())
else:
    html = html.replace('<meta charset="utf-8">', '<meta charset="utf-8"><link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">')

tmp = ROOT / "analysis" / "output" / "report.html"
tmp.write_text(html, encoding="utf-8")
out = ROOT / "report" / "Logistics_Operations_Review.pdf"
out.parent.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1123, "height": 794})
    page.goto(tmp.as_uri())
    page.wait_for_selector("body[data-ready='1']")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(500)
    page.pdf(path=str(out), width="297mm", height="210mm", print_background=True, prefer_css_page_size=True)
    browser.close()
print(f"wrote {out}")
