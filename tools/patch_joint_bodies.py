"""Emit a USD patch layer that points mis-wired joint bodies at their rigid body.

The defect this addresses: a joint's ``physics:body0`` or ``physics:body1``
names a prim one level inside the part -- usually the mesh --
rather than the prim carrying ``RigidBodyAPI``. Where neither end of a joint
then names a rigid body, NVIDIA's ``PhysicsJointChecker`` rejects it and PhysX
has no body to drive.

This writes a **patch layer**, never a corrected asset. The layer sublayers the
original, so the delivered file stays the authority and the override is one
readable block of text: what was changed, on which joint, and to what. That is
also what makes it something to hand back to whoever authored the asset.

The retarget is only ever made when the named prim lacks ``RigidBodyAPI`` and an
ancestor of it has one, which is the case where the author's intent is not in
doubt. A rel naming static geometry is left alone: attaching a hinge to a
non-rigid frame is how a door is pinned to the world, not a mistake.

Usage
-----
``python -m tools.patch_joint_bodies DELIVERY_DIR OUT_DIR [--verify]``
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import NamedTuple, Optional

from pxr import Usd, UsdGeom, UsdPhysics

_BODY_RELS = ("physics:body0", "physics:body1")


class Retarget(NamedTuple):
    """One relationship the patch layer rewrites.

    Attributes
    ----------
    joint_path : str
        Prim path of the joint being corrected.
    rel_name : str
        Which relationship, ``physics:body0`` or ``physics:body1``.
    old_target, new_target : str
        Prim path as authored, and the rigid body it is moved onto.
    """

    joint_path: str
    rel_name: str
    old_target: str
    new_target: str


def _rigid_ancestor(prim: Usd.Prim) -> Optional[Usd.Prim]:
    """Return the nearest ancestor carrying ``RigidBodyAPI``, if any.

    Parameters
    ----------
    prim : pxr.Usd.Prim
        Prim to search upwards from, exclusive of itself.

    Returns
    -------
    pxr.Usd.Prim or None
        The nearest rigid-body ancestor.
    """
    parent = prim.GetParent()
    while parent and parent.GetPath().pathString != "/":
        if parent.HasAPI(UsdPhysics.RigidBodyAPI):
            return parent
        parent = parent.GetParent()
    return None


def find_retargets(stage: Usd.Stage) -> list[Retarget]:
    """Find every joint relationship naming a prim inside its rigid body.

    Parameters
    ----------
    stage : pxr.Usd.Stage
        Stage to inspect.

    Returns
    -------
    list of Retarget
        One entry per relationship that should move, in traversal order.
    """
    retargets: list[Retarget] = []

    for prim in stage.Traverse():
        if not prim.IsA(UsdPhysics.Joint):
            continue
        for rel_name in _BODY_RELS:
            rel = prim.GetRelationship(rel_name)
            if not rel:
                continue
            targets = rel.GetTargets()
            if not targets:
                continue
            target_prim = stage.GetPrimAtPath(targets[0])
            if not target_prim or target_prim.HasAPI(UsdPhysics.RigidBodyAPI):
                continue
            body = _rigid_ancestor(target_prim)
            if body is None:
                continue
            retargets.append(
                Retarget(
                    joint_path=prim.GetPath().pathString,
                    rel_name=rel_name,
                    old_target=targets[0].pathString,
                    new_target=body.GetPath().pathString,
                )
            )

    return retargets


def _nested_overs(retargets: list[Retarget]) -> str:
    """Render the retargets as nested ``over`` blocks.

    Parameters
    ----------
    retargets : list of Retarget
        Relationships to rewrite, any order.

    Returns
    -------
    str
        The body of the patch layer.
    """
    by_joint: dict[str, list[Retarget]] = {}
    for item in retargets:
        by_joint.setdefault(item.joint_path, []).append(item)

    # A trie over prim paths, so sibling joints under one part share their
    # enclosing `over` instead of each reopening it.
    tree: dict = {}
    for joint_path, items in by_joint.items():
        node = tree
        for name in joint_path.strip("/").split("/"):
            node = node.setdefault(name, {})
        node["__edits__"] = items

    def render(node: dict, depth: int) -> list[str]:
        pad = "    " * depth
        lines: list[str] = []
        for name, child in node.items():
            if name == "__edits__":
                continue
            lines.append(f'{pad}over "{name}"')
            lines.append(f"{pad}{{")
            for edit in child.get("__edits__", []):
                lines.append(
                    f"{pad}    rel {edit.rel_name} = <{edit.new_target}>"
                )
            lines.extend(render(child, depth + 1))
            lines.append(f"{pad}}}")
        return lines

    return "\n".join(render(tree, 0))


def _carried_metadata(stage: Usd.Stage) -> str:
    """Restate the stage metadata the patch layer would otherwise drop.

    Layer metadata does not compose across sublayers -- USD reads it from the
    root layer, which the patch becomes. Left out, ``upAxis`` and
    ``metersPerUnit`` silently fall back to the USD defaults, so a Z-up asset
    in metres is reinterpreted as Y-up in centimetres: lying on its side at a
    hundredth of its size, with nothing raised anywhere.

    Parameters
    ----------
    stage : pxr.Usd.Stage
        Stage being patched.

    Returns
    -------
    str
        Metadata block for the patch layer's header.
    """
    lines = [f'    upAxis = "{UsdGeom.GetStageUpAxis(stage)}"']
    lines.append(f"    metersPerUnit = {UsdGeom.GetStageMetersPerUnit(stage)}")
    lines.append(
        f"    kilogramsPerUnit = {UsdPhysics.GetStageKilogramsPerUnit(stage)}"
    )
    default_prim = stage.GetDefaultPrim()
    if default_prim:
        lines.insert(0, f'    defaultPrim = "{default_prim.GetName()}"')
    return "\n".join(lines)


def write_patch(
    usd_path: Path, out_path: Path, retargets: list[Retarget], stage: Usd.Stage
) -> None:
    """Write a patch layer that sublayers ``usd_path``.

    Parameters
    ----------
    usd_path : pathlib.Path
        Asset the patch composes over.
    out_path : pathlib.Path
        File to write.
    retargets : list of Retarget
        Relationships to rewrite.
    stage : pxr.Usd.Stage
        Opened source stage, read for the metadata the patch has to restate.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Normalised, not dereferenced: a delivery is often staged as symlinks,
    # and a patch has to name the asset where the recipient keeps it rather
    # than wherever this machine happens to have unpacked the original.
    sublayer = os.path.relpath(
        os.path.abspath(usd_path), os.path.abspath(out_path.parent)
    )
    summary = "\n".join(
        f"#     {item.joint_path}.{item.rel_name}:\n"
        f"#         {item.old_target}\n"
        f"#      -> {item.new_target}"
        for item in retargets
    )
    out_path.write_text(
        f"""#usda 1.0
(
{_carried_metadata(stage)}
    subLayers = [
        @{sublayer}@
    ]
)

# Patch layer, not a corrected asset. The delivered file is sublayered below
# and remains the authority; the only opinions here are the joint body
# relationships listed, each moved off a prim with no RigidBodyAPI and onto
# the rigid body enclosing it.
#
{summary}

{_nested_overs(retargets)}
""",
        encoding="utf-8",
    )


def _verify(usd_path: Path, patch_path: Path) -> str:
    """Re-run the engine rules before and after, and report the joint verdicts.

    Parameters
    ----------
    usd_path, patch_path : pathlib.Path
        Original asset and the patch layer composing over it.

    Returns
    -------
    str
        One line comparing ``PhysicsJointChecker`` counts.
    """
    from app.readers import read_any_manifest
    from app.validation import validate_asset

    def joint_rejections(path: Path) -> int:
        manifest = read_any_manifest(path, asset_name=path.stem)
        report = validate_asset(path, manifest)
        return sum(
            1
            for issue in report.issues
            if issue.rule == "PhysicsJointChecker" and issue.blocking
        )

    return f"PhysicsJointChecker {joint_rejections(usd_path)} -> " f"{joint_rejections(patch_path)}"


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("delivery", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="re-run the engine rules on the patched composition",
    )
    args = parser.parse_args()

    files = sorted(
        p
        for p in args.delivery.rglob("*.usd*")
        if p.is_file() and p.suffix.lower() in {".usd", ".usda", ".usdc"}
    )

    written = 0
    for usd_path in files:
        stage = Usd.Stage.Open(str(usd_path))
        retargets = find_retargets(stage)
        if not retargets:
            continue

        out_path = args.out_dir / f"{usd_path.stem}_bodyfix.usda"
        write_patch(usd_path, out_path, retargets, stage)
        written += 1

        note = f" | {_verify(usd_path, out_path)}" if args.verify else ""
        print(f"{usd_path.stem:24} {len(retargets):3} retargets{note}")

    print(f"\n{written} patch layer(s) in {args.out_dir}")


if __name__ == "__main__":
    main()
