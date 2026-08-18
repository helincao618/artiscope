"""Read authored appearance (colour, texture) from USD materials.

This is a second, optional rendering of the same geometry :mod:`app.mesh_export`
already produces for the kinematic view. That view answers "which lump of
geometry moves independently" with a flat palette; this one answers "what did
the asset actually author" -- so a reviewer can also judge whether the surface
looks right, not just whether the joint does.

Shader graph traversal is bounded on purpose. ``UsdPreviewSurface`` is
followed through ``NodeGraph`` wrappers (common output of DCC-to-USD
exporters, which nest a texture behind a compound node rather than wiring it
directly) to find a ``UsdUVTexture``, but only for ``diffuseColor`` -- not
normal, roughness, or metallic maps. Every reference asset seen so far uses
those as constants, never textures, and chasing every possible input through
an arbitrary graph for a display-only viewer is not worth the surface it adds
for bugs to hide in. A part whose texture can be read carries it; everything
else falls back to its constant (or default) values, and that fallback is
never silent -- see ``notes`` below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pxr import Gf, Sdf, Usd, UsdShade

# UsdPreviewSurface's own defaults, used whenever a value cannot be read.
_DEFAULT_COLOR = (0.7, 0.7, 0.7)
_DEFAULT_METALLIC = 0.0
_DEFAULT_ROUGHNESS = 0.7

# Assumed when a texture's UV primvar cannot be traced back to a name. "st" is
# UsdPreviewSurface's own convention and what every reference asset uses.
_FALLBACK_UV_PRIMVAR = "st"


@dataclass(frozen=True)
class MaterialAppearance:
    """What a material contributes to the textured view.

    Attributes
    ----------
    base_color : tuple of float
        RGB in ``0..1``, either the shader's constant ``diffuseColor`` or a
        neutral default when neither a constant nor a texture could be read.
    texture_path : pathlib.Path, optional
        Resolved path to a ``diffuseColor`` texture, when present and
        readable from this disk.
    uv_primvar : str
        Name of the primvar the texture reads UV coordinates from.
    metallic : float
        Constant metallic factor in ``0..1``.
    roughness : float
        Constant roughness factor in ``0..1``.
    notes : list of str
        Human-readable record of any fallback this function had to take,
        e.g. a texture reference that does not resolve on this disk.
    """

    base_color: tuple[float, float, float] = _DEFAULT_COLOR
    texture_path: Optional[Path] = None
    uv_primvar: str = _FALLBACK_UV_PRIMVAR
    metallic: float = _DEFAULT_METALLIC
    roughness: float = _DEFAULT_ROUGHNESS
    notes: list[str] = field(default_factory=list)


def _as_vec3(value) -> Optional[tuple[float, float, float]]:
    """Coerce a USD colour-like value to a plain RGB tuple, or ``None``."""
    if value is None:
        return None
    if isinstance(value, (Gf.Vec3f, Gf.Vec3d)):
        return (float(value[0]), float(value[1]), float(value[2]))
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        return (float(value[0]), float(value[1]), float(value[2]))
    return None


def _resolve_output(
    source: UsdShade.ConnectionSourceInfo, seen: set[str]
) -> Optional[UsdShade.ConnectionSourceInfo]:
    """Follow a connection through ``NodeGraph`` wrappers to a ``Shader``.

    DCC-to-USD exporters commonly wrap a texture in a compound ``NodeGraph``
    rather than wiring ``diffuseColor`` straight to the ``UsdUVTexture``. This
    walks through that indirection until it lands on an actual ``Shader``
    prim, an unresolvable dead end, or a cycle (guarded by ``seen``).

    Parameters
    ----------
    source : pxr.UsdShade.ConnectionSourceInfo
        The connection to resolve.
    seen : set of str
        Prim paths already visited, to stop infinite recursion on a
        malformed graph.

    Returns
    -------
    pxr.UsdShade.ConnectionSourceInfo or None
        A connection whose source is a ``Shader`` prim, or ``None``.
    """
    prim = source.source.GetPrim()
    path = prim.GetPath().pathString
    if path in seen:
        return None
    seen.add(path)

    if prim.IsA(UsdShade.Shader):
        return source

    node_graph = UsdShade.NodeGraph(prim)
    if not node_graph:
        return None
    output = node_graph.GetOutput(source.sourceName)
    if not output:
        return None
    nested = output.GetConnectedSources()[0]
    if not nested:
        return None
    return _resolve_output(nested[0], seen)


def _find_texture_shader(input_attr: UsdShade.Input) -> Optional[UsdShade.Shader]:
    """Return the ``UsdUVTexture`` shader feeding ``input_attr``, if any."""
    sources = input_attr.GetConnectedSources()[0]
    if not sources:
        return None
    resolved = _resolve_output(sources[0], set())
    if resolved is None:
        return None
    shader = UsdShade.Shader(resolved.source.GetPrim())
    if shader.GetIdAttr().Get() != "UsdUVTexture":
        return None
    return shader


def _texture_uv_primvar(texture_shader: UsdShade.Shader) -> str:
    """Best-effort name of the primvar a texture shader reads UV from.

    Falls back to :data:`_FALLBACK_UV_PRIMVAR` when the ``st`` input is
    unconnected, or its source cannot be resolved to a literal name -- this
    reader does not evaluate ``NodeGraph`` interface attributes, only follows
    them one level.
    """
    st_input = texture_shader.GetInput("st")
    if not st_input:
        return _FALLBACK_UV_PRIMVAR
    sources = st_input.GetConnectedSources()[0]
    if not sources:
        return _FALLBACK_UV_PRIMVAR
    reader = UsdShade.Shader(sources[0].source.GetPrim())
    varname_input = reader.GetInput("varname")
    if varname_input:
        value = varname_input.Get()
        if isinstance(value, str) and value:
            return value
    return _FALLBACK_UV_PRIMVAR


def resolve_appearance(material: Optional[UsdShade.Material]) -> MaterialAppearance:
    """Read the textured-view appearance a material authors.

    Parameters
    ----------
    material : pxr.UsdShade.Material, optional
        Bound material, or ``None`` when the geometry has no binding at all.

    Returns
    -------
    MaterialAppearance
        Always returned, falling back to neutral defaults with an explanatory
        note when nothing usable could be read.
    """
    if material is None:
        return MaterialAppearance(notes=["no material bound; showing neutral grey"])

    surface = material.ComputeSurfaceSource()[0]
    if not surface:
        return MaterialAppearance(
            notes=[
                f"'{material.GetPath()}' has no surface shader; showing neutral grey"
            ]
        )

    notes: list[str] = []
    base_color = _DEFAULT_COLOR
    texture_path: Optional[Path] = None
    uv_primvar = _FALLBACK_UV_PRIMVAR

    diffuse_input = surface.GetInput("diffuseColor")
    if diffuse_input:
        texture_shader = _find_texture_shader(diffuse_input)
        if texture_shader is not None:
            file_input = texture_shader.GetInput("file")
            asset = file_input.Get() if file_input else None
            resolved = getattr(asset, "resolvedPath", "") if asset else ""
            if resolved and Path(resolved).is_file():
                texture_path = Path(resolved)
                uv_primvar = _texture_uv_primvar(texture_shader)
            else:
                raw = getattr(asset, "path", str(asset)) if asset else "<unknown>"
                notes.append(
                    f"'{surface.GetPath()}' textures diffuseColor with '{raw}', "
                    f"which does not resolve on this disk; showing neutral grey "
                    f"instead (see the portability report for the missing file)"
                )
        else:
            constant = _as_vec3(diffuse_input.Get())
            if constant is not None:
                base_color = constant

    metallic = _DEFAULT_METALLIC
    metallic_input = surface.GetInput("metallic")
    if metallic_input and not metallic_input.GetConnectedSources()[0]:
        value = metallic_input.Get()
        if isinstance(value, (int, float)):
            metallic = float(value)

    roughness = _DEFAULT_ROUGHNESS
    roughness_input = surface.GetInput("roughness")
    if roughness_input and not roughness_input.GetConnectedSources()[0]:
        value = roughness_input.Get()
        if isinstance(value, (int, float)):
            roughness = float(value)

    return MaterialAppearance(
        base_color=base_color,
        texture_path=texture_path,
        uv_primvar=uv_primvar,
        metallic=metallic,
        roughness=roughness,
        notes=notes,
    )


def bound_material(prim: Usd.Prim) -> Optional[UsdShade.Material]:
    """Return the material bound to ``prim`` (mesh or ``GeomSubset``), if any."""
    binding = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
    return binding if binding else None
