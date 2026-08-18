"""Run NVIDIA's Omni Asset Validator and attach its verdicts to parts.

Authoring the rules is not this project's job -- see ``DIRECTION.md``. The
validator encodes what PhysX actually requires of a USD asset, tracks the
engine as it changes, and is Apache-2.0 with no dependencies. Reimplementing
that would mean maintaining a permanently worse copy.

What the validator does *not* do is say which door is broken. It reports prim
paths against rule names, which serves someone writing rules and does very
little for someone trying to understand an asset. This module closes that gap:
it maps every issue onto the part or joint it concerns, so the viewer can put
the verdict back onto the geometry it is about.

The validator is optional. It is a separate package and a machine without it
should still get a working viewer, so absence is reported as a status rather
than raised.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, computed_field

from .models import JointManifest

# The validator grades issues on its own scale. Anything at or above this is
# something the engine will actually trip over, as opposed to advice.
_BLOCKING_SEVERITIES = frozenset({"FAILURE", "ERROR"})

# The validator is not safe to run from two threads at once: concurrent calls
# deadlock outright, taking the whole service with them. FastAPI runs sync
# endpoints in a threadpool, and the page asks for a verdict and a findings
# report at the same moment on first load -- so on a cold cache this fires
# reliably, not rarely. Serialised here rather than at the endpoints, because
# every caller has the same constraint and only this module knows why.
_ENGINE_LOCK = threading.Lock()


class ValidatorStatus(str, Enum):
    """Whether the validator ran, and what came back.

    ``not_applicable``
        The asset is not USD. The rules check ``UsdPhysics`` and PhysX
        schemas, which a URDF does not have, so nothing ran and nothing
        passed. Distinct from ``clean`` on purpose.
    ``unavailable``
        The package is not installed. Not an error -- it is an optional
        dependency and the viewer works without it.
    ``failed``
        It ran but could not read the asset.
    ``clean``
        It ran and found nothing.
    ``issues``
        It ran and found something.
    """

    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    CLEAN = "clean"
    ISSUES = "issues"


class ValidationIssue(BaseModel):
    """One rule violation, resolved onto the asset where possible.

    Attributes
    ----------
    rule : str
        Validator rule that fired, e.g. ``PhysicsJointChecker``.
    severity : str
        Validator severity, upper case.
    blocking : bool
        Whether this is something the engine will trip over rather than
        advice.
    message : str
        The validator's own wording, kept verbatim so it stays greppable
        against NVIDIA's documentation.
    prim_path : str, optional
        Prim the issue is attached to.
    part_id : str, optional
        Part the prim belongs to, when it could be resolved. This is the key
        the viewer highlights on.
    joint_id : str, optional
        Joint the prim belongs to, when the issue is about one.
    subject : str, optional
        Display name of whatever it resolved to.
    """

    rule: str
    severity: str
    blocking: bool
    message: str
    prim_path: Optional[str] = None
    part_id: Optional[str] = None
    joint_id: Optional[str] = None
    subject: Optional[str] = None


class ValidationReport(BaseModel):
    """Everything the validator said about one asset.

    Attributes
    ----------
    status : ValidatorStatus
        Outcome of the run.
    detail : str
        Plain-language summary, shown when there is nothing to list.
    validator_version : str, optional
        Version that produced this, so a report can be reproduced.
    issues : list of ValidationIssue
        Every issue found, blocking ones first.
    """

    status: ValidatorStatus
    detail: str = ""
    validator_version: Optional[str] = None
    issues: list[ValidationIssue] = Field(default_factory=list)

    @computed_field
    @property
    def blocking_count(self) -> int:
        """Return the number of issues the engine will trip over."""
        return sum(1 for issue in self.issues if issue.blocking)

    @computed_field
    @property
    def advisory_count(self) -> int:
        """Return the number of issues that are advice rather than defects."""
        return len(self.issues) - self.blocking_count

    @computed_field
    @property
    def affected_part_ids(self) -> list[str]:
        """Return the parts carrying at least one blocking issue."""
        seen: dict[str, None] = {}
        for issue in self.issues:
            if issue.blocking and issue.part_id:
                seen[issue.part_id] = None
        return list(seen)

    @computed_field
    @property
    def by_rule(self) -> list[dict]:
        """Summarise issues per rule, blocking rules first.

        A single asset can raise dozens of advisory issues from one rule --
        66 primvar warnings on a cabinet, for instance. Listing them
        individually would bury the five that stop the doors working, so the
        viewer leads with this and expands on demand.
        """
        grouped: dict[str, dict] = {}
        for issue in self.issues:
            entry = grouped.setdefault(
                issue.rule, {"rule": issue.rule, "count": 0, "blocking": False}
            )
            entry["count"] += 1
            entry["blocking"] = entry["blocking"] or issue.blocking
        return sorted(
            grouped.values(), key=lambda e: (not e["blocking"], -e["count"])
        )


@contextmanager
def _quiet_validator_logging():
    """Suppress the validator's plugin-startup chatter for one call.

    It logs five INFO lines about its plugin system on every single run, which
    would swamp the service log once an asset list is being scanned.
    """
    logger = logging.getLogger("omni.asset_validator")
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        yield
    finally:
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def _validator_version() -> Optional[str]:
    """Return the installed validator version, or None if absent."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - stdlib since 3.8
        return None
    try:
        return version("omniverse-asset-validator")
    except PackageNotFoundError:
        return None


def _resolve_subject(
    prim_path: Optional[str], manifest: JointManifest
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Attribute a prim path to the part and joint it belongs to.

    A joint prim lives somewhere under, beside, or entirely away from the part
    it drives -- USD keeps the namespace hierarchy and the kinematic graph as
    two independent structures. So resolution goes by longest matching prefix
    over both parts and joints, and whichever match is deeper wins.

    Parameters
    ----------
    prim_path : str, optional
        Path reported by the validator.
    manifest : JointManifest
        Manifest to resolve against.

    Returns
    -------
    tuple
        ``(part_id, joint_id, subject_name)``, any of which may be None when
        the path belongs to neither -- a material or a stage-level issue.
    """
    if not prim_path:
        return None, None, None

    matched_joint = None
    best = -1
    for joint in manifest.joints:
        if prim_path == joint.prim_path or prim_path.startswith(
            joint.prim_path + "/"
        ):
            if len(joint.prim_path) > best:
                best, matched_joint = len(joint.prim_path), joint

    part_id = part_name = None
    best = -1
    for part in manifest.parts:
        if prim_path == part.id or prim_path.startswith(part.id + "/"):
            if len(part.id) > best:
                best, part_id, part_name = len(part.id), part.id, part.name

    if matched_joint is None:
        return part_id, None, part_name

    # A joint issue is reported against the joint's child part, because that
    # is the piece a person looks at when told a hinge is wrong.
    child = (
        manifest.part_by_id(matched_joint.child_part)
        if matched_joint.child_part
        else None
    )
    if child is not None:
        part_id, part_name = child.id, child.name

    return part_id, matched_joint.id, matched_joint.name or part_name


def validate_asset(usd_path: Path, manifest: JointManifest) -> ValidationReport:
    """Run every validator rule over an asset and resolve the results.

    All categories are run, not just Physics. A broken material reference or a
    missing texture is just as much a reason an asset misbehaves downstream,
    and filtering them out here would hide them for no gain.

    Parameters
    ----------
    usd_path : pathlib.Path
        Asset to validate.
    manifest : JointManifest
        Manifest of the same asset, used to attribute issues to parts.

    Returns
    -------
    ValidationReport
        What the validator said, with issues mapped onto the asset. Never
        raises: a validator that cannot run is reported, not thrown.
    """
    if manifest.source_format != "usd":
        return ValidationReport(
            status=ValidatorStatus.NOT_APPLICABLE,
            detail=(
                f"Engine-correctness rules read UsdPhysics, which a "
                f"{manifest.source_format.upper()} file does not have. Nothing "
                f"was checked — this asset has not passed anything."
            ),
        )

    try:
        # Imported under the quiet guard because the validator announces its
        # plugin system over five INFO lines the first time it is loaded.
        with _quiet_validator_logging():
            from omni.asset_validator import ValidationEngine
    except ImportError:
        return ValidationReport(
            status=ValidatorStatus.UNAVAILABLE,
            detail=(
                "Omni Asset Validator is not installed, so engine-correctness "
                "rules were not checked. Install 'omniverse-asset-validator' "
                "to enable them."
            ),
        )

    version = _validator_version()
    try:
        with _ENGINE_LOCK, _quiet_validator_logging():
            results = ValidationEngine().validate(str(usd_path))
    except Exception as error:  # noqa: BLE001 - third party, unknown failures
        return ValidationReport(
            status=ValidatorStatus.FAILED,
            detail=f"Omni Asset Validator could not read this asset: {error}",
            validator_version=version,
        )

    issues = [
        _to_issue(raw, manifest) for raw in _iter_raw_issues(results)
    ]
    issues.sort(key=lambda issue: (not issue.blocking, issue.rule))

    if not issues:
        return ValidationReport(
            status=ValidatorStatus.CLEAN,
            detail="Omni Asset Validator found no issues.",
            validator_version=version,
        )

    return ValidationReport(
        status=ValidatorStatus.ISSUES,
        detail=f"Omni Asset Validator raised {len(issues)} issue(s).",
        validator_version=version,
        issues=issues,
    )


def _iter_raw_issues(results) -> list:
    """Return the issue objects from a validator result.

    ``Results.issues`` is a property in 1.18, but it has been a method in
    other releases. Accepting both costs one line and stops a validator
    upgrade from taking the viewer down with it.
    """
    candidate = getattr(results, "issues", None)
    if candidate is None:
        return []
    return list(candidate() if callable(candidate) else candidate)


def _to_issue(raw, manifest: JointManifest) -> ValidationIssue:
    """Convert one validator issue into our own shape.

    Parameters
    ----------
    raw : object
        Issue as returned by the validator.
    manifest : JointManifest
        Manifest to attribute the issue against.

    Returns
    -------
    ValidationIssue
        Normalised issue with part and joint attribution filled in.
    """
    severity = str(getattr(raw, "severity", "") or "").rsplit(".", 1)[-1].upper()
    rule = getattr(raw, "rule", None)
    rule_name = getattr(rule, "__name__", None) or str(rule or "unknown")

    at = getattr(raw, "at", None)
    prim_path = None
    if at is not None:
        path = getattr(at, "path", None) or getattr(at, "GetPath", None)
        if callable(path):
            path = path()
        prim_path = str(path) if path else None

    part_id, joint_id, subject = _resolve_subject(prim_path, manifest)

    return ValidationIssue(
        rule=rule_name,
        severity=severity or "UNKNOWN",
        blocking=severity in _BLOCKING_SEVERITIES,
        message=str(getattr(raw, "message", "") or ""),
        prim_path=prim_path,
        part_id=part_id,
        joint_id=joint_id,
        subject=subject,
    )
