"""Checks over the patch layer that retargets mis-wired joint bodies.

A patch is only worth having if it changes exactly what it claims to and
nothing else. Both halves are pinned here: that the retarget lands, and that
composing the layer leaves the asset otherwise as it was found.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pxr")

from pxr import Usd, UsdGeom  # noqa: E402

from tools.patch_joint_bodies import find_retargets, write_patch  # noqa: E402

MIS_WIRED_USDA = """#usda 1.0
(
    defaultPrim = "root"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "root"
{
    def Xform "Frame"
    {
        def Mesh "geo"
        {
            point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
    }
    def Xform "Door" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {
        def Mesh "Door"
        {
            point3f[] points = [(0, 0, 1), (1, 0, 1), (0, 1, 1)]
            int[] faceVertexCounts = [3]
            int[] faceVertexIndices = [0, 1, 2]
        }
        def PhysicsRevoluteJoint "RevoluteJoint"
        {
            uniform token physics:axis = "Z"
            rel physics:body0 = </root/Frame/geo>
            rel physics:body1 = </root/Door/Door>
        }
    }
}
"""


@pytest.fixture()
def asset(tmp_path):
    """Write the reference defect: ``body1`` naming the mesh inside the door."""
    path = tmp_path / "Asset.usda"
    path.write_text(MIS_WIRED_USDA, encoding="utf-8")
    return path


@pytest.fixture()
def patched(asset, tmp_path):
    """Return the composed stage of the patch layer over ``asset``."""
    stage = Usd.Stage.Open(str(asset))
    out_path = tmp_path / "patches" / "Asset_bodyfix.usda"
    write_patch(asset, out_path, find_retargets(stage), stage)
    return Usd.Stage.Open(str(out_path))


class TestWhatItChanges:
    """The one opinion the layer is allowed to hold."""

    def test_a_body_inside_a_rigid_body_is_moved_onto_it(self, asset):
        stage = Usd.Stage.Open(str(asset))

        retargets = find_retargets(stage)

        assert [(r.rel_name, r.new_target) for r in retargets] == [
            ("physics:body1", "/root/Door")
        ]

    def test_the_composed_joint_names_the_rigid_body(self, patched):
        joint = patched.GetPrimAtPath("/root/Door/RevoluteJoint")

        targets = joint.GetRelationship("physics:body1").GetTargets()

        assert [t.pathString for t in targets] == ["/root/Door"]

    def test_a_body_with_no_rigid_ancestor_is_left_alone(self, patched):
        # body0 names static geometry. Pinning a hinge to a non-rigid frame is
        # how a door is attached to the world, so there is nothing to correct
        # and guessing at one would invent a body the author never wrote.
        joint = patched.GetPrimAtPath("/root/Door/RevoluteJoint")

        targets = joint.GetRelationship("physics:body0").GetTargets()

        assert [t.pathString for t in targets] == ["/root/Frame/geo"]


class TestWhatItMustNotChange:
    """Layer metadata does not compose across sublayers.

    USD reads it from the root layer, which the patch becomes. Left unstated,
    a Z-up asset in metres composes as Y-up in centimetres -- on its side at a
    hundredth of its size, with no error raised anywhere. A patch that does
    that is worse than no patch.
    """

    def test_the_up_axis_survives(self, patched):
        assert UsdGeom.GetStageUpAxis(patched) == "Z"

    def test_the_scale_survives(self, patched):
        assert UsdGeom.GetStageMetersPerUnit(patched) == 1.0

    def test_the_default_prim_survives(self, patched):
        assert patched.GetDefaultPrim().GetName() == "root"

    def test_the_geometry_still_composes(self, patched):
        assert patched.GetPrimAtPath("/root/Frame/geo").IsValid()
