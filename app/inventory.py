"""Describe what an articulated asset does and does not state about itself.

This is deliberately not a pass/fail gate. There is no agreed definition yet
of what a "good" articulated asset is, and inventing one in code would freeze
a guess into the tooling before anyone has looked at enough deliveries to have
an opinion.

What it is instead is a vocabulary. Each observation names one dimension along
which an articulated asset can vary -- does it have travel stops, is it
modelled at its zero, do the bodies carry a mass, is there a drive -- and
reports what this particular file says on that dimension. Read a dozen assets
and the pattern in what is routinely missing is the raw material for an audit
framework. Read one and you at least know what you were handed.

The findings are descriptive on purpose:

``authored``
    The file states it.
``absent``
    The file does not. A consumer will have to supply or guess it. Often
    perfectly legitimate -- a free hinge has no drive by design.
``unusual``
    Stated, but outside what is ordinarily seen. Worth a look, not a verdict.
``inconsistent``
    The data contradicts itself: an inverted range, a part driven by two
    joints. Not a matter of taste -- these cannot be satisfied at once.

Everything runs on the manifest alone -- no USD, no geometry, no engine -- so
it evaluates instantly on any manifest whatever format it was read from.

``manifest.warnings`` is deliberately not replayed here. It is the reader's
parsing log: it mixes facts about the asset with facts about this reader, and
three of its entries restate an observation produced properly below, so
forwarding it wholesale filed tool limits as asset defects and reported the
same defect twice under two different words.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, computed_field

from .models import JointManifest, JointType

# A revolute joint whose range exceeds this is more likely a data error than a
# real hinge. A door that opens 270 deg exists; one that opens 400 does not.
_MAX_PLAUSIBLE_REVOLUTE_DEG = 360.0

# How far a joint anchor may sit outside its child part's bounding box before
# it is worth remarking on. Real hinges sit on the edge of a door, occasionally
# just beyond it, but not half a metre away.
_ANCHOR_TOLERANCE_M = 0.05

# How far a joint's two body frames may sit apart in the authored pose before
# that pose can no longer be treated as the joint's zero. Tight, because the
# frames are meant to coincide exactly and anything above numerical noise
# means the asset was modelled part-way through its travel.
_REST_FRAME_TOLERANCE_M = 0.001
_REST_FRAME_TOLERANCE_DEG = 0.5


class Finding(str, Enum):
    """What the asset says on one dimension. See the module docstring."""

    AUTHORED = "authored"
    ABSENT = "absent"
    UNUSUAL = "unusual"
    INCONSISTENT = "inconsistent"


class Observation(BaseModel):
    """One dimension of an asset, and what this asset says on it.

    Attributes
    ----------
    id : str
        Stable identifier for the dimension, so two deliveries of the same
        asset can be diffed.
    dimension : str
        Human-readable name of the dimension, as a noun phrase. Observations
        sharing a dimension are meant to be read together.
    finding : Finding
        What the asset states.
    detail : str
        What was actually found, in plain language.
    subject : str, optional
        Name of the part or joint this is about.
    part_id, joint_id : str, optional
        Identifiers of what this is about, when it is about something in
        particular. ``subject`` is for reading; these are for landing on --
        without them an observation naming a door is a dead end, and the
        reader has to find that door in the tree themselves.
    """

    id: str
    dimension: str
    finding: Finding
    detail: str
    subject: Optional[str] = None
    part_id: Optional[str] = None
    joint_id: Optional[str] = None


class AssetInventory(BaseModel):
    """Everything the manifest says about one asset, dimension by dimension.

    Attributes
    ----------
    asset_name, source_path : str
        Identity of what was read.
    generated_at : str
        ISO-8601 UTC timestamp.
    part_count, joint_count : int
        Size of the kinematic tree.
    observations : list of Observation
        All findings, in dimension order.
    """

    asset_name: str
    source_path: str
    generated_at: str
    part_count: int
    joint_count: int
    observations: list[Observation] = Field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """Return the number of observations per finding."""
        result = {finding.value: 0 for finding in Finding}
        for observation in self.observations:
            result[observation.finding.value] += 1
        return result

    @computed_field
    @property
    def completeness(self) -> str:
        """Summarise the inventory in one word.

        This ranks how much a consumer would have to supply themselves. It is
        navigational -- it says where to look first, not whether the asset is
        acceptable.

        Returns
        -------
        str
            One of ``complete``, ``partial`` or ``inconsistent``.
        """
        counts = self.counts
        if counts["inconsistent"]:
            return "inconsistent"
        if counts["absent"]:
            return "partial"
        return "complete"


def _observe_structure(manifest: JointManifest) -> list[Observation]:
    """Describe the shape of the kinematic tree.

    Parameters
    ----------
    manifest : JointManifest
        Manifest to inspect.

    Returns
    -------
    list of Observation
        Structural observations.
    """
    observations: list[Observation] = []

    if manifest.root_part is None:
        observations.append(
            Observation(
                id="structure.root",
                dimension="Kinematic root",
                finding=Finding.INCONSISTENT,
                detail="No part is free of a driving joint, so the tree has no "
                "base to hang off.",
            )
        )
    else:
        root = manifest.part_by_id(manifest.root_part)
        observations.append(
            Observation(
                id="structure.root",
                dimension="Kinematic root",
                finding=Finding.AUTHORED,
                detail=f"'{root.name}' is the base.",
                subject=root.name,
                part_id=root.id,
            )
        )

    if not manifest.joints:
        observations.append(
            Observation(
                id="structure.articulated",
                dimension="Articulation",
                finding=Finding.ABSENT,
                detail="No revolute or prismatic joints. This is a rigid asset.",
            )
        )
        return observations

    observations.append(
        Observation(
            id="structure.articulated",
            dimension="Articulation",
            finding=Finding.AUTHORED,
            detail=f"{len(manifest.joints)} joint(s) across "
            f"{len(manifest.parts)} part(s).",
        )
    )

    # A part that holds another one up is a base, whether or not it is *the*
    # base. A delivered asset has one; an assembled scene has one per appliance
    # standing in it, and calling the other twelve orphans said the file was
    # wrong about something it had authored correctly.
    part_ids = {p.id for p in manifest.parts}
    bases = {j.parent_part for j in manifest.joints}
    bases |= {f.parent_part for f in manifest.fixed_joints}
    bases &= part_ids
    # More than one base means the file assembles several articulated things,
    # which is the only way to tell a scene from an asset without being told.
    # Geometry with no joint is then the room itself -- walls, floor, props --
    # and stating that is right where warning about it is not.
    is_assembly = len(bases) > 1

    for part in manifest.parts:
        if part.id == manifest.root_part or part.id in bases:
            continue
        driving = manifest.joints_of_child(part.id)
        welded = manifest.fixed_of_child(part.id)
        if not driving and welded:
            # A weld is an authored answer to "what holds this piece up", not
            # a gap. Reporting it as an orphan contradicts the parts tree,
            # which shows the same part correctly attached.
            parent = manifest.part_by_id(welded[0].parent_part)
            parent_name = parent.name if parent else welded[0].parent_part
            observations.append(
                Observation(
                    id="structure.welded_part",
                    dimension="Articulation",
                    finding=Finding.AUTHORED,
                    detail=f"'{part.name}' is welded to '{parent_name}' and "
                    f"moves rigidly with it, so it has no travel of its own.",
                    subject=part.name,
                    part_id=part.id,
                )
            )
        elif not driving and is_assembly:
            observations.append(
                Observation(
                    id="structure.static_part",
                    dimension="Articulation",
                    finding=Finding.AUTHORED,
                    detail=f"'{part.name}' carries no joint and holds nothing "
                    f"up: static geometry in an assembled scene.",
                    subject=part.name,
                    part_id=part.id,
                )
            )
        elif not driving:
            observations.append(
                Observation(
                    id="structure.orphan_part",
                    dimension="Articulation",
                    finding=Finding.UNUSUAL,
                    detail=f"'{part.name}' has no joint, so it cannot move and "
                    f"is not the base either.",
                    subject=part.name,
                    part_id=part.id,
                )
            )
        elif len(driving) > 1:
            observations.append(
                Observation(
                    id="structure.multi_joint_part",
                    dimension="Articulation",
                    finding=Finding.INCONSISTENT,
                    detail=f"'{part.name}' is driven by {len(driving)} joints, "
                    f"which describes a closed loop this tool cannot model.",
                    subject=part.name,
                    part_id=part.id,
                )
            )

    return observations


def _observe_limits(manifest: JointManifest) -> list[Observation]:
    """Describe each joint's range of motion.

    Parameters
    ----------
    manifest : JointManifest
        Manifest to inspect.

    Returns
    -------
    list of Observation
        Travel observations.
    """
    observations: list[Observation] = []

    for joint in manifest.joints:
        if not joint.limits.authored or not joint.limits.is_bounded:
            observations.append(
                Observation(
                    id="limits.authored",
                    dimension="Travel stops",
                    finding=Finding.ABSENT,
                    detail=f"'{joint.name}' has no authored limits: it spins or "
                    f"slides without ever stopping.",
                    subject=joint.name,
                    joint_id=joint.id,
                    part_id=joint.child_part,
                )
            )
            continue

        span = joint.limits.upper - joint.limits.lower
        if span <= 0.0:
            observations.append(
                Observation(
                    id="limits.range",
                    dimension="Travel range",
                    finding=Finding.INCONSISTENT,
                    detail=f"'{joint.name}' has an empty or inverted range "
                    f"({joint.limits.lower_raw} .. {joint.limits.upper_raw}); "
                    f"it cannot move.",
                    subject=joint.name,
                    joint_id=joint.id,
                    part_id=joint.child_part,
                )
            )
            continue

        if joint.type is JointType.REVOLUTE:
            span_deg = math.degrees(span)
            if span_deg > _MAX_PLAUSIBLE_REVOLUTE_DEG:
                observations.append(
                    Observation(
                        id="limits.range",
                        dimension="Travel range",
                        finding=Finding.UNUSUAL,
                        detail=f"'{joint.name}' swings {span_deg:.0f} deg, more "
                        f"than a full turn.",
                        subject=joint.name,
                        joint_id=joint.id,
                        part_id=joint.child_part,
                    )
                )
                continue
            detail = f"'{joint.name}' swings {span_deg:.1f} deg."
        else:
            detail = f"'{joint.name}' slides {span * 1000.0:.1f} mm."

        observations.append(
            Observation(
                id="limits.range",
                dimension="Travel range",
                finding=Finding.AUTHORED,
                detail=detail,
                subject=joint.name,
                joint_id=joint.id,
                part_id=joint.child_part,
            )
        )

    return observations


def _observe_anchors(manifest: JointManifest) -> list[Observation]:
    """Describe where each joint pivot sits relative to the part it moves.

    A pivot metres away from its door is the classic symptom of a frame or
    unit mistake, and it is invisible in a static render -- the asset only
    looks wrong once something drives it.

    Parameters
    ----------
    manifest : JointManifest
        Manifest to inspect.

    Returns
    -------
    list of Observation
        Pivot placement observations.
    """
    observations: list[Observation] = []

    for joint in manifest.joints:
        part = manifest.part_by_id(joint.child_part)
        if part is None or part.bbox_min is None or part.bbox_max is None:
            continue

        overshoot = max(
            max(low - value, value - high, 0.0)
            for value, low, high in zip(
                joint.anchor_world, part.bbox_min, part.bbox_max
            )
        )
        if overshoot > _ANCHOR_TOLERANCE_M:
            observations.append(
                Observation(
                    id="anchor.placement",
                    dimension="Pivot placement",
                    finding=Finding.UNUSUAL,
                    detail=f"'{joint.name}' pivots {overshoot * 1000.0:.0f} mm "
                    f"outside the bounds of '{part.name}'.",
                    subject=joint.name,
                    joint_id=joint.id,
                    part_id=part.id,
                )
            )
        else:
            observations.append(
                Observation(
                    id="anchor.placement",
                    dimension="Pivot placement",
                    finding=Finding.AUTHORED,
                    detail=f"'{joint.name}' pivots within '{part.name}'.",
                    subject=joint.name,
                    joint_id=joint.id,
                    part_id=part.id,
                )
            )

    return observations


def _observe_attachment(manifest: JointManifest) -> list[Observation]:
    """Describe whether each joint's child relationship names what it drives.

    This reader climbs from a joint's body1 target to the nearest rigid body
    or known part, so a relationship pointing one level too deep still looks
    and drives correctly here. That climb is a convenience for this tool, not
    a correction to the file: a physics engine will act on the path the file
    actually names, and if that path carries no rigid-body schema, nothing
    will move the way this viewer just showed. This was the exact shape of
    the defect the kitchen cabinets shipped with -- clean in every other
    dimension, unusable in an engine.

    Parameters
    ----------
    manifest : JointManifest
        Manifest to inspect.

    Returns
    -------
    list of Observation
        One observation per joint whose child relationship needed climbing.
    """
    offenders = [
        joint for joint in manifest.joints if joint.child_attachment_raw_path
    ]
    if not offenders:
        return []

    return [
        Observation(
            id="attachment.child_target",
            dimension="Body attachment",
            finding=Finding.INCONSISTENT,
            detail=f"'{joint.name}' names body1 "
            f"'{joint.child_attachment_raw_path}', not the part this viewer "
            f"drives it as. This reader climbed the hierarchy to find a body "
            f"it could work with; a physics engine will not, and may find "
            f"nothing movable at the authored path.",
            subject=joint.name,
            joint_id=joint.id,
            part_id=joint.child_part,
        )
        for joint in offenders
    ]


def _observe_rest_pose(manifest: JointManifest) -> list[Observation]:
    """Describe whether the asset was modelled in its joints' zero pose.

    A joint's coordinate is zero where its two body frames coincide. If they
    already coincide in the delivered geometry, the modelled pose is the zero
    pose and every position reported here is measured from it.

    If they do not, the asset was authored part-way through its travel, and
    the numbers stay internally consistent while silently referring to a
    different origin than the geometry shows -- a door drawn ajar but reported
    as shut. Worth noticing precisely because nothing else looks wrong.

    Parameters
    ----------
    manifest : JointManifest
        Manifest to inspect.

    Returns
    -------
    list of Observation
        One observation per joint whose frames disagree, or a single one
        covering the whole asset.
    """
    if not manifest.joints:
        return []

    offenders = [
        joint
        for joint in manifest.joints
        if joint.rest_frame_offset_m > _REST_FRAME_TOLERANCE_M
        or joint.rest_frame_offset_deg > _REST_FRAME_TOLERANCE_DEG
    ]

    if not offenders:
        return [
            Observation(
                id="rest_pose.frames_coincide",
                dimension="Zero pose",
                finding=Finding.AUTHORED,
                detail="Both frames of every joint coincide as delivered, so "
                "the modelled pose is the zero pose.",
            )
        ]

    return [
        Observation(
            id="rest_pose.frames_coincide",
            dimension="Zero pose",
            finding=Finding.UNUSUAL,
            detail=f"'{joint.name}' has its two frames "
            f"{joint.rest_frame_offset_m * 1000.0:.1f} mm and "
            f"{joint.rest_frame_offset_deg:.1f} deg apart as delivered, so the "
            f"geometry is not at this joint's zero. Positions shown for it are "
            f"measured from a different origin than the shape suggests.",
            subject=joint.name,
            joint_id=joint.id,
            part_id=joint.child_part,
        )
        for joint in offenders
    ]


def _observe_drives(manifest: JointManifest) -> list[Observation]:
    """Describe what the drive parameters actually do.

    An inert drive is not a defect -- a free hinge is a legitimate design --
    but it is the difference between "the door holds position" and "the door
    swings under gravity", and a delivery note claiming realistic feel is not
    supported by stiffness zero.

    Parameters
    ----------
    manifest : JointManifest
        Manifest to inspect.

    Returns
    -------
    list of Observation
        Drive observations.
    """
    observations: list[Observation] = []

    for joint in manifest.joints:
        drive = joint.drive
        if not drive.present:
            observations.append(
                Observation(
                    id="drive.state",
                    dimension="Drive",
                    finding=Finding.ABSENT,
                    detail=f"'{joint.name}' has no drive: a free, passive joint.",
                    subject=joint.name,
                    joint_id=joint.id,
                    part_id=joint.child_part,
                )
            )
            continue

        if not drive.is_active:
            observations.append(
                Observation(
                    id="drive.state",
                    dimension="Drive",
                    finding=Finding.AUTHORED,
                    detail=f"'{joint.name}' has a drive applied but with zero "
                    f"stiffness and damping, so it behaves as a free joint.",
                    subject=joint.name,
                    joint_id=joint.id,
                    part_id=joint.child_part,
                )
            )
            continue

        # Not every format states both gains. URDF has damping and no
        # stiffness at all, so only what the file actually said is reported.
        stated = [
            f"{label}={value:g}"
            for label, value in (
                ("stiffness", drive.stiffness),
                ("damping", drive.damping),
            )
            if value is not None
        ]
        observations.append(
            Observation(
                id="drive.state",
                dimension="Drive",
                finding=Finding.AUTHORED,
                detail=f"'{joint.name}' is actively driven"
                + (f" ({', '.join(stated)})." if stated else "."),
                subject=joint.name,
                joint_id=joint.id,
                part_id=joint.child_part,
            )
        )

        if (
            drive.target_position is not None
            and joint.limits.is_bounded
            and not (
                joint.limits.lower <= drive.target_position <= joint.limits.upper
            )
        ):
            observations.append(
                Observation(
                    id="drive.target_in_range",
                    dimension="Drive target",
                    finding=Finding.UNUSUAL,
                    detail=f"'{joint.name}' targets {drive.target_position:g} "
                    f"but can only reach {joint.limits.lower:g} .. "
                    f"{joint.limits.upper:g}. This is a return spring, not a "
                    f"position hold -- intentional for a button, a mistake "
                    f"anywhere else.",
                    subject=joint.name,
                    joint_id=joint.id,
                    part_id=joint.child_part,
                )
            )

    return observations


def _observe_mass(manifest: JointManifest) -> list[Observation]:
    """Describe whether simulated bodies carry an authored mass.

    Parameters
    ----------
    manifest : JointManifest
        Manifest to inspect.

    Returns
    -------
    list of Observation
        Mass observations.
    """
    bodies = [p for p in manifest.parts if p.is_rigid_body]
    if not bodies:
        return []

    missing = [p for p in bodies if not p.mass.mass_authored]
    if not missing:
        return [
            Observation(
                id="mass.authored",
                dimension="Mass",
                finding=Finding.AUTHORED,
                detail=f"All {len(bodies)} rigid bodies have an explicit mass.",
            )
        ]

    names = ", ".join(p.name for p in missing)
    return [
        Observation(
            id="mass.authored",
            dimension="Mass",
            finding=Finding.ABSENT,
            detail=f"{len(missing)} of {len(bodies)} rigid bodies have no "
            f"authored mass, so an engine will guess one from density: "
            f"{names}.",
        )
    ]


def build_inventory(manifest: JointManifest) -> AssetInventory:
    """Describe every dimension of one manifest.

    Parameters
    ----------
    manifest : JointManifest
        Manifest to describe.

    Returns
    -------
    AssetInventory
        Observations in dimension order.
    """
    observations: list[Observation] = []
    observations.extend(_observe_structure(manifest))
    observations.extend(_observe_limits(manifest))
    observations.extend(_observe_anchors(manifest))
    observations.extend(_observe_attachment(manifest))
    observations.extend(_observe_rest_pose(manifest))
    observations.extend(_observe_mass(manifest))
    observations.extend(_observe_drives(manifest))

    return AssetInventory(
        asset_name=manifest.asset_name,
        source_path=manifest.source_path,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        part_count=len(manifest.parts),
        joint_count=len(manifest.joints),
        observations=observations,
    )
