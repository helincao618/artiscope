"""Local range overrides.

What these pin down is not that a patch works -- that part is three lines --
but that a patch cannot become invisible. An override that silently replaced
the authored range, or applied without saying so, would make the tool misreport
a delivery in exactly the situation where someone is trusting it.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.findings import EngineCheck, FindingReport, Modality, Source, from_patches
from app.patches import apply_overrides, load_patch
from app.usd_reader import read_manifest
from app.validation import ValidatorStatus


@pytest.fixture()
def dishwasher(dishwasher_usd: Path):
    """Manifest of the dishwasher, which mixes prismatic and revolute joints."""
    return read_manifest(dishwasher_usd)


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    (tmp_path / f"{name}.json").write_text(json.dumps(payload))
    return tmp_path


def test_no_patch_directory_means_no_overrides(dishwasher):
    """Patching is opt-in; the default configuration loads none."""
    assert load_patch(None, "Dishwasher") == {}
    assert apply_overrides(dishwasher, {}) == []
    assert all(j.override_limits is None for j in dishwasher.joints)


def test_an_override_drives_a_new_range_without_touching_the_authored_one(
    tmp_path, dishwasher
):
    """The two values coexist. This is the whole design in one assertion."""
    patch_dir = _write(
        tmp_path,
        "Dishwasher",
        {
            "joints": {
                "Dishwasher_Container001": {
                    "lower": 0,
                    "upper": 250,
                    "reason": "test",
                }
            }
        },
    )
    apply_overrides(dishwasher, load_patch(patch_dir, "Dishwasher"))

    joint = next(
        j
        for j in dishwasher.joints
        if j.child_part.endswith("Dishwasher_Container001")
    )
    assert joint.override_limits.upper == pytest.approx(0.250)
    assert joint.limits.lower == pytest.approx(-0.300)
    assert joint.limits.upper == pytest.approx(0.0)


def test_a_trailing_star_matches_by_prefix(tmp_path, dishwasher):
    """Eight identical bins should not need eight identical entries."""
    patch_dir = _write(
        tmp_path,
        "Dishwasher",
        {"joints": {"Dishwasher_Container*": {"upper": 250, "reason": "t"}}},
    )
    apply_overrides(dishwasher, load_patch(patch_dir, "Dishwasher"))

    overridden = [j for j in dishwasher.joints if j.override_limits]
    assert len(overridden) == 2
    assert all("Container" in j.child_part for j in overridden)


def test_patch_values_are_read_in_the_units_the_ui_shows(tmp_path, dishwasher):
    """Millimetres for prismatic, degrees for revolute, converted to SI here.

    Writing SI in the patch file would reintroduce the unit confusion these
    overrides exist to work around.
    """
    patch_dir = _write(
        tmp_path,
        "Dishwasher",
        {
            "joints": {
                "Dishwasher_Container001": {"upper": 250, "reason": "t"},
                "Dishwasher_Door001": {"upper": 90, "reason": "t"},
            }
        },
    )
    apply_overrides(dishwasher, load_patch(patch_dir, "Dishwasher"))

    by_suffix = {j.child_part.rsplit("/", 1)[-1]: j for j in dishwasher.joints}
    assert by_suffix["Dishwasher_Container001"].override_limits.upper == (
        pytest.approx(0.250)
    )
    assert by_suffix["Dishwasher_Door001"].override_limits.upper == (
        pytest.approx(math.radians(90))
    )


def test_a_key_matching_nothing_is_reported_rather_than_ignored(tmp_path, dishwasher):
    """A stale patch is how a fixed asset goes on looking broken."""
    patch_dir = _write(
        tmp_path,
        "Dishwasher",
        {"joints": {"Dishwasher_NoSuchPart": {"upper": 1, "reason": "t"}}},
    )
    unmatched = apply_overrides(dishwasher, load_patch(patch_dir, "Dishwasher"))
    assert unmatched == ["Dishwasher_NoSuchPart"]


def test_an_unreadable_patch_does_not_take_the_asset_down(tmp_path):
    """The asset is the point; the patch is a convenience."""
    (tmp_path / "Dishwasher.json").write_text("{not json")
    assert load_patch(tmp_path, "Dishwasher") == {}


def test_every_override_becomes_a_finding_that_quotes_the_authored_range(
    tmp_path, dishwasher
):
    """An override nobody can see is worse than no override."""
    patch_dir = _write(
        tmp_path,
        "Dishwasher",
        {
            "joints": {
                "Dishwasher_Container001": {
                    "lower": 0,
                    "upper": 250,
                    "reason": "authored range is unusable",
                }
            }
        },
    )
    apply_overrides(dishwasher, load_patch(patch_dir, "Dishwasher"))
    findings = from_patches(dishwasher)

    assert len(findings) == 1
    assert findings[0].source is Source.PATCH
    assert "250.0mm" in findings[0].detail
    assert "300.0mm" in findings[0].detail
    assert "authored range is unusable" in findings[0].detail


def test_an_override_never_grades_the_delivery(tmp_path, dishwasher):
    """It is a statement about this tool, so it stays out of ``fault_count``."""
    patch_dir = _write(
        tmp_path,
        "Dishwasher",
        {"joints": {"Dishwasher_Container*": {"upper": 250, "reason": "t"}}},
    )
    apply_overrides(dishwasher, load_patch(patch_dir, "Dishwasher"))
    findings = from_patches(dishwasher)

    assert all(f.modality is Modality.LIMITS for f in findings)
    report = FindingReport(
        asset_name="Dishwasher",
        engine=EngineCheck(status=ValidatorStatus.CLEAN),
        findings=findings,
    )
    assert report.fault_count == 0
