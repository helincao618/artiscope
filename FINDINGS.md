# How this tool says something is wrong

`DIRECTION.md` settles which half of the problem this repo owns. This settles
the half it does own: once six independent checks have each produced a verdict,
what makes them one report rather than six.

Implemented in `app/findings.py`.

## The decision

**A finding is classified by what kind of statement it is, not by which check
produced it.** Provenance is attribution — it belongs on the card, in small
print. It is not allowed to decide the structure of the page.

Every finding carries three independent facts:

| Field | Answers | Decides |
|---|---|---|
| `modality` | What kind of statement is this? | Its weight, its wording, which section it lands in |
| `scope` | What is it about — the stage, a part, a joint? | Where it lands and whether it can be clicked onto |
| `source` | Who says so? | Nothing structural. One line of small print |

## The modalities

In descending weight. The order is a product decision and is written out
explicitly in `_MODALITY_ORDER` rather than inherited from declaration order.

- **`rejects`** — a physics engine will refuse this. External, binary,
  actionable.
- **`contradicts`** — the file contradicts itself: an inverted range, a closed
  loop, a reference to a path that is not there. No agreed bar is needed to
  call these wrong, which is exactly what separates them from everything below.
- **`advises`** — authored and workable, but outside what is ordinarily seen.
- **`omits`** — the file does not state it, so a consumer supplies or guesses
  it. Frequently legitimate: a free hinge has no drive by design.
- **`states`** — the file states it. The dull majority, and the baseline the
  four above are read against.
- **`limits`** — *this tool* could not do something. Not a statement about the
  asset, which is why it is last and why it is excluded from `fault_count`.

`rejects` and `contradicts` together are what `fault_count` counts and what
marks a part in the viewport. Advisories are survivable by definition and tool
limits are not the asset's doing.

## The UI is coarser than the data, on purpose

Six modalities is the right grain to *reason* with and the wrong grain to
*read*. The dock shows four sections:

| Section | Modalities |
|---|---|
| Errors | `rejects`, `contradicts` |
| Warnings | `advises` |
| Declarations | `omits`, `states` |
| Notes | `limits` |

Error, warning and note is what every compiler, linter and SARIF consumer
already means by those words. A private vocabulary would have to be learnt
before the report could be read, and a six-rung severity ladder is more than
anyone sustains in conversation — the words have to survive being said out
loud to a colleague.

Merging `rejects` with `contradicts` costs nothing: both mean *fix this before
delivering*, `fault_count` already added them together, and which one a given
row is stays legible from its card. The split that does matter is that
declarations are not on the ladder at all. They are not a severity — they are
what the asset says about itself — and ranking them under warnings implied a
bar that section explicitly disclaims.

The six modalities stay in the payload, so export, filtering and any future
API keep the finer grain. Presentation grain and data grain are separate
decisions, and only the first one has to fit in someone's head.

Each section names, declaratively, which fact its cards put in their label
slot. It is a design decision per section, not something derivable from the
findings: two attempts to infer it from whether `source` or `modality` varied
produced a column reading `ADVISES` under "Warnings" and one reading
`MANIFEST` under "Declarations", each of which is the heading again in
different words.

## Why provenance is not the axis

It was, and it produced two specific errors that no amount of restyling could
have fixed.

**It ranked the wrong things above each other.** NVIDIA's 66 `IndexedPrimvar`
advisories on one cabinet sat in the section headed by the engine's authority,
while the mis-wired `body1` this reader found independently sat in a section
captioned "not a verdict — there is no agreed bar yet". That caption is true of
`states` and `omits`. It is false of a joint whose child relationship names a
prim with no rigid-body schema, and the section could not tell the two apart
because it was defined by who spoke rather than by what was said.

**It hid our most valuable check inside someone else's verdict.**
`portability.py` was rendered as a note appended to the NVIDIA panel. That
check exists *because* NVIDIA does not make it: `MissingReferenceChecker` asks
whether paths resolve on this disk, not whether they survive the folder
travelling alone, and 24 of `Cabinet`'s 28 texture references break
on delivery while the validator stays silent. Filing our answer under their
heading credited them with the opposite of what they said.

The trust asymmetry that motivated a provenance split is real, and it survives
in `source`: NVIDIA's rules encode PhysX behaviour and track the engine, ours
do not. But it is a reason to *attribute* a line, not a reason to sort by it.
And it is narrower than it looks — the prim-path-to-part mapping that makes
their output usable is ours (`validation._resolve_subject`), so a wrong subject
on an NVIDIA finding is our bug, not theirs.

## Why scope is three levels, and why joints are the awkward one

`stage`, `part`, `joint`. A joint is an edge between two parts, not an object
in the scene, so there is nothing to highlight when one is at fault. Every
joint-scoped finding therefore also carries the child part — the piece a person
looks at when told a hinge is wrong. `validation._resolve_subject` already made
that choice for NVIDIA's issues; `inventory.py` now makes the same one, which
is what turned the declarations from sentences into destinations.

## What went wrong before, concretely

Worth keeping because each was invisible until the vocabulary forced them into
the same list.

**The reader's parsing log was being replayed as asset findings.**
`manifest.warnings` is written for whoever is debugging the reader. It mixes
statements about the asset with statements about this tool, and three of its
entries — no root found, no authored limits, `body1` naming a raw path —
restate an observation `inventory.py` already produces properly. Forwarding it
wholesale meant a mis-wired door was reported twice under two different words,
and that "this reader does not model that joint type" was filed as something
the *asset* got wrong.

The log stays a log. `findings.py` promotes only the two entries that genuinely
describe this tool, matched on a marker; nothing else reads it to decide what
to show. The marker matching is a real coupling and is named as such in the
module — the alternative was restructuring sixteen call sites across two
readers, and the reader tests that pin exact warning counts are load-bearing.

**NVIDIA's advisories were a dead end.** Only blocking issues were ever
resolved onto parts, so `IndexedPrimvarChecker ×66` was a number with nothing
behind it. Scope now comes from the issue's own resolution regardless of
severity.

**Reading at the wrong granularity manufactured 64 faults**, found against an
assembled 47-asset scene. A part was defined as a direct child of the root
prim. That is true of a delivered asset
and false of a scene, which nests its parts one level deeper under a per-
placement Xform, so every appliance became a single part: 50 found where the
stage carries 156 rigid bodies, and 79 joints collapsed onto 13 child parts.
Each of those joints then reported a `Body attachment` contradiction, because
`resolve_body_prim` had to climb away from the prim `body1` names to reach
anything the reader called a part — 51 of them against prims that do carry a
rigid-body schema, which is to say against correct wiring. The remaining 28
are real, and NVIDIA's `PhysicsJointChecker` independently counts 28. On top of
that, 13 `Articulation` findings called an appliance a closed loop for being
driven by its own five doors' worth of hinges.

The signal that separated tool from asset was available before any of the
analysis: **a joint that reads clean alone and faulty in a scene cannot be the
asset, because the asset has not changed.** Two counts disagreeing about the
same file — parts against rigid bodies — would also have caught it.

`discover_part_prims` now derives the part set from the physics structure: the
shallowest prim that owns visual geometry and holds no rigid body further down.
A rigid body is a leaf body to an engine, so there is nothing smaller inside
one to look for, and nothing anywhere fixes the depth. `part_node_name` became
the path relative to the root for the same reason — a scene repeats a name
across instances (four prims called `Fruit001`), and a colliding GLB
node name attributes one part's geometry to another.

Joining on a name has a second edge, and the first fix landed on it: three.js
strips `[].:/` from every node name it loads, so a path-shaped name matched in
Python and not in the browser, and 157 of 163 parts ended up with no geometry
bound to their pivot — visible as a cabinet moving with the room instead of its
door moving with its hinge. The separator is now a hyphen. Worth keeping
because of where it hid: **a join that both sides of the Python compute
correctly can still be broken by the consumer that actually renders it**, and
no amount of manifest-to-GLB testing in Python can see it.

This is the third face of the failure the joint-coverage work named: unread
looking like absent invents defects, silently corrected looking like correct
hides them, and reading at the wrong grain invents them again. All three are a
parser holding an undeclared assumption about how an asset is shaped.

**One base was assumed, so every other base was called an orphan.**
`structure.orphan_part` fired for each part that no joint drives except
`root_part`, which is right for a delivered asset and wrong for a scene: an
appliance's carcass is a base, and a wall is the room. It produced 65 warnings
on `Room.usd` and buried the one case that means something —
`Sink`'s tap handle, which reaches this state because its joint is a `D6`
the reader does not model.

Two facts now separate the cases, both derived rather than configured. A part
that is body0 of any joint holds something up and is a base, whether or not it
is *the* base. And **more than one base is what distinguishes an assembly from
an asset** — the only signal available without being told which one this file
is. In an assembly, jointless geometry becomes `structure.static_part`, a
`states` declaration; in a single asset the `advises` orphan stands. The check
that matters kept firing, and 65 sentences claiming the file got something
wrong stopped.

**A scale in the transform chain was read as part of the joint's rotation.**
`ExtractRotationQuat()` takes an orthonormal basis off a world matrix, and the
four centimetre assets reach the reader as `0.01 * R` because the scene resolves
their units with a scale op. The extraction then returns a different rotation
entirely — 19 of `Room.usd`'s 79 axes moved, the worst by 89°, which is
how the refrigerator's doors came to hinge about a horizontal axis. It also
produced 27 false `rest_pose.frames_coincide` advisories.

What kept it hidden is worth more than the fix: the same scale is *correct* in
the anchor, where it performs the unit conversion, so every anchor landed inside
the part it moves and the `tests/test_mesh_export.py` check that pins exactly
that passed throughout. The two halves of a joint's pose are independent, as
`Joint` says in so many words — *in the right place* and *moving through the
right range* — and **an anchor that checks out is no evidence about the axis.**
Nothing tested the axis against a second opinion. A delivered asset could not
have supplied one either: all 14 are unchanged by the correction, because on
their own their units come from `metersPerUnit` and never enter a transform.

**The up axis was never in the report.** A Y-up asset lands on its side in
Isaac. It was a banner over the viewport and nothing else: not a finding, not
in any count, not in the downloaded report. It is now `stage.up_axis`, an
`advises` finding — the file is internally consistent and passes every rule, it
just uses a convention its consumers do not.

## Banners promote findings; they never originate them

Three banners sit over the viewport: up axis, unread joints, changed geometry.
Each is also a finding in the report. That is not duplication — it is the rule
that keeps them honest. The report is collapsible and these three change what
is on screen, so they get promoted; and because the finding is the original,
none of them can go missing from the downloaded report the way the up axis did.

Anything that is only ever a banner is a bug.

## Every dimension describes mechanism; none describes purpose

The seven observations `inventory.py` produces — structure, limits, anchors,
attachment, rest pose, mass, drives — all answer one question: *how does this
move?* None answers *what is it for?* An asset can be complete on all seven and
still be unusable by the task that wanted it, because nothing in the file says
where a cup may be set down or which of five hinges is the door.

This is not a gap invented from first principles. Benchmark platforms that
consume assets of exactly this kind carry the layer explicitly, and its shape
is worth recording even though this repo does not implement it:

- **Usable volumes are geometry in the asset**, not coordinates in task code —
  a prim per region declaring the interior of a cabinet, rack level zero of an
  oven, the left basin of a sink.
- **A region is parented to the link it belongs to**, so opening a drawer moves
  its interior region with it. An affordance is a function of joint state, not
  a static annotation. Parenting it to the stationary carcass instead is the
  failure that does not raise anything.
- **Joints carry a functional role beside their kinematic type.** `revolute` and
  `door` are orthogonal facts; a consumer asks whether the door is open, not
  what angle joint three is at. An asset that states only the type forces every
  consumer to maintain its own map of which joint is which.

Measured against the 47-file delivery this repo is tested on: 6424 prims, zero
region prims of any kind. So the answer to *does this asset say what it is for*
is uniformly *it does not* — an `omits` declaration across the whole delivery,
which is precisely the kind of sentence this tool exists to say out loud rather
than leave for a consumer to discover at integration time.

Two constraints belong with the idea if the dimension is ever built, because
both were learnt from an implementation that gets them wrong. A convention
carried by prim *names* has no validation point — a region named `reg_intr`
instead of `reg_int` is silently nobody's region — so the declaration wants a
schema or at least a namespaced attribute. And a region's **rotation has to
survive**: reading position and scale while discarding rotation turns a slanted
rack into an axis-aligned box, which produces placements outside the region and
reports nothing.

The taxonomy above needs no change to absorb this. Modality and source apply
unaltered; only `scope` is awkward again, since a region is a volume attached to
a part rather than a part, a joint or the stage.

## What is left

- Six of the reader's warnings carry real information about the asset and have
  no dedicated observation: non-unit scale on a part, multiple roots, a joint
  with no `body1`, one driving a body outside every known part, one anchored
  outside, an unrecognised axis token. They currently reach nobody. Each wants
  a proper producer in `inventory.py`, at which point the reader's log stops
  being interesting to the UI at all.
- `advises` mixes NVIDIA's advisories with ours at equal weight. That is
  defensible today because both are survivable, but 66 primvar warnings and one
  pivot half a metre from its door do not deserve the same rank, and `source`
  is currently the only thing separating them.
