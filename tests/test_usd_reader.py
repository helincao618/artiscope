"""Check the reader against the joint parameters the example assets author.

Every number asserted here is a value written down in ``examples/`` -- read
their ``doc`` strings for what each asset is deliberately doing wrong. So these
are round-trip assertions: the file states a limit in degrees on a Z axis with
an inert drive, and the reader has to hand back exactly that, in SI, with the
authored value kept alongside.

The two assets carry opposite conventions on purpose. Between them they cover
both motion modes, both mass conventions (authored and absent), both collision
conventions (separate guide hulls and collider-on-the-render-mesh), and the
presence and absence of a drive that can actually exert force.

Point ``ARTISCOPE_FIXTURE_DIR`` at a corpus of your own to run the structural
tests against real deliveries; the ones pinning specific authored numbers are
written against the examples.
"""

from __future__ import annotations

import math

import pytest
from pxr import Gf

from app.models import JointType
from app.usd_reader import _frame_disagreement, read_manifest

DEG = math.pi / 180.0


@pytest.fixture(scope="session")
def cabinet(cabinet_usd):
    """Manifest of the five-door cabinet."""
    return read_manifest(cabinet_usd)


@pytest.fixture(scope="session")
def dishwasher(dishwasher_usd):
    """Manifest of the dishwasher."""
    return read_manifest(dishwasher_usd)


class TestCabinet:
    """Cabinet: one static carcass plus five doors on free hinges."""

    def test_stage_is_z_up_metres(self, cabinet):
        assert cabinet.stage_meters_per_unit == pytest.approx(1.0)
        assert cabinet.stage_up_axis == "Z"

    def test_part_and_joint_counts(self, cabinet):
        assert len(cabinet.parts) == 6
        assert len(cabinet.joints) == 5

    def test_carcass_is_root(self, cabinet):
        root = cabinet.part_by_id(cabinet.root_part)
        assert root is not None
        assert root.name == "Cabinet"
        assert root.is_root

    def test_masses_are_authored_catalogue_values(self, cabinet):
        by_name = {p.name: p for p in cabinet.parts}
        assert by_name["Cabinet"].mass.mass_kg == pytest.approx(20.0)
        for index in range(1, 6):
            door = by_name[f"Cabinet_Door00{index}"]
            assert door.mass.mass_kg == pytest.approx(2.0)
            assert door.mass.mass_authored

    def test_every_joint_is_revolute_about_z(self, cabinet):
        for joint in cabinet.joints:
            assert joint.type is JointType.REVOLUTE
            assert joint.axis_token == "Z"
            assert joint.axis_world == pytest.approx([0.0, 0.0, 1.0], abs=1e-6)

    def test_four_doors_swing_one_way_and_the_fifth_the_other(self, cabinet):
        limits = {j.name: (j.limits.lower, j.limits.upper) for j in cabinet.joints}
        for index in range(1, 5):
            lower, upper = limits[f"Cabinet_Door00{index}"]
            assert lower == pytest.approx(-90.0 * DEG)
            assert upper == pytest.approx(0.0)

        lower, upper = limits["Cabinet_Door005"]
        assert lower == pytest.approx(0.0)
        assert upper == pytest.approx(90.0 * DEG)

    def test_limits_keep_their_authored_degrees(self, cabinet):
        joint = next(
            j for j in cabinet.joints if j.name == "Cabinet_Door001"
        )
        assert joint.limits.lower_raw == pytest.approx(-90.0)
        assert joint.limits.raw_unit.value == "degree"
        assert joint.limits.authored

    def test_drives_are_applied_but_inert(self, cabinet):
        # The distinction that matters: a DriveAPI exists on all five doors,
        # but with zero stiffness and damping they are free hinges. Reporting
        # only "a drive is present" would read as the opposite.
        for joint in cabinet.joints:
            assert joint.drive.present
            assert not joint.drive.is_active
            assert joint.drive.stiffness == pytest.approx(0.0)
            assert joint.drive.damping == pytest.approx(0.0)

    def test_all_doors_hang_off_the_carcass(self, cabinet):
        for joint in cabinet.joints:
            assert joint.parent_part == cabinet.root_part

    def test_every_door_names_a_body1_one_level_too_deep(self, cabinet):
        # The reference case for a joint the engine cannot drive as authored:
        # body1 names a duplicate-named prim nested inside the door, which
        # carries no rigid-body schema at all. This reader still climbs to
        # the door for kinematics -- but must say so rather than pretend the
        # file named the door directly.
        for joint in cabinet.joints:
            assert joint.child_attachment_raw_path is not None
            assert joint.child_attachment_raw_path != joint.child_part
            assert joint.child_attachment_raw_path.startswith(
                joint.child_part + "/"
            )

    def test_reads_with_exactly_the_known_attachment_warnings(self, cabinet):
        # Five doors, five warnings, nothing else -- the point of this test is
        # that the warning is precise rather than a catch-all, so a real new
        # problem would still stand out against it.
        mismatches = [
            w for w in cabinet.warnings if "rigid-body schema" in w
        ]
        assert len(mismatches) == 5
        assert len(cabinet.warnings) == 5


class TestDishwasher:
    """Dishwasher: two motion modes in one asset, one real drive."""

    def test_part_and_joint_counts(self, dishwasher):
        assert len(dishwasher.parts) == 5
        assert len(dishwasher.joints) == 4

    def test_static_body_is_the_root(self, dishwasher):
        # The base carries no rigid-body schema at all -- it is the static
        # anchor -- so root detection cannot lean on RigidBodyAPI.
        root = dishwasher.part_by_id(dishwasher.root_part)
        assert root is not None
        assert root.name == "Dishwasher_Body001"
        assert not root.is_rigid_body

    def test_no_mass_is_authored_anywhere(self, dishwasher):
        # Opposite convention to the cabinet within the same delivery: an
        # engine would fall back to density here, so nothing may be reported
        # as an authored value.
        for part in dishwasher.parts:
            assert not part.mass.mass_authored
            assert part.mass.mass_kg is None

    def test_baskets_slide_300_mm_along_y(self, dishwasher):
        for name in ("Dishwasher_Container001", "Dishwasher_Container002"):
            joint = next(j for j in dishwasher.joints if j.name == name)
            assert joint.type is JointType.PRISMATIC
            assert joint.axis_token == "Y"
            assert joint.limits.lower == pytest.approx(-0.3, abs=1e-6)
            assert joint.limits.upper == pytest.approx(0.0)

    def test_door_opens_30_degrees_about_x(self, dishwasher):
        joint = next(j for j in dishwasher.joints if j.name == "Dishwasher_Door001")
        assert joint.type is JointType.REVOLUTE
        assert joint.axis_token == "X"
        assert joint.limits.lower == pytest.approx(0.0)
        assert joint.limits.upper == pytest.approx(30.0 * DEG)

    def test_the_button_is_the_only_driven_joint(self, dishwasher):
        driven = [j for j in dishwasher.joints if j.drive.is_active]
        assert [j.name for j in driven] == ["Dishwasher_Button001"]

        button = driven[0]
        assert button.type is JointType.PRISMATIC
        assert button.limits.upper == pytest.approx(0.003, abs=1e-6)
        assert button.drive.stiffness == pytest.approx(3e-4, rel=1e-3)
        assert button.drive.damping == pytest.approx(3e-5, rel=1e-3)

    def test_button_target_sits_outside_its_own_travel(self, dishwasher):
        # -0.05 m against a 0..0.003 m range: a spring that only ever pulls
        # back, never a position to hold. Worth preserving because it is the
        # one piece of evidence that drive parameters here were chosen per
        # function rather than filled in wholesale.
        button = next(
            j for j in dishwasher.joints if j.name == "Dishwasher_Button001"
        )
        assert button.drive.target_position == pytest.approx(-0.05, abs=1e-6)
        assert button.drive.target_position < button.limits.lower

    def test_the_button_and_door_share_the_cabinets_defect(self, dishwasher):
        # Same asset, same delivery pipeline: the button and the door are
        # wired one level too deep, exactly like the cabinet's five doors.
        # The two sliding containers are not -- one is a direct hit, the
        # other only climbs on its static (body0) side, which is the benign,
        # expected shape for a joint anchored to a base with no rigid-body
        # schema of its own.
        mismatched = {
            j.name for j in dishwasher.joints if j.child_attachment_raw_path
        }
        assert mismatched == {"Dishwasher_Button001", "Dishwasher_Door001"}

    def test_reads_with_exactly_the_known_attachment_warnings(self, dishwasher):
        mismatches = [
            w for w in dishwasher.warnings if "rigid-body schema" in w
        ]
        assert len(mismatches) == 2
        assert len(dishwasher.warnings) == 2


class TestUnitNormalisation:
    """Angles and lengths must reach the manifest in SI, never as authored."""

    def test_degrees_become_radians(self, cabinet):
        joint = next(
            j for j in cabinet.joints if j.name == "Cabinet_Door005"
        )
        assert joint.limits.upper_raw == pytest.approx(90.0)
        assert joint.limits.upper == pytest.approx(math.pi / 2.0)

    def test_prismatic_limits_stay_in_metres(self, dishwasher):
        joint = next(
            j for j in dishwasher.joints if j.name == "Dishwasher_Container001"
        )
        assert joint.limits.raw_unit.value == "stage_unit"
        assert joint.limits.lower == pytest.approx(joint.limits.lower_raw)


class TestRestPose:
    """Both assets are modelled at their joints' zero, and it is measured."""

    @pytest.mark.parametrize("fixture", ["cabinet", "dishwasher"])
    def test_the_authored_pose_is_the_zero_pose(self, fixture, cabinet, dishwasher):
        # The viewer treats delivered geometry as q = 0. That only holds when
        # a joint's two body frames already coincide, so it is measured rather
        # than assumed: an asset authored half-open would otherwise be driven
        # from the wrong origin without a word of warning.
        manifest = cabinet if fixture == "cabinet" else dishwasher
        for joint in manifest.joints:
            assert joint.rest_frame_offset_m == pytest.approx(0.0, abs=1e-6)
            assert joint.rest_frame_offset_deg == pytest.approx(0.0, abs=1e-3)

    def test_a_displaced_frame_would_be_detected(self):
        # Every real asset here reads zero, so on its own the assertion above
        # would pass just as happily against a reader that never compared the
        # frames at all. No delivery exhibits the defect yet, so the detection
        # is pinned directly instead.
        identity = Gf.Quatd(1.0, Gf.Vec3d(0.0, 0.0, 0.0))
        half = math.radians(30.0) / 2.0
        turned = Gf.Quatd(math.cos(half), Gf.Vec3d(0.0, 0.0, math.sin(half)))

        metres, degrees = _frame_disagreement(
            Gf.Vec3d(0.0, 0.0, 0.0), identity, Gf.Vec3d(0.1, 0.0, 0.0), turned
        )
        assert metres == pytest.approx(0.1)
        assert degrees == pytest.approx(30.0, abs=1e-6)

    def test_coinciding_frames_read_as_zero(self):
        identity = Gf.Quatd(1.0, Gf.Vec3d(0.0, 0.0, 0.0))
        point = Gf.Vec3d(1.0, -2.0, 3.0)
        metres, degrees = _frame_disagreement(point, identity, point, identity)
        assert metres == pytest.approx(0.0)
        assert degrees == pytest.approx(0.0, abs=1e-9)


class TestVisualGeometryDiscovery:
    """Guide-purpose colliders are excluded; collider-tagged meshes are not."""

    def test_cabinet_excludes_its_guide_hulls(self, cabinet):
        # A convex hull hangs under the carcass with purpose=guide, beside the
        # six-face render mesh. Counting it would inflate the face budget and
        # put an invisible box on screen.
        carcass = next(p for p in cabinet.parts if p.name == "Cabinet")
        assert carcass.visual_face_count == 6

    def test_dishwasher_keeps_meshes_that_double_as_colliders(self, dishwasher):
        # Every dishwasher part has CollisionAPI applied to its render mesh.
        # Filtering on the collision schema instead of purpose would leave the
        # asset with no geometry at all.
        for part in dishwasher.parts:
            assert part.visual_face_count > 0

    def test_cabinets_guide_hulls_are_reported_as_collision_not_visual(
        self, cabinet
    ):
        # The seventeen convex hulls excluded from visual_face_count above are
        # not merely dropped -- they are the asset's actual collider, distinct
        # from the render mesh it sits beside.
        for part in cabinet.parts:
            assert part.collision.has_collision
            assert not part.collision.shares_visual_geometry
            assert part.collision.approximation in {"convexHull", "sdf"}

    def test_dishwashers_colliders_are_reported_as_sharing_the_visual_mesh(
        self, dishwasher
    ):
        # The opposite of the cabinet: CollisionAPI sits on the same mesh
        # visual_face_count already counted, so there is nothing to overlay.
        for part in dishwasher.parts:
            assert part.collision.has_collision
            assert part.collision.shares_visual_geometry

    def test_neither_asset_authors_a_physics_material(self, cabinet, dishwasher):
        # Neither delivery binds UsdPhysics.MaterialAPI anywhere -- the render
        # material both bind for the default purpose must not be mistaken for
        # one just because the physics-purpose lookup falls back to it.
        for part in [*cabinet.parts, *dishwasher.parts]:
            assert part.physics_material.name is None
            assert part.physics_material.static_friction is None

    def test_parts_have_world_bounds(self, cabinet):
        for part in cabinet.parts:
            assert part.bbox_min is not None
            assert part.bbox_max is not None
            assert all(
                lo <= hi for lo, hi in zip(part.bbox_min, part.bbox_max)
            )


_PHYSICS_PROPERTIES_USDA = """#usda 1.0
(
    defaultPrim = "root"
    upAxis = "Z"
    metersPerUnit = 0.01
)

def Xform "root"
{
    def Xform "PartA" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI", "PhysicsMassAPI"]
    )
    {
        double3 xformOp:translate = (5, 0, 0)
        uniform token[] xformOpOrder = ["xformOp:translate"]
        float physics:mass = 2.5
        point3f physics:centerOfMass = (10, 0, 0)

        def Mesh "Visual"
        {
            point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
        def Scope "Collisions"
        {
            def Mesh "Hull" (
                prepend apiSchemas = ["PhysicsCollisionAPI", "PhysicsMeshCollisionAPI"]
            )
            {
                uniform token purpose = "guide"
                uniform token physics:approximation = "convexHull"
                point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
                int[] faceVertexCounts = [3]
                int[] faceVertexIndices = [0, 1, 2]
            }
        }
    }

    def Xform "PartB" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {
        def Mesh "Visual" (
            prepend apiSchemas = ["PhysicsCollisionAPI"]
        )
        {
            rel material:binding:physics = </root/PhysMat>
            point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }

    def Material "PhysMat" (
        prepend apiSchemas = ["PhysicsMaterialAPI"]
    )
    {
        float physics:staticFriction = 0.4
        float physics:dynamicFriction = 0.35
        float physics:restitution = 0.1
    }
}
"""


@pytest.fixture()
def physics_properties_usd(tmp_path):
    """A stage exercising what neither reference delivery authors: a centre
    of mass away from the origin, on a translated part in a non-1.0-scale
    stage, plus a physics material actually bound via the ``physics``
    purpose rather than the render fallback."""
    path = tmp_path / "PhysicsProperties.usda"
    path.write_text(_PHYSICS_PROPERTIES_USDA)
    return path


class TestPhysicsProperties:
    """Mass placement and physics-material binding, on a synthetic asset --
    neither reference delivery authors a centre of mass or a physics
    material, so the unit- and frame-conversion logic has nothing real to
    be checked against."""

    def test_center_of_mass_is_placed_in_world_space_and_metres(
        self, physics_properties_usd
    ):
        manifest = read_manifest(physics_properties_usd)
        part_a = next(p for p in manifest.parts if p.name == "PartA")

        # Exactly as authored: local frame, raw stage units (centimetres).
        assert part_a.mass.center_of_mass == pytest.approx([10.0, 0.0, 0.0])
        # Translated by the part's own (5, 0, 0), then the stage's 0.01
        # metres-per-unit applied: (5 + 10) * 0.01 = 0.15.
        assert part_a.mass.center_of_mass_world == pytest.approx(
            [0.15, 0.0, 0.0], abs=1e-6
        )

    def test_a_part_with_no_authored_centre_of_mass_places_no_marker(
        self, physics_properties_usd
    ):
        manifest = read_manifest(physics_properties_usd)
        part_b = next(p for p in manifest.parts if p.name == "PartB")
        assert part_b.mass.center_of_mass_world is None

    def test_a_collider_apart_from_the_visual_mesh_is_reported_as_such(
        self, physics_properties_usd
    ):
        manifest = read_manifest(physics_properties_usd)
        part_a = next(p for p in manifest.parts if p.name == "PartA")
        assert part_a.collision.has_collision
        assert not part_a.collision.shares_visual_geometry
        assert part_a.collision.approximation == "convexHull"

    def test_a_collider_on_the_render_mesh_is_reported_as_sharing_it(
        self, physics_properties_usd
    ):
        manifest = read_manifest(physics_properties_usd)
        part_b = next(p for p in manifest.parts if p.name == "PartB")
        assert part_b.collision.has_collision
        assert part_b.collision.shares_visual_geometry

    def test_a_physics_purpose_binding_is_read_as_a_physics_material(
        self, physics_properties_usd
    ):
        manifest = read_manifest(physics_properties_usd)
        part_b = next(p for p in manifest.parts if p.name == "PartB")
        material = part_b.physics_material
        assert material.name == "PhysMat"
        assert material.static_friction == pytest.approx(0.4)
        assert material.dynamic_friction == pytest.approx(0.35)
        assert material.restitution == pytest.approx(0.1)

    def test_a_part_with_no_physics_binding_gets_an_empty_material(
        self, physics_properties_usd
    ):
        manifest = read_manifest(physics_properties_usd)
        part_a = next(p for p in manifest.parts if p.name == "PartA")
        assert part_a.physics_material.name is None


class TestFixedJoints:
    """A weld is structure, not a missing degree of freedom.

    The oven welds its glass panel onto the door with a FixedJoint:
    Body001 --revolute--> Door001 --fixed--> Door001_Clear. Treating that
    weld like a joint type this reader truly cannot read (dropping it
    entirely) used to leave both Body001 and Door001_Clear looking like
    unconnected roots, and picked whichever came first in stage-traversal
    order -- the tiny glass panel -- as *the* root. Door001 and everything
    under Body001 then fell through to the unattached-part fallback and
    were driven about a fabricated Z axis through the origin instead of
    their real hinge, which is what made the door's slider visibly swing
    the geometry to the wrong place.
    """

    def test_the_weld_does_not_create_a_second_root(self, oven_usd):
        manifest = read_manifest(oven_usd)
        by_name = {p.name: p for p in manifest.parts}

        assert [p.name for p in manifest.parts if p.is_root] == [
            "WallOven_Body001"
        ]
        assert manifest.root_part == by_name["WallOven_Body001"].id
        assert not any(
            "more than one part is not driven" in w for w in manifest.warnings
        )

    def test_the_weld_is_readable_as_a_fixed_attachment(self, oven_usd):
        manifest = read_manifest(oven_usd)
        by_name = {p.name: p for p in manifest.parts}

        assert len(manifest.fixed_joints) == 1
        weld = manifest.fixed_joints[0]
        assert weld.parent_part == by_name["WallOven_Door001"].id
        assert weld.child_part == by_name["WallOven_Door001_Clear"].id

    def test_the_door_still_hangs_off_the_body_not_the_glass(self, oven_usd):
        # The regression this guards: with the weld dropped, Door001's own
        # revolute joint used to be unreachable from the (wrong) root and
        # never made it into the driveable tree at all.
        manifest = read_manifest(oven_usd)
        by_name = {p.name: p for p in manifest.parts}
        door_joint = next(
            j for j in manifest.joints if j.name == "WallOven_Door001"
        )
        assert door_joint.parent_part == by_name["WallOven_Body001"].id

    def test_a_fixed_joint_no_longer_reads_as_unmodelled(self, oven_usd):
        manifest = read_manifest(oven_usd)
        assert not [w for w in manifest.warnings if "does not model" in w]


_SPHERICAL_JOINT_USDA = """#usda 1.0
(
    defaultPrim = "root"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "root"
{
    def Xform "Base" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
    def Xform "Ball" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 2), (1, 0, 2), (0, 1, 2)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
    def PhysicsSphericalJoint "Ball_Joint"
    {
        rel physics:body0 = </root/Base>
        rel physics:body1 = </root/Ball>
    }
}
"""


_WORLD_ANCHOR_USDA = """#usda 1.0
(
    defaultPrim = "root"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "root"
{
    def Xform "Body" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
    def Xform "Trim" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 3), (1, 0, 3), (0, 1, 3)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
    def Xform "Door" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 1), (1, 0, 1), (0, 1, 1)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
    def PhysicsFixedJoint "WorldAnchor"
    {
        rel physics:body1 = </root/Body>
    }
    def PhysicsRevoluteJoint "Door_Joint"
    {
        uniform token physics:axis = "Z"
        rel physics:body0 = </root/Body>
        rel physics:body1 = </root/Door>
    }
}
"""


class TestWorldAnchoredBase:
    """A fixed joint with no ``body0`` pins a body to the world.

    It names one part, not two, so reading it as an attachment makes the base
    look like somebody's child and costs it the root. The delivery shows both
    ways that goes wrong: on Shelf nothing else qualified and
    the report claimed the tree had no base at all, and on Fridge an
    unjointed collider quietly took the title instead of the body.
    """

    def test_the_pinned_body_is_the_base(self, tmp_path):
        usd_path = tmp_path / "asset.usda"
        usd_path.write_text(_WORLD_ANCHOR_USDA, encoding="utf-8")

        manifest = read_manifest(usd_path)

        assert manifest.root_part == "/root/Body"

    def test_it_wins_the_root_over_a_part_that_merely_has_no_parent(self, tmp_path):
        # Trim is jointed to nothing at all, so it is a root by omission.
        # Body is a root by declaration, and the file's own answer wins.
        usd_path = tmp_path / "asset.usda"
        usd_path.write_text(_WORLD_ANCHOR_USDA, encoding="utf-8")

        manifest = read_manifest(usd_path)

        assert [p.name for p in manifest.parts if p.is_root] == ["Body"]

    def test_the_anchor_is_not_an_attachment_between_two_parts(self, tmp_path):
        usd_path = tmp_path / "asset.usda"
        usd_path.write_text(_WORLD_ANCHOR_USDA, encoding="utf-8")

        manifest = read_manifest(usd_path)

        assert manifest.fixed_joints == []

    def test_a_weld_between_two_real_parts_is_still_an_attachment(self, oven_usd):
        # The counterpart: this fix must not swallow genuine welds.
        manifest = read_manifest(oven_usd)

        assert len(manifest.fixed_joints) == 1


class TestUnmodelledJoints:
    """A joint we truly cannot read has to be announced, not dropped."""

    def test_an_asset_we_fully_read_stays_quiet(self, cabinet_usd):
        # The counterpart: the warning must not be noise, or it stops meaning
        # anything when it does fire.
        manifest = read_manifest(cabinet_usd)
        assert not [w for w in manifest.warnings if "does not model" in w]

    def test_a_joint_type_this_reader_cannot_read_is_reported(self, tmp_path):
        # Unlike a fixed joint (see TestFixedJoints), a spherical joint has a
        # degree of freedom this reader genuinely has no representation for,
        # so it must still be announced rather than silently dropped.
        usd_path = tmp_path / "asset.usda"
        usd_path.write_text(_SPHERICAL_JOINT_USDA, encoding="utf-8")

        manifest = read_manifest(usd_path)
        unread = [w for w in manifest.warnings if "does not model" in w]

        assert len(unread) == 1
        assert "PhysicsSphericalJoint" in unread[0]
        assert "/root/Ball_Joint" in unread[0]


_INVISIBLE_MESH_USDA = """#usda 1.0
(
    defaultPrim = "root"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "root"
{
    def Xform "Part001" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI", "PhysicsMassAPI"]
    )
    {
        float physics:mass = 1.0
        def Mesh "shown"
        {
            point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
        def Mesh "hidden"
        {
            token visibility = "invisible"
            point3f[] points = [(0, 0, 5), (1, 0, 5), (0, 1, 5)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
}
"""


class TestVisibility:
    """A mesh the asset switched off must not appear as if it were shown."""

    def test_an_invisible_mesh_is_excluded_from_the_part(self, tmp_path):
        usd_path = tmp_path / "asset.usda"
        usd_path.write_text(_INVISIBLE_MESH_USDA, encoding="utf-8")

        manifest = read_manifest(usd_path)

        part = manifest.parts[0]
        # Two triangles authored, one of them invisible: only one should be
        # counted, or the reported shape and the rendered shape disagree.
        assert part.visual_face_count == 1


_INSTANCED_PART_USDA = """#usda 1.0
(
    defaultPrim = "root"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "_prototypes"
{
    def Xform "Door"
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
}

def Xform "root"
{
    def Xform "Base" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 0), (2, 0, 0), (0, 2, 0)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
    def Xform "Door001" (
        instanceable = true
        prepend references = </_prototypes/Door>
    )
    {
    }
}
"""


class TestInstancing:
    """A part authored as a USD instance must not vanish without a word."""

    def test_geometry_inside_an_instance_is_still_found(self, tmp_path):
        # Before Usd.TraverseInstanceProxies() was added to every traversal
        # here, this part had zero visual faces and was silently absent from
        # both the manifest and the exported GLB.
        usd_path = tmp_path / "asset.usda"
        usd_path.write_text(_INSTANCED_PART_USDA, encoding="utf-8")

        manifest = read_manifest(usd_path)

        door = next(p for p in manifest.parts if p.name == "Door001")
        assert door.visual_face_count == 1

    def test_using_an_instance_is_called_out_by_name(self, tmp_path):
        usd_path = tmp_path / "asset.usda"
        usd_path.write_text(_INSTANCED_PART_USDA, encoding="utf-8")

        manifest = read_manifest(usd_path)

        flagged = [w for w in manifest.warnings if "USD instance" in w]
        assert len(flagged) == 1
        assert "/root/Door001" in flagged[0]


# A two-door cabinet, delivered on its own: parts one level under the root.
_CABINET_USDA = """#usda 1.0
(
    defaultPrim = "Cabinet"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "Cabinet"
{
    def Xform "Carcass"
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 0), (2, 0, 0), (0, 2, 0)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
    def Xform "Door001" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
    def Xform "Door002" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 1), (1, 0, 1), (0, 1, 1)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
    def PhysicsRevoluteJoint "Hinge001"
    {
        uniform token physics:axis = "Z"
        rel physics:body0 = </Cabinet/Carcass>
        rel physics:body1 = </Cabinet/Door001>
        float physics:lowerLimit = 0
        float physics:upperLimit = 90
    }
    def PhysicsRevoluteJoint "Hinge002"
    {
        uniform token physics:axis = "Z"
        rel physics:body0 = </Cabinet/Carcass>
        rel physics:body1 = </Cabinet/Door002>
        float physics:lowerLimit = 0
        float physics:upperLimit = 90
    }
}
"""

# The same cabinet referenced twice into a room, which is where the parts stop
# sitting directly under the root.
_ROOM_USDA = """#usda 1.0
(
    defaultPrim = "root"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "root"
{
    def Xform "Wall"
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 0), (9, 0, 0), (0, 9, 0)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
    def Xform "Cabinet_01" (
        prepend references = @cabinet.usda@
    )
    {
    }
    def Xform "Cabinet_02" (
        prepend references = @cabinet.usda@
    )
    {
    }
}
"""


class TestAssembledScene:
    """A scene nests its parts one level deeper than a delivered asset.

    Reading the direct children of the root as the parts collapses each
    appliance into a single body, and every joint inside it then appears to
    drive that one body: a room of clean assets reports a wall of body
    mismatches and closed loops that no asset contains.
    """

    @staticmethod
    def _room(tmp_path):
        (tmp_path / "cabinet.usda").write_text(_CABINET_USDA, encoding="utf-8")
        room_path = tmp_path / "room.usda"
        room_path.write_text(_ROOM_USDA, encoding="utf-8")
        return room_path

    def test_parts_are_found_below_a_referenced_asset(self, tmp_path):
        manifest = read_manifest(self._room(tmp_path))

        names = sorted(p.name for p in manifest.parts)
        assert names == [
            "Carcass",
            "Carcass",
            "Door001",
            "Door001",
            "Door002",
            "Door002",
            "Wall",
        ]

    def test_each_joint_drives_a_part_of_its_own(self, tmp_path):
        manifest = read_manifest(self._room(tmp_path))

        driven = [j.child_part for j in manifest.joints]
        assert len(driven) == 4
        assert len(set(driven)) == 4

    def test_a_repeated_part_name_stays_a_distinct_glb_node(self, tmp_path):
        manifest = read_manifest(self._room(tmp_path))

        node_names = [p.node_name for p in manifest.parts]
        assert len(set(node_names)) == len(node_names)
        assert "Cabinet_01-Door001" in node_names

    def test_no_node_name_is_one_the_browser_would_rewrite(self, tmp_path):
        # three.js strips [].:/ from every node name it loads, to keep its
        # animation track syntax parseable. A name containing one survives the
        # GLB and vanishes in the viewer, so the manifest asks for a node that
        # is not there and the part never joins the joint that moves it.
        manifest = read_manifest(self._room(tmp_path))

        reserved = set("[].:/")
        offenders = [
            p.node_name
            for p in manifest.parts
            if reserved & set(p.node_name) or p.node_name != p.node_name.strip()
        ]
        assert offenders == []

    def test_a_delivered_asset_still_names_its_nodes_by_part(self, tmp_path):
        # The scene fix must not rename anything in the flat case, or every
        # manifest-to-GLB join for a single asset moves with it.
        usd_path = tmp_path / "cabinet.usda"
        usd_path.write_text(_CABINET_USDA, encoding="utf-8")

        manifest = read_manifest(usd_path)

        assert sorted(p.node_name for p in manifest.parts) == [
            "Carcass",
            "Door001",
            "Door002",
        ]


# A centimetre asset, as four of the fifteen deliveries are authored.
_CM_ASSET_USDA = """#usda 1.0
(
    defaultPrim = "Fridge"
    upAxis = "Z"
    metersPerUnit = 0.01
)

def Xform "Fridge" (
    prepend apiSchemas = ["PhysicsArticulationRootAPI"]
)
{
    def Xform "Body" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 0), (60, 0, 0), (0, 0, 180)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
    def Xform "Door" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 0), (60, 0, 0), (0, 0, 90)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
    def PhysicsRevoluteJoint "Hinge"
    {
        uniform token physics:axis = "Z"
        rel physics:body0 = </Fridge/Body>
        rel physics:body1 = </Fridge/Door>
        point3f physics:localPos0 = (0, 0, 50)
        float physics:lowerLimit = 0
        float physics:upperLimit = 100
    }
}
"""

# The metre room that places it, resolving centimetres with a scale op and
# turning it to face the wall.
_CM_IN_ROOM_USDA = """#usda 1.0
(
    defaultPrim = "root"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "root"
{
    def Xform "Fridge_01" (
        prepend references = @fridge.usda@
    )
    {
        double3 xformOp:translate = (2, 0, 0)
        quatf xformOp:orient = (0.70710677, 0.70710677, 0, 0)
        float3 xformOp:scale:unitsResolve = (0.01, 0.01, 0.01)
        uniform token[] xformOpOrder = [
            "xformOp:translate", "xformOp:orient", "xformOp:scale:unitsResolve"
        ]
    }
}
"""


class TestUnitsResolvedByScale:
    """A scale in the transform chain is not part of the joint's orientation.

    Reading the rotation straight off a world matrix assumes an orthonormal
    basis. An asset that resolves its centimetres with a scale op reaches the
    reader as ``0.01 * R``, and that reading swings the axis by as much as a
    right angle: the fridge doors came out hinged on a horizontal axis.
    """

    @staticmethod
    def _room(tmp_path):
        (tmp_path / "fridge.usda").write_text(_CM_ASSET_USDA, encoding="utf-8")
        room_path = tmp_path / "room.usda"
        room_path.write_text(_CM_IN_ROOM_USDA, encoding="utf-8")
        return room_path

    def test_the_hinge_axis_only_follows_the_rotation(self, tmp_path):
        manifest = read_manifest(self._room(tmp_path))

        (joint,) = manifest.joints
        # The room turns the asset a quarter turn about X, so the door's own
        # +Z hinge points along -Y. The 0.01 must not enter this at all.
        assert joint.axis_world == pytest.approx([0.0, -1.0, 0.0], abs=1e-6)

    def test_the_anchor_still_reads_in_metres(self, tmp_path):
        # The scale belongs in the anchor, where it converts the units, so
        # dropping it from the whole matrix is not the fix.
        manifest = read_manifest(self._room(tmp_path))

        (joint,) = manifest.joints
        assert joint.anchor_world == pytest.approx([2.0, -0.5, 0.0], abs=1e-6)
