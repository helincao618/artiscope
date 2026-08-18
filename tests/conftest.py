"""Test fixtures.

The reference assets are the ones in ``examples/``, hand-authored as USDA text
so that a fresh checkout runs the whole suite with nothing to download. Each
one carries a specific, documented defect -- read its ``doc`` string -- and the
numbers asserted against it are the numbers it authors. A reader that stops
reproducing them has regressed.

The tree is addressed by its own constant rather than through the service's
asset directory, even though both currently point at ``examples/``. Sharing one
setting meant whatever someone pointed the service at silently decided what the
suite tested, and a path that did not resolve silently decided that most of the
suite did not run. Override with ``ARTISCOPE_FIXTURE_DIR`` to run the same
assertions against a corpus of your own.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_FIXTURE_DIR_ENV = "ARTISCOPE_FIXTURE_DIR"

ASSET_ROOT = (
    Path(os.environ[_FIXTURE_DIR_ENV]).expanduser()
    if os.environ.get(_FIXTURE_DIR_ENV)
    else REPO_ROOT / "examples"
)


@pytest.fixture(scope="session")
def asset_root() -> Path:
    """Return the reference asset tree, skipping when it is not present.

    Returns
    -------
    pathlib.Path
        Directory holding the reference assets.
    """
    if not ASSET_ROOT.is_dir():
        pytest.skip(f"reference assets not available: {ASSET_ROOT}")
    return ASSET_ROOT


def _asset(folder: str, stem: str) -> Path:
    """Return the path to a reference USD, skipping the test when absent.

    Parameters
    ----------
    folder : str
        Asset folder name under the example tree.
    stem : str
        USD file stem.

    Returns
    -------
    pathlib.Path
        Path to the USD file.
    """
    path = ASSET_ROOT / folder / f"{stem}.usda"
    if not path.exists():
        pytest.skip(f"reference asset not available: {path}")
    return path


@pytest.fixture(scope="session")
def cabinet_usd() -> Path:
    """Five-door cabinet: revolute joints only, all masses authored."""
    return _asset("Cabinet", "Cabinet")


@pytest.fixture(scope="session")
def dishwasher_usd() -> Path:
    """Dishwasher: revolute and prismatic mixed, one genuinely driven joint."""
    return _asset("Dishwasher", "Dishwasher")


@pytest.fixture(scope="session")
def oven_usd() -> Path:
    """Wall oven: holds a FixedJoint welding the glass onto the door.

    The reference case for "an asset states more than we read". Dropping that
    weld silently is what once made this asset look structurally broken.
    """
    return _asset("WallOven", "WallOven")
