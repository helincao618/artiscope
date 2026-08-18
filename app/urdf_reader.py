"""Read a URDF into the same manifest a USD asset produces.

URDF is not what this lab is for -- see ``DIRECTION.md`` -- but the manifest was
built format-neutral, and being able to open a robot description someone hands
you is plainly useful. What it buys is looking and driving. It cannot buy an
engine verdict: NVIDIA's validator reads ``UsdPhysics``, and a URDF has none.

Parsed by hand rather than with ``yourdfpy``, which costs 31 transitive
packages including scipy. URDF has been frozen for over a decade and the subset
that matters here -- links, joints, origins, axes, limits, inertials -- is a
few hundred lines of ElementTree.

Two structural differences from USD are worth holding on to while reading this:

* **URDF is a strict tree.** ``parent`` and ``child`` are namespace and
  kinematics at once, so the joint-points-at-the-wrong-prim defect that the
  cabinets have cannot be expressed here.
* **URDF is already SI**, fixed at metres and radians, with no stage units and
  no up-axis metadata to reconcile.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

from .models import (
    DriveInfo,
    Joint,
    JointLimits,
    JointManifest,
    JointType,
    MassInfo,
    Part,
    RawUnit,
)
from .usd_scene import UsdSceneError

URDF_SUFFIXES = (".urdf", ".xacro")

# URDF joint types, and what this tool does with each. 'continuous' is a
# revolute joint with no stops rather than a separate mechanism.
_MOVING_TYPES = {
    "revolute": JointType.REVOLUTE,
    "continuous": JointType.REVOLUTE,
    "prismatic": JointType.PRISMATIC,
}
_STATIC_TYPES = {"fixed"}
# Expressible in URDF, not modelled by this viewer. Announced, never dropped
# in silence -- absent and unread must not look the same.
_UNMODELLED_TYPES = {"floating", "planar"}


def _floats(text: Optional[str], count: int, default: tuple) -> list[float]:
    """Parse a whitespace-separated float triple, falling back on ``default``."""
    if not text:
        return list(default)
    try:
        values = [float(token) for token in text.split()]
    except ValueError:
        return list(default)
    return values if len(values) == count else list(default)


def _rpy_to_quat(roll: float, pitch: float, yaw: float) -> list[float]:
    """Convert URDF's fixed-axis roll-pitch-yaw to a ``(w, x, y, z)`` quaternion.

    URDF composes as Rz(yaw) * Ry(pitch) * Rx(roll). The manifest stores
    quaternions in USD's ``(w, x, y, z)`` order so both readers agree.

    Parameters
    ----------
    roll, pitch, yaw : float
        Angles in radians.

    Returns
    -------
    list of float
        Quaternion as ``[w, x, y, z]``.
    """
    half_roll, half_pitch, half_yaw = roll / 2.0, pitch / 2.0, yaw / 2.0
    sin_r, cos_r = math.sin(half_roll), math.cos(half_roll)
    sin_p, cos_p = math.sin(half_pitch), math.cos(half_pitch)
    sin_y, cos_y = math.sin(half_yaw), math.cos(half_yaw)
    return [
        cos_r * cos_p * cos_y + sin_r * sin_p * sin_y,
        sin_r * cos_p * cos_y - cos_r * sin_p * sin_y,
        cos_r * sin_p * cos_y + sin_r * cos_p * sin_y,
        cos_r * cos_p * sin_y - sin_r * sin_p * cos_y,
    ]


def _rotate(quat: list[float], vector: list[float]) -> list[float]:
    """Rotate ``vector`` by the ``(w, x, y, z)`` quaternion ``quat``."""
    w, x, y, z = quat
    vx, vy, vz = vector
    # v + 2w(q x v) + 2(q x (q x v)), expanded.
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return [
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    ]


def _quat_multiply(a: list[float], b: list[float]) -> list[float]:
    """Compose two ``(w, x, y, z)`` quaternions."""
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return [
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ]


def _origin_of(element) -> tuple[list[float], list[float]]:
    """Read an ``<origin>`` child into a translation and a quaternion."""
    origin = element.find("origin") if element is not None else None
    if origin is None:
        return [0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]
    xyz = _floats(origin.get("xyz"), 3, (0.0, 0.0, 0.0))
    rpy = _floats(origin.get("rpy"), 3, (0.0, 0.0, 0.0))
    return xyz, _rpy_to_quat(*rpy)


def _mass_of(link) -> MassInfo:
    """Read a link's ``<inertial>`` block."""
    inertial = link.find("inertial")
    if inertial is None:
        return MassInfo()
    mass_element = inertial.find("mass")
    if mass_element is None or mass_element.get("value") is None:
        return MassInfo()
    try:
        mass_kg = float(mass_element.get("value"))
    except ValueError:
        return MassInfo()

    centre, _ = _origin_of(inertial)
    return MassInfo(
        mass_kg=mass_kg,
        mass_authored=True,
        center_of_mass=centre,
        center_of_mass_authored=inertial.find("origin") is not None,
    )


def _drive_of(joint) -> DriveInfo:
    """Read a joint's actuation, such as URDF states it.

    URDF has no drive gains. What it has is ``<limit effort=... velocity=...>``
    and an optional ``<dynamics>`` with friction and damping. Reporting effort
    as a drive would overstate the file, so only damping is carried across and
    the absence of gains is left visible.
    """
    dynamics = joint.find("dynamics")
    limit = joint.find("limit")

    damping = None
    if dynamics is not None and dynamics.get("damping") is not None:
        try:
            damping = float(dynamics.get("damping"))
        except ValueError:
            damping = None

    max_force = None
    if limit is not None and limit.get("effort") is not None:
        try:
            max_force = float(limit.get("effort"))
        except ValueError:
            max_force = None

    present = damping is not None or max_force is not None
    return DriveInfo(
        present=present,
        is_active=bool(damping),
        damping=damping,
        max_force=max_force,
        drive_type="urdf-effort" if max_force is not None else None,
    )


def _limits_of(joint, joint_type: str) -> JointLimits:
    """Read a joint's stops.

    A ``continuous`` joint is unbounded by definition, which is a materially
    different asset from one limited to 90 degrees, so it is recorded as
    un-authored rather than given an invented range.
    """
    if joint_type == "continuous":
        return JointLimits(raw_unit=RawUnit.RADIAN, authored=False)

    limit = joint.find("limit")
    if limit is None:
        return JointLimits(raw_unit=RawUnit.RADIAN, authored=False)

    def _read(name: str) -> Optional[float]:
        raw = limit.get(name)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    lower, upper = _read("lower"), _read("upper")
    unit = RawUnit.RADIAN if joint_type == "revolute" else RawUnit.STAGE_UNIT
    return JointLimits(
        lower=lower,
        upper=upper,
        lower_raw=lower,
        upper_raw=upper,
        raw_unit=unit,
        authored=lower is not None or upper is not None,
    )


def _world_pose(
    link_name: str,
    parent_of: dict,
    origin_of: dict,
    cache: dict,
) -> tuple[list[float], list[float]]:
    """Accumulate a link's pose in world space by walking up the tree.

    URDF states every joint relative to its parent link, so a world-space
    anchor -- which is what the viewer draws and what the USD reader already
    produces -- has to be composed down the chain.

    Parameters
    ----------
    link_name : str
        Link to locate.
    parent_of : dict
        Link name to its parent link name.
    origin_of : dict
        Link name to its joint origin, as ``(xyz, quat)`` in parent space.
    cache : dict
        Memo of already-resolved poses, also the recursion guard.

    Returns
    -------
    tuple
        ``(translation, quaternion)`` in world space.
    """
    if link_name in cache:
        return cache[link_name]

    parent = parent_of.get(link_name)
    if parent is None or parent == link_name:
        pose = ([0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0])
        cache[link_name] = pose
        return pose

    # Guard against a malformed file describing a cycle: claim the origin
    # rather than recursing forever.
    cache[link_name] = ([0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0])

    parent_xyz, parent_quat = _world_pose(parent, parent_of, origin_of, cache)
    local_xyz, local_quat = origin_of[link_name]
    rotated = _rotate(parent_quat, local_xyz)
    pose = (
        [parent_xyz[i] + rotated[i] for i in range(3)],
        _quat_multiply(parent_quat, local_quat),
    )
    cache[link_name] = pose
    return pose


def read_urdf_manifest(
    urdf_path: Path, asset_name: Optional[str] = None
) -> JointManifest:
    """Build a joint manifest from a URDF file.

    Parameters
    ----------
    urdf_path : pathlib.Path
        Path to the asset.
    asset_name : str, optional
        Short name for the asset. Defaults to the robot's declared name, then
        the file stem.

    Returns
    -------
    JointManifest
        Parts, joints and any warnings raised along the way. ``source_format``
        is ``urdf``, which is what the service keys the tier off.

    Raises
    ------
    app.usd_scene.UsdSceneError
        If the file cannot be parsed or declares no links. Reusing the USD
        error type keeps the service's failure handling in one place; the
        message says which format actually failed.
    """
    urdf_path = Path(urdf_path)
    try:
        root = ElementTree.parse(urdf_path).getroot()
    except ElementTree.ParseError as error:
        raise UsdSceneError(f"{urdf_path.name} is not readable URDF: {error}") from error

    if root.tag != "robot":
        raise UsdSceneError(
            f"{urdf_path.name} has no <robot> element, so it is not a URDF"
        )

    warnings: list[str] = []
    links = {
        link.get("name"): link
        for link in root.findall("link")
        if link.get("name")
    }
    if not links:
        raise UsdSceneError(f"{urdf_path.name} declares no links")

    if urdf_path.suffix.lower() == ".xacro" or "xacro" in root.attrib.get(
        "{http://www.ros.org/wiki/xacro}", ""
    ):
        warnings.append(
            "This looks like an unexpanded xacro. Macros are not evaluated, so "
            "what is shown is only the literal content of the file."
        )

    parent_of: dict[str, str] = {}
    origin_of: dict[str, tuple[list[float], list[float]]] = {}
    moving: list[tuple] = []

    for joint in root.findall("joint"):
        name = joint.get("name") or "joint"
        kind = (joint.get("type") or "").lower()
        parent_element = joint.find("parent")
        child_element = joint.find("child")
        parent = parent_element.get("link") if parent_element is not None else None
        child = child_element.get("link") if child_element is not None else None

        if not child or child not in links:
            warnings.append(f"Joint '{name}' names no child link that exists.")
            continue

        xyz, quat = _origin_of(joint)
        parent_of[child] = parent if parent in links else child
        origin_of[child] = (xyz, quat)

        if kind in _STATIC_TYPES:
            continue
        if kind in _UNMODELLED_TYPES:
            warnings.append(
                f"Joint '{name}' is a {kind} joint, which this reader does not "
                f"model. It is missing from the parts tree below."
            )
            continue
        if kind not in _MOVING_TYPES:
            warnings.append(
                f"Joint '{name}' has type '{kind or 'unset'}', which this reader "
                f"does not model. It is missing from the parts tree below."
            )
            continue
        moving.append((name, kind, parent, child, joint))

    pose_cache: dict = {}
    # A link is only a root if no joint of any kind -- moving, fixed, or a
    # type this reader cannot drive -- names it as a child. `parent_of` is
    # populated for all three (see the loop above), and each already places
    # the link relative to its stated parent, so a link named as a fixed or
    # unmodelled joint's child is exactly as much "not a root" as one driven
    # by a hinge; leaving it out of `driven` would make it look like an
    # unconnected second base and hand it a fabricated pivot instead of the
    # position the file already states.
    driven = set(parent_of)

    parts = [
        Part(
            id=name,
            name=name,
            node_name=name,
            # Every URDF link is a body the solver moves; there is no static
            # marker equivalent to USD's optional RigidBodyAPI.
            is_rigid_body=True,
            is_root=name not in driven,
            mass=_mass_of(link),
        )
        for name, link in links.items()
    ]

    joints = []
    for name, kind, parent, child, element in moving:
        anchor, frame = _world_pose(child, parent_of, origin_of, pose_cache)
        axis_element = element.find("axis")
        axis_local = _floats(
            axis_element.get("xyz") if axis_element is not None else None,
            3,
            (1.0, 0.0, 0.0),
        )
        joints.append(
            Joint(
                id=name,
                name=name,
                prim_path=name,
                type=_MOVING_TYPES[kind],
                parent_part=parent if parent in links else None,
                child_part=child,
                axis_token=" ".join(f"{c:g}" for c in axis_local),
                axis_world=_rotate(frame, axis_local),
                anchor_world=anchor,
                frame_quat_world=frame,
                limits=_limits_of(element, kind),
                drive=_drive_of(element),
            )
        )

    roots = [part.id for part in parts if part.is_root]
    if len(roots) > 1:
        warnings.append(
            f"{len(roots)} links are driven by no joint, so the tree has more "
            f"than one root: {', '.join(sorted(roots))}."
        )

    return JointManifest(
        asset_name=asset_name or root.get("name") or urdf_path.stem,
        source_path=str(urdf_path),
        source_format="urdf",
        # URDF has no stage metadata to reconcile: it is metres, and Z-up by
        # near-universal convention.
        stage_meters_per_unit=1.0,
        stage_up_axis="Z",
        parts=parts,
        joints=joints,
        root_part=roots[0] if roots else None,
        warnings=warnings,
    )
