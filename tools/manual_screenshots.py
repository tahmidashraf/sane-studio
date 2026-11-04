"""Capture every screen and option state of SANE Studio for the user manual.

Drives a running server like a user would (Playwright + Chromium) and writes PNGs to
web/manual/img. Uses the bundled sample dataset so anyone can reproduce the pictures.

Usage: python tools/manual_screenshots.py [base_url]   (server must be running)
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "manual" / "img"
# extra files for the "mixed upload" picture: a real EEG file with many pass-through
# channels and an eye-tracker file that shares the .edf extension
MIXED = ["D:/sane_testdata/edf/sub-01_ses-01_task-offline_run-01_eeg.edf",
         "D:/sane_testdata2/edf/ds002312/sub-A0100_task-OcularLDT_run-02_eyetrack.edf"]


def shot(target, name, **kw):
    target.screenshot(path=str(OUT / f"{name}.png"), **kw)
    print("saved", name)


def unpin(page):
    """The top bar is sticky; unpinned it cannot cover tall element screenshots."""
    page.add_style_tag(content="header.topbar { position: static !important; }")


def set_range(page, sel, value):
    page.eval_on_selector(sel, "(e, v) => { e.value = v; e.dispatchEvent(new Event('input', {bubbles: true})); }", value)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, color_scheme="light")
        pg = ctx.new_page()

        # ---- 1. home / upload
        pg.goto(BASE)
        pg.wait_for_selector("#samples .sample")
        unpin(pg)
        pg.wait_for_timeout(600)
        shot(pg, "home")
        shot(pg.locator("header.topbar"), "topbar-start")
        shot(pg.locator("#drop"), "home-dropzone")
        shot(pg.locator("#samples"), "home-samples")
        shot(pg.locator("aside.how"), "home-how")
        shot(pg.locator("footer.site"), "footer")

        # ---- 2. review (sample dataset: three subjects -> cohort)
        pg.locator("#samples .sample").first.click()
        pg.wait_for_selector("#screenConfig:not([hidden])")
        pg.wait_for_timeout(600)
        shot(pg, "review")
        shot(pg.locator("header.topbar"), "topbar-session")
        shot(pg.locator(".config > .panel").first, "review-table")
        params = pg.locator("aside.params")
        shot(params, "settings-auto-cohort")
        set_range(pg, "#spAlpha", 0.5)
        shot(params, "settings-spatial-manual")
        pg.click("#spAuto")
        pg.click("#refSeg [data-v=normative]")
        shot(params, "settings-normative")
        pg.click("#refSeg [data-v=auto]")
        pg.locator(".sub-in").first.fill("sub-002")
        shot(pg.locator(".config > .panel").first, "review-subject-edit")
        pg.locator(".sub-in").first.fill("sub-002")

        # ---- 3. processing
        pg.click("#runBtn")
        pg.wait_for_selector("#screenProc:not([hidden])")
        pg.wait_for_timeout(900)
        shot(pg, "processing")

        # ---- 4. results workspace
        pg.wait_for_selector("#screenWork:not([hidden])", timeout=600_000)
        pg.wait_for_timeout(3000)
        shot(pg, "workspace")
        shot(pg.locator("aside.rail"), "ws-rail")
        shot(pg.locator(".rec-head"), "ws-header")
        shot(pg.locator("#kpis"), "ws-kpis")
        viewer = pg.locator(".panel.viewer")
        shot(viewer, "viewer-split")
        shot(pg.locator(".viewer .toolbar"), "viewer-toolbar")
        for mode in ("overlay", "in", "out", "diff"):
            pg.click(f"#viewSeg [data-v={mode}]")
            pg.wait_for_timeout(1500)
            shot(viewer, f"viewer-{mode}")
        pg.click("#viewSeg [data-v=split]")
        pg.select_option("#bandSel", "theta")
        pg.wait_for_timeout(1500)
        shot(viewer, "viewer-band-theta")
        pg.select_option("#bandSel", "broad")
        pg.wait_for_timeout(800)
        values = pg.eval_on_selector_all("#chSel option", "els => els.map(e => e.value)")
        region = next((v for v in values if v.startswith("r:")), None)
        if region:
            pg.select_option("#chSel", region)
            pg.wait_for_timeout(1000)
            shot(viewer, "viewer-region")
        if "aux" in values:
            pg.select_option("#chSel", "aux")
            pg.wait_for_timeout(1000)
            shot(viewer, "viewer-aux-only")
        pg.select_option("#chSel", "all")
        pg.wait_for_timeout(1500)
        box = pg.locator("#cvIn").bounding_box()
        pg.mouse.move(box["x"] + box["width"] * 0.45, box["y"] + box["height"] * 0.22)
        pg.wait_for_timeout(400)
        shot(pg.locator("#plots"), "viewer-hover")
        pg.mouse.click(box["x"] + box["width"] * 0.45, box["y"] + box["height"] * 0.22)
        pg.mouse.move(box["x"] - 40, box["y"] - 40)
        pg.wait_for_timeout(800)
        shot(viewer, "viewer-selected")
        shot(pg.locator(".timeline"), "viewer-timeline")
        pg.click("#scaleUp")
        pg.click("#scaleUp")
        pg.wait_for_timeout(500)
        shot(viewer, "viewer-scale-up")
        diff = pg.locator(".diffmap")
        shot(diff, "viewer-diff-line")
        pg.click("#diffMapSeg [data-v=map]")
        pg.wait_for_timeout(500)
        shot(diff, "viewer-diff-map")
        pg.click("#diffMapSeg [data-v=line]")
        pg.click("#scaleDown")
        pg.click("#scaleDown")

        tabs = pg.locator(".panel:has(#tabs)")
        for t in ("spectrum", "bands", "topo", "spatial", "report"):
            pg.click(f"#tabs [data-t={t}]")
            pg.wait_for_timeout(1800)
            shot(tabs, f"tab-{t}")
        pg.click("#tabs [data-t=topo]")
        pg.select_option("#topoSel", "alpha")
        pg.wait_for_timeout(1200)
        shot(tabs, "tab-topo-alpha")
        pg.click("#tabs [data-t=spectrum]")
        pg.wait_for_timeout(800)

        pg.click("#themeBtn")
        pg.wait_for_timeout(1500)
        shot(pg, "workspace-dark")
        pg.click("#themeBtn")
        pg.wait_for_timeout(500)

        # ---- 5. expired session message
        pg.goto(f"{BASE}/#000000000000")
        pg.wait_for_selector("#toast:not([hidden])")
        shot(pg, "toast-expired")

        # ---- 6. a mixed upload: ready rows with pass-through summaries, a skipped file
        files = [f for f in MIXED if Path(f).exists()]
        if files:
            pg.goto(BASE)
            pg.wait_for_selector("#samples .sample")
            unpin(pg)
            pg.set_input_files("#fileInput", files)
            pg.wait_for_selector("#screenConfig:not([hidden])", timeout=600_000)
            pg.wait_for_timeout(800)
            det = pg.locator("#recRows details.pass-list")
            if det.count():
                det.first.evaluate("d => d.open = true")
            shot(pg.locator(".config > .panel").first, "review-mixed")

        # ---- 7. phone width
        phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True)
        pp = phone.new_page()
        pp.goto(BASE)
        pp.wait_for_selector("#samples .sample")
        pp.wait_for_timeout(600)
        shot(pp, "phone-home")
        browser.close()


def to_webp(max_width=1800, quality=86):
    """PNG -> WebP, at most 1800 px wide: sharp on high-density screens, about 8 MB for the set."""
    from PIL import Image

    for png in sorted(OUT.glob("*.png")):
        im = Image.open(png)
        if im.width > max_width:
            im = im.resize((max_width, round(im.height * max_width / im.width)), Image.LANCZOS)
        im.save(png.with_suffix(".webp"), "WEBP", quality=quality, method=6)
        png.unlink()


if __name__ == "__main__":
    main()
    to_webp()
