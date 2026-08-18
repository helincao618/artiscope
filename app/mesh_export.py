"""Export a USD asset as a GLB with one named node per part.

Why not simply flatten the asset into a single mesh: the viewer has to move
individual parts, so the part boundaries are the whole point. Each part
becomes one named node, and the manifest refers to those names.

Coordinate frame
----------------
Geometry is written in **stage space**, unconverted: same axes, same origin,
metres. glTF conventionally wants Y-up, but converting here would put the
mesh in one frame and the manifest's joint anchors in another, and every
subsequent bug would be a frame bug. Instead the viewer applies a single
rotation to the whole scene for display, leaving mesh coordinates and joint
anchors directly comparable -- and both still comparable with ``usdview``.

Appearance
----------
Every part is exported twice, as two independent sets of nodes in the same
GLB: a flat one, in distinct palette colours, and a textured one, carrying
whatever material the asset actually authored. The viewer toggles between
them by hiding one set and showing the other -- no refetch, no rebuild, and
both stay driveable by the same joints because both sit under the same rig.

The flat set answers "which lump of geometry moves independently": a palette
gives that away at a glance and a wood-grain texture does not, which is why
it stays the default. The textured set answers a different question -- does
the surface look like what was authored -- and exists for exactly the same
reason the mesh-report banner does: a reviewer should not have to take this
tool's word for what an asset contains. See :mod:`app.usd_materials` for how
much of a shader graph the textured set actually reads (bounded, and every
fallback it takes is recorded as a warning).

Node naming ties the two sets together: the flat mesh for a part keeps
exactly the part's node name (unchanged from before textures existed, so
nothing else in this codebase had to change), and each textured piece for
that part is named ``f"{part_name}~mat{n}"``.

A third, optional node per part carries collision-only geometry -- hulls that
sit in a ``purpose=guide`` scope, never drawn as part of the two sets above --
named ``f"{part_name}~col0"``. A part whose collider *is* its visual mesh
(``Dishwasher``'s convention) gets no such node: that geometry is already
on screen, and exporting it twice would only double the file for no new
information.
"""

from __future__ import annotations

import colorsys
from pathlib import Path
from typing import Optional

import numpy as np
import trimesh
from PIL import Image
from pxr import Usd, UsdGeom, UsdShade

from .usd_materials import MaterialAppearance, bound_material, resolve_appearance
from .usd_scene import (
    UsdSceneError,
    discover_part_prims,
    is_visual_mesh,
    iter_collision_meshes,
    iter_visual_meshes,
    open_stage,
    part_node_name,
    world_transform,
)

# Separator between a part's node name and its textured-variant index. Not
# ``.`` or ``/``: both show up in legitimate USD prim names and would make a
# textured node indistinguishable from a flat one sharing that name.
MATERIAL_NODE_SEP = "~mat"

# Same reasoning as MATERIAL_NODE_SEP, for the collision-overlay node.
COLLISION_NODE_SEP = "~col"

# Above this, a part is decimated before export. Chosen so the reference
# kitchen assets (~8 k faces for the largest part) pass through untouched
# while a pathological delivery cannot stall the browser.
DEFAULT_FACE_BUDGET = 150_000


class MeshExportError(RuntimeError):
    """Raised when an asset yields no exportable geometry."""


def _part_palette(count: int) -> list[list[int]]:
    """Build a set of visually distinct RGBA colours.

    Evenly spaced hues at fixed saturation and value, so adjacent parts never
    land on near-identical colours regardless of how many there are.

    Parameters
    ----------
    count : int
        Number of colours needed.

    Returns
    -------
    list of list of int
        RGBA colours with components in ``0..255``.
    """
    colours: list[list[int]] = []
    for index in range(max(count, 1)):
        hue = (index / max(count, 1) + 0.58) % 1.0
        red, green, blue = colorsys.hsv_to_rgb(hue, 0.45, 0.92)
        colours.append(
            [int(red * 255), int(green * 255), int(blue * 255), 255]
        )
    return colours


def _triangulate(counts: np.ndarray, indices: np.ndarray) -> Optional[np.ndarray]:
    """Fan-triangulate USD polygonal faces.

    USD meshes carry arbitrary polygon sizes; glTF only takes triangles. A fan
    is correct for the convex quads and triangles these assets are built from.

    Parameters
    ----------
    counts : numpy.ndarray
        Vertex count per face.
    indices : numpy.ndarray
        Flat vertex index buffer.

    Returns
    -------
    numpy.ndarray or None
        ``(n, 3)`` triangle indices, or ``None`` when the face data is empty
        or inconsistent.
    """
    if counts.size == 0 or indices.size == 0:
        return None
    if int(counts.sum()) != indices.size:
        return None

    triangles: list[np.ndarray] = []
    offset = 0
    for count in counts:
        count = int(count)
        if count >= 3:
            face = indices[offset : offset + count]
            fan = np.column_stack(
                [
                    np.full(count - 2, face[0]),
                    face[1 : count - 1],
                    face[2:count],
                ]
            )
            triangles.append(fan)
        offset += count

    if not triangles:
        return None
    return np.vstack(triangles)


def _mesh_to_trimesh(
    mesh_prim: Usd.Prim, to_metres: float
) -> Optional[trimesh.Trimesh]:
    """Convert one USD mesh prim into a world-space triangle mesh.

    Parameters
    ----------
    mesh_prim : pxr.Usd.Prim
        A prim that :func:`app.usd_scene.is_visual_mesh` accepted.
    to_metres : float
        Stage-unit-to-metre factor.

    Returns
    -------
    trimesh.Trimesh or None
        The converted mesh, or ``None`` when the prim carries no usable
        geometry.
    """
    usd_mesh = UsdGeom.Mesh(mesh_prim)
    points = usd_mesh.GetPointsAttr().Get()
    counts = usd_mesh.GetFaceVertexCountsAttr().Get()
    indices = usd_mesh.GetFaceVertexIndicesAttr().Get()
    if not points or not counts or not indices:
        return None

    vertices = np.asarray(points, dtype=np.float64)
    faces = _triangulate(
        np.asarray(counts, dtype=np.int64), np.asarray(indices, dtype=np.int64)
    )
    if faces is None:
        return None

    matrix = np.asarray(world_transform(mesh_prim), dtype=np.float64)
    # USD matrices are row-vector: v' = v * M. numpy's convention is the
    # transpose of that, hence the right-multiply by the 3x3 block.
    vertices = vertices @ matrix[:3, :3] + matrix[3, :3]
    vertices *= to_metres

    orientation = usd_mesh.GetOrientationAttr().Get()
    if orientation == UsdGeom.Tokens.leftHanded:
        faces = faces[:, ::-1]

    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


def _part_mesh(
    part_prim: Usd.Prim, to_metres: float, face_budget: int
) -> tuple[Optional[trimesh.Trimesh], list[str]]:
    """Merge a part's visual meshes into a single world-space mesh.

    Parameters
    ----------
    part_prim : pxr.Usd.Prim
        Part to convert.
    to_metres : float
        Stage-unit-to-metre factor.
    face_budget : int
        Face count above which the part is decimated.

    Returns
    -------
    tuple
        ``(mesh, warnings)``. ``mesh`` is ``None`` when the part yielded no
        geometry.
    """
    warnings: list[str] = []
    pieces = [
        mesh
        for mesh in (
            _mesh_to_trimesh(prim, to_metres)
            for prim in iter_visual_meshes(part_prim)
        )
        if mesh is not None
    ]
    if not pieces:
        return None, warnings

    merged = pieces[0] if len(pieces) == 1 else trimesh.util.concatenate(pieces)

    if len(merged.faces) > face_budget:
        try:
            merged = merged.simplify_quadric_decimation(face_count=face_budget)
            warnings.append(
                f"part '{part_prim.GetName()}' exceeded the {face_budget} face "
                f"budget and was decimated for display only"
            )
        except Exception:  # noqa: BLE001 - optional backend, never fatal
            warnings.append(
                f"part '{part_prim.GetName()}' has {len(merged.faces)} faces, "
                f"over the {face_budget} budget, and could not be decimated; "
                f"the viewer may be slow"
            )

    return merged, warnings


def _part_collision_mesh(
    part_prim: Usd.Prim, to_metres: float
) -> tuple[Optional[trimesh.Trimesh], bool]:
    """Merge a part's collision-only geometry into one world-space mesh.

    Mirrors :func:`_part_mesh`, but for collision hulls rather than visual
    meshes. A collider that is also a visual mesh (``Dishwasher``'s
    convention: ``UsdPhysics.CollisionAPI`` applied straight to the render
    mesh) is skipped here -- it already exported in the flat/textured pass,
    and re-exporting identical geometry under a second name would only grow
    the file for nothing a reviewer could not already see.

    Not decimated, unlike the flat mesh: a simplified collision hull no
    longer shows what a physics engine actually collides against, which is
    the entire point of drawing it.

    Parameters
    ----------
    part_prim : pxr.Usd.Prim
        Part to convert.
    to_metres : float
        Stage-unit-to-metre factor.

    Returns
    -------
    tuple
        ``(mesh, shares_visual_geometry)``. ``mesh`` is ``None`` when the
        part has no collision geometry distinct from its visual mesh --
        either because it has none at all, or because ``shares_visual_geometry``
        is ``True``.
    """
    pieces: list[trimesh.Trimesh] = []
    shares_visual_geometry = False
    for mesh_prim in iter_collision_meshes(part_prim):
        if is_visual_mesh(mesh_prim):
            shares_visual_geometry = True
            continue
        piece = _mesh_to_trimesh(mesh_prim, to_metres)
        if piece is not None:
            pieces.append(piece)

    if not pieces:
        return None, shares_visual_geometry
    merged = pieces[0] if len(pieces) == 1 else trimesh.util.concatenate(pieces)
    return merged, shares_visual_geometry


def _triangulate_corners(
    counts: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Fan-triangulate by *corner* position rather than by vertex index.

    Unlike :func:`_triangulate`, indices here point into the flat per-corner
    arrays (one entry per face-vertex, unwelded) rather than into a shared
    vertex array. That is what a per-corner attribute like UV -- authored
    ``faceVarying``, so the same point can carry a different UV in each face
    it touches -- needs to survive triangulation.

    Parameters
    ----------
    counts : numpy.ndarray
        Vertex count per face.

    Returns
    -------
    tuple
        ``(triangle_corners, face_id_per_triangle)``. ``triangle_corners`` is
        ``(n, 3)`` indices into the corner arrays; ``face_id_per_triangle``
        names which original polygon each row came from, for splitting by
        ``GeomSubset`` afterwards.
    """
    triangles: list[np.ndarray] = []
    face_ids: list[np.ndarray] = []
    offset = 0
    for face_index, count in enumerate(counts):
        count = int(count)
        if count >= 3:
            corners = np.arange(offset, offset + count)
            fan = np.column_stack(
                [
                    np.full(count - 2, corners[0]),
                    corners[1 : count - 1],
                    corners[2:count],
                ]
            )
            triangles.append(fan)
            face_ids.append(np.full(count - 2, face_index, dtype=np.int64))
        offset += count

    if not triangles:
        return np.zeros((0, 3), dtype=np.int64), np.zeros((0,), dtype=np.int64)
    return np.vstack(triangles), np.concatenate(face_ids)


def _corner_primvar(
    usd_mesh: UsdGeom.Mesh, primvar_name: str, corner_count: int
) -> Optional[np.ndarray]:
    """Read a primvar and expand it to one value per face-corner.

    Handles every interpolation USD allows for a primvar (``constant``,
    ``uniform``, ``vertex``, ``faceVarying``); anything else, or a primvar
    that is missing or empty, yields ``None`` rather than a guess.

    Parameters
    ----------
    usd_mesh : pxr.UsdGeom.Mesh
        Mesh the primvar belongs to.
    primvar_name : str
        Primvar to read, e.g. ``"st"``.
    corner_count : int
        Length of the mesh's flat ``faceVertexIndices`` -- the per-corner
        arrays this must align with.

    Returns
    -------
    numpy.ndarray or None
        ``(corner_count, k)`` array, or ``None`` if unreadable.
    """
    primvar = UsdGeom.PrimvarsAPI(usd_mesh).GetPrimvar(primvar_name)
    if not primvar or not primvar.HasValue():
        return None
    values = primvar.Get()
    if not values:
        return None
    values_arr = np.asarray(values, dtype=np.float64)

    if primvar.IsIndexed():
        indices = np.asarray(primvar.GetIndices(), dtype=np.int64)
        values_arr = values_arr[indices]

    interpolation = primvar.GetInterpolation()
    if interpolation == UsdGeom.Tokens.faceVarying:
        return values_arr if len(values_arr) == corner_count else None
    if interpolation == UsdGeom.Tokens.constant:
        return np.tile(values_arr[0], (corner_count, 1))
    if interpolation == UsdGeom.Tokens.uniform:
        counts = usd_mesh.GetFaceVertexCountsAttr().Get()
        if not counts or len(values_arr) != len(counts):
            return None
        return np.repeat(values_arr, np.asarray(counts, dtype=np.int64), axis=0)
    if interpolation == UsdGeom.Tokens.vertex:
        point_indices = usd_mesh.GetFaceVertexIndicesAttr().Get()
        if not point_indices or values_arr.shape[0] <= max(point_indices, default=-1):
            return None
        return values_arr[np.asarray(point_indices, dtype=np.int64)]
    return None


def _subset_face_groups(
    mesh_prim: Usd.Prim, face_count: int
) -> list[tuple[np.ndarray, Optional[UsdShade.Material]]]:
    """Split a mesh's faces into material groups.

    A mesh with ``GeomSubset`` children carries more than one material;
    whatever faces no subset claims (there should be none, but an
    inconsistently authored asset can leave some) fall back to the mesh's own
    binding, so nothing is silently dropped from the textured view.

    Parameters
    ----------
    mesh_prim : pxr.Usd.Prim
        Mesh to inspect.
    face_count : int
        Total polygon count, for finding unclaimed faces.

    Returns
    -------
    list of tuple
        ``(face_indices, material)`` pairs. ``material`` is ``None`` when
        nothing is bound for that group.
    """
    subsets = UsdGeom.Subset.GetAllGeomSubsets(UsdGeom.Imageable(mesh_prim))
    if not subsets:
        return [(np.arange(face_count, dtype=np.int64), bound_material(mesh_prim))]

    groups: list[tuple[np.ndarray, Optional[UsdShade.Material]]] = []
    claimed: set[int] = set()
    for subset in subsets:
        indices = np.asarray(subset.GetIndicesAttr().Get() or [], dtype=np.int64)
        if indices.size == 0:
            continue
        claimed.update(int(i) for i in indices)
        groups.append((indices, bound_material(subset.GetPrim())))

    leftover = sorted(set(range(face_count)) - claimed)
    if leftover:
        groups.append((np.asarray(leftover, dtype=np.int64), bound_material(mesh_prim)))
    return groups


def _mesh_material_pieces(
    mesh_prim: Usd.Prim, to_metres: float
) -> list[tuple[trimesh.Trimesh, Optional[np.ndarray], MaterialAppearance]]:
    """Split one USD mesh into per-material triangle meshes, with UVs.

    Unlike :func:`_mesh_to_trimesh`, this does not weld shared vertices --
    a per-corner attribute like UV needs each face-corner kept distinct, so
    positions are gathered per corner instead of indexed by USD point id.

    Parameters
    ----------
    mesh_prim : pxr.Usd.Prim
        A prim that :func:`app.usd_scene.is_visual_mesh` accepted.
    to_metres : float
        Stage-unit-to-metre factor.

    Returns
    -------
    list of tuple
        ``(mesh, uv_or_none, appearance)`` per distinct material found.
    """
    usd_mesh = UsdGeom.Mesh(mesh_prim)
    points = usd_mesh.GetPointsAttr().Get()
    counts = usd_mesh.GetFaceVertexCountsAttr().Get()
    indices = usd_mesh.GetFaceVertexIndicesAttr().Get()
    if not points or not counts or not indices:
        return []

    counts_arr = np.asarray(counts, dtype=np.int64)
    indices_arr = np.asarray(indices, dtype=np.int64)
    if int(counts_arr.sum()) != indices_arr.size:
        return []

    matrix = np.asarray(world_transform(mesh_prim), dtype=np.float64)
    corner_positions = np.asarray(points, dtype=np.float64)[indices_arr]
    corner_positions = corner_positions @ matrix[:3, :3] + matrix[3, :3]
    corner_positions *= to_metres

    tri_corners, tri_face_ids = _triangulate_corners(counts_arr)
    if tri_corners.size == 0:
        return []
    if usd_mesh.GetOrientationAttr().Get() == UsdGeom.Tokens.leftHanded:
        tri_corners = tri_corners[:, ::-1]

    uv_cache: dict[str, Optional[np.ndarray]] = {}
    pieces: list[tuple[trimesh.Trimesh, Optional[np.ndarray], MaterialAppearance]] = []
    for face_indices, material in _subset_face_groups(mesh_prim, len(counts_arr)):
        appearance = resolve_appearance(material)
        tri_mask = np.isin(tri_face_ids, face_indices)
        selected = tri_corners[tri_mask]
        if selected.size == 0:
            continue

        corner_ids = np.unique(selected)
        remap = np.zeros(corner_positions.shape[0], dtype=np.int64)
        remap[corner_ids] = np.arange(len(corner_ids))
        local_faces = remap[selected]
        local_positions = corner_positions[corner_ids]

        uv = None
        if appearance.texture_path is not None:
            if appearance.uv_primvar not in uv_cache:
                uv_cache[appearance.uv_primvar] = _corner_primvar(
                    usd_mesh, appearance.uv_primvar, len(indices_arr)
                )
            full_uv = uv_cache[appearance.uv_primvar]
            if full_uv is None:
                appearance = MaterialAppearance(
                    base_color=appearance.base_color,
                    metallic=appearance.metallic,
                    roughness=appearance.roughness,
                    notes=appearance.notes
                    + [
                        f"'{mesh_prim.GetPath()}' has a diffuse texture but no "
                        f"readable '{appearance.uv_primvar}' UVs; showing its "
                        f"base colour instead"
                    ],
                )
            else:
                uv = full_uv[corner_ids][:, :2]

        mesh = trimesh.Trimesh(
            vertices=local_positions, faces=local_faces, process=False
        )
        pieces.append((mesh, uv, appearance))

    return pieces


def _material_visual(
    uv: Optional[np.ndarray], appearance: MaterialAppearance
) -> tuple[trimesh.visual.texture.TextureVisuals, list[str]]:
    """Build a trimesh visual carrying a part's authored appearance.

    Parameters
    ----------
    uv : numpy.ndarray or None
        Per-vertex UV coordinates, when a texture is being applied.
    appearance : MaterialAppearance
        Resolved colour/texture to render with.

    Returns
    -------
    tuple
        ``(visual, warnings)``. A texture image that exists on disk but
        cannot be decoded degrades to the constant colour, noted as a
        warning rather than failing the export.
    """
    warnings: list[str] = []
    red, green, blue = appearance.base_color
    base_color_factor = [
        int(round(red * 255)),
        int(round(green * 255)),
        int(round(blue * 255)),
        255,
    ]

    texture_image = None
    if uv is not None and appearance.texture_path is not None:
        try:
            texture_image = Image.open(appearance.texture_path)
            texture_image.load()
        except Exception as exc:  # noqa: BLE001 - any decode failure, same fallback
            warnings.append(
                f"texture '{appearance.texture_path}' could not be decoded "
                f"({exc}); showing its base colour instead"
            )
            texture_image = None

    material = trimesh.visual.material.PBRMaterial(
        baseColorFactor=(
            [255, 255, 255, 255] if texture_image is not None else base_color_factor
        ),
        baseColorTexture=texture_image,
        metallicFactor=float(appearance.metallic),
        roughnessFactor=float(appearance.roughness),
        doubleSided=True,
    )
    visual = trimesh.visual.texture.TextureVisuals(
        uv=uv if texture_image is not None else None, material=material
    )
    return visual, warnings


def export_parts_glb(
    usd_path: Path, glb_path: Path, face_budget: int = DEFAULT_FACE_BUDGET
) -> tuple[Path, list[str]]:
    """Write a GLB holding one named node per part of a USD asset.

    Node names come from :func:`app.usd_scene.part_node_name`, the same
    function the manifest reader uses, so the viewer can join the two.

    Parameters
    ----------
    usd_path : pathlib.Path
        Source asset.
    glb_path : pathlib.Path
        Destination ``.glb``. Parent directories are created.
    face_budget : int, optional
        Per-part face ceiling before decimation is attempted.

    Returns
    -------
    tuple
        ``(glb_path, warnings)``.

    Raises
    ------
    MeshExportError
        If no part yielded geometry.
    app.usd_scene.UsdSceneError
        If the stage cannot be opened.
    """
    stage = open_stage(usd_path)
    to_metres = float(UsdGeom.GetStageMetersPerUnit(stage) or 1.0)

    part_prims = discover_part_prims(stage)
    if not part_prims:
        raise UsdSceneError(f"no parts with visual geometry in {usd_path}")

    palette = _part_palette(len(part_prims))
    scene = trimesh.Scene()
    warnings: list[str] = []
    exported = 0

    for index, part_prim in enumerate(part_prims):
        mesh, part_warnings = _part_mesh(part_prim, to_metres, face_budget)
        warnings.extend(part_warnings)
        if mesh is None:
            warnings.append(
                f"part '{part_prim.GetName()}' produced no geometry and is "
                f"missing from the GLB"
            )
            continue

        # Vertex colours rather than face colours: the GLB exporter converts
        # face colours to vertex colours anyway, via a scipy sparse matmul.
        # A part is a single flat colour, so writing them per vertex loses
        # nothing and drops a heavyweight dependency.
        mesh.visual = trimesh.visual.ColorVisuals(
            mesh=mesh,
            vertex_colors=np.tile(palette[index], (len(mesh.vertices), 1)),
        )
        name = part_node_name(part_prim)
        scene.add_geometry(mesh, node_name=name, geom_name=name)
        exported += 1

        piece_index = 0
        for mesh_prim in iter_visual_meshes(part_prim):
            for piece_mesh, uv, appearance in _mesh_material_pieces(
                mesh_prim, to_metres
            ):
                warnings.extend(appearance.notes)
                visual, visual_warnings = _material_visual(uv, appearance)
                warnings.extend(visual_warnings)
                piece_mesh.visual = visual
                piece_name = f"{name}{MATERIAL_NODE_SEP}{piece_index}"
                scene.add_geometry(
                    piece_mesh, node_name=piece_name, geom_name=piece_name
                )
                piece_index += 1

        collision_mesh, _ = _part_collision_mesh(part_prim, to_metres)
        if collision_mesh is not None:
            collision_name = f"{name}{COLLISION_NODE_SEP}0"
            scene.add_geometry(
                collision_mesh, node_name=collision_name, geom_name=collision_name
            )

    if exported == 0:
        raise MeshExportError(f"no exportable geometry in {usd_path}")

    glb_path.parent.mkdir(parents=True, exist_ok=True)
    glb_path.write_bytes(scene.export(file_type="glb"))
    # The same material is routinely bound to several parts (five cabinet
    # doors sharing one handle material, say), so the same textured-view
    # fallback would otherwise repeat once per part. Order-preserving so the
    # first occurrence -- usually the most specific -- stays first.
    return glb_path, list(dict.fromkeys(warnings))
