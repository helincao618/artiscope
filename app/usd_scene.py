"""Shared USD traversal helpers.

Both the joint reader and the mesh exporter need to agree on two questions:
which prims make up a *part*, and which meshes are the *visual* geometry. If
they disagree, the manifest and the GLB stop lining up and the viewer silently
shows the wrong thing. Hence one module, used by both.

Two conventions in the wild
---------------------------
``Cabinet`` keeps visual and collision geometry apart: a render mesh
plus a ``Collisions/`` scope of ``purpose=guide`` hulls. ``Dishwasher`` does
the opposite -- one mesh per part that is simultaneously the render mesh and
the collider, with ``UsdPhysics.CollisionAPI`` applied directly to it.

So "is this mesh visual?" must be answered by ``purpose``, never by the
presence of a collision schema. Filtering on ``CollisionAPI`` would discard
every part of the dishwasher.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional

from pxr import Gf, Usd, UsdGeom, UsdPhysics

# Purposes that end up on screen. ``guide`` and ``proxy`` do not.
_VISIBLE_PURPOSES = (UsdGeom.Tokens.default_, UsdGeom.Tokens.render)


class UsdSceneError(RuntimeError):
    """Raised when a stage cannot be opened or has no usable root."""


def open_stage(usd_path: Path) -> Usd.Stage:
    """Open a USD stage for reading.

    Parameters
    ----------
    usd_path : pathlib.Path
        Path to a ``.usd`` / ``.usda`` / ``.usdc`` file.

    Returns
    -------
    pxr.Usd.Stage
        The opened stage.

    Raises
    ------
    UsdSceneError
        If the file does not exist or USD declines to open it.
    """
    if not usd_path.exists():
        raise UsdSceneError(f"USD file not found: {usd_path}")
    stage = Usd.Stage.Open(str(usd_path))
    if stage is None:
        raise UsdSceneError(f"could not open USD stage: {usd_path}")
    return stage


def get_root_prim(stage: Usd.Stage) -> Usd.Prim:
    """Return the stage's default prim, falling back to the pseudo-root.

    Parameters
    ----------
    stage : pxr.Usd.Stage
        Stage to inspect.

    Returns
    -------
    pxr.Usd.Prim
        The prim the search for parts starts from.

    Raises
    ------
    UsdSceneError
        If the stage exposes no usable root.
    """
    root = stage.GetDefaultPrim()
    if root and root.IsValid():
        return root
    pseudo = stage.GetPseudoRoot()
    if pseudo and pseudo.IsValid():
        return pseudo
    raise UsdSceneError("stage has no default prim and no pseudo-root")


def is_visual_mesh(prim: Usd.Prim) -> bool:
    """Return whether ``prim`` is a mesh that should be rendered.

    Decided by the computed USD purpose and the computed visibility, both
    inherited down the hierarchy, so a whole ``Collisions/`` scope marked
    ``guide``, or a branch someone switched off with ``visibility =
    invisible`` further up, is excluded even when the mesh itself carries
    neither attribute.

    Parameters
    ----------
    prim : pxr.Usd.Prim
        Prim to test.

    Returns
    -------
    bool
        ``True`` for a renderable mesh.
    """
    if not prim.IsA(UsdGeom.Mesh):
        return False
    imageable = UsdGeom.Imageable(prim)
    if imageable.ComputePurpose() not in _VISIBLE_PURPOSES:
        return False
    return imageable.ComputeVisibility() != UsdGeom.Tokens.invisible


def iter_visual_meshes(scope: Usd.Prim) -> Iterator[Usd.Prim]:
    """Yield every renderable mesh at or below ``scope``.

    Traverses through ``instanceable`` prims rather than stopping at them.
    Plain ``Usd.PrimRange`` treats an instance as a leaf -- its own children
    are real prims in the composed scene graph, but iterating without
    ``Usd.TraverseInstanceProxies()`` skips straight past them, so a part
    authored as an instanceable reference would silently export as empty
    geometry with no error anywhere.

    Parameters
    ----------
    scope : pxr.Usd.Prim
        Subtree root to search.

    Yields
    ------
    pxr.Usd.Prim
        Renderable mesh prims, in traversal order.
    """
    if is_visual_mesh(scope):
        yield scope
    for prim in Usd.PrimRange(scope, Usd.TraverseInstanceProxies()):
        if prim != scope and is_visual_mesh(prim):
            yield prim


def is_collision_mesh(prim: Usd.Prim) -> bool:
    """Return whether ``prim`` is a mesh a physics engine collides against.

    Decided purely by ``UsdPhysics.CollisionAPI``, never by ``purpose`` --
    the mirror image of :func:`is_visual_mesh`. ``Dishwasher`` applies the
    schema straight onto its render meshes, so filtering on purpose here
    would miss every collider it has; ``Cabinet`` keeps its hulls
    in a ``purpose=guide`` scope that carries the schema too, so this catches
    both conventions with one check.

    Parameters
    ----------
    prim : pxr.Usd.Prim
        Prim to test.

    Returns
    -------
    bool
        ``True`` for a mesh a physics engine would collide against.
    """
    return prim.IsA(UsdGeom.Mesh) and prim.HasAPI(UsdPhysics.CollisionAPI)


def iter_collision_meshes(scope: Usd.Prim) -> Iterator[Usd.Prim]:
    """Yield every collision mesh at or below ``scope``.

    Same instance-proxy traversal as :func:`iter_visual_meshes`, so a part
    authored as an instanceable reference does not silently lose its
    collision geometry the way it would lose its visual geometry without it.

    Parameters
    ----------
    scope : pxr.Usd.Prim
        Subtree root to search.

    Yields
    ------
    pxr.Usd.Prim
        Collision mesh prims, in traversal order.
    """
    if is_collision_mesh(scope):
        yield scope
    for prim in Usd.PrimRange(scope, Usd.TraverseInstanceProxies()):
        if prim != scope and is_collision_mesh(prim):
            yield prim


def iter_instanced_prims(stage: Usd.Stage) -> Iterator[Usd.Prim]:
    """Yield every prim in ``stage`` that is itself an instance.

    Used only to decide whether to warn: this reader does traverse through
    instances (see :func:`iter_visual_meshes`), but that path is exercised by
    a hand-built test fixture, not by anything in an actual delivery yet, so
    an asset that relies on it is worth calling out rather than trusting
    silently.

    Parameters
    ----------
    stage : pxr.Usd.Stage
        Stage to scan.

    Yields
    ------
    pxr.Usd.Prim
        Prims for which ``IsInstance()`` is true.
    """
    for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies()):
        if prim.IsInstance():
            yield prim


def rigid_body_paths(stage: Usd.Stage) -> set[str]:
    """Return the path of every prim carrying a rigid-body schema.

    Parameters
    ----------
    stage : pxr.Usd.Stage
        Stage to inspect.

    Returns
    -------
    set of str
        Prim paths, as strings.
    """
    return {
        prim.GetPath().pathString
        for prim in stage.Traverse(
            Usd.TraverseInstanceProxies(Usd.PrimAllPrimsPredicate)
        )
        if prim.HasAPI(UsdPhysics.RigidBodyAPI)
    }


def _has_rigid_body_below(prim: Usd.Prim, body_paths: set[str]) -> bool:
    """Return whether a strict descendant of ``prim`` is a rigid body."""
    prefix = prim.GetPath().pathString + "/"
    return any(path.startswith(prefix) for path in body_paths)


def _collect_parts(
    prim: Usd.Prim, body_paths: set[str], parts: list[Usd.Prim]
) -> None:
    """Append the parts at or below ``prim`` to ``parts``, in stage order."""
    if prim.GetPath().pathString not in body_paths and _has_rigid_body_below(
        prim, body_paths
    ):
        for child in prim.GetChildren():
            _collect_parts(child, body_paths, parts)
        return
    if any(True for _ in iter_visual_meshes(prim)):
        parts.append(prim)


def discover_part_prims(stage: Usd.Stage) -> list[Usd.Prim]:
    """Return the prims that represent the asset's parts.

    A part is the shallowest prim that owns visual geometry and holds no
    rigid body further down: a rigid body is a leaf body as far as a physics
    engine is concerned, so there is nothing smaller to look for inside one.
    Material scopes and other bookkeeping prims drop out naturally because
    they hold no renderable geometry.

    Depth is deliberately not fixed. In a single delivered asset the parts sit
    one level under the root, but an assembled scene nests them one level
    deeper again (``/root/<asset instance>/<part>``). Treating the direct
    children as the parts there would collapse every door of an appliance into
    the appliance itself, and every joint driving those doors would then appear
    to drive the whole appliance -- reported as a body mismatch and a closed
    loop that the asset does not contain.

    Parameters
    ----------
    stage : pxr.Usd.Stage
        Stage to inspect.

    Returns
    -------
    list of pxr.Usd.Prim
        Part prims in stage order.
    """
    body_paths = rigid_body_paths(stage)
    parts: list[Usd.Prim] = []
    for child in get_root_prim(stage).GetChildren():
        _collect_parts(child, body_paths, parts)
    return parts


def part_node_name(part_prim: Usd.Prim) -> str:
    """Return the GLB node name for a part.

    The manifest and the exported GLB are joined on this string, so it is
    defined once here rather than derived independently on each side. That
    makes it the prim's path relative to the root rather than its name: an
    assembled scene repeats a name across asset instances (four prims called
    ``Fruit001``), and a collision there would attribute one part's
    geometry to another. For a single delivered asset, whose parts sit
    directly under the root, this is the part name unchanged.

    The ancestor separator is a hyphen, not the ``/`` the path uses, because
    three.js strips ``[].:/`` from every node name it loads
    (``PropertyBinding.sanitizeNodeName``) to keep its animation track syntax
    parseable. A ``/`` here therefore survives the export and disappears in
    the browser, leaving the viewer unable to find the geometry for a part and
    the part unattached to the joint that moves it. A hyphen cannot occur in a
    USD prim name, so it stays unambiguous.

    Parameters
    ----------
    part_prim : pxr.Usd.Prim
        A prim returned by :func:`discover_part_prims`.

    Returns
    -------
    str
        Node name to use in the GLB.
    """
    root_path = get_root_prim(part_prim.GetStage()).GetPath()
    relative = part_prim.GetPath().MakeRelativePath(root_path)
    return "-".join(relative.pathString.split("/"))


def resolve_body_prim(
    stage: Usd.Stage, target_path: str, part_paths: set[str]
) -> Optional[Usd.Prim]:
    """Map a joint's body target onto the part that owns it.

    Needed because the two reference assets are inconsistent about what a
    ``body0`` / ``body1`` relationship points at: sometimes the part's Xform,
    sometimes the mesh nested one level inside it, occasionally both within a
    single file. Walking up until a known part is reached normalises this.

    A rigid-body schema on the way up is preferred, because that is the prim
    a physics engine would treat as the body. Falling back to the part level
    also handles the static base, which carries no rigid-body schema at all
    (``Dishwasher_Body001``).

    Parameters
    ----------
    stage : pxr.Usd.Stage
        Stage the path belongs to.
    target_path : str
        Prim path taken from a ``body0`` / ``body1`` relationship.
    part_paths : set of str
        Paths of the known parts, used as the stopping condition.

    Returns
    -------
    pxr.Usd.Prim or None
        The owning part prim, or ``None`` if the target resolves outside every
        known part.
    """
    prim = stage.GetPrimAtPath(target_path)
    if not prim or not prim.IsValid():
        return None

    rigid_body: Optional[Usd.Prim] = None
    walker = prim
    while walker and walker.IsValid() and not walker.IsPseudoRoot():
        if rigid_body is None and walker.HasAPI(UsdPhysics.RigidBodyAPI):
            rigid_body = walker
        if walker.GetPath().pathString in part_paths:
            return walker
        walker = walker.GetParent()

    return rigid_body


def world_transform(prim: Usd.Prim) -> Gf.Matrix4d:
    """Return the local-to-world matrix of ``prim`` at the default time.

    Parameters
    ----------
    prim : pxr.Usd.Prim
        Prim to evaluate. Non-transformable prims yield the identity.

    Returns
    -------
    pxr.Gf.Matrix4d
        Local-to-world transform.
    """
    xformable = UsdGeom.Xformable(prim)
    if not xformable:
        return Gf.Matrix4d(1.0)
    return xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())


def has_non_uniform_scale(matrix: Gf.Matrix4d, tolerance: float = 1e-4) -> bool:
    """Return whether ``matrix`` carries a scale other than 1.

    Scale on a body makes the joint anchor ambiguous: USD physics reasons in
    unscaled body space, so a scaled body is worth flagging to the reviewer
    rather than silently trusting.

    Parameters
    ----------
    matrix : pxr.Gf.Matrix4d
        Transform to test.
    tolerance : float, optional
        Allowed deviation from unit length per axis.

    Returns
    -------
    bool
        ``True`` when any axis deviates from unit scale.
    """
    for row in range(3):
        length = Gf.Vec3d(
            matrix[row][0], matrix[row][1], matrix[row][2]
        ).GetLength()
        if abs(length - 1.0) > tolerance:
            return True
    return False
