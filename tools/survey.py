"""Survey a delivery: which assets are articulated, and how.

Answers the question you have before you have any other question about a drop
of assets -- how many of these actually move, and which ones are interesting
enough to build a reference set from.

Usage
-----
``python -m tools.survey DELIVERY_DIR [--csv OUT.csv]``

Prints one row per USD file, sorted by joint count.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from app.usd_reader import read_manifest


def survey_file(usd_path: Path) -> dict:
    """Read one asset and summarise its articulation.

    Parameters
    ----------
    usd_path : pathlib.Path
        USD file to read.

    Returns
    -------
    dict
        Summary row. ``error`` is set and the counts are zero when the file
        cannot be read.
    """
    row = {
        "asset": usd_path.stem,
        "path": str(usd_path),
        "size_mb": round(usd_path.stat().st_size / 1e6, 1),
        "parts": 0,
        "joints": 0,
        "revolute": 0,
        "prismatic": 0,
        "driven": 0,
        "unbounded": 0,
        "no_mass": 0,
        "error": "",
    }

    # Broad on purpose: a survey exists to tell you which files are broken, so
    # one unreadable asset must not take the other forty-six with it.
    try:
        manifest = read_manifest(usd_path)
    except Exception as error:  # noqa: BLE001
        row["error"] = str(error)[:120]
        return row

    types = Counter(joint.type.value for joint in manifest.joints)
    row.update(
        parts=len(manifest.parts),
        joints=len(manifest.joints),
        revolute=types.get("revolute", 0),
        prismatic=types.get("prismatic", 0),
        driven=sum(1 for j in manifest.joints if j.drive.is_active),
        unbounded=sum(1 for j in manifest.joints if not j.limits.is_bounded),
        no_mass=sum(
            1 for p in manifest.parts if p.is_rigid_body and not p.mass.mass_authored
        ),
    )
    return row


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("delivery", type=Path)
    parser.add_argument("--csv", type=Path, default=None)
    args = parser.parse_args()

    files = sorted(
        p
        for p in args.delivery.rglob("*.usd*")
        if p.is_file() and p.suffix.lower() in {".usd", ".usda", ".usdc"}
    )
    if not files:
        sys.exit(f"no USD files under {args.delivery}")

    rows = [survey_file(path) for path in files]
    rows.sort(key=lambda r: (-r["joints"], -r["parts"], r["asset"]))

    header = f"{'asset':38} {'parts':>5} {'joints':>6} {'rev':>4} {'pri':>4} {'drv':>4} {'MB':>7}"
    print(header)
    print("-" * len(header))
    for row in rows:
        note = "  ERROR" if row["error"] else ""
        print(
            f"{row['asset'][:38]:38} {row['parts']:5} {row['joints']:6} "
            f"{row['revolute']:4} {row['prismatic']:4} {row['driven']:4} "
            f"{row['size_mb']:7}{note}"
        )

    articulated = [r for r in rows if r["joints"] > 0]
    print(
        f"\n{len(articulated)} of {len(rows)} files are articulated; "
        f"{sum(r['joints'] for r in rows)} joints total"
    )

    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {args.csv}")


if __name__ == "__main__":
    main()
