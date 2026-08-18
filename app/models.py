"""Joint manifest schema.

The manifest is the product of this repo, not an intermediate file: it is the
vocabulary for talking to a supplier about what an articulated asset should
carry, and the format-neutral seam that lets a USD, URDF or MJCF reader feed
the same viewer.

Two conventions that the rest of the codebase relies on
-------------------------------------------------------
1. **Everything is SI.** Angles are radians, lengths are metres, regardless of
   how the source file stored them. The as-authored value and its unit are
   kept alongside so a reviewer can reconcile against ``usdview``.
2. **"Absent" is never faked as a number.** A property the source file did not
   author is reported with ``authored=False`` and a ``None`` value. USD returns
   sentinels such as ``(-inf, -inf, -inf)`` for an un-authored centre of mass;
   passing that through as data would silently turn "the artist never set this"
   into "the artist set it to negative infinity".
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

Vec3 = list[float]
Quat = list[float]  # (w, x, y, z), matching USD's Gf.Quat* ordering.


class JointType(str, Enum):
    """Motion modes this tool understands.

    Restricted on purpose. These two modes cover the overwhelming majority of
    articulated assets in practice, and each maps to exactly one degree of
    freedom -- which is what makes limit enforcement a clamp rather than a
    solver.
    """

    REVOLUTE = "revolute"
    PRISMATIC = "prismatic"


class RawUnit(str, Enum):
    """Unit a limit was authored in, before SI normalisation."""

    DEGREE = "degree"
    STAGE_UNIT = "stage_unit"
    # URDF fixes its units at radians and metres, so for that source the raw
    # value and the SI one coincide. Recorded anyway: "already SI" and
    # "converted from degrees" are different facts about the file.
    RADIAN = "radian"


class MassInfo(BaseModel):
    """Mass properties of a part, with authorship tracked separately.

    Attributes
    ----------
    mass_kg : float, optional
        Mass in kilograms, or ``None`` when the source file did not author one.
    mass_authored : bool
        Whether the mass was explicitly written in the source file. When
        ``False`` a physics engine would fall back to a density estimate, so
        the value is not a statement of intent by the asset author.
    center_of_mass : list of float, optional
        Centre of mass in the part's local frame, exactly as authored (still
        in stage units), or ``None`` when un-authored.
    center_of_mass_authored : bool
        Whether the centre of mass was explicitly written.
    center_of_mass_world : list of float, optional
        The same point transformed to world space and converted to metres --
        the frame the viewer places geometry in. ``None`` whenever
        ``center_of_mass`` is, since there is nothing to place.
    """

    mass_kg: Optional[float] = None
    mass_authored: bool = False
    center_of_mass: Optional[Vec3] = None
    center_of_mass_authored: bool = False
    center_of_mass_world: Optional[Vec3] = None


class CollisionInfo(BaseModel):
    """What a physics engine would collide against for this part.

    Attributes
    ----------
    has_collision : bool
        Whether any prim in the part carries ``UsdPhysics.CollisionAPI``, on
        its own mesh or on a nested one.
    shares_visual_geometry : bool
        Whether the collider is the same mesh as the visual geometry
        (``Dishwasher``'s convention) rather than a separate hull in a
        ``purpose=guide`` scope (``Cabinet``'s). Only the latter
        gets a distinct overlay node in the exported GLB -- see
        ``app.mesh_export``.
    approximation : str, optional
        USD's collision approximation token (e.g. ``convexHull``,
        ``convexDecomposition``, ``none``), read off the first collider
        found. ``None`` when no ``PhysicsMeshCollisionAPI`` is applied.
    """

    has_collision: bool = False
    shares_visual_geometry: bool = False
    approximation: Optional[str] = None


class PhysicsMaterialInfo(BaseModel):
    """The physical material bound to a part's collision geometry.

    Governs contact behaviour a physics engine would simulate -- how much a
    part resists sliding against another, and how much of an impact it
    returns rather than absorbs. None of this affects how the part looks;
    it exists only for a reviewer asking what an engine would do with it.

    Attributes
    ----------
    path : str, optional
        Prim path of the bound ``UsdPhysics.MaterialAPI`` material.
    name : str, optional
        Short display name, the material prim's own name.
    static_friction, dynamic_friction : float, optional
        Coulomb friction coefficients, as authored.
    restitution : float, optional
        Bounciness, from 0 (fully inelastic) to 1 (fully elastic), as
        authored.
    """

    path: Optional[str] = None
    name: Optional[str] = None
    static_friction: Optional[float] = None
    dynamic_friction: Optional[float] = None
    restitution: Optional[float] = None


class DriveInfo(BaseModel):
    """Joint drive parameters.

    A drive can be *applied* yet inert: a ``PhysicsDriveAPI`` whose stiffness
    and damping are both zero is a free hinge, not a driven one, and assets
    ship that way routinely. ``is_active`` captures the distinction so a
    reviewer is not misled by the mere presence of the schema.

    Attributes
    ----------
    present : bool
        Whether a drive schema is applied to the joint at all.
    is_active : bool
        Whether the drive can actually exert force, i.e. stiffness or damping
        is non-zero.
    stiffness, damping, max_force, target_position, target_velocity : float, optional
        Drive parameters as authored. ``target_position`` is in SI units
        (radians for revolute, metres for prismatic).
    drive_type : str, optional
        USD's drive type token, typically ``force`` or ``acceleration``.
    """

    present: bool = False
    is_active: bool = False
    stiffness: Optional[float] = None
    damping: Optional[float] = None
    max_force: Optional[float] = None
    target_position: Optional[float] = None
    target_velocity: Optional[float] = None
    drive_type: Optional[str] = None


class JointLimits(BaseModel):
    """Range of the joint's single degree of freedom.

    Attributes
    ----------
    lower, upper : float, optional
        Limits in SI units (radians for revolute, metres for prismatic).
        ``None`` means the joint is unbounded along that direction.
    lower_raw, upper_raw : float, optional
        The values exactly as they appear in the source file.
    raw_unit : RawUnit
        The unit ``*_raw`` are expressed in.
    authored : bool
        Whether limits were explicitly written. An un-authored joint is free to
        spin, which is a materially different asset from one limited to 90 deg.
    """

    lower: Optional[float] = None
    upper: Optional[float] = None
    lower_raw: Optional[float] = None
    upper_raw: Optional[float] = None
    raw_unit: RawUnit = RawUnit.DEGREE
    authored: bool = False

    @property
    def is_bounded(self) -> bool:
        """Return ``True`` when both ends of the range are finite."""
        return self.lower is not None and self.upper is not None


class LimitOverride(BaseModel):
    """A locally supplied range, standing in for one the file gets wrong.

    This never replaces :attr:`Joint.limits`, which keeps saying whatever the
    source file says. It sits beside it, so a consumer that wants the truth and
    a viewer that wants a usable drag range can read the same manifest and each
    get what it needs. An override that quietly overwrote the authored value
    would make this tool's central claim -- that it reports the file as it is --
    false exactly where someone is relying on it.

    Overrides are per-asset, per-joint and hand-written. They generalise to
    nothing and are not meant to: see ``local/patches/README.md``.

    Attributes
    ----------
    lower, upper : float, optional
        Substitute range in the same SI units as :class:`JointLimits`.
    reason : str
        Why the authored range is unusable, in one line. Shown to the user
        rather than kept as a code comment, because an override nobody can see
        is worse than no override.
    """

    lower: Optional[float] = None
    upper: Optional[float] = None
    reason: str


class Part(BaseModel):
    """A rigid piece of the asset: either the static base or one movable body.

    Attributes
    ----------
    id : str
        Stable identifier. The source prim path, which is unique per stage.
    name : str
        Short display name.
    node_name : str
        Name of the matching node in the exported GLB. The viewer joins the
        manifest to the geometry on this key, so the mesh exporter and this
        reader must agree on it.
    is_rigid_body : bool
        Whether the source marked this part as a simulated rigid body. A part
        that is not one is a static anchor.
    is_root : bool
        Whether this part is the root of the kinematic tree, i.e. never the
        child of a joint.
    mass : MassInfo
        Mass properties.
    collision : CollisionInfo
        What a physics engine would collide against for this part.
    physics_material : PhysicsMaterialInfo
        The physics material bound to the part. Every field on it is
        ``None`` when nothing is bound.
    visual_face_count : int
        Triangle-ish face count of the part's visual geometry, for a rough
        sense of budget.
    bbox_min, bbox_max : list of float, optional
        World-space axis-aligned bounds of the visual geometry.
    """

    id: str
    name: str
    node_name: str
    is_rigid_body: bool = False
    is_root: bool = False
    mass: MassInfo = Field(default_factory=MassInfo)
    collision: CollisionInfo = Field(default_factory=CollisionInfo)
    physics_material: PhysicsMaterialInfo = Field(default_factory=PhysicsMaterialInfo)
    visual_face_count: int = 0
    bbox_min: Optional[Vec3] = None
    bbox_max: Optional[Vec3] = None


class Joint(BaseModel):
    """One degree of freedom connecting a parent part to a child part.

    The pose fields answer two independent questions that a reviewer needs to
    tell apart: *is the joint in the right place* (``anchor_world`` and
    ``frame_quat_world`` -- the 6-DOF pose of the joint frame) and *does it
    move through the right range* (``axis_world`` and ``limits`` -- the single
    free DOF). A joint can be correct on one and wrong on the other.

    Attributes
    ----------
    id, name, prim_path : str
        Identity of the joint in the source file.
    type : JointType
        Motion mode.
    parent_part, child_part : str
        ``Part.id`` of the two bodies the joint connects.
    axis_world : list of float
        Unit vector of the free axis in world space.
    axis_token : str
        The axis as named in the source file, e.g. ``Z``.
    anchor_world : list of float
        Joint origin in world space.
    frame_quat_world : list of float
        Orientation of the joint frame in world space, as ``(w, x, y, z)``.
    limits : JointLimits
        Range of motion, always as the source file states it.
    override_limits : LimitOverride, optional
        A locally patched range for the viewer to drive, when the authored one
        is unusable. ``None`` unless a patch file supplied one. ``limits`` is
        unaffected either way.
    drive : DriveInfo
        Drive parameters.
    rest_frame_offset_m, rest_frame_offset_deg : float
        How far apart the joint's two frames sit in the pose the file was
        authored in.

        A joint defines a frame on each body: one from ``localPos0``/
        ``localRot0`` on the parent, one from ``localPos1``/``localRot1`` on
        the child. The joint's coordinate is zero where those two frames
        coincide. When they already coincide as authored -- both of these are
        zero -- the modelled pose *is* the zero pose, and a viewer can treat
        the delivered geometry as ``q = 0``.

        When they do not, the asset was authored part-way through its travel
        (or is internally inconsistent), and every position this tool reports
        is measured from the wrong origin. Recording the discrepancy rather
        than assuming it away is what keeps that from being silent.
    child_attachment_raw_path : str, optional
        ``None`` when ``child_part`` is exactly what the source file's body1
        relationship names. Otherwise, the raw path it names instead --
        which this reader resolved up to ``child_part`` for kinematics, but
        did not resolve *away*.

        USD lets body0/body1 point at any prim; this reader climbs to the
        nearest rigid body or known part so driving still works when a
        relationship points one level too deep. That climb is worth doing --
        it is also worth not hiding. A joint whose body1 relationship names a
        prim with no rigid-body schema at all cannot be moved by a physics
        engine as authored, regardless of what this reader inferred the
        intent to be.
    """

    id: str
    name: str
    prim_path: str
    type: JointType
    parent_part: str
    child_part: str
    axis_world: Vec3
    axis_token: str
    anchor_world: Vec3
    frame_quat_world: Quat
    limits: JointLimits = Field(default_factory=JointLimits)
    override_limits: Optional[LimitOverride] = None
    drive: DriveInfo = Field(default_factory=DriveInfo)
    rest_frame_offset_m: float = 0.0
    rest_frame_offset_deg: float = 0.0
    child_attachment_raw_path: Optional[str] = None


class FixedAttachment(BaseModel):
    """A rigid, non-interactive attachment between two parts.

    USD lets an asset weld a decorative or non-moving piece (glass, trim, a
    handle) onto another part with a ``PhysicsFixedJoint`` instead of merging
    it into the same mesh. There is no degree of freedom here, so it is never
    shown as a control -- but the child part's position is still dictated by
    what it is fixed to, not by nothing. Dropping the attachment from the
    kinematic tree entirely, the way a joint type this tool truly cannot
    model is dropped, would make the child look like an unconnected second
    root and place it at a fabricated pivot instead of its real one.

    Attributes
    ----------
    id, name, prim_path : str
        Identity of the joint in the source file.
    parent_part, child_part : str
        ``Part.id`` of the two bodies the attachment connects.
    anchor_world : list of float
        Attachment origin in world space, used only to place ``child_part``
        correctly in the pivot hierarchy; there is no motion to anchor.
    """

    id: str
    name: str
    prim_path: str
    parent_part: str
    child_part: str
    anchor_world: Vec3


class JointManifest(BaseModel):
    """Everything needed to inspect and drive one articulated asset.

    Attributes
    ----------
    schema_version : str
        Version of this schema, so a stored manifest stays interpretable.
    asset_name : str
        Short name of the asset, used as the URL key by the service.
    source_path : str
        Absolute path of the file this was read from.
    source_format : str
        Format of the source, currently always ``usd``.
    stage_meters_per_unit : float
        Scale factor of the source stage, recorded for reconciliation. All
        lengths in this manifest are already converted to metres.
    stage_up_axis : str
        Up axis of the source stage, either ``Y`` or ``Z``.
    parts, joints : list
        The kinematic tree.
    fixed_joints : list
        Rigid, non-interactive attachments -- welds this tool cannot drive
        but still positions correctly. Kept separate from ``joints`` so
        nothing that walks the interactive controls (sliders, the joint
        list, the idle tour) has to special-case a joint with no DOF.
    root_part : str, optional
        ``Part.id`` of the tree root.
    warnings : list of str
        Anything the reader found suspicious but could recover from, recorded
        rather than raised because an odd asset still needs to be inspectable.

        This is a parsing log, not a findings list. It is written for whoever
        is debugging the reader, it mixes statements about the asset with
        statements about this reader's limits, and several entries duplicate
        an observation ``inventory.py`` produces properly. ``findings.py``
        promotes the two entries that are genuinely about this tool and
        ignores the rest; nothing else should read it to decide what to show.
    """

    schema_version: str = SCHEMA_VERSION
    asset_name: str
    source_path: str
    source_format: str = "usd"
    stage_meters_per_unit: float = 1.0
    stage_up_axis: str = "Z"
    parts: list[Part] = Field(default_factory=list)
    joints: list[Joint] = Field(default_factory=list)
    fixed_joints: list[FixedAttachment] = Field(default_factory=list)
    root_part: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)

    def part_by_id(self, part_id: str) -> Optional[Part]:
        """Return the part with ``part_id``, or ``None`` if absent.

        Parameters
        ----------
        part_id : str
            Identifier to look up.

        Returns
        -------
        Part or None
            The matching part.
        """
        for part in self.parts:
            if part.id == part_id:
                return part
        return None

    def joints_of_child(self, part_id: str) -> list[Joint]:
        """Return the joints for which ``part_id`` is the moving child.

        Parameters
        ----------
        part_id : str
            Identifier of the child part.

        Returns
        -------
        list of Joint
            Matching joints. Normally zero or one; more than one means the
            source describes a closed loop, which this tool does not model.
        """
        return [j for j in self.joints if j.child_part == part_id]

    def fixed_of_child(self, part_id: str) -> list[FixedAttachment]:
        """Return the fixed attachments that weld ``part_id`` onto something.

        The counterpart to :meth:`joints_of_child`. A part with no joint but a
        weld is attached, just not driveable, and anything reasoning about
        whether a part hangs off the tree has to consult both.

        Parameters
        ----------
        part_id : str
            Identifier of the welded child part.

        Returns
        -------
        list of FixedAttachment
            Matching attachments.
        """
        return [f for f in self.fixed_joints if f.child_part == part_id]
