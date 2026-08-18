# Where the boundary is, and why

This records one decision: **which half of the problem this repo owns.** It was
settled on 2026-08-05 after discovering that the other half already exists,
and it is written down because the reasoning is easy to lose and expensive to
re-litigate.

## The decision

**Authoring the rules is not ours. Everything from running them to making them
understood is.**

`artiscope` does not implement its own correctness rules for articulated USD.
It consumes NVIDIA's [Omni Asset
Validator](https://docs.omniverse.nvidia.com/kit/docs/asset-validator) and
spends its own effort on the part the validator will never do: putting a
verdict back onto the thing it is about, in 3D, in a sentence a person can act
on.

## What forced it

The validator found a defect this tool had been actively hiding.

`Cabinet` was, on our own reading, the cleanest asset in the
delivery: every joint limited, every mass authored, nothing absent. It was
recommended as the reference for what a good articulated asset looks like.

The validator failed all five of its joints. `RigidBodyAPI` sits on
`/root/Cabinet_Door001`, but the joint's `body1` points one level
deeper, at `/root/Cabinet_Door001/Cabinet_Door001`, which
carries no `RigidBodyAPI`. In a physics engine those doors do not open.
`Cabinet2` fails all ten joints, `Dishwasher` two of four.
`Stovetop012` points straight at its rigid body and passes clean, so this is
not a false positive.

Our reader missed it because `resolve_body_prim` walks up the namespace looking
for a known part, which silently repaired the mis-wiring and rendered a
perfectly working door.

**The lesson is not "we had a bug".** It is that a home-grown rule layer fails
in the worst available direction: it manufactures confidence. A missing tool
leaves you uncertain, which is safe. A tool that silently repairs defects leaves
you certain and wrong, which is how a broken asset gets recommended as the
baseline. Rules are the part of this problem where being 90% right is worse
than not playing.

## Why not write our own rules

- NVIDIA knows what PhysX requires of USD better than we ever will; the rules
  encode engine behaviour, not opinion.
- Those requirements move with engine versions. Self-hosting them is a
  commitment to track them forever, which nobody here has agreed to fund.
- Apache-2.0, zero dependencies, no Kit, no GPU, no EULA. The cost of adopting
  is near zero, so the bar for reimplementing is correspondingly absurd.

## Why the validator alone is not enough

Its output is a list of prim paths and rule violations. That serves someone
writing rules. It does very little for someone trying to *understand an asset*:
after reading those five lines you still do not know which five doors, what is
wrong with them, or what it looks like when they move.

We already hold the missing half — the prim-path-to-part mapping, the 3D
selection, the joint inspector. Feeding the validator's JSON into it is a small
change with a large payoff, and that ratio is the whole argument for doing the
integration rather than telling people to run a CLI.

## What this settles about positioning

Earlier rounds drifted between "acceptance tool", "presentation tool" and
"workbench" because there was no hard line between what we should build and
what we should not. There is one now — and it is a line about *maintenance*,
not about capability:

| The user's question | Answered by | Implementation maintained by |
|---|---|---|
| Is this asset correct for the engine? | `artiscope` | NVIDIA (Omni Asset Validator) |
| What does this asset *say* about itself? | `artiscope` | us (`inventory.py`) |
| What is it, and what does it do when it moves? | `artiscope` | us (reader and viewer) |
| Why does this verdict matter, on which part? | `artiscope` | us (`validation.py`) |

**A capability this repo integrates is a capability this repo has.** A tool is
what it delivers, not the subset of it that was typed here. So the left column
is the honest description of the tool, and nothing about consuming the
validator makes the first row less its own to answer for.

What the right column governs is narrower and purely practical: which
failures we can fix ourselves, what we would have to re-verify if a
dependency moved, and what we would rewrite versus swap. Those are real
operational facts. They are not a statement about scope, and reading them as
one is what produced the earlier drift.

`artiscope` is where a supplier delivery gets understood: it reads what the
file states, shows what it does when it moves, and puts an engine verdict
back onto the part it concerns.

## The name

`artiscope`: `arti` for articulation, `-scope` for the instrument you look
through. Two earlier candidates were dropped because each made a claim the
tool does not — `qa` claims acceptance, and this tool deliberately refuses to
grade; `viewer` claims only half the job, leaving out the verdict. A name
should stop moving once it is no longer wrong, so this is recorded to close
the question rather than to praise the answer.

The test a candidate has to pass, given the boundary above: it must not
promise a verdict as the whole product, must not promise simulation, must not
promise authoring, and must cover both halves — inspecting an asset *and*
moving it.

- `analyzer`, `validator`, `checker`, `qa`, `audit` — name a verdict-only
  product and drop the seeing and driving half, which is most of the daily
  use. (Note this is *not* because the rules are NVIDIA's; see the positioning
  section — the capability is ours to claim either way.)
- `intake`, `triage`, `review`, `gate` — describe the job more precisely than
  `lab` does, and were seriously considered for that reason. Rejected because
  each drags the acceptance framing back in, which is the drift this document
  exists to end.
- `viewer`, `view` — the tool is explicitly distinguished from browser USD
  renderers elsewhere in this file; naming it after them undoes that.
- `studio`, `workbench`, `forge` — over-claim, and `workbench` already
  drifted once.

`lab` survives because it is a place a thing is taken to be examined and
poked at, promising neither judgement nor production. It is vague, but vague
at the right level: the tool genuinely does four things — read, describe,
drive, relay a verdict — and any sharper verb would name one and hide three.

Two things worth knowing rather than re-deriving:

- **`arti` is an established prefix in this field, not a coinage.** It reads
  as invented if you have not met the family, which is the trap: Arti-PG
  (ICCV 2025) introduces itself as "Articulated Object Procedural Generation
  toolbox, a.k.a. Arti-PG"; Artiverse (CVPR 2026) ships 5.4k articulated
  objects in both URDF and USD; ArtVIP ships articulated digital assets in
  USD; ArtiBoost, ArtiGrasp, ArtiLatent and Articraft all take the same
  prefix. The convention is recent — most of it 2025 and later — so it is
  legible immediately to people who work on articulated assets and opaque to
  everyone else. That is the right way round for this audience. `artiscope`
  itself is unclaimed.
- **It collides with Tor's `Arti`**, the Rust rewrite. Worth knowing, not
  worth acting on: the whole family above already coexists with it, because
  nobody searching for one lands in the other's domain.

## USD is the deep path; URDF is a courtesy

This is an articulated **asset** lab, so USD is the format that gets depth. But
the manifest was built format-neutral, so reading a URDF costs little and being
able to open one is plainly useful. The rule is:

| | USD | URDF |
|---|---|---|
| Read, view, drive the joints | yes | yes |
| Describe what the file states | yes | yes |
| Engine-correctness verdict | yes | **no** |
| Portability check | yes | yes |
| Export | never | never |

**The asymmetry is not a gap to close, it is the point.** Omni Asset Validator
checks the `UsdPhysics` and PhysX schemas. A URDF has neither, so no amount of
work on our side produces an engine verdict for one. Depth is only available on
the USD path, which is a reason to stay close to the NVIDIA ecosystem rather
than a limitation of it: an asset authored as USD against PhysX conventions is
one Isaac Sim can consume directly, and one this tool can say something
substantive about.

The UI therefore has to state which tier an asset is in. A URDF that shows no
blocking issues has not passed anything — nothing ran.

**No export, in any direction.** Not URDF out, not USD out. Writing a converter
means owning the semantics of everything it drops, and USD to URDF is lossy in
ways that matter here: closed loops, D6 joints, per-axis drives, variants,
instancing and materials have no URDF equivalent. Producing a file that looks
complete and silently is not would be the same failure this project already got
caught by once.

**A patch layer is not an export, and the difference is worth stating.**
`tools/patch_joint_bodies.py` writes USD: a layer that sublayers the delivered
asset and overrides the joint body relationships NVIDIA rejects. It converts
nothing, so there is nothing to drop — the delivered file composes underneath
and stays the authority, and what was changed is a few lines of readable text
rather than a whole file claiming to be equivalent to another. That is the
opposite of the failure above, which is why it is allowed where an exporter is
not. It stays out of the service for the same reason `survey.py` does: the
product surface reads and explains, and anything that writes is an offline
hand-off to whoever owns the asset.

**No new dependency for URDF.** `yourdfpy` is the obvious candidate and pulls in
31 packages including scipy, shapely and a pinned trimesh -- scipy being one
this project deliberately routed around in the GLB exporter. URDF is a frozen,
simple XML format; parsing it is about a hundred lines and sits on our side of
the line drawn above: rules are NVIDIA's, reading is ours.

## Why not adopt the browser viewers too

There are many: `gkjohnson/urdf-loaders`, `urdf-viz`, `yourdfpy`, MuJoCo
`simulate`, Mechaverse, Ermine Robot Viewer, `needle-tools/usd-viewer`. Their
interaction vocabulary is worth copying — collision-geometry toggles and
centre-of-mass markers are now in (see the Display panel's Physics group);
inertia ellipsoids remain on our gap list.

Adopting one would mean building on it instead of writing our own reader and
viewer. The reason not to is not quality — it is that they assume a different
*input*. A robot description is a text file: self-contained, a strict tree,
links and joints named explicitly, units fixed by the spec, geometry
referenced as external meshes. What arrives here is an artist's Omniverse
export: binary USDC, a namespace hierarchy independent of the kinematic
graph, physics applied as API schemas on arbitrary prims, and a material
graph.

That difference is not abstract. It is where every defect this project has
actually hit comes from:

| Trap in the delivery | In URDF |
|---|---|
| `body1` points one level inside the door, at a prim with no `RigidBodyAPI` | Inexpressible — a joint references top-level link *names*, never a link's internals |
| Limits authored in degrees, lengths in stage units | Inexpressible — the spec fixes radians and metres, so there is no conversion to get wrong |
| `purpose=guide` collision hulls mixed in with render meshes | Inexpressible — `<visual>` and `<collision>` are separate tags |
| Textures resolving into a sibling asset's folder | Inexpressible — no material graph, only mesh file paths |
| Multi-DOF joint on a generic `PhysicsJoint` prim | Inexpressible — joint type is a closed enum |
| A weld that carries structure (`FixedJoint`) | Expressible, and trivial there: explicit `type="fixed"` with unambiguous parent/child names. In USD it is one of several joint prim types, anywhere in the namespace, whose relationships may target non-body prims |

The first five are why a URDF-first tool contains no code for them — it never
had the problem. The last is the honest case: the same defect class exists in
both, and this project shipped it in *both* readers (see `urdf_reader.py`) —
but getting it right in URDF is a one-line predicate and in USD it is not.

So: copy the interaction ideas, do not adopt the stack. Adopting one buys the
easy half — a viewport with sliders — and leaves the whole of the hard half,
reading a USD delivery correctly, still to write. Were the input URDF, the
opposite call would be just as obvious: adopt `urdf-loaders` and write
nothing. The format decides this, not the quality of the tools.

## What about the USD-side tools

The section above is about robot-model viewers. The sharper question is why
not use something that already speaks USD, and the answer is not "there is
nothing" — there is quite a lot. It is worth being exact about what each one
does and does not give, because "no USD viewer exists" would be an absurd
claim to build a project on.

| | Reads UsdPhysics articulation | Drives the joints | Zero-install, shareable | Says *why* something is wrong |
|---|---|---|---|---|
| `usdview` (Pixar) | as data only | no | no — not in the `usd-core` wheel; needs a source build or Omniverse | no |
| Isaac Sim / USD Composer | yes | yes, real PhysX | no — RTX GPU, tens of GB, EULA, minutes to boot | no: a mis-wired joint simply does not move |
| Browser USD renderers (`needle-tools/usd-viewer`, USDZ loaders) | no | no | yes | no |
| `artiscope` | yes | kinematically, clamped | yes | yes — inventory plus validator verdicts on the geometry |

**Isaac Sim is the honest competitor**, and on the hardest axis it is simply
better: it runs the actual engine, so a door opens because PhysX opened it,
not because we approximated a hinge. What it does not do is describe. When a
joint is mis-authored, Isaac Sim's output is that nothing moves, and the
reason is left to the reviewer.

### Which one to reach for

Capabilities are not the same as jobs, and picking by feature table is how a
team ends up installing Isaac Sim to answer a question a URL would have
answered:

- **`usdview`** — when the question is about the USD *file*: what prims
  exist, what an attribute is actually authored as, what a layer composed to.
  It is the ground truth for "what does the file literally say", and this
  tool's readings should be reconcilable against it.
- **Isaac Sim / USD Composer** — when the question is whether the asset
  behaves under real physics: contacts, gravity, drives, a robot actually
  grasping the handle. Anything past kinematics belongs here, and the answer
  is worth the boot time.
- **Browser USD renderers** — when the question is only what it looks like.
- **`artiscope`** — when the question is *what is this delivery, and is
  anything wrong with it*, and the person asking should not have to install
  anything to find out. Triage before an asset enters the library, not
  simulation after it does.

Stated as a boundary rather than a feature list: **this tool is where a
supplier delivery gets understood; Isaac Sim is where an understood asset
gets simulated.** Sending someone to Isaac Sim to answer "why does this door
not open" is a real answer, just an expensive one — and it still ends with
them reading prim paths, which is the part we do instead.

That the combination is empty says nothing about its difficulty — only that
nobody has needed it in exactly this shape yet.

## How replaceable is any of this

The two halves answer differently, and conflating them is how a team talks
itself into building a platform it does not need.

**The rule half is fully replaceable, and we already replaced it.** That is
what consuming the validator *means*. If a better engine-correctness checker
appears, we switch to it; nothing in this repo encodes a rule worth defending.

**The reading half has exactly one full substitute today, and it is Isaac
Sim** — which reads and drives this articulation better than we do, at a
weight that removes the property the tool exists for: someone opening a link
and understanding an asset in a minute. Everything lighter is aimed
elsewhere, so adopting it means inheriting its assumptions along with its
features.

What that half is actually worth is measurable, so it is recorded rather than
asserted. Across a 15-asset corpus of delivered kitchen appliances — 86
authored joints — this reader models 85: 50 revolute, 32 prismatic, 3 fixed.
The one it does not is a tap handle: two coupled rotational DOFs on a generic
`PhysicsJoint` prim, which is announced rather than flattened into a
single-axis approximation.

The cost of replacement is not the 3D view; that part is a weekend against
three.js. It is the accumulated USD-reading fidelity: climbing to the real
rigid body when `body1` points one level too deep, instance proxies,
`GeomSubset` material splits, degrees versus stage units, `purpose=guide`
colliders, invisible meshes, welds that carry structure. Every one of those
was a defect found against a real delivery, not a hypothetical.

**That is a body of evidence, not a moat, and it should not be defended as
one.** None of it is novel or hard; it is one supplier's output, absorbed one
bug at a time. The right posture is therefore to keep this small — currently
~5k lines of Python and ~2.5k of viewer, much of it docstring — and to stay
willing to throw it away the day something USD-first ships with validator
integration built in. Investing past that point buys a platform nobody asked
for.

## Licensing

`omniverse-asset-validator` is Apache-2.0, published by NVIDIA, with **no
dependencies of its own**. The only thing it pulls in transitively is
`usd-core` (`LicenseRef-TOST-1.0`, Pixar's modified Apache 2.0), which this
project already depends on. Commercial use, modification and closed
distribution are all permitted. Attribution is carried in `NOTICE`.
