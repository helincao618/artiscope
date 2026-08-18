"""Runtime configuration and asset discovery.

The integration surface is deliberately a directory of USD files, matching how
deliveries actually arrive: someone drops a folder on disk. Point
``ARTISCOPE_ASSET_DIR`` at it and every asset underneath becomes
reviewable, with no ingest step, database or registration.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


# .usdz is Pixar's own single-file package (an uncompressed zip with the
# default layer first) and needs no special handling here: pxr's own stage
# resolver mounts it, so it flows through the same reader as a bare .usd.
USD_SUFFIXES = (".usd", ".usda", ".usdc", ".usdz")

# URDF is read but never written, and gets no engine verdict -- see
# DIRECTION.md. It is here so a robot description someone hands you can at
# least be opened and driven.
URDF_SUFFIXES = (".urdf",)

ASSET_SUFFIXES = (*USD_SUFFIXES, *URDF_SUFFIXES)

REPO_ROOT = Path(__file__).resolve().parents[1]

# The examples shipped with the repo, so a fresh checkout has something to
# open before it has anything of yours. Each carries a documented defect and is
# small enough to read as text -- see their ``doc`` strings and examples/README.
#
# A directory of one folder per asset is the shape to point this at, not one
# delivery nested inside another: keys come from the containing folder, so
# overlapping trees make two assets fight over one key. The test fixtures
# resolve this path independently rather than reading this setting, so that
# pointing the service elsewhere cannot silently change what the suite covers
# (see ``tests/conftest.py``).
_DEFAULT_ASSET_DIR = REPO_ROOT / "examples"


@dataclass(frozen=True)
class Settings:
    """Resolved service settings.

    Attributes
    ----------
    asset_dir : pathlib.Path
        Directory scanned for USD assets.
    cache_dir : pathlib.Path
        Where exported GLBs are kept between requests.
    face_budget : int
        Per-part face ceiling handed to the mesh exporter.
    patch_dir : pathlib.Path, optional
        Directory of local range overrides, or ``None`` to load none. Off
        unless asked for, so a fresh checkout and anything shown to whoever
        delivered the asset see the file's own numbers and nothing else.
    """

    asset_dir: Path
    cache_dir: Path
    face_budget: int
    patch_dir: Path | None = None


def load_settings() -> Settings:
    """Read settings from the environment, falling back to sane defaults.

    Returns
    -------
    Settings
        Resolved settings. The cache directory is created.
    """
    asset_dir = Path(
        os.environ.get("ARTISCOPE_ASSET_DIR", _DEFAULT_ASSET_DIR)
    ).expanduser()
    cache_dir = Path(
        os.environ.get("ARTISCOPE_CACHE_DIR", REPO_ROOT / "cache")
    ).expanduser()

    cache_dir.mkdir(parents=True, exist_ok=True)

    raw_patch_dir = os.environ.get("ARTISCOPE_PATCH_DIR")

    return Settings(
        asset_dir=asset_dir,
        cache_dir=cache_dir,
        face_budget=int(os.environ.get("ARTISCOPE_FACE_BUDGET", "150000")),
        patch_dir=Path(raw_patch_dir).expanduser() if raw_patch_dir else None,
    )


def _preferred_key(usd_path: Path, asset_dir: Path) -> str:
    """Choose a human-meaningful key for an asset.

    Assets are typically delivered as one folder per asset holding a USD plus
    its textures, and in that layout the folder is what people call the thing.
    ``Dishwasher/Dishwasher.usd`` and
    ``Dishwasher_with_drive/Dishwasher.usd`` share a file stem but are
    different assets, so the stem alone is not usable as a key.

    Parameters
    ----------
    usd_path : pathlib.Path
        Path to the USD file.
    asset_dir : pathlib.Path
        Root of the scan, never used as a key itself.

    Returns
    -------
    str
        Candidate key, not yet checked for uniqueness.
    """
    parent = usd_path.parent
    if parent != asset_dir:
        siblings = [
            p for p in parent.iterdir() if p.suffix.lower() in ASSET_SUFFIXES
        ]
        if len(siblings) == 1:
            return parent.name
    return usd_path.stem


def _walk_files(root: Path) -> Iterator[Path]:
    """Yield every file under ``root``, descending into symlinked directories.

    ``Path.rglob`` refuses to follow directory symlinks, so a delivery linked
    in from elsewhere scans as empty: the assets simply never appear, and the
    count reported to the UI looks perfectly plausible. Linking a delivery in
    rather than copying it is the normal case here (see ``_DEFAULT_ASSET_DIR``),
    so a scan that skips links is wrong rather than merely conservative.

    Parameters
    ----------
    root : pathlib.Path
        Directory to walk.

    Yields
    ------
    pathlib.Path
        Every file found underneath, symlinked subtrees included. Paths stay
        expressed through ``root``, so they remain relative to the scan root
        even when the bytes live elsewhere.

    Notes
    -----
    Following links admits the possibility of a cycle, so each directory is
    entered at most once by resolved path. That also means two links onto the
    same delivery contribute it once instead of twice.
    """
    seen: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        here = Path(dirpath)
        resolved = here.resolve()
        if resolved in seen:
            dirnames.clear()
            continue
        seen.add(resolved)
        for name in filenames:
            yield here / name


def discover_assets(asset_dir: Path) -> dict[str, Path]:
    """Find every USD asset under ``asset_dir``.

    Parameters
    ----------
    asset_dir : pathlib.Path
        Directory to scan, recursively.

    Returns
    -------
    dict
        Mapping of asset key to USD path, sorted by key. Empty when the
        directory does not exist, so a misconfigured path shows up as an empty
        list in the UI rather than a crash on startup.

    Notes
    -----
    Symlinked subdirectories are scanned like real ones; see ``_walk_files``.
    """
    if not asset_dir.is_dir():
        return {}

    assets: dict[str, Path] = {}
    for path in sorted(_walk_files(asset_dir)):
        if path.suffix.lower() not in ASSET_SUFFIXES:
            continue

        key = _preferred_key(path, asset_dir)
        if key in assets:
            relative = path.relative_to(asset_dir).with_suffix("")
            key = str(relative).replace(os.sep, "--")
        assets[key] = path

    return dict(sorted(assets.items()))
