"""Capture viewport screenshots of each display mode, for docs and slides.

Most of what this tool produces is a picture, and a picture is the one thing a
test suite cannot check. This drives the real page in a real browser and
writes the frames out so they can be looked at.

Usage
-----
``python -m tools.shots [OUT_DIR]``

Requires the service to be importable and playwright's chromium to be
installed. Starts and stops its own server on a free port.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

# One entry per picture: file stem, the toggles to switch on, and how long to
# let the scene settle before shooting.
SHOTS: list[tuple[str, list[str], int]] = [
    ("rest", [], 400),
    ("labels", ["#toggleGizmoAll"], 400),
    ("envelope", ["#toggleGizmoAll", "#toggleEnvelope"], 600),
    ("exploded", ["#toggleExploded"], 600),
]

DEFAULT_TOGGLES_ON = ["#toggleLabels"]


def _free_port() -> int:
    """Return a port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_service(port: int) -> subprocess.Popen:
    """Launch uvicorn on ``port`` and wait for it to answer."""
    process = subprocess.Popen(
        [
            str(VENV_PYTHON), "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return process
        except OSError:
            time.sleep(0.5)
    process.terminate()
    raise RuntimeError("service did not start")


def _stop_service(process: subprocess.Popen) -> None:
    """Shut the service down, escalating to SIGKILL if it ignores SIGTERM.

    A teardown that raises drowns out whatever the real failure was, which is
    exactly when the traceback matters most.
    """
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def capture(out_dir: Path, asset: str | None = None) -> list[Path]:
    """Write one screenshot per display mode.

    Parameters
    ----------
    out_dir : pathlib.Path
        Directory to write PNGs into. Created if absent.
    asset : str, optional
        Asset key to shoot. Defaults to whichever loads first.

    Returns
    -------
    list of pathlib.Path
        The files written.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    process = _start_service(port)
    written: list[Path] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                channel="chrome",
                args=[
                    "--no-sandbox",
                    "--use-gl=angle",
                    "--use-angle=swiftshader",
                    # Newer Chrome refuses swiftshader for WebGL without this,
                    # and the viewport then never renders the model at all.
                    "--enable-unsafe-swiftshader",
                ],
            )
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            # Idle behaviour off: a tour mid-frame would make every shot
            # depend on when it happened to be taken.
            # `networkidle` is unreliable across Chrome builds and is not the
            # readiness signal anyway: the parts list appearing is.
            page.goto(f"http://127.0.0.1:{port}/?idle=999999", wait_until="domcontentloaded")
            page.wait_for_selector(".object-row", timeout=60_000)
            if asset:
                page.select_option("#assetSelect", asset)
            page.wait_for_timeout(4000)  # let the intro sweep finish
            page.click("#btnReset")

            # The display toggles live in a dropdown that is closed by default,
            # and `check` refuses to touch what is not visible. It overlays the
            # canvas, though, so it is opened to click and shut again before
            # the shutter -- otherwise every frame has a menu across it.
            def _panel(visible: bool) -> None:
                page.eval_on_selector(
                    "#displayPanel", f"el => el.hidden = {str(not visible).lower()}"
                )

            for stem, toggles, settle in SHOTS:
                _panel(True)
                for selector in DEFAULT_TOGGLES_ON:
                    page.check(selector)
                for selector in toggles:
                    page.check(selector)
                _panel(False)
                page.wait_for_timeout(settle)

                path = out_dir / f"{stem}.png"
                page.query_selector("#canvasHost canvas").screenshot(path=str(path))
                written.append(path)

                _panel(True)
                for selector in toggles:
                    page.uncheck(selector)
                _panel(False)

            browser.close()
    finally:
        _stop_service(process)

    return written


def main() -> None:
    """Entry point."""
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "shots"
    for path in capture(out_dir):
        print(path)


if __name__ == "__main__":
    main()
