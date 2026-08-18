# Known limits

What this tool does not do, stated plainly. A viewer that hides its gaps is
worse than one that has them: **absent and unread must not look identical**, or
the tool manufactures defects that were never in the asset.

Everything here is either announced at runtime — in the manifest warnings, in a
banner over the viewport, or as a finding of its own — or listed below because
it is silent and you should know.

## Joint coverage

Only **revolute** and **prismatic** joints are interactive; they are what gets a
control.

`FixedJoint` is a third case, handled on purpose: no degree of freedom, so never
a control, but the part it names as `body1` is still positioned relative to
`body0` and moves rigidly with it (see `FixedAttachment` in `app/models.py`).

`SphericalJoint`, the generic `D6Joint` and anything else the reader does not
recognise are genuinely unmodelled, and are **announced** — in the manifest
warnings and in a banner — rather than dropped in silence.

Closed loops (real European cabinet hinges are four-bar linkages, not simple
pivots), screw threads, and coupled or mimic joints remain out of scope.

## No collisions, and no interference check

A revolute or prismatic joint has **one** degree of freedom, so honouring its
limits is a clamp on a scalar. Everything this tool is for — is the hinge on the
right edge, is the axis pointing the right way, does the door swing 90° or 270°
— is answered by kinematics alone. See [`DIRECTION.md`](../DIRECTION.md) for why
there is no physics engine.

The cost is real: **this tool does not see collisions.** Drive a dishwasher's
baskets out with its door shut and they slide straight through it.

Note carefully what that does *not* imply. Contact **detection** along a
kinematic sweep is pure geometry — sample the joint range, intersect the moving
part's mesh against the static ones — and needs no engine at all. Only contact
**response**, the forces and friction and stacking, does. So the interference
check is a cheap thing not built yet, not an expensive thing it is right to
skip.

## The viewer shows render geometry, not colliders

A simulator uses the collision meshes, and those are a separate set — one
cabinet carcass in a real delivery carried seventeen convex hulls. A collider
that is offset, oversized, or does not follow its door would look perfectly
fine here.

For a tool aimed at a physics pipeline this is the largest gap. Parts whose
colliders sit apart from the render mesh do get a collision-overlay node in the
GLB, so it is partially addressable today, but there is no visual/collision
toggle yet.

## Pivot placement is described loosely

The only test is whether the pivot falls inside the child part's bounding box,
so a hinge modelled dead-centre in a door goes unremarked. Real hinges sit on an
edge.

## The swept volume is ghosts, not a hull

Seven sampled poses of the part, not the true swept solid. Good enough to see
how much room something needs; not a clearance guarantee.

## Material reading is bounded on purpose

Only `diffuseColor` is followed from `UsdPreviewSurface` through a `NodeGraph`
wrapper to a `UsdUVTexture`. Normal, roughness and metallic maps are read only
as constants, never as textures.

A texture reference that does not resolve on disk falls back to neutral grey
rather than guessing, and that fallback is **named in the mesh-report banner**,
not silent. See `app/usd_materials.py` for the exact resolution rules and every
fallback it takes.

Subdivision surfaces render as their control cage, not the smoothed result.

## URDF is a courtesy, not the deep path

A URDF can be opened, described and driven, but gets **no engine verdict** —
NVIDIA's rules read `UsdPhysics`, and a URDF has none. Rules that did not run
say so as a finding of their own rather than leaving an empty rejection list to
be read as a clean bill.

URDF is parsed in-house (`app/urdf_reader.py`) rather than with `yourdfpy`,
which costs 31 transitive packages including scipy.

## Nothing is ever written back

The tool is strictly read-only on the asset. `tools/patch_joint_bodies.py` can
emit a **patch layer** that sublayers the original, so the delivered file stays
the authority — but it never edits an asset in place, and nothing is exported in
either direction.

---

## What was checked and found already correct

Less usual to write down, but it is the other half of an honest limits page —
these were suspected and turned out to be fine, so nobody needs to re-audit
them:

- **Joint anchor and axis placement** compose the full authored transform
  through USD's own `ComputeLocalToWorldTransform` rather than a hand-rolled
  walk, so pivot and non-uniform-scale composition match USD's own answer.
  Non-unit scale raises its own warning (`has_non_uniform_scale`).
- **Unit conversion for limits** is pinned in `tests/test_usd_reader.py` against
  assets that author degrees on revolute joints and stage units on prismatic
  ones — USD is inconsistent with itself here, and this is the number one bug
  source.
- **The GLB stays in stage space.** No Y-up conversion on export, even though
  glTF conventionally wants one: converting would leave mesh vertices in one
  frame and manifest anchors in another, and every subsequent bug would be a
  frame bug. The viewer applies a single rotation to the whole scene for
  display instead. `tests/test_mesh_export.py` pins this by asserting every
  joint anchor lands inside the bounds of the part it moves.
- **A part decimated past its face budget**, or one that produced no exportable
  geometry, used to be logged server-side only. It now reaches
  `/api/mesh_report/{asset}` and a banner over the viewport.
- **Double-sided materials.** The reference assets are thin single-walled shells
  with outward-only normals, so a single-sided material backface-culls exactly
  the wall that comes into view when a door opens — an oven cavity rendering as
  an empty black box that looks precisely like missing geometry. Every part's
  material is now double-sided.
