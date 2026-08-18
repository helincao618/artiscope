"""Local, hand-written range overrides for assets whose limits are unusable.

Some delivered joints carry a range that is arithmetically valid and
practically useless -- a 355 mm drawer authored with 1 mm of travel moves by
less than a pixel, so a reviewer cannot see the mechanism at all. The asset is
what is wrong, and this repo does not fix assets: it never writes to the source
file, and :attr:`~app.models.Joint.limits` keeps reporting exactly what the file
says no matter what is patched here.

What an override changes is only the range the *viewer* drives, so a joint can
be looked at. Three rules keep that from turning the tool into one that lies:

- the authored range stays in the manifest, untouched, beside the override;
- every applied override becomes a visible finding, so it cannot be mistaken
  for something the asset said;
- nothing is loaded unless ``ARTISCOPE_PATCH_DIR`` points somewhere.

Patches generalise to nothing. They are per-asset, per-joint, and correct only
for the delivery they were written against.

Patch file format
-----------------
One JSON file per asset, named for the asset key, e.g. ``Fridge.json``::

    {
      "note": "free-text, why this file exists",
      "joints": {
        "Fridge_Container*": {
          "lower": 0,
          "upper": 100,
          "reason": "authored 1 mm on a 355 mm bin; travel looks like a unit slip"
        }
      }
    }

Keys name the **moving part** -- what the viewer labels the joint with -- and a
trailing ``*`` matches by prefix, so eight identical bins take one entry.

Values are in the units the UI shows: **millimetres** for prismatic joints and
**degrees** for revolute ones. Writing SI here would reintroduce the exact
unit confusion these patches exist to work around.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from .models import JointManifest, JointType, LimitOverride


def _to_si(value: Optional[float], joint_type: JointType) -> Optional[float]:
    """Convert a patch value from display units to SI.

    Parameters
    ----------
    value : float, optional
        Millimetres for a prismatic joint, degrees for a revolute one.
    joint_type : JointType
        Decides which conversion applies.

    Returns
    -------
    float or None
        Metres, radians, or ``None`` when ``value`` was ``None``.
    """
    if value is None:
        return None
    if joint_type is JointType.REVOLUTE:
        return math.radians(value)
    return value / 1000.0


def _matches(pattern: str, part_name: str) -> bool:
    """Return whether ``part_name`` satisfies a patch key.

    A trailing ``*`` matches by prefix; anything else is exact. Deliberately
    not :mod:`fnmatch` -- a patch key that quietly matched more than its author
    intended is the failure mode this whole module has to avoid.
    """
    if pattern.endswith("*"):
        return part_name.startswith(pattern[:-1])
    return part_name == pattern


def load_patch(patch_dir: Optional[Path], asset_key: str) -> dict:
    """Read the patch file for one asset, if there is one.

    Parameters
    ----------
    patch_dir : pathlib.Path, optional
        Directory holding patch files. ``None`` disables patching entirely.
    asset_key : str
        Asset key, matching the JSON file's stem.

    Returns
    -------
    dict
        Parsed patch, or an empty dict when patching is off, no file exists, or
        the file is unreadable. A broken patch must never take the asset down
        with it -- the asset is the point, the patch is a convenience.
    """
    if patch_dir is None:
        return {}

    path = Path(patch_dir) / f"{asset_key}.json"
    if not path.is_file():
        return {}

    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def apply_overrides(manifest: JointManifest, patch: dict) -> list[str]:
    """Attach patch ranges to the joints they name.

    Sets :attr:`~app.models.Joint.override_limits` and leaves
    :attr:`~app.models.Joint.limits` alone.

    Parameters
    ----------
    manifest : JointManifest
        Manifest to annotate, modified in place.
    patch : dict
        Parsed patch as returned by :func:`load_patch`.

    Returns
    -------
    list of str
        One line per patch key that matched nothing. A patch entry silently
        applying to no joint is how a stale patch survives a redelivery
        unnoticed, so the caller is told rather than left to assume it worked.
    """
    entries = patch.get("joints") or {}
    unmatched: list[str] = []

    for pattern, spec in entries.items():
        matched = False
        for joint in manifest.joints:
            part = manifest.part_by_id(joint.child_part)
            name = part.name if part else joint.name
            if not _matches(pattern, name):
                continue
            joint.override_limits = LimitOverride(
                lower=_to_si(spec.get("lower"), joint.type),
                upper=_to_si(spec.get("upper"), joint.type),
                reason=spec.get("reason", "locally overridden"),
            )
            matched = True
        if not matched:
            unmatched.append(pattern)

    return unmatched
