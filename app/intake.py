"""Accept an asset dropped into the browser and place it in the asset library.

The library is a directory on disk, so taking delivery of a new asset means
writing a folder into it -- there is no database to register with and no
ingest pipeline to run. That keeps the drop path honest: whatever the scanner
would have found had someone copied the folder by hand is exactly what it
finds after an upload.

Three shapes arrive in practice. A bare ``.usd``/``.usdz`` travels fine on its
own -- ``.usdz`` already carries its textures inside the package -- and a
``.zip`` of the asset folder is how anything else with sibling textures has to
travel. Everything else is refused rather than guessed at.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import ASSET_SUFFIXES

ZIP_SUFFIX = ".zip"

# A delivery that unpacks to more than this is far more likely to be a whole
# scene library than one asset, and silently filling the disk is a worse
# outcome than making someone unzip it themselves.
MAX_UNPACKED_BYTES = 2_000_000_000


class IntakeError(Exception):
    """Raised when an upload cannot be accepted."""


@dataclass(frozen=True)
class IntakeResult:
    """Outcome of accepting one upload.

    Attributes
    ----------
    key : str
        Asset key the scanner will expose it under.
    directory : pathlib.Path
        Folder created inside the asset library.
    usd_count : int
        Number of USD files that landed.
    """

    key: str
    directory: Path
    usd_count: int


def _safe_stem(filename: str) -> str:
    """Reduce an uploaded filename to a directory name that cannot escape.

    Parameters
    ----------
    filename : str
        Client-supplied name, entirely untrusted.

    Returns
    -------
    str
        Bare stem with no separators or parent references.

    Raises
    ------
    IntakeError
        When nothing usable remains.
    """
    stem = Path(filename.replace("\\", "/")).name
    stem = Path(stem).stem.strip().strip(".")
    if not stem or set(stem) <= {"."} or "/" in stem:
        raise IntakeError(f"'{filename}' is not a usable asset name")
    return stem


def _target_dir(asset_dir: Path, stem: str) -> Path:
    """Reserve a fresh folder for an incoming asset.

    Refuses to reuse an existing name: overwriting a delivery already in the
    library, in place and without asking, is the kind of loss that is noticed
    long after it happened.

    Parameters
    ----------
    asset_dir : pathlib.Path
        Root of the asset library.
    stem : str
        Sanitised asset name.

    Returns
    -------
    pathlib.Path
        Created directory.

    Raises
    ------
    IntakeError
        When the name is already taken.
    """
    target = asset_dir / stem
    if target.exists():
        raise IntakeError(
            f"'{stem}' is already in the library; rename it or remove the "
            f"existing copy first"
        )
    target.mkdir(parents=True)
    return target


def _extract_zip(payload: Path, target: Path) -> int:
    """Unpack a zipped asset folder into ``target``.

    Parameters
    ----------
    payload : pathlib.Path
        Zip file on disk.
    target : pathlib.Path
        Directory to unpack into, already created.

    Returns
    -------
    int
        Number of USD files written.

    Raises
    ------
    IntakeError
        When the archive is malformed, oversized, or tries to write outside
        ``target``.
    """
    try:
        archive = zipfile.ZipFile(payload)
    except zipfile.BadZipFile as error:
        raise IntakeError(f"not a readable zip: {error}") from error

    with archive:
        total = sum(info.file_size for info in archive.infolist())
        if total > MAX_UNPACKED_BYTES:
            raise IntakeError(
                f"archive unpacks to {total / 1e9:.1f} GB, over the "
                f"{MAX_UNPACKED_BYTES / 1e9:.1f} GB limit"
            )

        resolved_target = target.resolve()
        for info in archive.infolist():
            if info.is_dir():
                continue
            # Zip entries carry arbitrary paths, including '../..' and
            # absolute ones. Resolve first, then insist the result stayed
            # inside the folder we created.
            destination = (target / info.filename).resolve()
            if not destination.is_relative_to(resolved_target):
                raise IntakeError(
                    f"archive entry '{info.filename}' points outside the "
                    f"asset folder"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as sink:
                sink.write(source.read())

    return sum(
        1
        for path in target.rglob("*")
        if path.is_file() and path.suffix.lower() in ASSET_SUFFIXES
    )


def accept_upload(filename: str, payload: Path, asset_dir: Path) -> IntakeResult:
    """Place an uploaded asset into the library.

    Parameters
    ----------
    filename : str
        Original name from the browser, used only for its suffix and stem.
    payload : pathlib.Path
        Temporary file holding the uploaded bytes.
    asset_dir : pathlib.Path
        Root of the asset library.

    Returns
    -------
    IntakeResult
        Where the asset landed and how many USD files it holds.

    Raises
    ------
    IntakeError
        When the upload is not a USD or a zip, is unusable, or contains no
        USD at all. The partially written folder is removed first, so a
        rejected upload leaves the library exactly as it was.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in (*ASSET_SUFFIXES, ZIP_SUFFIX):
        raise IntakeError(
            f"'{filename}' is not an asset this viewer reads. Drop a USD "
            f"({', '.join(ASSET_SUFFIXES)}) or a .zip of the asset folder."
        )

    asset_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(filename)
    target = _target_dir(asset_dir, stem)

    try:
        if suffix == ZIP_SUFFIX:
            usd_count = _extract_zip(payload, target)
        else:
            (target / f"{stem}{suffix}").write_bytes(payload.read_bytes())
            usd_count = 1

        if not usd_count:
            raise IntakeError(f"'{filename}' contains no readable asset file")
    except Exception:
        _remove_tree(target)
        raise

    return IntakeResult(key=stem, directory=target, usd_count=usd_count)


def _remove_tree(directory: Path) -> None:
    """Delete a directory and everything under it, ignoring absence."""
    if not directory.exists():
        return
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink()
        else:
            path.rmdir()
    directory.rmdir()
