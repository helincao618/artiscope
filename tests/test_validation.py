"""Attributing a validator verdict to the part it is about.

The rules themselves are NVIDIA's and are not retested here. What is ours, and
therefore what these cover, is the mapping from a prim path back onto a part or
joint -- and the promise that a missing validator degrades instead of breaking.
"""

from __future__ import annotations

import threading

import pytest

from app.models import Joint, JointLimits, JointManifest, JointType, Part
from app.usd_reader import read_manifest
from app.validation import (
    ValidationReport,
    ValidatorStatus,
    _resolve_subject,
    _to_issue,
    validate_asset,
)


def _manifest() -> JointManifest:
    """Build a two-part manifest shaped like the cabinets in the delivery.

    The joint prim deliberately sits one level below the part prim, which is
    how the real assets are authored and the reason resolution cannot just
    compare paths for equality.
    """
    return JointManifest(
        asset_name="Cabinet",
        source_path="/tmp/Cabinet.usd",
        generated_at="2026-08-05T00:00:00Z",
        parts=[
            Part(id="/root/Body", name="Body", node_name="Body", is_root=True),
            Part(id="/root/Door001", name="Door001", node_name="Door001"),
        ],
        joints=[
            Joint(
                id="/root/Door001/Door001/RevoluteJoint",
                name="RevoluteJoint",
                prim_path="/root/Door001/Door001/RevoluteJoint",
                type=JointType.REVOLUTE,
                parent_part="/root/Body",
                child_part="/root/Door001",
                axis_token="Z",
                axis_world=[0.0, 0.0, 1.0],
                anchor_world=[0.0, 0.0, 0.0],
                frame_quat_world=[1.0, 0.0, 0.0, 0.0],
                limits=JointLimits(lower=0.0, upper=1.57, authored=True),
            )
        ],
        root_part="/root/Body",
    )


class _FakeIssue:
    """Stand-in shaped like an ``omni.asset_validator`` issue."""

    class _At:
        def __init__(self, path):
            self.path = path

    def __init__(self, path, severity="IssueSeverity.FAILURE", rule=None):
        self.at = self._At(path) if path else None
        self.severity = severity
        self.rule = rule or type("PhysicsJointChecker", (), {})
        self.message = "must point at a prim with an enabled RigidBodyAPI"


class TestResolvingOntoTheAsset:
    """Turning a prim path into something a person can click."""

    def test_a_joint_issue_lands_on_the_part_that_joint_moves(self):
        # Being told a prim path violates a rule is not actionable. Being
        # shown which door will not open is.
        part_id, joint_id, subject = _resolve_subject(
            "/root/Door001/Door001/RevoluteJoint", _manifest()
        )

        assert part_id == "/root/Door001"
        assert joint_id == "/root/Door001/Door001/RevoluteJoint"
        assert subject == "RevoluteJoint"

    def test_a_plain_part_prim_resolves_to_that_part(self):
        part_id, joint_id, subject = _resolve_subject("/root/Door001", _manifest())

        assert (part_id, joint_id, subject) == ("/root/Door001", None, "Door001")

    def test_a_prim_belonging_to_neither_resolves_to_nothing(self):
        # Material and stage-level issues are real and must still be reported,
        # just without a part to pin them to.
        assert _resolve_subject("/root/Looks/Map__38", _manifest()) == (
            None,
            None,
            None,
        )

    def test_an_absent_path_is_not_an_error(self):
        assert _resolve_subject(None, _manifest()) == (None, None, None)

    def test_the_deepest_matching_part_wins(self):
        # '/root/Body' is a prefix of nothing here, but a nested layout would
        # match several parts and the innermost is the right answer.
        manifest = _manifest()
        manifest.parts.append(
            Part(id="/root/Door001/Handle", name="Handle", node_name="Handle")
        )

        part_id, _, _ = _resolve_subject("/root/Door001/Handle/Mesh", manifest)

        assert part_id == "/root/Door001/Handle"


class TestSeverity:
    """Separating what breaks the engine from what is advice."""

    def test_a_failure_is_marked_blocking(self):
        issue = _to_issue(_FakeIssue("/root/Door001"), _manifest())

        assert issue.blocking
        assert issue.severity == "FAILURE"
        assert issue.rule == "PhysicsJointChecker"

    def test_a_warning_is_not_blocking(self):
        # One cabinet raises 66 primvar warnings. Treating those as defects
        # would bury the five that stop the doors working.
        issue = _to_issue(
            _FakeIssue("/root/Door001", severity="IssueSeverity.WARNING"),
            _manifest(),
        )

        assert not issue.blocking


class TestReportSummaries:
    """What the picker and the panel read off a report."""

    def test_issues_are_grouped_per_rule_with_blocking_rules_first(self):
        manifest = _manifest()
        report = ValidationReport(
            status=ValidatorStatus.ISSUES,
            issues=[
                _to_issue(
                    _FakeIssue("/root/Door001", severity="IssueSeverity.WARNING"),
                    manifest,
                ),
                _to_issue(_FakeIssue("/root/Door001"), manifest),
            ],
        )

        assert report.blocking_count == 1
        assert report.advisory_count == 1
        assert report.by_rule[0]["blocking"] is True

    def test_only_blocking_issues_mark_a_part(self):
        manifest = _manifest()
        report = ValidationReport(
            status=ValidatorStatus.ISSUES,
            issues=[
                _to_issue(
                    _FakeIssue("/root/Door001", severity="IssueSeverity.WARNING"),
                    manifest,
                )
            ],
        )

        assert report.affected_part_ids == []


class TestFormatsWithNoRules:
    """URDF gets looked at and driven, never graded."""

    def test_a_urdf_is_not_checked_and_does_not_claim_to_be_clean(self, tmp_path):
        # 'clean' and 'nothing ran' must be distinguishable, or a robot
        # description silently reads as having passed NVIDIA's rules.
        manifest = _manifest()
        manifest.source_format = "urdf"

        report = validate_asset(tmp_path / "robot.urdf", manifest)

        assert report.status is ValidatorStatus.NOT_APPLICABLE
        assert report.status is not ValidatorStatus.CLEAN
        assert report.blocking_count == 0
        assert "has not passed anything" in report.detail


class TestDegradingWithoutTheValidator:
    """The validator is optional and its absence must not break the viewer."""

    def test_a_missing_validator_is_reported_rather_than_raised(
        self, tmp_path, monkeypatch
    ):
        import builtins

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name.startswith("omni.asset_validator"):
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)

        report = validate_asset(tmp_path / "missing.usd", _manifest())

        assert report.status is ValidatorStatus.UNAVAILABLE
        assert "not installed" in report.detail
        assert report.issues == []

    def test_an_unreadable_asset_is_reported_rather_than_raised(self, tmp_path):
        pytest.importorskip("omni.asset_validator")
        broken = tmp_path / "broken.usd"
        broken.write_bytes(b"not a usd file at all")

        report = validate_asset(broken, _manifest())

        assert report.status in (ValidatorStatus.FAILED, ValidatorStatus.ISSUES)


class TestConcurrency:
    """The validator is not safe to run from two threads at once.

    FastAPI runs sync endpoints in a threadpool, and the page asks for a
    verdict and a findings report in the same breath on first load. Before
    ``validate_asset`` serialised the engine, that pair deadlocked on a cold
    cache and took the whole service down with it -- no error, no timeout, no
    log line: the first page load simply never finished.
    """

    def test_concurrent_calls_all_return(self, cabinet_usd):
        pytest.importorskip("omni.asset_validator")
        manifest = read_manifest(cabinet_usd)
        returned: list[ValidationReport] = []
        failed: list[BaseException] = []

        def call() -> None:
            try:
                returned.append(validate_asset(cabinet_usd, manifest))
            except BaseException as error:  # noqa: BLE001 - reported, not raised
                failed.append(error)

        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            # Generous, but finite: the failure being guarded against is a
            # hang, so joining without a timeout would hang the suite too.
            thread.join(timeout=60)

        assert not [t for t in threads if t.is_alive()], "validator deadlocked"
        assert not failed
        assert len(returned) == 4
        assert {r.status for r in returned} == {returned[0].status}
