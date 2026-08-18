"""Reading a URDF into the same manifest a USD asset produces.

URDF is the shallow path on purpose -- look and drive, no engine verdict, no
export (see DIRECTION.md). What matters here is that the manifest it produces
is indistinguishable in shape from the USD one, so everything downstream works
without a special case, and that the places URDF genuinely differs are recorded
rather than smoothed over.
"""

from __future__ import annotations

import math

import pytest

from app.models import JointType, RawUnit
from app.urdf_reader import read_urdf_manifest
from app.usd_scene import UsdSceneError

CABINET = """<?xml version="1.0"?>
<robot name="Cabinet">
  <link name="base">
    <inertial><mass value="20.0"/><origin xyz="0 0 0.4"/></inertial>
  </link>
  <link name="door"><inertial><mass value="2.5"/></inertial></link>
  <link name="drawer"/>
  <link name="knob"/>

  <joint name="hinge" type="revolute">
    <parent link="base"/><child link="door"/>
    <origin xyz="-0.3 0.2 0.4"/>
    <axis xyz="0 0 1"/>
    <limit lower="0" upper="1.5708" effort="10" velocity="1"/>
    <dynamics damping="0.5"/>
  </joint>

  <joint name="slide" type="prismatic">
    <parent link="base"/><child link="drawer"/>
    <origin xyz="0 0 0.1"/>
    <axis xyz="0 1 0"/>
    <limit lower="0" upper="0.35"/>
  </joint>

  <joint name="spin" type="continuous">
    <parent link="door"/><child link="knob"/>
    <origin xyz="0.28 0.03 0"/>
    <axis xyz="0 1 0"/>
  </joint>
</robot>
"""


def _write(tmp_path, text, name="robot.urdf"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestTheManifestLooksLikeAnyOther:
    """Downstream code must not be able to tell the formats apart."""

    def test_links_and_joints_become_parts_and_joints(self, tmp_path):
        manifest = read_urdf_manifest(_write(tmp_path, CABINET))

        assert manifest.source_format == "urdf"
        assert manifest.asset_name == "Cabinet"
        assert len(manifest.parts) == 4
        assert len(manifest.joints) == 3

    def test_the_undriven_link_is_the_root(self, tmp_path):
        manifest = read_urdf_manifest(_write(tmp_path, CABINET))

        assert manifest.root_part == "base"
        assert [p.name for p in manifest.parts if p.is_root] == ["base"]

    def test_inertial_mass_is_carried_across_with_its_authorship(self, tmp_path):
        manifest = read_urdf_manifest(_write(tmp_path, CABINET))

        base = manifest.part_by_id("base")
        knob = manifest.part_by_id("knob")

        assert (base.mass.mass_kg, base.mass.mass_authored) == (20.0, True)
        # A link with no <inertial> has not stated a mass, and that is
        # different from stating zero.
        assert knob.mass.mass_kg is None
        assert not knob.mass.mass_authored


class TestGeometryIsResolvedToWorld:
    """URDF states poses per-parent; the viewer needs them in world space."""

    def test_a_nested_joint_anchor_is_composed_down_the_chain(self, tmp_path):
        # 'spin' hangs off 'door', which hangs off 'base', so its world anchor
        # is the sum of both origins -- not the 0.28 it states locally.
        manifest = read_urdf_manifest(_write(tmp_path, CABINET))

        spin = next(j for j in manifest.joints if j.name == "spin")

        assert spin.anchor_world == pytest.approx([-0.02, 0.23, 0.4])

    def test_a_rotated_parent_turns_the_child_axis(self, tmp_path):
        # A yaw of 90 degrees on the parent joint must swing the child's axis
        # from +X to +Y, or every gizmo below it points the wrong way.
        urdf = """<?xml version="1.0"?>
        <robot name="Arm">
          <link name="base"/><link name="a"/><link name="b"/>
          <joint name="yaw" type="revolute">
            <parent link="base"/><child link="a"/>
            <origin xyz="0 0 0" rpy="0 0 1.5707963"/>
            <axis xyz="0 0 1"/><limit lower="0" upper="1"/>
          </joint>
          <joint name="tip" type="revolute">
            <parent link="a"/><child link="b"/>
            <origin xyz="1 0 0"/>
            <axis xyz="1 0 0"/><limit lower="0" upper="1"/>
          </joint>
        </robot>
        """
        manifest = read_urdf_manifest(_write(tmp_path, urdf))

        tip = next(j for j in manifest.joints if j.name == "tip")

        assert tip.axis_world == pytest.approx([0.0, 1.0, 0.0], abs=1e-6)
        assert tip.anchor_world == pytest.approx([0.0, 1.0, 0.0], abs=1e-6)


class TestUnitsAndLimits:
    """URDF is already SI, which is a fact worth recording rather than hiding."""

    def test_revolute_limits_are_radians_and_need_no_conversion(self, tmp_path):
        manifest = read_urdf_manifest(_write(tmp_path, CABINET))

        hinge = next(j for j in manifest.joints if j.name == "hinge")

        assert hinge.type is JointType.REVOLUTE
        assert hinge.limits.raw_unit is RawUnit.RADIAN
        assert hinge.limits.upper == pytest.approx(math.pi / 2, abs=1e-4)
        assert hinge.limits.upper_raw == hinge.limits.upper

    def test_a_continuous_joint_is_revolute_with_no_authored_stops(self, tmp_path):
        # Unbounded is a materially different asset from one limited to 90
        # degrees, so no range may be invented for it.
        manifest = read_urdf_manifest(_write(tmp_path, CABINET))

        spin = next(j for j in manifest.joints if j.name == "spin")

        assert spin.type is JointType.REVOLUTE
        assert not spin.limits.authored
        assert spin.limits.lower is None and spin.limits.upper is None

    def test_effort_and_damping_are_reported_without_inventing_gains(self, tmp_path):
        # URDF has no stiffness. Reporting one would overstate the file.
        manifest = read_urdf_manifest(_write(tmp_path, CABINET))

        hinge = next(j for j in manifest.joints if j.name == "hinge")

        assert hinge.drive.present
        assert hinge.drive.damping == 0.5
        assert hinge.drive.max_force == 10.0
        assert hinge.drive.stiffness is None


class TestWhatIsNotModelled:
    """Absent and unread must never look the same."""

    def test_a_fixed_joint_is_structure_not_a_missing_joint(self, tmp_path):
        # It welds two links and contributes no degree of freedom, so it
        # belongs in the tree but not in the joint list.
        urdf = """<?xml version="1.0"?>
        <robot name="Welded">
          <link name="base"/><link name="glass"/>
          <joint name="weld" type="fixed">
            <parent link="base"/><child link="glass"/>
            <origin xyz="0 0 1"/>
          </joint>
        </robot>
        """
        manifest = read_urdf_manifest(_write(tmp_path, urdf))

        assert manifest.joints == []
        assert manifest.root_part == "base"
        assert not any("weld" in w for w in manifest.warnings)

    def test_a_floating_joint_is_announced_rather_than_dropped(self, tmp_path):
        urdf = """<?xml version="1.0"?>
        <robot name="Floaty">
          <link name="base"/><link name="payload"/>
          <joint name="drift" type="floating">
            <parent link="base"/><child link="payload"/>
          </joint>
        </robot>
        """
        manifest = read_urdf_manifest(_write(tmp_path, urdf))

        assert manifest.joints == []
        assert any("does not model" in w for w in manifest.warnings)
        assert any("drift" in w for w in manifest.warnings)


class TestRefusals:
    """A file that cannot be read says so."""

    def test_malformed_xml_is_reported_not_raised_as_a_parse_error(self, tmp_path):
        with pytest.raises(UsdSceneError, match="not readable URDF"):
            read_urdf_manifest(_write(tmp_path, "<robot><link"))

    def test_a_file_that_is_not_a_robot_is_refused(self, tmp_path):
        with pytest.raises(UsdSceneError, match="not a URDF"):
            read_urdf_manifest(_write(tmp_path, "<scene><thing/></scene>"))

    def test_a_robot_with_no_links_is_refused(self, tmp_path):
        with pytest.raises(UsdSceneError, match="no links"):
            read_urdf_manifest(_write(tmp_path, '<robot name="Empty"/>'))
