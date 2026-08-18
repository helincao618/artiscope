# Example assets

Five small articulated assets, hand-authored as USDA text. They are what the
service shows you before you point it at anything of your own, and what the test
suite asserts against — so a fresh checkout runs green with nothing to download.

Each one is a few hundred lines of readable text. Open them. Every asset's
stage-level `doc` string says what it is deliberately doing wrong and why.

| Asset | Parts | Joints | Carries |
|---|---|---|---|
| `Cabinet` | 6 | 5 revolute | Every hinge names a `body1` one level too deep; two materials texture from a folder that does not exist; drives applied but inert; four doors swing one way and the fifth the other |
| `Dishwasher` | 5 | 1 revolute, 3 prismatic | The opposite conventions to `Cabinet` — colliders on the render meshes, no mass authored anywhere, a static base with no rigid-body schema; one genuinely driven joint whose target sits outside its own travel |
| `Dishwasher_with_drive` | 5 | 1 revolute, 3 prismatic | Identical to `Dishwasher` but for a door drive that can actually exert force. The control for "does this tool notice a drive at all" |
| `WallOven` | 3 | 1 revolute, 1 fixed | A glass panel welded onto the door with a `FixedJoint`: no degree of freedom, but still structure |
| `Stovetop` | 5 | 4 revolute | Nothing wrong with it. The negative control — engine rules pass with nothing to report |

## Why they are deliberately broken

A tool that reports faults is only useful if a clean bill of health means
something. Four of these five carry a specific, documented defect; the fifth
carries none, and the suite asserts that the tool says so rather than showing an
empty panel.

The defects are not invented, either. Each is a shape that turns up in real
delivered assets, catalogued in
[`docs/usd-articulation-traps.md`](../docs/usd-articulation-traps.md).

Two of them are confirmed independently by NVIDIA's Omni Asset Validator rather
than only by this repo's own reader:

```
Cabinet      PhysicsJointChecker × 5, MissingReferenceChecker × 2, MassChecker × 1
Dishwasher   PhysicsJointChecker × 2
Stovetop     clean
WallOven     clean
```

That overlap is the interesting case, and `Cabinet` exists for it: two checks see
one defect, and the report has to count it once.

## Using your own assets instead

```bash
ARTISCOPE_ASSET_DIR=/path/to/your/delivery \
  .venv/bin/python -m uvicorn app.main:app --port 8099
```

Point it at a directory holding one folder per asset. Keys come from the
containing folder, so overlapping trees make two assets fight over one key.

To run the test suite against a corpus of your own, set
`ARTISCOPE_FIXTURE_DIR`. The structural assertions travel; the ones pinning
specific authored numbers are written against the assets here.
