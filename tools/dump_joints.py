"""Print or export the joint structure of a USD asset.

The terminal counterpart to the web UI, for when a one-line answer beats
opening a browser -- diffing two deliveries of the same asset, checking a
supplier's fix landed, or piping a manifest into something else.

Usage
-----
::

    python -m tools.dump_joints ASSET.usd
    python -m tools.dump_joints ASSET.usd --json manifest.json
    python -m tools.dump_joints ASSETS_DIR --recursive
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.models import Joint, JointManifest, JointType  # noqa: E402
from app.usd_reader import read_manifest  # noqa: E402
from app.usd_scene import UsdSceneError  # noqa: E402

USD_SUFFIXES = (".usd", ".usda", ".usdc")


def format_range(joint: Joint) -> str:
    """Render a joint's travel in the unit a human thinks in.

    Radians are correct for the manifest and useless for reading, so degrees
    and millimetres are used here.

    Parameters
    ----------
    joint : Joint
        Joint to describe.

    Returns
    -------
    str
        Human-readable range, or a note that the joint is unbounded.
    """
    if not joint.limits.is_bounded:
        return "unbounded (no stop)"
    if joint.type is JointType.REVOLUTE:
        lower = math.degrees(joint.limits.lower)
        upper = math.degrees(joint.limits.upper)
        return f"{lower:7.1f}deg .. {upper:6.1f}deg  ({upper - lower:.1f}deg travel)"
    lower_mm = joint.limits.lower * 1000.0
    upper_mm = joint.limits.upper * 1000.0
    return f"{lower_mm:7.1f}mm .. {upper_mm:6.1f}mm  ({upper_mm - lower_mm:.1f}mm travel)"


def format_drive(joint: Joint) -> str:
    """Describe a joint's drive in terms of what it actually does.

    Parameters
    ----------
    joint : Joint
        Joint to describe.

    Returns
    -------
    str
        One-line drive summary.
    """
    drive = joint.drive
    if not drive.present:
        return "no drive (free)"
    if not drive.is_active:
        return "drive applied but inert (stiffness=0, damping=0) -- free"
    return (
        f"driven: stiffness={drive.stiffness:g} damping={drive.damping:g} "
        f"target={drive.target_position:g}"
    )


def print_manifest(manifest: JointManifest) -> None:
    """Write a manifest to stdout as an indented report.

    Parameters
    ----------
    manifest : JointManifest
        Manifest to print.
    """
    print(f"\n{manifest.asset_name}")
    print("=" * max(len(manifest.asset_name), 60))
    print(
        f"  stage: up={manifest.stage_up_axis} "
        f"metersPerUnit={manifest.stage_meters_per_unit:g}"
    )
    print(f"  parts: {len(manifest.parts)}   joints: {len(manifest.joints)}")

    print("\n  parts")
    for part in manifest.parts:
        tags = []
        if part.is_root:
            tags.append("root")
        tags.append("rigid body" if part.is_rigid_body else "static")
        mass = (
            f"{part.mass.mass_kg:g} kg"
            if part.mass.mass_authored
            else "mass not authored"
        )
        print(
            f"    {part.name:36s} {', '.join(tags):18s} "
            f"{mass:20s} {part.visual_face_count:6d} faces"
        )

    print("\n  joints")
    if not manifest.joints:
        print("    (none -- this asset is not articulated)")
    for joint in manifest.joints:
        axis = ", ".join(f"{c:+.3f}" for c in joint.axis_world)
        anchor = ", ".join(f"{c:+.3f}" for c in joint.anchor_world)
        print(f"    {joint.name}  [{joint.type.value}]")
        print(f"      range   {format_range(joint)}")
        print(f"      axis    {joint.axis_token} -> world ({axis})")
        print(f"      anchor  ({anchor}) m")
        print(f"      drive   {format_drive(joint)}")

    if manifest.warnings:
        print("\n  warnings")
        for warning in manifest.warnings:
            print(f"    ! {warning}")
    print()


def collect_usd_paths(target: Path, recursive: bool) -> list[Path]:
    """Resolve a file or directory argument to a list of USD files.

    Parameters
    ----------
    target : pathlib.Path
        File or directory given on the command line.
    recursive : bool
        Whether to descend into subdirectories when ``target`` is a directory.

    Returns
    -------
    list of pathlib.Path
        Sorted USD paths.
    """
    if target.is_file():
        return [target]
    pattern = "**/*" if recursive else "*"
    return sorted(
        path
        for path in target.glob(pattern)
        if path.is_file() and path.suffix.lower() in USD_SUFFIXES
    )


def main(argv: list[str] | None = None) -> int:
    """Run the command-line entry point.

    Parameters
    ----------
    argv : list of str, optional
        Argument vector. Defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Process exit code: 0 on success, 1 if any asset failed to read.
    """
    parser = argparse.ArgumentParser(
        description="Print the joint structure of one or more USD assets."
    )
    parser.add_argument("target", type=Path, help="USD file or directory of them")
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="write the manifest as JSON (a directory when reading several)",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="descend into subdirectories when the target is a directory",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the report; useful with --json",
    )
    args = parser.parse_args(argv)

    if not args.target.exists():
        parser.error(f"no such file or directory: {args.target}")

    paths = collect_usd_paths(args.target, args.recursive)
    if not paths:
        parser.error(f"no USD files found under {args.target}")

    json_is_dir = args.json is not None and len(paths) > 1
    if json_is_dir:
        args.json.mkdir(parents=True, exist_ok=True)

    failures = 0
    for path in paths:
        try:
            manifest = read_manifest(path)
        except UsdSceneError as error:
            print(f"error: {path}: {error}", file=sys.stderr)
            failures += 1
            continue

        if not args.quiet:
            print_manifest(manifest)

        if args.json is not None:
            out = (
                args.json / f"{manifest.asset_name}.json"
                if json_is_dir
                else args.json
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            if not args.quiet:
                print(f"  wrote {out}\n")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
