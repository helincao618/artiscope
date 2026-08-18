"""Pick a reader by file format, so nothing downstream has to care.

Both readers produce the same :class:`~app.models.JointManifest`, which is what
makes the inventory, the viewer and the kinematic rig work unchanged on either
input. The one place the difference has to survive is
``JointManifest.source_format``, because engine-correctness rules exist for USD
and do not exist for URDF, and the UI must not let those look the same.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .config import URDF_SUFFIXES
from .models import JointManifest


def is_urdf(path: Path) -> bool:
    """Return whether ``path`` should be read as URDF."""
    return Path(path).suffix.lower() in URDF_SUFFIXES


def read_any_manifest(
    path: Path, asset_name: Optional[str] = None
) -> JointManifest:
    """Read an asset of either supported format into a manifest.

    Parameters
    ----------
    path : pathlib.Path
        Asset to read.
    asset_name : str, optional
        Short name. Defaults to whatever the format-specific reader prefers.

    Returns
    -------
    JointManifest
        Format-neutral description of the kinematic tree.

    Raises
    ------
    app.usd_scene.UsdSceneError
        If the asset cannot be read.
    """
    if is_urdf(path):
        from .urdf_reader import read_urdf_manifest

        return read_urdf_manifest(path, asset_name=asset_name)

    from .usd_reader import read_manifest

    return read_manifest(path, asset_name=asset_name)


def export_any_glb(
    path: Path, glb_path: Path, face_budget: Optional[int] = None
) -> tuple[Path, list[str]]:
    """Export per-part geometry for an asset of either supported format.

    Parameters
    ----------
    path : pathlib.Path
        Asset to export.
    glb_path : pathlib.Path
        Destination ``.glb``.
    face_budget : int, optional
        Per-part face ceiling. Defaults to the exporter's own.

    Returns
    -------
    tuple
        ``(glb_path, warnings)``.
    """
    from .mesh_export import DEFAULT_FACE_BUDGET

    budget = DEFAULT_FACE_BUDGET if face_budget is None else face_budget

    if is_urdf(path):
        from .urdf_mesh_export import export_urdf_parts_glb

        return export_urdf_parts_glb(path, glb_path, face_budget=budget)

    from .mesh_export import export_parts_glb

    return export_parts_glb(path, glb_path, face_budget=budget)
