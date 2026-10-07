"""Print the user manual (/manual) to web/manual/SANE_Studio_Manual.pdf with page numbers.

Usage: python tools/manual_pdf.py [base_url]   (server must be running)
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "web" / "manual" / "SANE_Studio_Manual.pdf"
FOOTER = ("<div style='width:100%;font:9px IBM Plex Sans,Arial,sans-serif;color:#7a8697;padding:0 14mm;"
          "display:flex;justify-content:space-between'><span>SANE Studio user manual</span>"
          "<span>Page <span class='pageNumber'></span> of <span class='totalPages'></span></span></div>")


def jpeg_for_print(route):
    """Serve each screenshot as a JPEG while printing: Chromium embeds JPEGs as they are,
    but stores WebP losslessly, which made the PDF ten times larger."""
    import io

    from PIL import Image

    im = Image.open(ROOT / "web" / "manual" / "img" / Path(route.request.url).name).convert("RGB")
    if im.width > 1400:
        im = im.resize((1400, round(im.height * 1400 / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82, optimize=True, progressive=True)
    route.fulfill(status=200, content_type="image/jpeg", body=buf.getvalue())


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900}, color_scheme="light")
        page.route("**/static/manual/img/*.webp", jpeg_for_print)
        page.goto(f"{BASE}/manual")
        page.evaluate("""async () => {
            for (const i of document.images) i.loading = 'eager';
            await Promise.all([...document.images].map(i => i.complete ? null : new Promise(r => { i.onload = i.onerror = r; })));
            await document.fonts.ready;
        }""")
        page.pdf(path=str(PDF), format="A4", print_background=True, display_header_footer=True,
                 header_template="<span></span>", footer_template=FOOTER,
                 margin={"top": "14mm", "bottom": "16mm", "left": "14mm", "right": "14mm"})
        browser.close()
    print(f"{PDF} ({PDF.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
