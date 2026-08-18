"""HTTP service behind artiscope.

Three things are served per asset: the joint manifest, a GLB with one node per
part, and an inventory of what the asset states about itself. The browser
joins the first two on node name and drives the joints itself, so nothing here
needs to know about rendering, and no physics engine is involved anywhere.

GLB export is cached against the source file's modification time: re-exporting
a 40 MB stage on every page load would make the UI feel broken, but a stale
cache after a supplier re-delivers would be worse. The validator's verdict is
cached the same way and for the same reason -- see ``_ensure_validation``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import shutil
import tempfile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .config import Settings, discover_assets, load_settings
from .findings import FindingReport, build_report
from .intake import IntakeError, accept_upload
from .mesh_export import MeshExportError
from .models import JointManifest
from .inventory import AssetInventory, build_inventory
from .portability import PortabilityReport, check_portability
from .patches import apply_overrides, load_patch
from .readers import export_any_glb, read_any_manifest
from .usd_scene import UsdSceneError
from .validation import ValidationReport, validate_asset

logger = logging.getLogger("artiscope")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="artiscope",
    description="See and exercise what an articulated asset is made of.",
    version="1.0",
)

settings: Settings = load_settings()

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _resolve(asset_key: str) -> Path:
    """Look up an asset's USD path by key.

    Parameters
    ----------
    asset_key : str
        Key from ``/api/assets``.

    Returns
    -------
    pathlib.Path
        Path to the USD file.

    Raises
    ------
    fastapi.HTTPException
        404 when the key is unknown. Re-scanning on every request keeps a
        freshly dropped delivery visible without a restart.
    """
    assets = discover_assets(settings.asset_dir)
    path = assets.get(asset_key)
    if path is None:
        raise HTTPException(status_code=404, detail=f"unknown asset: {asset_key}")
    return path


def _load_manifest(asset_key: str) -> JointManifest:
    """Read the manifest for an asset, mapping reader failures to HTTP errors.

    Parameters
    ----------
    asset_key : str
        Asset key.

    Returns
    -------
    JointManifest
        The manifest.

    Raises
    ------
    fastapi.HTTPException
        422 when the file cannot be interpreted as an articulated asset.

    Notes
    -----
    Local range overrides are attached here, at the one point every endpoint
    goes through, so no consumer can accidentally get an unpatched manifest and
    none of them has to know patching exists. What is attached is additive:
    ``Joint.limits`` still reports the file's own numbers.
    """
    path = _resolve(asset_key)
    try:
        manifest = read_any_manifest(path, asset_name=asset_key)
    except UsdSceneError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    apply_overrides(manifest, load_patch(settings.patch_dir, asset_key))
    return manifest


def _validation_cache_path(asset_key: str, usd_path: Path) -> Path:
    """Return where the validator's verdict for one asset is cached.

    Keyed on the source file's modification time, like the GLB cache, so a
    re-delivered asset is re-checked rather than answered from a stale verdict.

    Parameters
    ----------
    asset_key : str
        Asset key.
    usd_path : pathlib.Path
        The asset the verdict is about.

    Returns
    -------
    pathlib.Path
        Cache file path. May not exist.
    """
    stamp = int(usd_path.stat().st_mtime)
    return settings.cache_dir / f"{asset_key}-{stamp}.validation.json"


def _cached_validation(asset_key: str, usd_path: Path) -> ValidationReport | None:
    """Return a previously computed validator verdict, without computing one.

    This is what the picker uses. Running the validator costs a fraction of a
    second on a single appliance but the better part of a minute on an
    assembled room, and the picker asks about every asset in the directory at
    once: computing on demand there made the whole list time out rather than
    just the one heavy entry arrive late.

    Parameters
    ----------
    asset_key : str
        Asset key.
    usd_path : pathlib.Path
        The asset the verdict is about.

    Returns
    -------
    ValidationReport or None
        The cached verdict, or None when none has been computed yet.
    """
    cache_path = _validation_cache_path(asset_key, usd_path)
    if not cache_path.is_file():
        return None
    try:
        return ValidationReport.model_validate_json(cache_path.read_text())
    except (ValidationError, ValueError, OSError):
        # A truncated or half-written cache file says nothing about the asset,
        # so drop it and report the verdict as not yet known.
        cache_path.unlink(missing_ok=True)
        return None


def _ensure_validation(
    asset_key: str, usd_path: Path, manifest: JointManifest
) -> ValidationReport:
    """Return the validator's verdict for one asset, computing it if needed.

    Parameters
    ----------
    asset_key : str
        Asset key.
    usd_path : pathlib.Path
        The asset to check.
    manifest : JointManifest
        Manifest of the same asset, used to attribute issues to parts.

    Returns
    -------
    ValidationReport
        The verdict, freshly computed and cached on a miss.
    """
    cached = _cached_validation(asset_key, usd_path)
    if cached is not None:
        return cached

    report = validate_asset(usd_path, manifest)
    cache_path = _validation_cache_path(asset_key, usd_path)
    for stale in settings.cache_dir.glob(f"{asset_key}-*.validation.json"):
        if stale != cache_path:
            stale.unlink(missing_ok=True)
    cache_path.write_text(report.model_dump_json())
    return report


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Serve the review UI.

    Returns
    -------
    fastapi.responses.HTMLResponse
        The single-page app.
    """
    page = STATIC_DIR / "index.html"
    if not page.is_file():
        raise HTTPException(status_code=500, detail="UI assets are missing")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/api/assets")
def list_assets(include_static: bool = False) -> dict:
    """List assets found in the configured directory.

    Each entry is summarised cheaply enough to build the picker without
    exporting geometry, but a broken asset still appears, carrying its error,
    so it can be seen rather than silently omitted. This tool exists to look
    at joints; a delivery-sized directory is mostly parts that have none, and
    a picker that lists every wall and prop alongside them buries the handful
    worth opening. So by default a readable asset with zero joints is left
    out here -- reading one is still fully supported by every other endpoint,
    this is only about what the picker leads with.

    The validator's verdict is reported only when it is already cached.
    Computing it here is what "cheaply" rules out: it is the one check whose
    cost scales with the asset rather than with the list, and one assembled
    room in the directory was enough to push the whole response past the
    point where the UI gives up. ``validator_status`` is None for anything
    not yet checked, and the UI fills those in afterwards, one at a time.

    Parameters
    ----------
    include_static : bool, optional
        Include readable, zero-joint assets too. Set by the UI whenever a
        specific asset was just asked for by name (an upload, a direct link)
        rather than being browsed for, since a static asset someone dropped
        on purpose is not clutter.

    Returns
    -------
    dict
        ``asset_dir``, whether that directory exists, and a list of asset
        summaries.
    """
    entries = []
    for key, path in discover_assets(settings.asset_dir).items():
        entry: dict = {
            "key": key,
            "path": str(path),
            "size_mb": round(path.stat().st_size / 1e6, 2),
        }
        try:
            manifest = read_any_manifest(path, asset_name=key)
        except UsdSceneError as error:
            entry.update({"error": str(error), "part_count": 0, "joint_count": 0})
        else:
            report = _cached_validation(key, path)
            entry.update(
                {
                    "part_count": len(manifest.parts),
                    "joint_count": len(manifest.joints),
                    "articulated": bool(manifest.joints),
                    "completeness": build_inventory(manifest).completeness,
                    "validator_status": report.status.value if report else None,
                    "blocking_count": report.blocking_count if report else None,
                }
            )
            if not manifest.joints and not include_static:
                continue
        entries.append(entry)

    return {
        "asset_dir": str(settings.asset_dir),
        "asset_dir_exists": settings.asset_dir.is_dir(),
        "assets": entries,
    }


@app.post("/api/assets", status_code=201)
async def upload_asset(file: UploadFile = File(...)) -> dict:
    """Take delivery of an asset dropped onto the page.

    The upload is streamed to a temporary file first so a large delivery never
    has to sit in memory, then handed to the intake rules, which are the only
    thing that decides where bytes may land.

    Parameters
    ----------
    file : fastapi.UploadFile
        A USD file (including ``.usdz``), or a zip of the asset folder.

    Returns
    -------
    dict
        The new asset key and how many USD files arrived with it.

    Raises
    ------
    fastapi.HTTPException
        400 when the upload is not something this viewer can read.
    """
    with tempfile.TemporaryDirectory() as scratch:
        payload = Path(scratch) / "upload"
        with payload.open("wb") as sink:
            shutil.copyfileobj(file.file, sink)

        try:
            result = accept_upload(file.filename or "", payload, settings.asset_dir)
        except IntakeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    logger.info("accepted upload %s -> %s", file.filename, result.directory)
    return {"key": result.key, "usd_count": result.usd_count}


@app.get("/api/manifest/{asset_key}", response_model=JointManifest)
def get_manifest(asset_key: str) -> JointManifest:
    """Return the joint manifest for one asset.

    Parameters
    ----------
    asset_key : str
        Asset key.

    Returns
    -------
    JointManifest
        Parts, joints, limits and drives.
    """
    return _load_manifest(asset_key)


def _ensure_glb(asset_key: str) -> tuple[Path, list[str]]:
    """Export (or reuse the cached) GLB, returning it with its export warnings.

    A part decimated past its face budget, or one that produced no geometry
    at all, used to be logged server-side and nowhere else -- invisible to
    the one person who could tell whether it mattered. Caching the warnings
    alongside the GLB, keyed on the same modification-time stamp, means they
    survive to be served without re-exporting the mesh on every request.

    Parameters
    ----------
    asset_key : str
        Asset key.

    Returns
    -------
    tuple
        ``(glb_path, warnings)``.

    Raises
    ------
    fastapi.HTTPException
        422 when the asset yields no exportable geometry.
    """
    usd_path = _resolve(asset_key)
    stamp = int(usd_path.stat().st_mtime)
    glb_path = settings.cache_dir / f"{asset_key}-{stamp}.glb"
    warnings_path = glb_path.with_suffix(".warnings.json")

    if not glb_path.is_file():
        for stale in settings.cache_dir.glob(f"{asset_key}-*.glb"):
            stale.unlink(missing_ok=True)
        for stale in settings.cache_dir.glob(f"{asset_key}-*.warnings.json"):
            stale.unlink(missing_ok=True)
        try:
            _, warnings = export_any_glb(
                usd_path, glb_path, face_budget=settings.face_budget
            )
        except (MeshExportError, UsdSceneError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        warnings_path.write_text(json.dumps(warnings))
        for warning in warnings:
            logger.warning("%s: %s", asset_key, warning)
        return glb_path, warnings

    if warnings_path.is_file():
        return glb_path, json.loads(warnings_path.read_text())
    return glb_path, []


@app.get("/api/glb/{asset_key}")
def get_glb(asset_key: str) -> FileResponse:
    """Return the per-part GLB for one asset, exporting it if needed.

    Parameters
    ----------
    asset_key : str
        Asset key.

    Returns
    -------
    fastapi.responses.FileResponse
        The GLB.

    Raises
    ------
    fastapi.HTTPException
        422 when the asset yields no exportable geometry.
    """
    glb_path, _ = _ensure_glb(asset_key)
    return FileResponse(
        glb_path, media_type="model/gltf-binary", filename=f"{asset_key}.glb"
    )


@app.get("/api/mesh_report/{asset_key}")
def get_mesh_report(asset_key: str) -> dict:
    """Report anything the GLB could not represent exactly as authored.

    Decimation and dropped geometry change what is on screen without
    touching the manifest, so they cannot live in the inventory -- which
    describes the manifest alone -- and need their own honest accounting.

    Parameters
    ----------
    asset_key : str
        Asset key.

    Returns
    -------
    dict
        ``{"warnings": [...]}``, empty when the GLB matches the source
        exactly.
    """
    _, warnings = _ensure_glb(asset_key)
    return {"warnings": warnings}


@app.get("/api/inventory/{asset_key}", response_model=AssetInventory)
def get_inventory(asset_key: str) -> AssetInventory:
    """Describe what one asset states about itself, dimension by dimension.

    Parameters
    ----------
    asset_key : str
        Asset key.

    Returns
    -------
    AssetInventory
        Observations across every dimension.
    """
    return build_inventory(_load_manifest(asset_key))


@app.get("/api/validation/{asset_key}", response_model=ValidationReport)
def get_validation(asset_key: str) -> ValidationReport:
    """Run NVIDIA's rules over one asset and attach the results to its parts.

    Parameters
    ----------
    asset_key : str
        Asset key.

    Returns
    -------
    ValidationReport
        Issues resolved onto the parts and joints they concern.
    """
    path = _resolve(asset_key)
    return _ensure_validation(asset_key, path, _load_manifest(asset_key))


@app.get("/api/portability/{asset_key}", response_model=PortabilityReport)
def get_portability(asset_key: str) -> PortabilityReport:
    """Report whether the asset folder can travel on its own.

    Parameters
    ----------
    asset_key : str
        Asset key.

    Returns
    -------
    PortabilityReport
        Every external file reference, classified.
    """
    return check_portability(_resolve(asset_key))


@dataclass(frozen=True)
class _AssetChecks:
    """Every check's output for one asset, each computed once.

    Attributes
    ----------
    manifest : JointManifest
        The kinematic tree, with any local overrides attached.
    inventory : AssetInventory
        What the asset states about itself, dimension by dimension.
    validation : ValidationReport
        The validator's verdict.
    portability : PortabilityReport
        Whether the asset folder can travel on its own.
    mesh_warnings : list of str
        Anything the GLB could not represent as authored.
    findings : FindingReport
        The five above, normalised into one vocabulary.
    """

    manifest: JointManifest
    inventory: AssetInventory
    validation: ValidationReport
    portability: PortabilityReport
    mesh_warnings: list[str]
    findings: FindingReport


def _run_checks(asset_key: str) -> _AssetChecks:
    """Run every check over one asset and normalise the results.

    Both the findings view and the combined report need the same underlying
    results, so they are gathered here and handed over together. Recomputing
    them per view meant validating twice per combined report, which on an
    assembled room is a minute of work for an answer already in hand.

    Parameters
    ----------
    asset_key : str
        Asset key.

    Returns
    -------
    _AssetChecks
        Each check's own output, plus the findings built from them.
    """
    path = _resolve(asset_key)
    manifest = _load_manifest(asset_key)
    inventory = build_inventory(manifest)
    validation = _ensure_validation(asset_key, path, manifest)
    portability = check_portability(path)
    _, mesh_warnings = _ensure_glb(asset_key)
    return _AssetChecks(
        manifest=manifest,
        inventory=inventory,
        validation=validation,
        portability=portability,
        mesh_warnings=mesh_warnings,
        findings=build_report(
            manifest=manifest,
            inventory=inventory,
            validation=validation,
            portability=portability,
            mesh_warnings=mesh_warnings,
        ),
    )


@app.get("/api/findings/{asset_key}", response_model=FindingReport)
def get_findings(asset_key: str) -> FindingReport:
    """Return everything the tool has to say about one asset, in one list.

    The six checks behind this answer different questions and used to be read
    through six different shapes. Here each result carries what kind of
    statement it is, what it is about and who says so, which is what lets the
    UI order by weight instead of by which check happened to produce it.

    Parameters
    ----------
    asset_key : str
        Asset key.

    Returns
    -------
    FindingReport
        Findings sorted heaviest modality first.
    """
    return _run_checks(asset_key).findings


@app.get("/api/report/{asset_key}")
def get_combined_report(asset_key: str) -> dict:
    """Return everything known about one asset as a single document.

    The four views answer different questions -- what the asset says, whether
    the engine will accept it, whether it can be moved, and whether what is on
    screen is exactly what was authored -- and a reader comparing deliveries
    wants them side by side rather than in four downloads.

    Parameters
    ----------
    asset_key : str
        Asset key.

    Returns
    -------
    dict
        Findings, manifest, and each check's own output in one payload. The
        findings are what a reader compares deliveries on; the rest is kept so
        a verdict can be traced back to the check that produced it.
    """
    checks = _run_checks(asset_key)
    return {
        "asset_key": asset_key,
        "findings": checks.findings.model_dump(mode="json"),
        "manifest": checks.manifest.model_dump(mode="json"),
        "inventory": checks.inventory.model_dump(mode="json"),
        "validation": checks.validation.model_dump(mode="json"),
        "portability": checks.portability.model_dump(mode="json"),
        "mesh": {"warnings": checks.mesh_warnings},
    }


@app.get("/health")
def health() -> dict:
    """Report service liveness and how many assets are visible.

    ``asset_dir_exists`` is reported separately from the count because a
    misconfigured directory scans as empty, and a zero count on its own reads
    as "this delivery has nothing in it" rather than "nothing is mounted here".

    Returns
    -------
    dict
        Status payload.
    """
    assets = discover_assets(settings.asset_dir)
    return {
        "status": "ok",
        "asset_dir": str(settings.asset_dir),
        "asset_dir_exists": settings.asset_dir.is_dir(),
        "asset_count": len(assets),
    }
