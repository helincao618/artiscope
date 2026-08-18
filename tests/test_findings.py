"""Checks over the one vocabulary every check is expressed in.

The value of ``findings.py`` is entirely in its classification: if a rejection
sorts below a description, or a limit of this reader is counted against the
asset, the module has failed at the only thing it does. These tests pin that
classification against the reference delivery rather than against fabricated
data, because the mistakes being guarded here are ones the real assets made.
"""

from __future__ import annotations

import pytest

from app.config import discover_assets
from app.findings import (
    EngineCheck,
    Finding,
    Modality,
    Scope,
    Source,
    _drop_subsumed,
    build_report,
    from_portability,
    from_inventory,
    from_reader,
    from_stage,
    from_validation,
)
from app.inventory import build_inventory
from app.models import Joint, JointLimits, JointManifest, JointType, Part
from app.portability import AssetReference, PortabilityReport, check_portability
from app.readers import read_any_manifest
from app.validation import ValidationReport, ValidatorStatus, validate_asset
from conftest import ASSET_ROOT


def _report(key: str):
    """Build a full finding report for one reference asset."""
    assets = discover_assets(ASSET_ROOT)
    if key not in assets:
        pytest.skip(f"reference asset {key} not available")
    path = assets[key]
    manifest = read_any_manifest(path, asset_name=key)
    return manifest, build_report(
        manifest=manifest,
        inventory=build_inventory(manifest),
        validation=validate_asset(path, manifest),
        portability=check_portability(path),
        mesh_warnings=[],
    )


@pytest.fixture(scope="module")
def cabinet():
    """The delivery that motivated the whole taxonomy."""
    return _report("Cabinet")


@pytest.fixture(scope="module")
def stovetop():
    """The asset that passes NVIDIA's rules, as the negative control."""
    return _report("Stovetop")


def test_findings_are_ordered_by_weight_not_by_which_check_ran(cabinet):
    """The point of the module.

    Six checks run in whatever order the service calls them. What reaches the
    reader has to be ordered by how much each statement matters, or the
    ordering is an implementation detail leaking onto the screen.
    """
    _, report = cabinet
    order = [f.modality for f in report.findings]
    ranks = [
        [
            Modality.REJECTS,
            Modality.CONTRADICTS,
            Modality.ADVISES,
            Modality.OMITS,
            Modality.STATES,
            Modality.LIMITS,
        ].index(m)
        for m in order
    ]
    assert ranks == sorted(ranks)


def test_the_cabinets_mis_wired_joints_are_rejections_not_descriptions(cabinet):
    """``body1`` pointing at a prim with no rigid body is the reference defect.

    It has to land above anything descriptive, which is exactly what the old
    two-section layout could not express -- it lived in a section captioned
    "not a verdict".
    """
    _, report = cabinet
    rejects = [f for f in report.findings if f.modality is Modality.REJECTS]
    assert any(f.rule == "PhysicsJointChecker" for f in rejects)


def test_one_defect_is_counted_once_even_though_two_checks_see_it(cabinet):
    """NVIDIA and our reader read the same property and fire on the same joint.

    Reporting both states one defect twice and doubles the fault count for
    it, which is the very thing this module exists to stop. The engine's
    verdict is the one that survives -- heavier, and not answerable with "it
    works in our pipeline".
    """
    _, report = cabinet
    rejected_joints = {
        f.joint_id for f in report.findings if f.rule == "PhysicsJointChecker"
    }
    assert rejected_joints

    ours = {
        f.joint_id
        for f in report.findings
        if f.id == "attachment.child_target"
    }
    assert not (ours & rejected_joints)


def test_a_joint_the_validator_missed_is_still_ours_to_report():
    """Suppression is per joint, not per asset.

    Dropping our check wholesale the moment NVIDIA mentions any joint would
    silence it on every other joint in the file.
    """
    covered = Finding(
        id="nvidia.PhysicsJointChecker",
        modality=Modality.REJECTS,
        scope=Scope.JOINT,
        source=Source.NVIDIA,
        dimension="PhysicsJointChecker",
        detail="rejected",
        joint_id="/root/A/Joint",
        rule="PhysicsJointChecker",
    )
    same_joint = Finding(
        id="attachment.child_target",
        modality=Modality.CONTRADICTS,
        scope=Scope.JOINT,
        source=Source.MANIFEST,
        dimension="Body attachment",
        detail="mis-wired",
        joint_id="/root/A/Joint",
    )
    other_joint = same_joint.model_copy(update={"joint_id": "/root/B/Joint"})

    kept = _drop_subsumed([covered, same_joint, other_joint])

    assert covered in kept
    assert same_joint not in kept
    assert other_joint in kept


def test_the_two_reference_failures_are_different_in_kind_not_degree():
    """A path that is not there is wrong anywhere; one that escapes is not.

    NVIDIA is silent on the second by design: its reference checker asks
    whether paths resolve now, not whether they survive the folder travelling
    on its own, which is what "deliver one asset" means.
    """
    report = PortabilityReport(
        asset_folder="/deliveries/Cabinet",
        references=[
            AssetReference(
                raw_path="./texture/albedo.jpg",
                resolves=True,
                inside_asset_folder=True,
            ),
            AssetReference(
                raw_path="../Shelf/texture/albedo.jpg",
                resolves=True,
                inside_asset_folder=False,
            ),
            AssetReference(
                raw_path="./texture/gone.jpg",
                resolves=False,
                inside_asset_folder=False,
            ),
        ],
    )
    by_id = {f.id: f for f in from_portability(report)}

    assert by_id["references.broken"].modality is Modality.CONTRADICTS
    assert by_id["references.escaping"].modality is Modality.ADVISES
    assert all(f.source is Source.REFERENCES for f in by_id.values())
    # The reference that resolves inside the folder is not a finding at all.
    assert len(by_id) == 2


def test_every_finding_about_a_joint_also_names_a_part_to_select(cabinet):
    """A finding that names a door has to be able to reach it.

    Joints are edges rather than objects, so there is nothing in the scene to
    highlight for one. Carrying the child part is what turns the sentence into
    a destination.
    """
    _, report = cabinet
    joint_findings = [f for f in report.findings if f.scope is Scope.JOINT]
    assert joint_findings
    assert all(f.part_id for f in joint_findings)


def _two_part_manifest(joints, name="Thing"):
    """Build a manifest over a fixed set of parts, varying only the joints."""
    return JointManifest(
        asset_name=name,
        source_path=f"/deliveries/{name}/{name}.usd",
        parts=[
            Part(id="/root/BaseA", name="BaseA", node_name="BaseA", is_root=True),
            Part(id="/root/DoorA", name="DoorA", node_name="DoorA"),
            Part(id="/root/BaseB", name="BaseB", node_name="BaseB"),
            Part(id="/root/DoorB", name="DoorB", node_name="DoorB"),
            Part(id="/root/Wall", name="Wall", node_name="Wall"),
        ],
        joints=joints,
        root_part="/root/BaseA",
    )


def _hinge(parent, child):
    """A minimal authored revolute joint between two parts."""
    return Joint(
        id=f"{child}/hinge",
        name="hinge",
        prim_path=f"{child}/hinge",
        type=JointType.REVOLUTE,
        parent_part=parent,
        child_part=child,
        axis_token="Z",
        axis_world=[0.0, 0.0, 1.0],
        anchor_world=[0.0, 0.0, 0.0],
        frame_quat_world=[1.0, 0.0, 0.0, 0.0],
        limits=JointLimits(lower=0.0, upper=1.57, authored=True),
    )


class TestAssemblyVersusAsset:
    """Geometry with no joint means different things in the two cases.

    A part that moves nothing and is moved by nothing is worth flagging in a
    delivered asset — `Sink`'s tap handle reaches this way, on a joint type
    the reader does not model. In an assembled scene it is the room: walls,
    floor, props. Reporting those as unusual said the file was wrong about 65
    things it had authored correctly, and buried the one case that means
    something under them.
    """

    def test_a_scene_states_its_static_geometry_rather_than_warning(self):
        # Two bases, so two articulated things: an assembly.
        manifest = _two_part_manifest(
            [_hinge("/root/BaseA", "/root/DoorA"), _hinge("/root/BaseB", "/root/DoorB")]
        )

        findings = {f.id: f for f in from_inventory(build_inventory(manifest))}

        assert findings["structure.static_part"].modality is Modality.STATES
        assert "structure.orphan_part" not in findings

    def test_a_base_that_is_not_the_base_is_not_an_orphan(self):
        manifest = _two_part_manifest(
            [_hinge("/root/BaseA", "/root/DoorA"), _hinge("/root/BaseB", "/root/DoorB")]
        )

        subjects = {
            f.subject for f in from_inventory(build_inventory(manifest))
            if f.id == "structure.static_part"
        }

        # BaseB holds DoorB up. Only the wall holds nothing.
        assert subjects == {"Wall"}

    def test_a_single_asset_still_warns_about_a_part_on_nothing(self):
        # One base, so one articulated thing: a delivered asset, in which a
        # part attached to nothing is the defect this check exists for.
        manifest = _two_part_manifest([_hinge("/root/BaseA", "/root/DoorA")])

        findings = {f.id: f for f in from_inventory(build_inventory(manifest))}

        assert findings["structure.orphan_part"].modality is Modality.ADVISES
        assert "structure.static_part" not in findings


def test_a_reader_limit_is_about_this_tool_and_not_about_the_asset():
    """An asset is not worse because this reader is narrower than USD.

    Only the two warnings that describe *this reader* are promoted. The rest
    of the parsing log describes the asset, and used to arrive as findings of
    its own alongside the observation that already covered it.
    """
    manifest = JointManifest(
        asset_name="Tap",
        source_path="/deliveries/Tap/Tap.usd",
        warnings=[
            "joint '/root/Tap/handle' is a PhysicsJoint, which this reader "
            "does not model; it is missing from the parts tree below",
            "2 prim(s) are USD instances: /root/Tap. This reader expands them",
            "joint '/root/Tap/spout' has no authored limits; it is free to "
            "travel without a stop",
        ],
    )
    findings = from_reader(manifest)

    assert {f.id for f in findings} == {"reader.unread_joint", "reader.instanced"}
    assert all(f.modality is Modality.LIMITS for f in findings)
    assert all(f.source is Source.READER for f in findings)


def test_the_reader_log_is_not_replayed_as_asset_findings(cabinet):
    """Five mis-wired doors produce five reader warnings and one finding kind.

    Before the split, the same defect arrived twice: once as a proper
    observation and once more as a verbatim reader-log line filed under a
    dimension of its own.
    """
    manifest, report = cabinet
    assert len(manifest.warnings) == 5
    assert from_reader(manifest) == []
    assert not [f for f in report.findings if f.detail in manifest.warnings]


def test_a_clean_asset_still_reports_that_the_rules_ran(stovetop):
    """Nothing found and nothing run look identical in an empty list."""
    _, report = stovetop
    assert report.fault_count == 0
    assert report.engine.ran
    assert not [f for f in report.findings if f.modality is Modality.REJECTS]


@pytest.mark.parametrize(
    "status",
    [
        ValidatorStatus.NOT_APPLICABLE,
        ValidatorStatus.UNAVAILABLE,
        ValidatorStatus.FAILED,
    ],
)
def test_rules_that_did_not_run_produce_a_limit_rather_than_a_pass(status):
    """An empty rejection list reads as a pass, and here that is always wrong.

    A URDF has no ``UsdPhysics`` for the rules to read, and a machine without
    the validator installed checked nothing at all. Both look identical to a
    clean asset unless the absence is itself reported.
    """
    findings = from_validation(
        ValidationReport(status=status, detail="nothing ran")
    )
    assert [f.id for f in findings] == ["engine.not_checked"]
    assert findings[0].modality is Modality.LIMITS
    assert not EngineCheck(status=status).ran


def test_a_stage_that_is_not_z_up_becomes_a_finding_rather_than_only_a_banner():
    """It used to be the one defect that never reached the downloaded report."""
    manifest, _ = _report("Stovetop")
    assert from_stage(manifest) == []

    manifest.stage_up_axis = "Y"
    findings = from_stage(manifest)
    assert len(findings) == 1
    assert findings[0].modality is Modality.ADVISES
    assert findings[0].scope is Scope.STAGE


