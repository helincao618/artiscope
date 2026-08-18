"""Report whether an asset folder can be moved somewhere else and still work.

The validator's ``MissingReferenceChecker`` answers a narrower question: does
every reference resolve *right now*, on this disk. That misses the failure mode
this delivery actually has. Two of the kitchen cabinets point their textures at
a **sibling asset's** folder::

    Cabinet -> ../Shelf/texture/tfhofbfc_4K_Albedo.jpg

Inside the full delivery every one of those resolves and the validator is
silent. Copy the cabinet folder out on its own -- which is exactly what
"deliver one asset" means -- and 24 of its 28 texture references break, with no
error at load time. The asset just renders untextured.

So a reference can be perfectly valid and still make the asset non-portable,
and only a check that knows where the asset folder *ends* can see it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

from pxr import Sdf, Usd, UsdShade
from pydantic import BaseModel, Field, computed_field

from .readers import is_urdf


_MDL_MODULE = re.compile(r"[\w.\-]+\.mdl", re.IGNORECASE)


def is_engine_module(raw_path: str) -> bool:
    """Return whether a reference names a module the engine itself supplies.

    A material reference carrying no directory component is a module name
    rather than a file path: MDL modules resolve through the renderer's own
    search path. ``OmniPBR.mdl`` and its siblings ship inside Omniverse and
    Isaac, so an asset naming one depends on the runtime, not on a file its
    folder forgot to carry. Off an Omniverse install there is no search path
    to consult and the name cannot resolve -- which is a fact about this
    machine, and grading the delivery down for it would be grading our own
    environment.

    Parameters
    ----------
    raw_path : str
        Reference exactly as authored.

    Returns
    -------
    bool
        True for a bare ``*.mdl`` module name.
    """
    return raw_path.lower().endswith(".mdl") and not any(
        sep in raw_path for sep in ("/", "\\")
    )


def mentions_engine_module(text: str) -> bool:
    """Return whether a message is about an engine-supplied MDL module.

    Used to read the validator's verdicts, which name the offending
    reference inside otherwise free prose.

    Parameters
    ----------
    text : str
        Message to inspect.

    Returns
    -------
    bool
        True when every ``*.mdl`` named is a bare module name, and at least
        one is named.
    """
    found = _MDL_MODULE.findall(text)
    return bool(found) and all(is_engine_module(name) for name in found)


class AssetReference(BaseModel):
    """One external file an asset depends on.

    Attributes
    ----------
    raw_path : str
        Path exactly as authored, which is what someone has to go and fix.
    resolves : bool
        Whether it resolves on this disk right now.
    inside_asset_folder : bool
        Whether the target lives under the asset's own folder. A reference
        that resolves but points outside will break the moment the folder
        travels alone.
    engine_module : bool
        Whether this names an engine-supplied MDL module rather than a file
        the folder is expected to carry. See :func:`is_engine_module`.
    """

    raw_path: str
    resolves: bool
    inside_asset_folder: bool
    engine_module: bool = False


class PortabilityReport(BaseModel):
    """Whether an asset folder is self-contained.

    Attributes
    ----------
    asset_folder : str
        Folder treated as the unit of delivery.
    references : list of AssetReference
        Every external file reference found, deduplicated.
    """

    asset_folder: str
    references: list[AssetReference] = Field(default_factory=list)

    @computed_field
    @property
    def broken(self) -> list[AssetReference]:
        """Return references that do not resolve on this disk.

        Engine-supplied modules are excluded: they are not files this folder
        was ever supposed to hold.
        """
        return [
            ref
            for ref in self.references
            if not ref.resolves and not ref.engine_module
        ]

    @computed_field
    @property
    def engine_modules(self) -> list[AssetReference]:
        """Return references the runtime supplies rather than the folder."""
        return [ref for ref in self.references if ref.engine_module]

    @computed_field
    @property
    def escaping(self) -> list[AssetReference]:
        """Return references that resolve but point outside the folder."""
        return [
            ref
            for ref in self.references
            if ref.resolves and not ref.inside_asset_folder
        ]

    @computed_field
    @property
    def self_contained(self) -> bool:
        """Whether the folder can be moved on its own without losing anything."""
        return not self.broken and not self.escaping


def check_portability(
    usd_path: Path, asset_folder: Optional[Path] = None
) -> PortabilityReport:
    """Find every external file an asset depends on and classify it.

    Parameters
    ----------
    usd_path : pathlib.Path
        Asset to inspect.
    asset_folder : pathlib.Path, optional
        Folder treated as the deliverable unit. Defaults to the directory
        holding the USD file.

    Returns
    -------
    PortabilityReport
        One entry per distinct authored path.
    """
    usd_path = Path(usd_path)
    folder = Path(asset_folder) if asset_folder else usd_path.parent
    # Normalised but *not* dereferenced. Containment here is a question about
    # the folder someone hands over, and a delivery is routinely staged as a
    # tree of symlinks into wherever the originals were unpacked. Path.resolve
    # follows those links out of the folder, so every texture in a staged
    # delivery reads as escaping when none of them are.
    absolute_folder = Path(os.path.abspath(folder))

    if is_urdf(usd_path):
        found = _urdf_references(usd_path)
    else:
        found = _usd_references(usd_path)

    seen: dict[str, AssetReference] = {}
    for raw_path, resolved in found:
        if raw_path in seen:
            continue
        resolves = bool(resolved and resolved.is_file())
        inside = bool(
            resolves
            and Path(os.path.abspath(resolved)).is_relative_to(absolute_folder)
        )
        seen[raw_path] = AssetReference(
            raw_path=raw_path,
            resolves=resolves,
            inside_asset_folder=inside,
            engine_module=is_engine_module(raw_path),
        )

    return PortabilityReport(
        asset_folder=str(folder),
        references=sorted(seen.values(), key=lambda ref: ref.raw_path),
    )


def _usd_references(usd_path: Path):
    """Yield ``(authored_path, resolved_path)`` for every USD texture."""
    stage = Usd.Stage.Open(str(usd_path))
    for prim in stage.Traverse(Usd.TraverseInstanceProxies()):
        if not prim.IsA(UsdShade.Shader):
            continue
        for attribute in prim.GetAttributes():
            if attribute.GetTypeName() != Sdf.ValueTypeNames.Asset:
                continue
            value = attribute.Get()
            if value is None or not value.path:
                continue
            yield value.path, (
                Path(value.resolvedPath) if value.resolvedPath else None
            )


def _urdf_references(urdf_path: Path):
    """Yield ``(authored_path, resolved_path)`` for every URDF mesh.

    A URDF's external dependencies are its geometry files, and they are the
    same portability hazard as USD's textures: ``package://`` URIs mean
    nothing outside a ROS workspace, so a description travels far more often
    than the meshes it names.
    """
    from .urdf_mesh_export import _resolve_mesh

    try:
        root = ElementTree.parse(urdf_path).getroot()
    except ElementTree.ParseError:
        return

    for mesh in root.iter("mesh"):
        raw = mesh.get("filename")
        if raw:
            yield raw, _resolve_mesh(raw, urdf_path)
