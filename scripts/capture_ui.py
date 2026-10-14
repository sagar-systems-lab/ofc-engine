"""Capture synthetic UI review frames from the local read-only preview."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import os

from playwright.sync_api import sync_playwright
from smoke_hosted import normalize_url


SHOTS = (
    ("home-desktop", "/", 1440, 900, "#home-title", None),
    ("home-mobile", "/", 390, 844, "#home-title", None),
    ("plan-desktop", "/", 1440, 900, "#headline", "[data-view-link='plan']"),
    ("proof-desktop", "/proof", 1440, 900, "#run-status", None),
    ("recorded-desktop", "/recorded", 1440, 900, "#import-status", None),
)


def capture(origin: str, directory: Path) -> None:
    origin = normalize_url(origin)
    directory.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=os.environ.get("OFC_CHROMIUM_PATH")
        )
        try:
            for name, path, width, height, marker, click in SHOTS:
                page = browser.new_page(
                    viewport={"width": width, "height": height},
                    device_scale_factor=1,
                    reduced_motion="reduce",
                )
                outside = []
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.on("request", lambda request:
                        outside.append(request.url)
                        if not request.url.startswith(origin + "/") else None)
                try:
                    print(f"CAPTURE_FRAME: {name}", flush=True)
                    page.goto(origin + path, wait_until="domcontentloaded", timeout=30000)
                    if click:
                        page.locator(click).click()
                    page.locator(marker).wait_for(state="visible", timeout=12000)
                    page.screenshot(
                        path=str(directory / f"{name}.png"),
                        full_page=True,
                        animations="disabled",
                    )
                    if outside or errors:
                        raise RuntimeError(f"{name}: unexpected request or browser error")
                    if page.evaluate(
                        "document.documentElement.scrollWidth > innerWidth + 1"
                    ):
                        raise RuntimeError(f"{name}: horizontal overflow")
                finally:
                    page.close()
        finally:
            browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        capture(args.base_url, args.output)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"UI_CAPTURE_FAILED: {error}", file=sys.stderr)
        return 1
    print(f"UI_CAPTURE_PASS: {args.output} ({len(SHOTS)} views)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
