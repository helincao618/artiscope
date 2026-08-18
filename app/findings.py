"""One vocabulary for everything this tool has to say about an asset.

Six independent checks produce statements about a delivery: NVIDIA's validator,
the manifest inventory, the reference portability check, the stage's own
declarations, the reader, and the GLB exporter. Each grew its own shape and its
own place on screen, and the result read as six tools stapled together -- the
same defect could appear twice under two different words, and a hard failure
could sit in a quieter section than a descriptive note.

This module is the normalisation layer. Every check keeps producing what it
produces; here each result is expressed as a :class:`Finding` carrying three
independent facts about itself:

``modality``
    What kind of statement this is. This is the axis that decides weight and
    wording, and it is deliberately *not* provenance: an NVIDIA advisory is
    lighter than a contradiction we found ourselves, and sorting by who spoke
    would have put them the other way round.
``scope``
    What the statement is about -- the whole stage, one part, or one joint.
    This decides where it lands and whether it can be clicked onto.
``source``
    Who says so. Attribution and nothing more. It belongs in a corner of the
    card, not in the structure of the page.

The reader's ``manifest.warnings`` is deliberately *not* a source here. It is a
parsing log written for whoever is debugging the reader, it mixes statements
about the asset with statements about this tool, and three of its entries
restate a finding the inventory already produces properly. Findings are
purpose-written; a log is a log.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, computed_field

from .inventory import AssetInventory, Finding as InventoryFinding
from .models import JointManifest, JointType
from .portability import PortabilityReport, mentions_engine_module
from .validation import ValidationReport, ValidatorStatus

# Substring the reader writes into the warning for a joint type it cannot
# model. Matching on text is a coupling worth naming: the readers emit free
# prose on purpose, and the two call sites this recognises are the only ones
# that describe *this tool's* limits rather than the asset's contents.
_UNREAD_MARKER = "which this reader does not model"
_INSTANCED_MARKER = "are USD instances"


class Scope(str, Enum):
    """What a finding is about.

    ``stage``
        The delivery as a whole: its up axis, its references, the shape of its
        kinematic tree. Nothing to click onto.
    ``part``
        One rigid piece.
    ``joint``
        One degree of freedom, i.e. an edge between two parts rather than a
        thing in the scene. Carries the child part as well, because that is
        the piece a person looks at when told a hinge is wrong.
    """

    STAGE = "stage"
    PART = "part"
    JOINT = "joint"


class Modality(str, Enum):
    """What kind of statement a finding is, in descending weight.

    ``rejects``
        A physics engine will refuse this. External, binary, actionable.
    ``contradicts``
        The file contradicts itself -- an inverted range, a closed loop, a
        reference to a path that is not there. No agreed bar is needed to call
        these wrong, which is what separates them from everything below.
    ``advises``
        Authored and workable, but outside what is ordinarily seen. Worth a
        look, not a verdict.
    ``omits``
        The file does not state it, so a consumer supplies or guesses it.
        Frequently legitimate: a free hinge has no drive by design.
    ``states``
        The file states it. The dull majority, and the baseline the four above
        are read against.
    ``limits``
        This tool could not do something -- a joint type it does not model, a
        mesh it decimated, a check that did not run. **Not a statement about
        the asset at all**, which is why it is last and why it is kept out of
        every count that grades the delivery.
    """

    REJECTS = "rejects"
    CONTRADICTS = "contradicts"
    ADVISES = "advises"
    OMITS = "omits"
    STATES = "states"
    LIMITS = "limits"


# Sort order for the whole report. Explicit rather than relying on enum
# declaration order, because the ordering is a product decision.
_MODALITY_ORDER = {
    Modality.REJECTS: 0,
    Modality.CONTRADICTS: 1,
    Modality.ADVISES: 2,
    Modality.OMITS: 3,
    Modality.STATES: 4,
    Modality.LIMITS: 5,
}

# Modalities that say something is wrong with the delivery. ``limits`` is
# excluded on purpose: an asset is not worse because this reader is narrower.
_FAULT_MODALITIES = frozenset({Modality.REJECTS, Modality.CONTRADICTS})

# Checks that overlap by construction, as ``our finding id -> NVIDIA rule``.
# Both read the same property of the same joint and fire together, so keeping
# both states one defect twice and doubles the fault count for it. The
# engine's verdict is the one kept: it is the heavier modality, and it is the
# one a supplier cannot answer with "it works in our pipeline".
_SUBSUMED_BY_RULE = {"attachment.child_target": "PhysicsJointChecker"}


class Source(str, Enum):
    """Who produced a finding.

    Attribution only. It earns a line of small print on the card and no place
    in the structure of the report -- see the module docstring.
    """

    NVIDIA = "nvidia"
    MANIFEST = "manifest"
    REFERENCES = "references"
    READER = "reader"
    MESH = "mesh"
    PATCH = "patch"


class Finding(BaseModel):
    """One thing worth saying about an asset.

    Attributes
    ----------
    id : str
        Stable identifier for the *kind* of finding, so repeats of the same
        check across five doors collapse into one entry, and so two deliveries
        of the same asset can be diffed.
    modality : Modality
        What kind of statement this is. Decides weight and section.
    scope : Scope
        What it is about.
    source : Source
        Who says so.
    dimension : str
        Heading this groups under, as a noun phrase. For NVIDIA findings this
        is the rule name, kept verbatim so it stays greppable against their
        documentation.
    detail : str
        What was actually found, in plain language.
    subject : str, optional
        Display name of the part or joint concerned.
    part_id : str, optional
        Part this lands on, when there is one. The key the viewer selects on.
    joint_id : str, optional
        Joint this lands on, when there is one.
    rule : str, optional
        NVIDIA rule name, when the finding came from the validator.
    """

    id: str
    modality: Modality
    scope: Scope
    source: Source
    dimension: str
    detail: str
    subject: Optional[str] = None
    part_id: Optional[str] = None
    joint_id: Optional[str] = None
    rule: Optional[str] = None


class EngineCheck(BaseModel):
    """Whether the engine-correctness rules ran at all.

    Carried beside the findings rather than inside them because "no rule
    fired" and "no rule ran" are different answers that look identical in an
    empty list, and the first is the only one that means anything passed.

    Attributes
    ----------
    status : ValidatorStatus
        Outcome of the validator run.
    detail : str
        Plain-language explanation, shown when there is nothing to list.
    validator_version : str, optional
        Version that produced the verdict, so a report can be reproduced.
    """

    status: ValidatorStatus
    detail: str = ""
    validator_version: Optional[str] = None

    @computed_field
    @property
    def ran(self) -> bool:
        """Whether the rules actually executed against this asset."""
        return self.status in (ValidatorStatus.CLEAN, ValidatorStatus.ISSUES)


class FindingReport(BaseModel):
    """Everything the tool has to say about one asset, in one vocabulary.

    Attributes
    ----------
    asset_name : str
        Asset this describes.
    engine : EngineCheck
        Whether the engine rules ran, and what came back.
    findings : list of Finding
        Every finding, heaviest modality first.
    """

    asset_name: str
    engine: EngineCheck
    findings: list[Finding] = Field(default_factory=list)

    @computed_field
    @property
    def counts(self) -> dict[str, int]:
        """Return the number of findings per modality."""
        result = {modality.value: 0 for modality in Modality}
        for finding in self.findings:
            result[finding.modality.value] += 1
        return result

    @computed_field
    @property
    def fault_count(self) -> int:
        """Return how many findings say something is actually wrong.

        Rejections and contradictions only. Advisories are excluded because
        they are survivable by definition, and tool limits because they are
        not the asset's doing.
        """
        return sum(1 for f in self.findings if f.modality in _FAULT_MODALITIES)

    @computed_field
    @property
    def affected_part_ids(self) -> list[str]:
        """Return the parts carrying at least one rejection or contradiction."""
        seen: dict[str, None] = {}
        for finding in self.findings:
            if finding.modality in _FAULT_MODALITIES and finding.part_id:
                seen[finding.part_id] = None
        return list(seen)


def _scope_of(part_id: Optional[str], joint_id: Optional[str]) -> Scope:
    """Return the narrowest scope a subject resolution supports."""
    if joint_id:
        return Scope.JOINT
    if part_id:
        return Scope.PART
    return Scope.STAGE


def from_validation(report: ValidationReport) -> list[Finding]:
    """Express NVIDIA's verdicts as findings.

    A validator that could not run produces a ``limits`` finding rather than
    nothing: an unchecked asset has not passed, and an empty list would read
    as though it had.

    One class of verdict is downgraded: a blocking complaint about an
    engine-supplied MDL module. Run outside Omniverse the validator has no
    MDL search path, so it reports ``OmniPBR.mdl`` as missing even though
    NVIDIA ships it -- a fact about where the check ran, not about the file.
    Nothing else is softened, because an engine verdict is the one thing a
    supplier cannot answer with "it works in our pipeline".

    Parameters
    ----------
    report : ValidationReport
        Result of :func:`app.validation.validate_asset`.

    Returns
    -------
    list of Finding
        One finding per issue, or a single ``limits`` finding when no rule ran.
    """
    if report.status not in (ValidatorStatus.CLEAN, ValidatorStatus.ISSUES):
        return [
            Finding(
                id="engine.not_checked",
                modality=Modality.LIMITS,
                scope=Scope.STAGE,
                source=Source.NVIDIA,
                dimension="Engine rules",
                detail=report.detail,
            )
        ]

    return [
        Finding(
            id=f"nvidia.{issue.rule}",
            modality=(
                Modality.REJECTS
                if issue.blocking and not mentions_engine_module(issue.message)
                else Modality.ADVISES
            ),
            scope=_scope_of(issue.part_id, issue.joint_id),
            source=Source.NVIDIA,
            dimension=issue.rule,
            detail=issue.message,
            subject=issue.subject,
            part_id=issue.part_id,
            joint_id=issue.joint_id,
            rule=issue.rule,
        )
        for issue in report.issues
    ]


# The inventory's descriptive vocabulary maps onto modality without loss.
# ``unusual`` becomes ``advises`` because both mean "authored, works, look at
# it"; ``inconsistent`` becomes ``contradicts`` because both mean the file
# cannot be satisfied as written.
_INVENTORY_MODALITY = {
    InventoryFinding.AUTHORED: Modality.STATES,
    InventoryFinding.ABSENT: Modality.OMITS,
    InventoryFinding.UNUSUAL: Modality.ADVISES,
    InventoryFinding.INCONSISTENT: Modality.CONTRADICTS,
}


def from_inventory(inventory: AssetInventory) -> list[Finding]:
    """Express what the manifest states as findings.

    Parameters
    ----------
    inventory : AssetInventory
        Result of :func:`app.inventory.build_inventory`.

    Returns
    -------
    list of Finding
        One finding per observation.
    """
    return [
        Finding(
            id=observation.id,
            modality=_INVENTORY_MODALITY[observation.finding],
            scope=_scope_of(observation.part_id, observation.joint_id),
            source=Source.MANIFEST,
            dimension=observation.dimension,
            detail=observation.detail,
            subject=observation.subject,
            part_id=observation.part_id,
            joint_id=observation.joint_id,
        )
        for observation in inventory.observations
    ]


def from_portability(report: PortabilityReport) -> list[Finding]:
    """Express the reference check as findings.

    The two failures are different in kind, not degree. A reference that does
    not resolve is a path the file names and does not have, which is a
    contradiction on any disk. A reference that resolves into a sibling
    asset's folder works perfectly here and breaks the moment the folder
    travels alone -- real, conditional, and therefore advice.

    Parameters
    ----------
    report : PortabilityReport
        Result of :func:`app.portability.check_portability`.

    Returns
    -------
    list of Finding
        One finding per broken or escaping reference.
    """
    findings = [
        Finding(
            id="references.broken",
            modality=Modality.CONTRADICTS,
            scope=Scope.STAGE,
            source=Source.REFERENCES,
            dimension="External references",
            detail=f"'{ref.raw_path}' does not resolve on this disk.",
        )
        for ref in report.broken
    ]
    findings += [
        Finding(
            id="references.engine_module",
            modality=Modality.ADVISES,
            scope=Scope.STAGE,
            source=Source.REFERENCES,
            dimension="External references",
            detail=f"'{ref.raw_path}' is a module the renderer supplies, not a "
            f"file this folder carries: it resolves inside Omniverse and Isaac "
            f"and nowhere else.",
        )
        for ref in report.engine_modules
    ]
    findings += [
        Finding(
            id="references.escaping",
            modality=Modality.ADVISES,
            scope=Scope.STAGE,
            source=Source.REFERENCES,
            dimension="External references",
            detail=f"'{ref.raw_path}' resolves, but points outside the asset "
            f"folder: it breaks as soon as this asset is delivered on its own.",
        )
        for ref in report.escaping
    ]
    return findings


def from_stage(manifest: JointManifest) -> list[Finding]:
    """Express what the stage declares about its own conventions.

    Only the up axis, for now. A Y-up asset is internally consistent and
    passes every rule; it simply lands on its side in Isaac and in any other
    consumer that assumes the Z-up convention USD physics is authored
    against. That makes it advice rather than a contradiction, and it makes it
    a finding rather than the viewport banner it used to be alone -- it was
    the one defect that never reached the downloaded report.

    Parameters
    ----------
    manifest : JointManifest
        Manifest to inspect.

    Returns
    -------
    list of Finding
        One finding when the stage is not Z-up, otherwise empty.
    """
    if manifest.stage_up_axis == "Z":
        return []
    return [
        Finding(
            id="stage.up_axis",
            modality=Modality.ADVISES,
            scope=Scope.STAGE,
            source=Source.MANIFEST,
            dimension="Stage orientation",
            detail=f"The stage declares {manifest.stage_up_axis}-up. Isaac and "
            f"USD physics assume Z-up, so this asset arrives lying on its side "
            f"unless the consumer rotates it.",
        )
    ]


def from_reader(manifest: JointManifest) -> list[Finding]:
    """Express what this reader could not do as findings.

    Only the two warnings that are about *this tool* are promoted. The rest of
    ``manifest.warnings`` describes the asset, and anything there worth
    reporting belongs in the inventory as a proper observation rather than as
    a parsing log entry replayed at the reader.

    Parameters
    ----------
    manifest : JointManifest
        Manifest to inspect.

    Returns
    -------
    list of Finding
        One finding per unread joint, plus one for expanded instances.
    """
    findings = [
        Finding(
            id="reader.unread_joint",
            modality=Modality.LIMITS,
            scope=Scope.STAGE,
            source=Source.READER,
            dimension="Joints this reader skipped",
            detail=warning,
        )
        for warning in manifest.warnings
        if _UNREAD_MARKER in warning
    ]
    findings += [
        Finding(
            id="reader.instanced",
            modality=Modality.LIMITS,
            scope=Scope.STAGE,
            source=Source.READER,
            dimension="Instancing",
            detail=warning,
        )
        for warning in manifest.warnings
        if _INSTANCED_MARKER in warning
    ]
    return findings


def from_mesh(warnings: list[str]) -> list[Finding]:
    """Express the gap between the source geometry and the GLB on screen.

    Parameters
    ----------
    warnings : list of str
        Warnings from the GLB export.

    Returns
    -------
    list of Finding
        One finding per warning.
    """
    return [
        Finding(
            id="mesh.export",
            modality=Modality.LIMITS,
            scope=Scope.STAGE,
            source=Source.MESH,
            dimension="Geometry shown",
            detail=warning,
        )
        for warning in warnings
    ]


def _format_range(
    lower: Optional[float], upper: Optional[float], joint_type: JointType
) -> str:
    """Render an SI range in the units the UI shows."""
    if lower is None or upper is None:
        return "unbounded"
    if joint_type is JointType.REVOLUTE:
        return f"{math.degrees(upper - lower):.1f}deg"
    return f"{(upper - lower) * 1000:.1f}mm"


def from_patches(manifest: JointManifest) -> list[Finding]:
    """Report every joint whose range the viewer is driving from a local patch.

    These are ``limits`` findings for the same reason a joint type this reader
    cannot model is: they say something about this tool, not about the asset,
    and so are kept out of every count that grades the delivery. The asset's
    own range is quoted alongside, because the point of surfacing an override
    is that nobody mistakes it for what the file says.

    Parameters
    ----------
    manifest : JointManifest
        Manifest, already annotated by :func:`app.patches.apply_overrides`.

    Returns
    -------
    list of Finding
        One per overridden joint. Empty when patching is off, which is the
        default.
    """
    findings = []
    for joint in manifest.joints:
        override = joint.override_limits
        if override is None:
            continue
        authored = _format_range(joint.limits.lower, joint.limits.upper, joint.type)
        patched = _format_range(override.lower, override.upper, joint.type)
        part = manifest.part_by_id(joint.child_part)
        findings.append(
            Finding(
                id="patch.limits_overridden",
                modality=Modality.LIMITS,
                scope=Scope.JOINT,
                source=Source.PATCH,
                dimension="Local override",
                detail=(
                    f"the viewer is driving {patched}; the file says "
                    f"{authored}. {override.reason}"
                ),
                subject=part.name if part else joint.name,
                part_id=joint.child_part,
                joint_id=joint.id,
            )
        )
    return findings


def _drop_subsumed(findings: list[Finding]) -> list[Finding]:
    """Drop findings an engine verdict on the same joint already covers.

    Matched per joint rather than per asset, so a check of ours still speaks
    on the joints the validator happened not to flag.

    Parameters
    ----------
    findings : list of Finding
        Every finding collected, in any order.

    Returns
    -------
    list of Finding
        The same list without the duplicates listed in ``_SUBSUMED_BY_RULE``.
    """
    covered = {
        (finding.rule, finding.joint_id)
        for finding in findings
        if finding.source is Source.NVIDIA and finding.joint_id
    }
    return [
        finding
        for finding in findings
        if (_SUBSUMED_BY_RULE.get(finding.id), finding.joint_id) not in covered
    ]


def build_report(
    manifest: JointManifest,
    inventory: AssetInventory,
    validation: ValidationReport,
    portability: PortabilityReport,
    mesh_warnings: list[str],
) -> FindingReport:
    """Collect every check into one ordered report.

    Parameters
    ----------
    manifest : JointManifest
        Manifest of the asset.
    inventory : AssetInventory
        What the manifest states.
    validation : ValidationReport
        What NVIDIA's rules said.
    portability : PortabilityReport
        Whether the folder can travel.
    mesh_warnings : list of str
        Warnings from the GLB export.

    Returns
    -------
    FindingReport
        Findings sorted heaviest modality first, then by dimension, so the
        order on screen never depends on which check happened to run first,
        and with the duplicates named in ``_SUBSUMED_BY_RULE`` removed.
    """
    findings = (
        from_validation(validation)
        + from_inventory(inventory)
        + from_portability(portability)
        + from_stage(manifest)
        + from_reader(manifest)
        + from_mesh(mesh_warnings)
        + from_patches(manifest)
    )
    findings = _drop_subsumed(findings)
    findings.sort(key=lambda f: (_MODALITY_ORDER[f.modality], f.dimension))

    return FindingReport(
        asset_name=manifest.asset_name,
        engine=EngineCheck(
            status=validation.status,
            detail=validation.detail,
            validator_version=validation.validator_version,
        ),
        findings=findings,
    )
