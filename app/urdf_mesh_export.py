"""Export a URDF's visual geometry as one GLB node per link.

Mirrors :mod:`app.mesh_export` so the viewer cannot tell the two sources apart:
one named, flat-coloured node per part, in world coordinates, keyed on the same
name the manifest uses.

The work URDF adds is resolving where the meshes actually are. A ``<mesh>``
element points at an STL, DAE or OBJ by a ``package://`` URI that only means
something inside a ROS workspace, and there is no workspace here. Those are
resolved against the file on disk on a best-effort basis and reported when they
cannot be found, because a link that silently renders as nothing looks exactly
like a link the robot does not have.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

import numpy as np
import trimesh

from .mesh_export import DEFAULT_FACE_BUDGET, MeshExportError, _part_palette
from .urdf_reader import _floats, _origin_of, _rotate
from .usd_scene import UsdSceneError

PACKAGE_SCHEME = "package://"
FILE_SCHEME = "file://"


def _resolve_mesh(raw: str, urdf_path: Path) -> Optional[Path]:
    """Locate a mesh file referenced by a URDF.

    ``package://pkg/meshes/x.stl`` is only resolvable against a ROS workspace.
    Rather than require one, this walks up from the URDF looking for a
    directory that makes the tail of the URI valid -- which covers the way
    robot description folders are actually laid out when someone sends you one.

    Parameters
    ----------
    raw : str
        Value of the ``filename`` attribute.
    urdf_path : pathlib.Path
        The URDF being read, used as the search origin.

    Returns
    -------
    pathlib.Path or None
        Existing file, or None when it cannot be found.
    """
    if raw.startswith(FILE_SCHEME):
        candidate = Path(raw[len(FILE_SCHEME) :])
        return candidate if candidate.is_file() else None

    if raw.startswith(PACKAGE_SCHEME):
        tail = raw[len(PACKAGE_SCHEME) :]
        package, _, remainder = tail.partition("/")
        bases = [urdf_path.parent, *urdf_path.parents]
        for base in bases:
            for candidate in (base / tail, base / remainder, base / package / remainder):
                if candidate.is_file():
                    return candidate
        return None

    direct = (urdf_path.parent / raw).resolve()
    if direct.is_file():
        return direct
    for base in urdf_path.parents:
        candidate = base / raw
        if candidate.is_file():
            return candidate
    return None


def _primitive(geometry) -> Optional[trimesh.Trimesh]:
    """Build a mesh for URDF's built-in shapes.

    Boxes, cylinders and spheres are common in hand-written descriptions and
    cost nothing to support, so a robot made entirely of primitives still
    renders instead of coming up empty.
    """
    box = geometry.find("box")
    if box is not None:
        return trimesh.creation.box(
            extents=_floats(box.get("size"), 3, (0.1, 0.1, 0.1))
        )

    cylinder = geometry.find("cylinder")
    if cylinder is not None:
        try:
            return trimesh.creation.cylinder(
                radius=float(cylinder.get("radius", 0.05)),
                height=float(cylinder.get("length", 0.1)),
            )
        except ValueError:
            return None

    sphere = geometry.find("sphere")
    if sphere is not None:
        try:
            return trimesh.creation.icosphere(
                radius=float(sphere.get("radius", 0.05))
            )
        except ValueError:
            return None

    return None


def _link_mesh(
    link, urdf_path: Path, face_budget: int
) -> tuple[Optional[trimesh.Trimesh], list[str]]:
    """Combine every ``<visual>`` of one link into a single mesh."""
    warnings: list[str] = []
    pieces: list[trimesh.Trimesh] = []
    name = link.get("name", "link")

    for visual in link.findall("visual"):
        geometry = visual.find("geometry")
        if geometry is None:
            continue

        mesh = None
        mesh_element = geometry.find("mesh")
        if mesh_element is not None:
            raw = mesh_element.get("filename") or ""
            located = _resolve_mesh(raw, urdf_path)
            if located is None:
                warnings.append(
                    f"link '{name}' references '{raw}', which is not on disk "
                    f"here; it is missing from the view"
                )
                continue
            try:
                loaded = trimesh.load(located, force="mesh")
            except Exception as error:  # noqa: BLE001 - many loader failures
                warnings.append(f"link '{name}': could not read {raw} ({error})")
                continue
            if isinstance(loaded, trimesh.Trimesh):
                mesh = loaded
                scale = _floats(mesh_element.get("scale"), 3, (1.0, 1.0, 1.0))
                if scale != [1.0, 1.0, 1.0]:
                    mesh.apply_scale(scale)
        else:
            mesh = _primitive(geometry)

        if mesh is None or mesh.is_empty:
            continue

        offset, quat = _origin_of(visual)
        if quat != [1.0, 0.0, 0.0, 0.0] or offset != [0.0, 0.0, 0.0]:
            mesh.vertices = np.array(
                [
                    [c + o for c, o in zip(_rotate(quat, vertex.tolist()), offset)]
                    for vertex in mesh.vertices
                ]
            )
        pieces.append(mesh)

    if not pieces:
        return None, warnings

    combined = trimesh.util.concatenate(pieces) if len(pieces) > 1 else pieces[0]
    if len(combined.faces) > face_budget:
        try:
            combined = combined.simplify_quadric_decimation(face_budget)
        except Exception:  # noqa: BLE001 - decimation is best effort
            warnings.append(
                f"link '{name}' has {len(combined.faces)} faces and could not "
                f"be decimated"
            )
    return combined, warnings


def export_urdf_parts_glb(
    urdf_path: Path, glb_path: Path, face_budget: int = DEFAULT_FACE_BUDGET
) -> tuple[Path, list[str]]:
    """Write a GLB holding one named node per link of a URDF.

    Parameters
    ----------
    urdf_path : pathlib.Path
        Source asset.
    glb_path : pathlib.Path
        Destination ``.glb``. Parent directories are created.
    face_budget : int, optional
        Per-link face ceiling before decimation is attempted.

    Returns
    -------
    tuple
        ``(glb_path, warnings)``.

    Raises
    ------
    MeshExportError
        If no link yielded geometry.
    app.usd_scene.UsdSceneError
        If the file cannot be parsed.
    """
    urdf_path = Path(urdf_path)
    try:
        root = ElementTree.parse(urdf_path).getroot()
    except ElementTree.ParseError as error:
        raise UsdSceneError(f"{urdf_path.name} is not readable URDF: {error}") from error

    links = [link for link in root.findall("link") if link.get("name")]
    if not links:
        raise UsdSceneError(f"no links in {urdf_path}")

    # World placement of each link, so the GLB arrives in the same frame the
    # USD path produces and the viewer's rig needs no special case.
    parent_of: dict[str, str] = {}
    origin_of: dict[str, tuple[list[float], list[float]]] = {}
    for joint in root.findall("joint"):
        child_element = joint.find("child")
        parent_element = joint.find("parent")
        child = child_element.get("link") if child_element is not None else None
        if not child:
            continue
        parent_of[child] = (
            parent_element.get("link") if parent_element is not None else None
        )
        origin_of[child] = _origin_of(joint)

    from .urdf_reader import _world_pose

    palette = _part_palette(len(links))
    scene = trimesh.Scene()
    warnings: list[str] = []
    exported = 0
    cache: dict = {}

    for index, link in enumerate(links):
        name = link.get("name")
        mesh, link_warnings = _link_mesh(link, urdf_path, face_budget)
        warnings.extend(link_warnings)
        if mesh is None:
            warnings.append(
                f"link '{name}' produced no geometry and is missing from the GLB"
            )
            continue

        translation, quat = _world_pose(name, parent_of, origin_of, cache)
        mesh.vertices = np.array(
            [
                [c + t for c, t in zip(_rotate(quat, vertex.tolist()), translation)]
                for vertex in mesh.vertices
            ]
        )
        mesh.visual = trimesh.visual.ColorVisuals(
            mesh=mesh,
            vertex_colors=np.tile(palette[index], (len(mesh.vertices), 1)),
        )
        scene.add_geometry(mesh, node_name=name, geom_name=name)
        exported += 1

    if exported == 0:
        raise MeshExportError(f"no exportable geometry in {urdf_path}")

    glb_path.parent.mkdir(parents=True, exist_ok=True)
    glb_path.write_bytes(scene.export(file_type="glb"))
    return glb_path, warnings
