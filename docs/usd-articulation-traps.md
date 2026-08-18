# Fifteen things USD articulation will do to you

Every entry below cost real time, and none of them is obvious from the USD
spec. Most were found the same way: the tool reported a defect, the defect
turned out to be in the tool, and the asset had been fine all along. That
failure mode — **a reader that cannot say "I did not read this" manufactures
defects that were never in the file** — is the thread running through the whole
list.

Several are reproducible against the assets in [`examples/`](../examples). Where
one is, it is named.

---

## Part one: three graphs, and every bug that came from conflating them

Worth stating first, because the same mistake keeps arriving in a new costume,
and each time it looks like an asset defect.

**The USD scene graph** is the prim hierarchy — `/root/Cabinet/Cabinet_Door001`
— a tree of nested transforms. `discover_part_prims` walks this one to decide
what a part is.

**The kinematic tree** is the graph the joints describe, its edges
`body0 → body1`. It has no required correspondence to the scene graph, and in
practice it routinely does not have one: five doors are *siblings* in the prim
hierarchy and *children of the carcass* in the kinematic tree. It is not always
a tree, either — an assembled scene is a forest, one root per appliance.

**The render graph** is a third. `app/static/app.js` deliberately builds it to
mirror the *kinematic tree*, nesting a pivot per joint, rather than the prim
hierarchy — otherwise dragging a hinge moves the wrong set of parts. The two
are joined by name, on `part_node_name`.

The third graph is not a redundant copy of the first two. three.js has no
notion of a joint, only of a node's parent and its own transform, so there is
no way to *not* build a tree here: whatever a hinge is supposed to carry along
has to already be nested under its pivot before the drag starts. The only
question is which of the other two graphs it copies. A naive GLB export copies
the scene graph, because that is the tree already sitting in the file — and
that is exactly what breaks the moment the kinematic tree disagrees with it.

Three defects in one afternoon, all of them this:

| Symptom | The false assumption |
|---|---|
| A whole appliance reported as one part, its 5 hinges "a closed loop" | kinematic parts sit at a fixed depth in the scene graph |
| A door swinging about the world origin while its cabinet stood still | the kinematic graph is a single tree |
| A cabinet moving with the room, its door not moving at all | the name joining the USD and render graphs survives the loader |

None of the three is visible from a single delivered asset, where all three
graphs happen to line up: parts one level under the root, one base, one name
each. That coincidence is what kept the assumptions invisible for as long as
the only inputs were single assets.

---

## Part two: the traps

### 1. Visual geometry is decided by `purpose`, never by `CollisionAPI`

Two conventions are both common, and both appear *within a single delivery*:

- a render mesh plus a `Collisions/` scope of `purpose=guide` hulls
  (`examples/Cabinet`)
- `CollisionAPI` applied directly to the render meshes, with no separate scope
  at all (`examples/Dishwasher`)

Filtering on the collision schema is the obvious thing to do, and it leaves the
second asset with **zero** geometry.

### 2. A body relationship does not reliably point at a rigid body

`body0` and `body1` sometimes target the part's Xform and sometimes the mesh
nested inside it — both spellings occur inside a single file. Resolution has to
walk up the ancestors until it reaches a known part.

And a static base often carries no rigid-body schema whatsoever, so root
detection cannot lean on `RigidBodyAPI` either.

The asymmetry matters. Climbing past `body0` is routine and not a defect.
Climbing past `body1` is: a physics engine needs *some* body it can actually
move, so a joint whose child relationship names a prim the reader had to climb
away from is a joint the engine cannot drive as authored — whatever the reader
inferred it meant. `examples/Cabinet` ships this defect on all five doors;
NVIDIA's `PhysicsJointChecker` agrees, independently.

### 3. A fixed joint with an empty `body0` pins a body to the world

It names one part, not two. Reading it as an attachment between parts makes the
base look like somebody's child and costs it the root — loudly on an asset with
no other candidate ("the tree has no base at all"), and silently on one where
an unjointed collider quietly takes the title instead of the body. The loud
version costs an afternoon; the quiet one ships.

### 4. A weld is structure, not a missing degree of freedom

`FixedJoint` has no degree of freedom, so it never gets a control — but the
part it names as `body1` is still positioned relative to `body0` and moves
rigidly with it.

Lumping it in with genuinely unreadable joint types, and dropping it, leaves
both the welded part and the body it was welded to looking like unconnected
roots. Whichever comes first in stage-traversal order wins the root, the real
hinge becomes unreachable from it, and the door swings about a fabricated axis
through the origin. See `examples/WallOven`, where a glass panel is welded onto
an oven door.

A warning about a gap is not the same thing as closing it.

### 5. Layer metadata does not compose across sublayers

USD reads `upAxis`, `metersPerUnit` and `defaultPrim` from the **root layer
only**. Anything that sublayers an asset — a patch layer, say — becomes that
root layer, and leaving them unstated composes a Z-up asset in metres as Y-up
in centimetres: on its side at a hundredth of its size, with no error raised
anywhere.

### 6. A world transform is not a rotation, and a joint axis only wants the rotation

`Gf.Matrix4d.ExtractRotationQuat()` reads an orthonormal basis straight off the
matrix. An asset that resolves its centimetres with an
`xformOp:scale:unitsResolve` arrives as `0.01 * R`, and the reading comes back
a *different rotation* — as much as a right angle out.

The anchor is unaffected, because the scale genuinely belongs there and is what
converts the units. So the joint looks correctly placed and hinged on the wrong
axis: a refrigerator's four doors swinging about a horizontal one. In one scene
it moved 19 of 79 axes, at worst by 89°, and manufactured 27 false
`rest_pose.frames_coincide` advisories.

`RemoveScaleShear()` first is the fix. Like the three graph bugs above, it
cannot be seen from a delivered asset: read on its own, the unit conversion
goes through `metersPerUnit` and never enters a transform at all.

### 7. Assets arrive by payload and may disagree about units

One assembled scene brought in 45 of its 63 top-level children as payloads, and
four of the assets declared `metersPerUnit = 0.01` against the others' `1.0`.
The scene corrected that per instance with an `xformOp:scale:unitsResolve`
rather than in the assets, so a sub-asset opened on its own sits at a different
scale from the same asset seen in the room.

It also declared two `metricsAssembler` sublayers that were absent from the
delivery. Harmless, because the correction was baked into the transform — but
it means a missing sublayer here is not evidence of a missing correction.

### 8. Joints are not in a `/Joints` scope

Each `RevoluteJoint` is routinely authored underneath the part it drives. Scan
the whole stage.

### 9. Parts are not at a fixed depth, and their names are not unique

A delivered asset puts them one level under the root. An assembled scene puts
each asset behind a placement Xform and its parts one level deeper again
(`/root/Cabinet_01/Cabinet_Door001`). Reading the root's direct children as the
parts therefore collapses a whole appliance into one body, and every joint
inside it appears to drive that body.

A scene also repeats part names across instances — four prims called
`Fruit001` among them — so a GLB node name has to be the path relative to the
root, not the part's name.

### 10. three.js silently rewrites node names, so the separator cannot be `/`

`PropertyBinding.sanitizeNodeName` strips `[].:/` from every name the loader
sees, to keep its animation-track syntax parseable. A `/` therefore survives
the GLB and **vanishes in the browser**: the manifest asks for a node that is
not there, nothing binds to the pivot, and the part sits in the loaded scene
graph moving with whatever is above it instead of with its own hinge — a whole
cabinet swinging with the room while its door stays put.

It cost the first attempt at scene support, on 157 of a room's 163 parts, and
it is invisible to any test that joins the manifest to the GLB in Python,
because Python is not what does the rewriting. The separator is a hyphen, which
cannot occur in a USD prim name; `tests/test_usd_reader.py` pins the reserved
set.

### 11. `Path.resolve` is the wrong tool for asking whether a folder contains a file

It follows symlinks, and a delivery is routinely staged as links into wherever
it was unpacked — so every texture in one reads as escaping the folder that
plainly holds it. Normalise without dereferencing instead: the question is
which folder carries the file, not which disk the bytes landed on.

### 12. "Every reference resolves" is not "this folder works somewhere else"

Two cabinets in one delivery pointed their textures at a sibling asset's
folder. Inside the full delivery every reference resolved and the validator was
silent. Copy one out on its own and 24 of 28 textures vanish, with no error.

This is why portability is checked separately from validation
(`app/portability.py`), and `examples/Cabinet` ships a small version of the
same defect.

### 13. Traversal skips instance proxies unless you ask

`Usd.PrimRange` and `stage.Traverse()` walk straight past `instanceable`
prims. A part authored as a USD instance produces zero faces and vanishes from
the parts list **with no error at all**. Every traversal needs
`Usd.TraverseInstanceProxies()`.

### 14. Purpose is not visibility

`is_visual_mesh` checking USD `purpose` but not `visibility` means a mesh
switched off with `visibility = "invisible"` still renders as if shown. They
are two independent switches and both have to be consulted.

### 15. `GLTFLoader` shares one material instance across meshes

Every mesh built from a single glTF material gets the *same* `THREE.Material`
object. Paint a selection or an error highlight onto `material.emissive` and
you have painted every part that shares that material.

When all six parts of an asset fall back to one neutral grey in the textured
variant, flagging five doors lights the carcass with them, and the
authored-materials view comes out uniformly pink. Nothing errors, nothing lands
in the DOM, and a suite of 132 tests had nothing to say about it, because the
only evidence is pixels.

**A wrong colour looks like a decision.** Fixed by cloning each mesh's
material; `tests/test_ui.py` now asserts no two parts share one.

---

## The pattern

Nine of the fifteen are the same shape: something was silently dropped, and the
silence was read as information about the asset. A collider filtered out, an
instance skipped, a weld discarded, a node name rewritten, a material shared.
In every case the file was fine and the tool was lying — confidently, with no
error anywhere.

Which is the argument for the one rule this codebase keeps: **anything not
read gets announced.** Absent and unread must not look identical, or the tool
manufactures defects. See [`FINDINGS.md`](../FINDINGS.md) for how that
announcement is structured, and [`DIRECTION.md`](../DIRECTION.md) for why the
correctness rules themselves are NVIDIA's rather than ours.
