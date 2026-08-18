"""Read a joint manifest out of a USD stage.

Runs on the plain ``usd-core`` pip wheel: no GPU, no Isaac Sim, no EULA, and
no Omniverse Kit boot. Extracting the joint structure of a 42 MB asset takes
well under a second, which is what makes an interactive review tool possible
at all.

What this reader deliberately does not assume
---------------------------------------------
* **That joints live in a dedicated scope.** Assets routinely author each
  ``RevoluteJoint`` underneath the door it drives rather than in a central
  ``/Joints`` scope, so the whole stage is scanned.
* **That a body relationship points at a rigid body.** See
  :func:`app.usd_scene.resolve_body_prim`.
* **That an un-authored value is a value.** Missing limits, mass and centre of
  mass are reported as absent rather than as the sentinel USD hands back.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from pxr import Gf, Usd, UsdGeom, UsdPhysics, UsdShade

from .models import (
    CollisionInfo,
    DriveInfo,
    FixedAttachment,
    Joint,
    JointLimits,
    JointManifest,
    JointType,
    MassInfo,
    Part,
    PhysicsMaterialInfo,
    RawUnit,
)
from .usd_scene import (
    UsdSceneError,
    discover_part_prims,
    get_root_prim,
    has_non_uniform_scale,
    is_visual_mesh,
    iter_collision_meshes,
    iter_instanced_prims,
    iter_visual_meshes,
    open_stage,
    part_node_name,
    resolve_body_prim,
    world_transform,
)

_AXIS_VECTORS = {
    "X": Gf.Vec3d(1.0, 0.0, 0.0),
    "Y": Gf.Vec3d(0.0, 1.0, 0.0),
    "Z": Gf.Vec3d(0.0, 0.0, 1.0),
}


def _is_finite(value: Optional[float]) -> bool:
    """Return whether ``value`` is a usable finite number.

    Parameters
    ----------
    value : float or None
        Candidate value.

    Returns
    -------
    bool
        ``True`` when the value is neither ``None``, ``inf`` nor ``nan``.
    """
    return value is not None and math.isfinite(value)


def _vec3(value) -> list[float]:
    """Convert a USD 3-vector to a plain list of floats.

    Parameters
    ----------
    value : pxr.Gf.Vec3f or pxr.Gf.Vec3d
        Vector to convert.

    Returns
    -------
    list of float
        ``[x, y, z]``.
    """
    return [float(value[0]), float(value[1]), float(value[2])]


def _read_mass(part_prim: Usd.Prim, to_metres: float) -> MassInfo:
    """Collect mass properties for a part.

    The schema may sit on the part itself or on a prim nested inside it, so
    the subtree is searched for the first prim carrying a mass schema.

    Un-authored values are reported as absent. USD returns
    ``(-inf, -inf, -inf)`` for a centre of mass that was never written, and a
    physics engine reads that as "compute it from the geometry" -- passing the
    sentinel through as data would misrepresent it as an authored value.

    Parameters
    ----------
    part_prim : pxr.Usd.Prim
        The part to inspect.
    to_metres : float
        Stage-unit-to-metre factor, applied only to ``center_of_mass_world``
        -- ``center_of_mass`` stays exactly as authored, matching what a
        reviewer would see in ``usdview``.

    Returns
    -------
    MassInfo
        Mass properties with authorship flags.
    """
    info = MassInfo()
    for prim in Usd.PrimRange(part_prim, Usd.TraverseInstanceProxies()):
        if not prim.HasAPI(UsdPhysics.MassAPI):
            continue
        mass_api = UsdPhysics.MassAPI(prim)

        mass_attr = mass_api.GetMassAttr()
        if mass_attr and mass_attr.HasAuthoredValue():
            value = mass_attr.Get()
            if _is_finite(value):
                info.mass_kg = float(value)
                info.mass_authored = True

        com_attr = mass_api.GetCenterOfMassAttr()
        if com_attr and com_attr.HasAuthoredValue():
            com = com_attr.Get()
            if com is not None and all(math.isfinite(c) for c in com):
                info.center_of_mass = _vec3(com)
                info.center_of_mass_authored = True
                # `center_of_mass` is in this prim's local frame and raw
                # stage units -- correct for "what the file states" but the
                # wrong frame and scale for placing a marker in the viewer,
                # which works in world space and metres (see `anchor_world`
                # on Joint for the same conversion).
                local = Gf.Vec3d(float(com[0]), float(com[1]), float(com[2]))
                world = world_transform(prim).Transform(local) * to_metres
                info.center_of_mass_world = _vec3(world)

        if info.mass_authored:
            break
    return info


def _read_collision(part_prim: Usd.Prim) -> CollisionInfo:
    """Collect what a physics engine would collide against for a part.

    Parameters
    ----------
    part_prim : pxr.Usd.Prim
        The part to inspect.

    Returns
    -------
    CollisionInfo
        Whether the part has a collider, whether that collider is the visual
        mesh itself, and the approximation the first collider authored.
    """
    info = CollisionInfo()
    for prim in iter_collision_meshes(part_prim):
        info.has_collision = True
        if is_visual_mesh(prim):
            info.shares_visual_geometry = True
        if info.approximation is None and prim.HasAPI(UsdPhysics.MeshCollisionAPI):
            approx_attr = UsdPhysics.MeshCollisionAPI(prim).GetApproximationAttr()
            if approx_attr and approx_attr.HasAuthoredValue():
                info.approximation = str(approx_attr.Get())
    return info


def _read_physics_material(part_prim: Usd.Prim) -> PhysicsMaterialInfo:
    """Collect the physics material bound to a part, if any.

    The binding may sit on the part itself or on a mesh nested inside it, so
    the subtree is searched the same way :func:`_read_mass` searches for a
    mass schema. Bound via the ``physics`` material purpose, distinct from
    the render material :func:`app.usd_materials.bound_material` resolves --
    an asset is free to look like wood and behave like rubber.

    Parameters
    ----------
    part_prim : pxr.Usd.Prim
        The part to inspect.

    Returns
    -------
    PhysicsMaterialInfo
        Friction and restitution of the bound material, or an empty
        ``PhysicsMaterialInfo`` when nothing is bound.
    """

    def _get(attr) -> Optional[float]:
        if attr is None or not attr.HasAuthoredValue():
            return None
        value = attr.Get()
        return float(value) if value is not None else None

    for prim in Usd.PrimRange(part_prim, Usd.TraverseInstanceProxies()):
        material = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial(
            materialPurpose="physics"
        )[0]
        if not material:
            continue
        material_prim = material.GetPrim()
        if not material_prim.IsValid() or not material_prim.HasAPI(
            UsdPhysics.MaterialAPI
        ):
            continue

        physics_material = UsdPhysics.MaterialAPI(material_prim)
        return PhysicsMaterialInfo(
            path=material_prim.GetPath().pathString,
            name=material_prim.GetName(),
            static_friction=_get(physics_material.GetStaticFrictionAttr()),
            dynamic_friction=_get(physics_material.GetDynamicFrictionAttr()),
            restitution=_get(physics_material.GetRestitutionAttr()),
        )
    return PhysicsMaterialInfo()


def _read_drive(prim: Usd.Prim, joint_type: JointType, to_metres: float) -> DriveInfo:
    """Collect drive parameters for a joint.

    Presence of the schema and the ability to exert force are reported
    separately: a drive whose stiffness and damping are both zero behaves as a
    free hinge, and assets ship that way routinely (see ``examples/Cabinet``).
    Reporting only "a drive is applied" would tell a reviewer the opposite of
    the truth.

    Parameters
    ----------
    prim : pxr.Usd.Prim
        Joint prim.
    joint_type : JointType
        Determines the drive instance name and the unit of the target
        position.
    to_metres : float
        Stage-unit-to-metre factor, applied to a prismatic target position.

    Returns
    -------
    DriveInfo
        Drive parameters, with ``present=False`` when no drive is applied.
    """
    instance = "angular" if joint_type is JointType.REVOLUTE else "linear"
    if not prim.HasAPI(UsdPhysics.DriveAPI, instance):
        return DriveInfo()

    drive = UsdPhysics.DriveAPI.Get(prim, instance)
    info = DriveInfo(present=True)

    def _get(attr) -> Optional[float]:
        if attr is None or not attr.HasAuthoredValue():
            return None
        value = attr.Get()
        return float(value) if value is not None else None

    info.stiffness = _get(drive.GetStiffnessAttr())
    info.damping = _get(drive.GetDampingAttr())
    info.max_force = _get(drive.GetMaxForceAttr())
    info.target_velocity = _get(drive.GetTargetVelocityAttr())

    target = _get(drive.GetTargetPositionAttr())
    if target is not None:
        info.target_position = (
            math.radians(target)
            if joint_type is JointType.REVOLUTE
            else target * to_metres
        )

    type_attr = drive.GetTypeAttr()
    if type_attr and type_attr.HasAuthoredValue():
        info.drive_type = str(type_attr.Get())

    info.is_active = bool(info.stiffness) or bool(info.damping)
    return info


def _read_limits(
    joint: UsdPhysics.Joint, joint_type: JointType, to_metres: float
) -> JointLimits:
    """Collect the range of motion, normalised to SI units.

    Parameters
    ----------
    joint : pxr.UsdPhysics.Joint
        Typed joint schema, either revolute or prismatic.
    joint_type : JointType
        Selects degrees-to-radians or stage-units-to-metres conversion.
    to_metres : float
        Stage-unit-to-metre factor.

    Returns
    -------
    JointLimits
        Limits in SI units alongside the as-authored values. An infinite bound
        is reported as ``None``, meaning unbounded travel in that direction.
    """
    lower_attr = joint.GetLowerLimitAttr()
    upper_attr = joint.GetUpperLimitAttr()
    lower_raw = lower_attr.Get() if lower_attr else None
    upper_raw = upper_attr.Get() if upper_attr else None

    authored = bool(
        (lower_attr and lower_attr.HasAuthoredValue())
        or (upper_attr and upper_attr.HasAuthoredValue())
    )

    if joint_type is JointType.REVOLUTE:
        unit = RawUnit.DEGREE
        convert = math.radians
    else:
        unit = RawUnit.STAGE_UNIT

        def convert(value: float) -> float:
            return value * to_metres

    return JointLimits(
        lower=convert(float(lower_raw)) if _is_finite(lower_raw) else None,
        upper=convert(float(upper_raw)) if _is_finite(upper_raw) else None,
        lower_raw=float(lower_raw) if lower_raw is not None else None,
        upper_raw=float(upper_raw) if upper_raw is not None else None,
        raw_unit=unit,
        authored=authored,
    )


def _joint_frame(
    stage: Usd.Stage,
    body_target: Optional[str],
    local_pos,
    local_rot,
    to_metres: float,
) -> tuple[Gf.Vec3d, Gf.Quatd]:
    """Place a joint frame in world space.

    The joint's ``localPos``/``localRot`` are expressed in the frame of the
    prim its body relationship targets -- which is not necessarily the part
    prim -- so the target's own world transform is used here, while part
    identity is resolved separately.

    Parameters
    ----------
    stage : pxr.Usd.Stage
        Stage being read.
    body_target : str or None
        Prim path from the body relationship. ``None`` means the joint is
        anchored to the world, in which case the local values are already
        world values.
    local_pos : pxr.Gf.Vec3f
        Joint origin in the target's local frame.
    local_rot : pxr.Gf.Quatf
        Joint orientation in the target's local frame.
    to_metres : float
        Stage-unit-to-metre factor.

    Returns
    -------
    tuple of (pxr.Gf.Vec3d, pxr.Gf.Quatd)
        World-space position (in metres) and orientation of the joint frame.
    """
    offset = Gf.Vec3d(
        float(local_pos[0]), float(local_pos[1]), float(local_pos[2])
    )
    rotation = Gf.Quatd(
        float(local_rot.GetReal()),
        Gf.Vec3d(*[float(c) for c in local_rot.GetImaginary()]),
    ).GetNormalized()

    if body_target is None:
        return offset * to_metres, rotation

    target_prim = stage.GetPrimAtPath(body_target)
    if not target_prim or not target_prim.IsValid():
        return offset * to_metres, rotation

    matrix = world_transform(target_prim)
    position = matrix.Transform(offset) * to_metres
    # ExtractRotationQuat reads an orthonormal basis straight off the matrix.
    # An asset that resolves centimetres with a scale op arrives here as
    # 0.01 * R, which that reading turns into an axis up to 90 degrees out.
    basis = matrix.RemoveScaleShear().ExtractRotationQuat().GetNormalized()
    return position, (basis * rotation).GetNormalized()


def _frame_disagreement(
    pos0: Gf.Vec3d, rot0: Gf.Quatd, pos1: Gf.Vec3d, rot1: Gf.Quatd
) -> tuple[float, float]:
    """Measure how far a joint's two body frames are from coinciding.

    Parameters
    ----------
    pos0, rot0 : pxr.Gf.Vec3d, pxr.Gf.Quatd
        World pose of the frame attached to ``body0``.
    pos1, rot1 : pxr.Gf.Vec3d, pxr.Gf.Quatd
        World pose of the frame attached to ``body1``.

    Returns
    -------
    tuple of (float, float)
        Separation in metres and in degrees. Both zero means the authored
        pose is the joint's zero pose.
    """
    dot = abs(
        rot0.GetReal() * rot1.GetReal()
        + Gf.Dot(rot0.GetImaginary(), rot1.GetImaginary())
    )
    angle = 2.0 * math.acos(min(1.0, max(-1.0, dot)))
    return float((pos0 - pos1).GetLength()), float(math.degrees(angle))


def _part_bounds(
    bbox_cache: UsdGeom.BBoxCache, part_prim: Usd.Prim, to_metres: float
) -> tuple[Optional[list[float]], Optional[list[float]]]:
    """Compute the world-space bounds of a part's visual geometry.

    Parameters
    ----------
    bbox_cache : pxr.UsdGeom.BBoxCache
        Cache configured for the visible purposes.
    part_prim : pxr.Usd.Prim
        Part to measure.
    to_metres : float
        Stage-unit-to-metre factor.

    Returns
    -------
    tuple
        ``(min, max)`` as lists of three floats, or ``(None, None)`` when the
        part has no computable extent.
    """
    bounds = bbox_cache.ComputeWorldBound(part_prim)
    box = bounds.ComputeAlignedRange()
    if box.IsEmpty():
        return None, None
    lo, hi = box.GetMin(), box.GetMax()
    return (
        [float(c) * to_metres for c in lo],
        [float(c) * to_metres for c in hi],
    )


def read_manifest(usd_path: Path, asset_name: Optional[str] = None) -> JointManifest:
    """Build a joint manifest from a USD file.

    Parameters
    ----------
    usd_path : pathlib.Path
        Path to the asset.
    asset_name : str, optional
        Short name for the asset. Defaults to the file stem.

    Returns
    -------
    JointManifest
        Parts, joints and any warnings raised along the way.

    Raises
    ------
    app.usd_scene.UsdSceneError
        If the stage cannot be opened or exposes no parts.
    """
    stage = open_stage(usd_path)
    to_metres = float(UsdGeom.GetStageMetersPerUnit(stage) or 1.0)
    up_axis = str(UsdGeom.GetStageUpAxis(stage) or UsdGeom.Tokens.z)
    warnings: list[str] = []

    instanced = [p.GetPath().pathString for p in iter_instanced_prims(stage)]
    if instanced:
        # Traversal below does descend into these (see
        # app.usd_scene.iter_visual_meshes), but against no real delivery that
        # actually uses instancing yet -- worth extra scrutiny rather than
        # quiet trust the first time it matters.
        warnings.append(
            f"{len(instanced)} prim(s) are USD instances: "
            f"{', '.join(instanced)}. This reader expands them for geometry "
            f"and joints, but that path has not been exercised against a "
            f"real delivery; check this asset's parts list carefully."
        )

    part_prims = discover_part_prims(stage)
    if not part_prims:
        raise UsdSceneError(
            f"no parts with visual geometry found under "
            f"{get_root_prim(stage).GetPath()} in {usd_path}"
        )
    part_paths = {p.GetPath().pathString for p in part_prims}

    bbox_cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
    )

    parts: list[Part] = []
    for prim in part_prims:
        face_count = 0
        for mesh_prim in iter_visual_meshes(prim):
            counts = UsdGeom.Mesh(mesh_prim).GetFaceVertexCountsAttr().Get()
            face_count += len(counts or [])
        bbox_min, bbox_max = _part_bounds(bbox_cache, prim, to_metres)

        if has_non_uniform_scale(world_transform(prim)):
            warnings.append(
                f"part '{prim.GetName()}' carries a non-unit scale; joint "
                f"anchors on it may not match what a physics engine uses"
            )

        parts.append(
            Part(
                id=prim.GetPath().pathString,
                name=prim.GetName(),
                node_name=part_node_name(prim),
                is_rigid_body=prim.HasAPI(UsdPhysics.RigidBodyAPI),
                mass=_read_mass(prim, to_metres),
                collision=_read_collision(prim),
                physics_material=_read_physics_material(prim),
                visual_face_count=face_count,
                bbox_min=bbox_min,
                bbox_max=bbox_max,
            )
        )

    joints, fixed_joints, world_anchored = _read_joints(
        stage, part_paths, to_metres, warnings
    )

    # A part fixed-jointed to another is not a root either, even though it
    # has no entry in `joints` -- it has an entry in `fixed_joints` instead.
    child_ids = {j.child_part for j in joints} | {f.child_part for f in fixed_joints}
    roots = [p for p in parts if p.id not in child_ids]
    # A part pinned to the world is the base because the file says so; a part
    # with no parent is one only because nothing claimed it. Where both exist,
    # the declared answer wins over the inferred one.
    roots.sort(key=lambda p: p.id not in world_anchored)
    root_id: Optional[str] = None
    if roots:
        root_id = roots[0].id
        roots[0].is_root = True
        if len(roots) > 1:
            extra = ", ".join(p.name for p in roots[1:])
            warnings.append(
                f"{len(roots)} parts are not driven by a joint; "
                f"'{roots[0].name}' is reported as the base and each of the "
                f"others anchors a tree of its own: {extra}"
            )
    else:
        warnings.append("every part is driven by a joint; no root found")

    return JointManifest(
        asset_name=asset_name or usd_path.stem,
        source_path=str(usd_path),
        stage_meters_per_unit=to_metres,
        stage_up_axis=up_axis,
        parts=parts,
        joints=joints,
        fixed_joints=fixed_joints,
        root_part=root_id,
        warnings=warnings,
    )


def _read_joints(
    stage: Usd.Stage,
    part_paths: set[str],
    to_metres: float,
    warnings: list[str],
) -> tuple[list[Joint], list[FixedAttachment]]:
    """Scan the whole stage for revolute, prismatic and fixed joints.

    Parameters
    ----------
    stage : pxr.Usd.Stage
        Stage to scan.
    part_paths : set of str
        Known part paths, used to resolve body relationships.
    to_metres : float
        Stage-unit-to-metre factor.
    warnings : list of str
        Mutated in place with anything recoverable but suspicious.

    Returns
    -------
    tuple of (list of Joint, list of FixedAttachment, set of str)
        Interactive joints and rigid attachments, both in stage traversal
        order, plus the ``Part.id`` of every body pinned to the world.
    """
    joints: list[Joint] = []
    fixed_joints: list[FixedAttachment] = []
    world_anchored: set[str] = set()

    for prim in stage.Traverse(Usd.TraverseInstanceProxies()):
        is_fixed = False
        if prim.IsA(UsdPhysics.RevoluteJoint):
            joint_type = JointType.REVOLUTE
            typed = UsdPhysics.RevoluteJoint(prim)
        elif prim.IsA(UsdPhysics.PrismaticJoint):
            joint_type = JointType.PRISMATIC
            typed = UsdPhysics.PrismaticJoint(prim)
        elif prim.IsA(UsdPhysics.FixedJoint):
            # No degree of freedom, so nothing below is driveable -- but
            # body1 is still positioned relative to body0, not free-floating.
            # Treating this like a joint type this tool truly cannot read
            # would drop that link and turn a welded-on part into what looks
            # like an unconnected second root.
            is_fixed = True
            typed = UsdPhysics.Joint(prim)
        elif prim.IsA(UsdPhysics.Joint):
            # A joint we cannot model at all must still be announced.
            # Staying silent makes a dropped joint look exactly like an
            # asset that never had one, which is the one mistake this tool
            # must not make.
            warnings.append(
                f"joint '{prim.GetPath().pathString}' is a "
                f"{prim.GetTypeName()}, which this reader does not model; it "
                f"is missing from the parts tree below"
            )
            continue
        else:
            continue

        path = prim.GetPath().pathString
        body0 = typed.GetBody0Rel().GetTargets()
        body1 = typed.GetBody1Rel().GetTargets()
        if not body1:
            warnings.append(f"joint '{path}' has no body1; skipped")
            continue

        parent_prim = (
            resolve_body_prim(stage, body0[0].pathString, part_paths)
            if body0
            else None
        )
        child_prim = resolve_body_prim(stage, body1[0].pathString, part_paths)
        if child_prim is None:
            warnings.append(
                f"joint '{path}' drives a body outside every known part; skipped"
            )
            continue
        if parent_prim is None and body0:
            warnings.append(
                f"joint '{path}' is anchored to a body outside every known "
                f"part; treated as anchored to the world"
            )

        if is_fixed:
            if parent_prim is None:
                # A fixed joint with an empty body0 is how USD pins a body to
                # the world, so this one connects a part to nothing rather
                # than two parts to each other. Recording it as an attachment
                # would make the base look like somebody's child and cost it
                # the root -- loudly on an asset with no other candidate, and
                # silently on one where an unjointed part takes the title
                # instead.
                world_anchored.add(child_prim.GetPath().pathString)
                continue

            anchor, _ = _joint_frame(
                stage,
                body0[0].pathString if body0 else None,
                typed.GetLocalPos0Attr().Get() or Gf.Vec3f(0.0),
                typed.GetLocalRot0Attr().Get() or Gf.Quatf(1.0),
                to_metres,
            )
            fixed_joints.append(
                FixedAttachment(
                    id=path,
                    name=child_prim.GetName(),
                    prim_path=path,
                    parent_part=(
                        parent_prim.GetPath().pathString if parent_prim else ""
                    ),
                    child_part=child_prim.GetPath().pathString,
                    anchor_world=_vec3(anchor),
                )
            )
            continue

        axis_token = str(typed.GetAxisAttr().Get() or "Z").upper()
        if axis_token not in _AXIS_VECTORS:
            warnings.append(
                f"joint '{path}' has unrecognised axis '{axis_token}'; assuming Z"
            )
            axis_token = "Z"

        anchor, frame_quat = _joint_frame(
            stage,
            body0[0].pathString if body0 else None,
            typed.GetLocalPos0Attr().Get() or Gf.Vec3f(0.0),
            typed.GetLocalRot0Attr().Get() or Gf.Quatf(1.0),
            to_metres,
        )
        axis_world = frame_quat.Transform(_AXIS_VECTORS[axis_token])
        axis_world = Gf.Vec3d(axis_world).GetNormalized()

        # The child-side frame is not needed to place the joint, but comparing
        # it against the parent-side one is what establishes that the authored
        # pose really is the zero pose. Reading only frame0 leaves that as an
        # unchecked assumption, and a door delivered half-open would then be
        # reported as closed.
        child_anchor, child_quat = _joint_frame(
            stage,
            body1[0].pathString,
            typed.GetLocalPos1Attr().Get() or Gf.Vec3f(0.0),
            typed.GetLocalRot1Attr().Get() or Gf.Quatf(1.0),
            to_metres,
        )
        offset_m, offset_deg = _frame_disagreement(
            anchor, frame_quat, child_anchor, child_quat
        )

        limits = _read_limits(typed, joint_type, to_metres)
        if not limits.authored:
            warnings.append(
                f"joint '{prim.GetName()}' has no authored limits; it is free "
                f"to travel without a stop"
            )

        # body0 is routinely climbed past on purpose -- a static base has no
        # rigid-body schema at all, and that is not a defect. body1 is not:
        # a physics engine needs *some* body it can actually move, so a joint
        # whose child relationship names a prim this reader had to climb away
        # from is a joint the engine cannot drive as authored, whatever this
        # reader inferred it meant.
        raw_child_path = body1[0].pathString
        child_attachment_raw_path = (
            None
            if raw_child_path == child_prim.GetPath().pathString
            else raw_child_path
        )
        if child_attachment_raw_path is not None:
            raw_child_prim = stage.GetPrimAtPath(raw_child_path)
            has_schema = bool(
                raw_child_prim
                and raw_child_prim.IsValid()
                and raw_child_prim.HasAPI(UsdPhysics.RigidBodyAPI)
            )
            warnings.append(
                f"joint '{path}' names body1 '{raw_child_path}'"
                + (
                    ", which carries no rigid-body schema"
                    if not has_schema
                    else ""
                )
                + f"; this reader treats '{child_prim.GetPath().pathString}' as "
                f"what it drives, but a physics engine acts on the authored "
                f"path, not this reader's guess"
            )

        # The prim name is a poor label: all five cabinet doors call their
        # joint "RevoluteJoint" and differ only by path. A reviewer identifies
        # a joint by the part it moves, so that is what gets displayed, with
        # `prim_path` retaining the faithful identity.
        joints.append(
            Joint(
                id=path,
                name=child_prim.GetName(),
                prim_path=path,
                type=joint_type,
                parent_part=(
                    parent_prim.GetPath().pathString if parent_prim else ""
                ),
                child_part=child_prim.GetPath().pathString,
                axis_world=_vec3(axis_world),
                axis_token=axis_token,
                anchor_world=_vec3(anchor),
                frame_quat_world=[
                    float(frame_quat.GetReal()),
                    *[float(c) for c in frame_quat.GetImaginary()],
                ],
                limits=limits,
                drive=_read_drive(prim, joint_type, to_metres),
                rest_frame_offset_m=offset_m,
                rest_frame_offset_deg=offset_deg,
                child_attachment_raw_path=child_attachment_raw_path,
            )
        )

    return joints, fixed_joints, world_anchored
