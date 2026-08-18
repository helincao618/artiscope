/* artiscope review UI.
 *
 * Loads a per-part GLB plus its joint manifest, rebuilds the kinematic tree in
 * three.js, and lets a reviewer drive each joint through its authored range.
 *
 * Two things worth knowing before reading the rest.
 *
 * Coordinate frames. The GLB and the manifest are both in USD stage space,
 * which is Z-up here. Everything below works in that space, unconverted, so
 * numbers on screen can be compared against the manifest and against usdview.
 * The single conversion to three.js's Y-up world happens on `sceneRoot`, and
 * nothing inside it needs to know.
 *
 * Limits without a solver. A revolute or prismatic joint has one degree of
 * freedom, so honouring its limits is a clamp on a scalar, not a simulation.
 * That is why this runs in a browser with no physics engine: what an engine
 * would add is gravity, contact and drive feel, none of which is what you are
 * checking when you ask "is this hinge in the right place, on the right axis,
 * with the right travel".
 */

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

// ── constants ───────────────────────────────────────────────────────

const SWEEP_SECONDS = 2.4;

/* Long on purpose. `<model-viewer>` resumes its orbit after 3 s, which suits a
 * product page; here you routinely spend longer than that reading the joint
 * table, and having the model start moving while you do is just noise.
 *
 * Overridable with `?idle=<ms>` for a screen that is meant to be looked at
 * rather than used -- a demo running in the corner of a room wants seconds,
 * not twenty of them. */
const IDLE_DELAY_MS = Number(
  new URLSearchParams(location.search).get("idle") ?? 20_000
);
const IDLE_ORBIT_SPEED = 0.5;

/* One joint per stop, then a pause at zero before the next. Slow enough to
 * follow; the pause is what keeps the stops from blurring into each other. */
const TOUR_STOP_SECONDS = 2.6;
const TOUR_PAUSE_MS = 700;

/* How many ghosts of a part to draw across its range. Enough to read as a
 * swept volume, few enough to still see the part itself through them. */
const ENVELOPE_SAMPLES = 7;
const EXPLODE_FRACTION = 0.45;

const LIMIT_FLASH_MS = 260;
const SLIDER_STEPS = 1000;
const RAD_TO_DEG = 180 / Math.PI;

const COLOR_AXIS = 0xf0f3f8;
const COLOR_SWEEP = 0xfbbf24;
const COLOR_NOW = 0x34d399;
const COLOR_SELECT = 0x60a5fa;
const COLOR_ENVELOPE = 0x7dd3fc;
const COLOR_FAULT = 0xf87171;
const COLOR_MASS = 0xc084fc;
const COLOR_COLLISION = 0xf59e0b;
const COLLISION_OPACITY = 0.35;

/* A tint strong enough to read against a flat palette colour swamps a texture,
 * and the materials view exists precisely so the surface can be judged. Only
 * selection tints at all, and only ever one part at a time. */
const SELECT_TINT_FLAT = 0.35;
const SELECT_TINT_TEXTURED = 0.16;

/* Creases sharper than this get an edge. Every edge would trace the
 * triangulation of each curve and read as noise rather than as an outline. */
const FAULT_EDGE_ANGLE = 30;

/* Past this many segments an outline stops being one. A wire dish rack is
 * hundreds of rods, every one of them a box with four hard creases, so tracing
 * its shape produces a scribble that hides the part instead of marking it. */
const FAULT_EDGE_LIMIT = 400;

// ── state ───────────────────────────────────────────────────────────

const state = {
  assetKey: null,
  manifest: null,
  report: null, // findings in one vocabulary; see app/findings.py
  faultsByPart: new Map(), // part id -> findings that say it is actually wrong
  rig: new Map(), // part id -> rig entry
  jointById: new Map(),
  jointValue: new Map(), // joint id -> current q (SI)
  selectedPartId: null,
  selectedJointId: null,
  meshes: [],
  gizmos: new Map(), // joint id -> gizmo group
  labels: new Map(), // joint id -> sprite
  envelopes: new Map(), // joint id -> group of ghost meshes
  massMarkers: new Map(), // part id -> marker+label group
  animating: false,
  touring: false,
};

let scene;
let sceneRoot; // stage space lives in here
let assetRoot; // rebuilt per asset
let camera;
let renderer;
let controls;
let raycaster;
let gridHelper;

const $ = (id) => document.getElementById(id);

// ── three.js bootstrap ──────────────────────────────────────────────

function initViewer() {
  const host = $("canvasHost");

  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x0b0e13, 1);
  host.appendChild(renderer.domElement);

  scene = new THREE.Scene();

  camera = new THREE.PerspectiveCamera(45, 1, 0.01, 500);
  camera.position.set(2.4, 1.8, 2.4);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;

  // Stage space is Z-up; three.js renders Y-up. One rotation here keeps every
  // anchor, axis and vertex below in the frame the manifest describes.
  sceneRoot = new THREE.Group();
  sceneRoot.rotation.x = -Math.PI / 2;
  scene.add(sceneRoot);

  scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x1a1f2a, 2.1));
  const key = new THREE.DirectionalLight(0xffffff, 1.5);
  key.position.set(3, 5, 2);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.5);
  fill.position.set(-3, 2, -2);
  scene.add(fill);

  gridHelper = new THREE.GridHelper(10, 20, 0x2a3140, 0x1a2028);
  scene.add(gridHelper);

  raycaster = new THREE.Raycaster();

  new ResizeObserver(resize).observe(host);
  resize();
  renderer.setAnimationLoop(render);
}

function resize() {
  const host = $("canvasHost");
  const width = host.clientWidth || 1;
  const height = host.clientHeight || 1;
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}

function render() {
  controls.update();
  renderer.render(scene, camera);
}

// ── asset loading ───────────────────────────────────────────────────

async function loadAssetList(preferredKey = null) {
  // The picker leads with articulated assets; a directory-sized delivery is
  // mostly parts with no joint at all, and listing every one of those beside
  // the handful worth opening would bury them. A preferred key means
  // something specific was just asked for -- an upload, a direct link -- so
  // that request always gets the full list rather than risking the very
  // thing someone dropped on purpose vanishing from the picker.
  const response = await fetch(
    `/api/assets${preferredKey ? "?include_static=true" : ""}`
  );
  const data = await response.json();
  const select = $("assetSelect");
  select.innerHTML = "";

  if (!data.assets.length) {
    /* An unmounted directory and a genuinely empty one both scan as zero
     * assets, and telling someone their delivery is empty when nothing is
     * mounted sends them looking in the wrong place entirely. */
    setOverlay(
      data.asset_dir_exists
        ? `No USD assets found under\n${data.asset_dir}`
        : `Asset directory does not exist:\n${data.asset_dir}\n\nSet ARTISCOPE_ASSET_DIR, or link the delivery in at that path.`
    );
    return;
  }

  for (const asset of data.assets) {
    const option = document.createElement("option");
    option.value = asset.key;
    option.textContent = optionLabel(asset);
    option.disabled = Boolean(asset.error);
    select.appendChild(option);
  }

  select.onchange = () => loadAsset(select.value);
  /* Not awaited: the verdicts are decoration on a list that is already usable,
   * and the heaviest asset in a delivery takes the better part of a minute to
   * check. */
  fillInVerdicts(data.assets, select);
  /* After an upload, land on what was just dropped -- including when it turned
   * out to be unreadable, since that is the answer the person was after. */
  const wanted =
    data.assets.find((a) => a.key === preferredKey) ??
    data.assets.find((a) => !a.error);
  if (!wanted) {
    return;
  }
  select.value = wanted.key;
  if (wanted.error) {
    setOverlay(`${wanted.key} could not be read.\n\n${wanted.error}`);
    return;
  }
  await loadAsset(wanted.key);
}

/* Bumped every time the picker is rebuilt, so a sweep still walking the old
 * list stops instead of writing verdicts onto options that no longer exist. */
let verdictSweepGeneration = 0;

function optionLabel(asset) {
  if (asset.error) {
    return `${asset.key}  (unreadable)`;
  }
  const counts = `${asset.part_count} parts, ${asset.joint_count} joints`;
  /* Three states, kept distinct: checked and defective, checked and clean, and
   * not checked yet. Collapsing the last two would report an asset nobody has
   * looked at as if it had passed. */
  let verdict = "  ·  unchecked";
  if (asset.validator_status) {
    verdict = asset.blocking_count
      ? `  ·  ⚠ ${asset.blocking_count} blocking`
      : "";
  }
  return `${asset.key}  ·  ${counts}${verdict}`;
}

/* The blocking count rides in the picker so a defective delivery is visible
 * before anyone thinks to open it. The server only reports verdicts it already
 * has cached, so the ones it does not are collected here instead. */
async function fillInVerdicts(assets, select) {
  const generation = ++verdictSweepGeneration;
  const pending = assets.filter((a) => !a.error && !a.validator_status);

  /* One at a time. The validator is CPU-bound and re-reads the whole stage, so
   * firing the list off in parallel would starve the asset the person is
   * actually waiting to look at. */
  for (const asset of pending) {
    if (generation !== verdictSweepGeneration) return;
    try {
      const report = await fetch(
        `/api/validation/${encodeURIComponent(asset.key)}`
      ).then(expectJson);
      asset.validator_status = report.status;
      asset.blocking_count = report.blocking_count;
    } catch (error) {
      /* A verdict that cannot be obtained leaves the asset unchecked, which is
       * exactly what it is. Opening the asset itself is unaffected. */
      console.warn(`Could not validate ${asset.key}`, error);
      continue;
    }
    if (generation !== verdictSweepGeneration) return;
    const option = [...select.options].find((o) => o.value === asset.key);
    if (option) option.textContent = optionLabel(asset);
  }
}

async function loadAsset(assetKey) {
  state.assetKey = assetKey;
  setOverlay("Loading…");

  try {
    const [manifest, report] = await Promise.all([
      fetch(`/api/manifest/${encodeURIComponent(assetKey)}`).then(expectJson),
      fetch(`/api/findings/${encodeURIComponent(assetKey)}`).then(expectJson),
    ]);
    state.manifest = manifest;
    state.report = report;
    state.jointById = new Map(manifest.joints.map((j) => [j.id, j]));
    state.faultsByPart = groupFaultsByPart(report);

    const gltf = await new GLTFLoader().loadAsync(
      `/api/glb/${encodeURIComponent(assetKey)}`
    );

    buildRig(gltf.scene, manifest);
    resetPose();
    renderObjectList();
    renderFindings();
    renderBanners();
    renderStatusStrip();
    // The first thing that moves, or the base when nothing does -- an empty
    // inspector on arrival would say the asset has nothing to show.
    selectPart(
      manifest.joints.length
        ? manifest.joints[0].child_part
        : manifest.root_part ?? manifest.parts[0]?.id ?? null
    );
    frameAsset();

    // Whatever was scrolled to belonged to the previous asset. Leaving the
    // panel where it was lands the reviewer halfway down a different asset's
    // detail with nothing saying why.
    $("rightPanel").scrollTop = 0;

    setOverlay(null);

    /* A sweep on load answers "is this articulated, and how far does each part
     * travel" before anyone has to know to ask -- a static model gives away
     * neither. All joints at once here, rather than the tour's one at a time,
     * because on arrival the question is "what is this" and not yet "what is
     * each piece". Not awaited, so the page is live and the first click
     * cancels it. */
    noteActivity(); // restart the idle countdown before the sweep, not after
    if (!prefersReducedMotion()) sweepAllJoints();
  } catch (error) {
    console.error(error);
    setOverlay(`Could not load ${assetKey}\n\n${error.message}`);
  }
}

async function expectJson(response) {
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${response.status}: ${body.slice(0, 300)}`);
  }
  return response.json();
}

function setOverlay(message) {
  const overlay = $("viewportOverlay");
  overlay.textContent = message ?? "";
  overlay.classList.toggle("hidden", !message);
}

// ── kinematic rig ───────────────────────────────────────────────────

/* Each movable part hangs off a pivot placed at its joint anchor, with the
 * mesh shifted back by the same amount. The mesh therefore renders at its
 * baked world coordinates while rotating the pivot rotates about the anchor,
 * which is exactly what a hinge does. No per-vertex work at drive time. */
function buildRig(gltfScene, manifest) {
  if (assetRoot) {
    sceneRoot.remove(assetRoot);
    disposeTree(assetRoot);
  }
  assetRoot = new THREE.Group();
  sceneRoot.add(assetRoot);

  state.rig = new Map();
  state.gizmos = new Map();
  state.labels = new Map();
  state.envelopes = new Map();
  state.massMarkers = new Map();
  state.meshes = [];

  const { byName: meshByName, texturedByPart, collisionByPart } = collectMeshes(gltfScene);
  const partById = new Map(manifest.parts.map((p) => [p.id, p]));
  const rootPart = partById.get(manifest.root_part) ?? manifest.parts[0];

  // Interactive joints and rigid (fixed-joint) attachments are walked
  // together, tagged by kind, so a part welded onto another -- glass onto a
  // door, say -- still gets a correctly placed pivot and still moves with
  // whatever it is welded to, even though it never gets a slider of its own.
  const jointsByParent = new Map();
  const addAttachment = (parentPart, entry) => {
    // A joint anchored to the world, or to a body that resolves outside every
    // known part, still has to be driveable. The root pivot never moves and
    // its frame is the stage frame, so it stands in for the world. Without
    // this the child never gets reached by the walk below, falls through to
    // the unattached-part fallback, and its slider then swings it about a
    // made-up Z axis through the origin.
    const parentId = partById.has(parentPart) ? parentPart : rootPart.id;
    if (!jointsByParent.has(parentId)) jointsByParent.set(parentId, []);
    jointsByParent.get(parentId).push(entry);
  };
  for (const joint of manifest.joints) {
    addAttachment(joint.parent_part, { joint, fixed: null });
  }
  for (const fixed of manifest.fixed_joints ?? []) {
    addAttachment(fixed.parent_part, { joint: null, fixed });
  }

  // A part carries two appearance variants side by side in the same pivot --
  // the flat palette mesh and, from app/mesh_export.py, zero or more textured
  // pieces built from the asset's own materials. Both move with exactly the
  // same joint, so toggling which is visible (applySurfaceMode) never needs to
  // touch the rig itself.
  const attach = (part, pivot, anchor) => {
    const mesh = meshByName.get(part.node_name) ?? null;
    const texturedMeshes = texturedByPart.get(part.node_name) ?? [];
    const collisionMeshes = collisionByPart.get(part.node_name) ?? [];
    for (const m of mesh ? [mesh, ...texturedMeshes] : texturedMeshes) {
      m.position.copy(anchor).negate();
      m.userData.partId = part.id;
      pivot.add(m);
      state.meshes.push(m);
    }
    for (const m of texturedMeshes) m.visible = false; // flat is the default
    // Not pushed to state.meshes: that array drives picking, framing and the
    // flat/wireframe surface toggle, none of which the collision overlay
    // should take part in -- it is a Display-only extra, never a thing to
    // click through to or judge as a surface.
    for (const m of collisionMeshes) {
      m.position.copy(anchor).negate();
      m.userData.partId = part.id;
      pivot.add(m);
    }
    const outline = faultOutline(part, mesh ?? texturedMeshes[0], anchor);
    if (outline) pivot.add(outline);
    return { mesh, texturedMeshes, collisionMeshes, outline };
  };

  const zero = new THREE.Vector3();

  // A delivered asset has one root. An assembled scene is a *forest* -- every
  // appliance standing in it has a base of its own that no joint drives -- so
  // walking from a single root reaches one appliance and leaves every other
  // appliance's joints out of the rig. Those joints still get sliders, and the
  // fallback below would have given their parts a pivot with no axis and no
  // anchor: pulling one swung a door about a made-up Z axis through the origin
  // while the appliance it belongs to stood still.
  const driven = new Set([
    ...manifest.joints.map((j) => j.child_part),
    ...(manifest.fixed_joints ?? []).map((f) => f.child_part),
  ]);
  const rootParts = manifest.parts.filter((p) => !driven.has(p.id));
  if (!rootParts.some((p) => p.id === rootPart.id)) rootParts.unshift(rootPart);

  // A root has no joint, so its mesh sits at baked coordinates directly.
  const queue = [];
  const seen = new Set();
  for (const part of rootParts) {
    const pivot = new THREE.Group();
    assetRoot.add(pivot);
    const attachment = attach(part, pivot, zero);
    state.rig.set(part.id, {
      part,
      pivot,
      ...attachment,
      joint: null,
      anchor: zero.clone(),
      anchorLocal: zero.clone(),
      axis: new THREE.Vector3(0, 0, 1),
      restPosition: zero.clone(),
    });
    queue.push(part.id);
    seen.add(part.id);
  }

  // Breadth-first so a parent's anchor is known before its children need it.
  while (queue.length) {
    const parentId = queue.shift();
    const parentRig = state.rig.get(parentId);
    for (const { joint, fixed } of jointsByParent.get(parentId) ?? []) {
      const childPart = joint ? joint.child_part : fixed.child_part;
      const part = partById.get(childPart);
      if (!part || seen.has(part.id)) continue;
      seen.add(part.id);

      const anchorSource = joint ? joint.anchor_world : fixed.anchor_world;
      const anchor = new THREE.Vector3(...anchorSource);
      const pivot = new THREE.Group();
      pivot.position.copy(anchor).sub(parentRig.anchor);
      parentRig.pivot.add(pivot);

      const attachment = attach(part, pivot, anchor);
      state.rig.set(part.id, {
        part,
        pivot,
        ...attachment,
        joint,
        anchor,
        anchorLocal: pivot.position.clone(),
        // The axis is stored in stage space and used as the pivot's local
        // axis. At rest every pivot is unrotated, so the two coincide -- and
        // once a parent moves, a hinge fixed to that parent should move with
        // it, which is what a local axis gives. A fixed attachment has no
        // axis to speak of; its pivot is never rotated, so the value here is
        // inert.
        axis: joint
          ? new THREE.Vector3(...joint.axis_world).normalize()
          : new THREE.Vector3(0, 0, 1),
        restPosition: pivot.position.clone(),
      });
      if (joint) state.jointValue.set(joint.id, 0);
      queue.push(part.id);
    }
  }

  // Parts the manifest never attaches to anything still have to be visible,
  // otherwise a structural defect looks like missing geometry.
  for (const part of manifest.parts) {
    if (state.rig.has(part.id)) continue;
    const pivot = new THREE.Group();
    assetRoot.add(pivot);
    const attachment = attach(part, pivot, zero);
    state.rig.set(part.id, {
      part,
      pivot,
      ...attachment,
      joint: null,
      anchor: zero.clone(),
      anchorLocal: zero.clone(),
      axis: new THREE.Vector3(0, 0, 1),
      restPosition: zero.clone(),
    });
  }

  state.labelHeight = labelHeightFor(state.meshes);
  for (const joint of manifest.joints) {
    buildGizmo(joint);
    buildEnvelope(joint);
  }
  for (const part of manifest.parts) buildMassMarker(part);
  applyGizmoVisibility();
  applyEnvelopeVisibility();
  applyMassMarkers();
  applyCollisionVisibility();
  updatePhysicsToggleAvailability();
  applyExploded();
  applySurfaceMode();
}

/* Ticking "Mass & centre of mass" or "Collision geometry" on an asset with
 * nothing for either to draw -- no part authors a centre of mass, or every
 * collider is the visual mesh itself -- used to look identical to the
 * feature being broken: the box goes blue, the viewport does not change.
 * Disabling the box up front, with the title saying which of the two
 * reasons applies, tells that apart from a bug without anyone having to ask. */
function updatePhysicsToggleAvailability() {
  const anyMass = state.massMarkers.size > 0;
  const anyCollision = [...state.rig.values()].some(
    (rig) => rig.collisionMeshes.length > 0
  );

  const massToggle = $("toggleMass");
  massToggle.disabled = !anyMass;
  massToggle.title = anyMass
    ? ""
    : "No part in this asset authors a centre of mass.";

  const collisionToggle = $("toggleCollision");
  collisionToggle.disabled = !anyCollision;
  collisionToggle.title = anyCollision
    ? ""
    : "Every collider in this asset is its visual mesh -- what is already on screen is what a physics engine would collide against.";
}

/* Every mesh belonging to a rig entry: the flat one (if any) plus every
 * textured piece. The two variants are geometrically identical, so anything
 * that only reads shape (bounding boxes, ghosts) can keep using `rig.mesh`
 * alone -- this is only for effects that touch the material or transform of
 * whichever variant is actually on screen. */
function meshesOf(rig) {
  return rig.mesh ? [rig.mesh, ...rig.texturedMeshes] : rig.texturedMeshes;
}

/* The outline is not a mesh -- nothing picks it and nothing tints it -- but it
 * traces one, so whatever moves the part has to move it too. */
function movablesOf(rig) {
  return rig.outline ? [...meshesOf(rig), rig.outline] : meshesOf(rig);
}

/* A part the engine will reject is marked with an edge, not a wash of colour.
 * A tint over the whole surface competes with whichever colour system is
 * running -- the per-part palette or the authored materials -- and on an asset
 * where most parts are rejected it does not mark the surface, it replaces it.
 * An edge is unmistakable and costs none of the surface it reports on.
 *
 * Built once, here, and only for the parts that need one: `faultsByPart` is
 * populated before the rig, and tracing every part to hide most of them would
 * be work spent on nothing. */
function faultOutline(part, source, anchor) {
  if (!source || !state.faultsByPart.has(part.id)) return null;
  const line = new THREE.LineSegments(
    outlineGeometry(source.geometry),
    new THREE.LineBasicMaterial({ color: COLOR_FAULT })
  );
  line.position.copy(anchor).negate();
  return line;
}

/* Trace the shape while the shape is simple enough to read, and fall back to
 * the volume it occupies when it is not. Both answer "which of these pieces
 * will the engine reject"; only the first also answers "what shape is it", and
 * that answer is worth nothing once it arrives as a thicket. */
function outlineGeometry(geometry) {
  const edges = new THREE.EdgesGeometry(geometry, FAULT_EDGE_ANGLE);
  if (edges.attributes.position.count / 2 <= FAULT_EDGE_LIMIT) return edges;
  edges.dispose();

  geometry.computeBoundingBox();
  const size = geometry.boundingBox.getSize(new THREE.Vector3());
  const centre = geometry.boundingBox.getCenter(new THREE.Vector3());
  const box = new THREE.EdgesGeometry(
    new THREE.BoxGeometry(size.x, size.y, size.z)
  );
  box.translate(centre.x, centre.y, centre.z);
  return box;
}

function surfaceMode() {
  return document.querySelector("input[name=surfaceMode]:checked").value;
}

/* Instant swap, no rebuild: both variants already sit in the rig, so
 * switching which one renders is a visibility flip, not a refetch. A part
 * with no textured piece at all (nothing bound anywhere in the source
 * asset, e.g. a part with no material and no fallback) keeps showing its
 * flat mesh even in "materials" mode -- better than a hole.
 *
 * Wireframe draws the flat variant, so the wires keep the per-part palette
 * and the parts stay tellable apart even with no surface left to look at.
 *
 * Physics Material also draws the flat variant, recoloured: the per-part
 * palette answers "which lump moves independently" and has nothing to do
 * with simulated contact, so this mode overrides it with a colour keyed to
 * the bound physics material's identity instead -- parts sharing a colour
 * share a material, which is the fact this mode exists to show. */
function applySurfaceMode() {
  const mode = surfaceMode();
  const wantMaterial = mode === "materials";
  const wantPhysicsMaterial = mode === "physics";
  for (const rig of state.rig.values()) {
    const hasTextured = rig.texturedMeshes.length > 0;
    if (rig.mesh) {
      rig.mesh.visible = !(wantMaterial && hasTextured);
      // Vertex colours and a flat override colour are mutually exclusive on
      // the same material -- leaving vertexColors on while setting `color`
      // would tint the palette rather than replace it.
      rig.mesh.material.vertexColors = !wantPhysicsMaterial;
      rig.mesh.material.color.copy(
        wantPhysicsMaterial
          ? physicsMaterialColor(physicsMaterialKey(rig.part))
          : rig.mesh.userData.baseColor
      );
      rig.mesh.material.needsUpdate = true;
    }
    for (const mesh of rig.texturedMeshes) mesh.visible = wantMaterial;
  }
  for (const mesh of state.meshes) mesh.material.wireframe = mode === "wireframe";
  // The selection tint is only drawn in Texture mode (see
  // refreshSelectionVisuals), so switching modes has to redecide it too.
  refreshSelectionVisuals();
}

/* A part with no physics material bound is still a group -- "unset" is as
 * real a bucket as any authored material name, and parts that share the
 * absence of one are exactly as related as parts that share a name. */
function physicsMaterialKey(part) {
  const material = part.physics_material;
  return material.name ?? material.path ?? "(unset)";
}

/* A stable hash rather than an index into the reader's part order, because
 * what this mode has to show is *identity* -- two parts on opposite sides of
 * the asset with the same material name need the same colour, which a
 * position-based palette like _part_palette in app/mesh_export.py cannot
 * give without both sides agreeing on an order they do not actually share. */
function physicsMaterialColor(key) {
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) {
    hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  }
  return new THREE.Color().setHSL((hash % 360) / 360, 0.55, 0.55);
}

/* Every collider distinct from its part's visual mesh, shown or hidden as
 * one group. A part whose collider *is* its visual mesh (Dishwasher's
 * convention) has nothing here to toggle -- what is on screen already is
 * what the engine collides against, and there is no second shell to layer
 * over it. */
function applyCollisionVisibility() {
  const show = $("toggleCollision").checked;
  for (const rig of state.rig.values()) {
    for (const mesh of rig.collisionMeshes) mesh.visible = show;
  }
}

/* Bake each mesh's world transform into its geometry and detach it, so the
 * vertices are in stage coordinates no matter what node hierarchy or base
 * transform the exporter happened to emit. */
/* Separator the exporter uses between a part's flat node name and its
 * textured-variant pieces (app/mesh_export.py: MATERIAL_NODE_SEP). Kept in
 * sync manually -- it is a naming convention shared across a network
 * boundary, not a value either side could import from the other. */
const MATERIAL_NODE_SEP = "~mat";

/* Same reasoning as MATERIAL_NODE_SEP, for the collision-overlay node
 * (app/mesh_export.py: COLLISION_NODE_SEP). */
const COLLISION_NODE_SEP = "~col";

function collectMeshes(gltfScene) {
  gltfScene.updateMatrixWorld(true);

  const found = [];
  gltfScene.traverse((object) => {
    if (object.isMesh) {
      found.push({ mesh: object, matrix: object.matrixWorld.clone(), names: nodeNames(object) });
    }
  });

  const byName = new Map();
  const texturedByPart = new Map(); // part node name -> [mesh, ...]
  const collisionByPart = new Map(); // part node name -> [mesh, ...]
  for (const { mesh, matrix, names } of found) {
    mesh.removeFromParent();
    mesh.geometry.applyMatrix4(matrix);
    mesh.position.set(0, 0, 0);
    mesh.quaternion.identity();
    mesh.scale.set(1, 1, 1);
    mesh.geometry.computeVertexNormals();
    mesh.geometry.computeBoundingBox();

    const collisionName = names.find((name) => name.includes(COLLISION_NODE_SEP));
    if (collisionName) {
      // A translucent shell, not a surface anyone is asked to judge -- no
      // vertex colours, no authored material, just enough to read the shape
      // an engine would actually collide against and where it departs from
      // what is drawn. Hidden until the Display toggle says otherwise.
      mesh.material = new THREE.MeshBasicMaterial({
        color: COLOR_COLLISION,
        transparent: true,
        opacity: COLLISION_OPACITY,
        depthWrite: false,
        side: THREE.DoubleSide,
      });
      mesh.visible = false;
      const partName = collisionName.slice(0, collisionName.indexOf(COLLISION_NODE_SEP));
      if (!collisionByPart.has(partName)) collisionByPart.set(partName, []);
      collisionByPart.get(partName).push(mesh);
      continue;
    }

    const taggedName = names.find((name) => name.includes(MATERIAL_NODE_SEP));
    if (taggedName) {
      // The textured variant: keep the material the GLB actually authored
      // (built in app/mesh_export.py from the source USD shader) instead of
      // rebuilding a flat one. Still forced double-sided for the same reason
      // as the flat mesh below, and sRGB-decoded so a JPEG's colours land
      // the way the source image actually looks rather than washed out.
      if (mesh.material) {
        // Cloned first: GLTFLoader hands every mesh built from the same glTF
        // material one shared THREE.Material, and this viewer paints
        // selection and fault state onto `material.emissive`. Shared, that
        // paints the whole asset -- five cabinet doors and the carcass all
        // fall back to one neutral-grey material, so flagging the doors the
        // engine rejects lit the carcass red along with them and the
        // authored-materials view was unreadable.
        mesh.material = mesh.material.clone();
        mesh.material.side = THREE.DoubleSide;
        if (mesh.material.map) mesh.material.map.colorSpace = THREE.SRGBColorSpace;
        mesh.material.needsUpdate = true;
      }
      const partName = taggedName.slice(0, taggedName.indexOf(MATERIAL_NODE_SEP));
      if (!texturedByPart.has(partName)) texturedByPart.set(partName, []);
      texturedByPart.get(partName).push(mesh);
      continue;
    }

    mesh.material = new THREE.MeshStandardMaterial({
      vertexColors: Boolean(mesh.geometry.getAttribute("color")),
      color: mesh.geometry.getAttribute("color") ? 0xffffff : 0x9aa6b8,
      roughness: 0.72,
      metalness: 0.03,
      flatShading: false,
      // These parts are thin single-walled shells authored with outward-only
      // normals -- normal for something never meant to be seen from inside.
      // But this viewer's whole point is opening doors and looking in, and
      // single-sided rendering culls exactly the wall that comes into view:
      // an open microwave with a fully modelled cavity renders as an empty
      // black box, indistinguishable from a cavity that was never modelled
      // at all. Double-sided costs nothing worth noticing at this part count.
      side: THREE.DoubleSide,
    });
    mesh.userData.baseColor = mesh.material.color.clone();
    for (const name of names) if (!byName.has(name)) byName.set(name, mesh);
  }
  return { byName, texturedByPart, collisionByPart };
}

/* A GLB writer may put the name on the mesh, on its parent node, or on the
 * geometry. Collect all three rather than guess. */
function nodeNames(mesh) {
  const names = [];
  if (mesh.name) names.push(mesh.name);
  if (mesh.parent && mesh.parent.name) names.push(mesh.parent.name);
  if (mesh.geometry && mesh.geometry.name) names.push(mesh.geometry.name);
  return names;
}

function disposeTree(root) {
  root.traverse((object) => {
    if (object.geometry) object.geometry.dispose();
    if (object.material) {
      const materials = Array.isArray(object.material) ? object.material : [object.material];
      for (const material of materials) {
        // The flat palette carries no texture, but a textured piece's
        // material can hold one -- undisposed, it is a WebGL texture leak
        // every time an asset with authored materials is closed.
        material.map?.dispose();
        material.dispose();
      }
    }
  });
}

// ── driving joints ──────────────────────────────────────────────────

/* The range the viewport drives, which is the authored one unless a local
 * patch supplied a stand-in. Only motion reads this. Everything that reports
 * what the asset says keeps reading joint.limits directly, so a patch can
 * never make the tool misquote the file -- see local/patches/README.md. */
function drivenLimits(joint) {
  return joint.override_limits ?? joint.limits;
}

function clampToLimits(joint, value) {
  const { lower, upper } = drivenLimits(joint);
  let clamped = value;
  let hit = false;
  if (lower !== null && clamped < lower) {
    clamped = lower;
    hit = true;
  }
  if (upper !== null && clamped > upper) {
    clamped = upper;
    hit = true;
  }
  return { value: clamped, hit };
}

/* The whole "it really has stops" behaviour, in one clamp. */
function setJointValue(jointId, value, { silent = false } = {}) {
  const joint = state.jointById.get(jointId);
  const rig = state.rig.get(joint.child_part);
  if (!rig) return;

  const { value: q, hit } = clampToLimits(joint, value);
  state.jointValue.set(jointId, q);

  if (joint.type === "revolute") {
    rig.pivot.quaternion.setFromAxisAngle(rig.axis, q);
  } else {
    rig.pivot.position.copy(rig.restPosition).addScaledVector(rig.axis, q);
  }

  updateGizmoMarker(joint);
  if (!silent) syncJointControls(jointId, hit);
}

function resetPose() {
  for (const joint of state.manifest.joints) setJointValue(joint.id, 0, { silent: true });
  for (const joint of state.manifest.joints) syncJointControls(joint.id, false);
}

function jointSpan(joint) {
  const limits = drivenLimits(joint);
  const lower = limits.lower ?? -Math.PI;
  const upper = limits.upper ?? Math.PI;
  return { lower, upper };
}

function formatValue(joint, value, decimals = 1) {
  return joint.type === "revolute"
    ? `${(value * RAD_TO_DEG).toFixed(decimals)}°`
    : `${(value * 1000).toFixed(decimals)} mm`;
}

/* USD hands a 0.3 m limit back as 0.30000001192092896 once it has been
 * through a float32 stage unit. Printing the raw double next to a label
 * saying "as authored" invites a hunt for a precision bug that is not
 * there -- the value in the file is 0.3. */
function formatAuthored(value, decimals = 4) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number" || !Number.isFinite(value)) return String(value);
  return String(Number(value.toFixed(decimals)));
}

/* Not a dash: a negative lower limit against one reads as a single number, and
 * "-90–0" is the range four of the five cabinet doors have. "to" is how a
 * joint limit is written in URDF and in every robotics reference, and unlike a
 * dash or an ellipsis it survives sitting next to a minus sign. */
function formatAuthoredRange(limits) {
  return `${formatAuthored(limits.lower_raw)} to ${formatAuthored(limits.upper_raw)}`;
}

// ── joint gizmo ─────────────────────────────────────────────────────

/* Drawn in the parent's frame, so it stays put while the child swings past
 * it. The travel arc is laid out in the direction the child actually moves,
 * which turns "is the range right" into something you read at a glance
 * instead of something you infer from two numbers. */
function buildGizmo(joint) {
  const rig = state.rig.get(joint.child_part);
  if (!rig) return;
  const parentRig = state.rig.get(joint.parent_part) ?? state.rig.get(state.manifest.root_part);
  if (!parentRig) return;

  const group = new THREE.Group();
  group.position.copy(rig.anchorLocal);
  group.renderOrder = 10;
  parentRig.pivot.add(group);

  const axis = rig.axis.clone();
  const reach = childReach(rig);
  const { u, v } = axisBasis(axis, rig);

  const line = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([
      axis.clone().multiplyScalar(-reach * 0.9),
      axis.clone().multiplyScalar(reach * 0.9),
    ]),
    depthlessMaterial(COLOR_AXIS, 0.9)
  );
  group.add(line);

  const { lower, upper } = jointSpan(joint);
  let marker;

  if (joint.type === "revolute") {
    const points = [];
    const segments = 64;
    for (let i = 0; i <= segments; i += 1) {
      const angle = lower + ((upper - lower) * i) / segments;
      points.push(arcPoint(u, v, reach, angle));
    }
    group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), depthlessMaterial(COLOR_SWEEP, 1)));

    for (const angle of [lower, upper]) {
      const stop = arcPoint(u, v, reach, angle);
      group.add(
        new THREE.Line(
          new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), stop]),
          depthlessMaterial(COLOR_SWEEP, 0.45)
        )
      );
    }
    marker = markerMesh(reach);
    marker.position.copy(arcPoint(u, v, reach, 0));
  } else {
    group.add(
      new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([
          axis.clone().multiplyScalar(lower),
          axis.clone().multiplyScalar(upper),
        ]),
        depthlessMaterial(COLOR_SWEEP, 1)
      )
    );
    marker = markerMesh(reach);
    marker.position.set(0, 0, 0);
  }

  group.add(marker);
  group.userData = { u, v, reach, marker, axis };
  state.gizmos.set(joint.id, group);
  buildLabel(joint, group, u, reach);
}

function arcPoint(u, v, radius, angle) {
  return u
    .clone()
    .multiplyScalar(Math.cos(angle) * radius)
    .addScaledVector(v, Math.sin(angle) * radius);
}

/* Prefer a basis that points at the child part, so the arc traces the path
 * the child sweeps rather than an arbitrary circle. */
function axisBasis(axis, rig) {
  let u = new THREE.Vector3(1, 0, 0);
  const box = rig.mesh?.geometry?.boundingBox;
  if (box) {
    const centre = box.getCenter(new THREE.Vector3());
    u = centre.sub(rig.anchor);
    u.addScaledVector(axis, -u.dot(axis));
  }
  if (u.lengthSq() < 1e-9) {
    u = Math.abs(axis.x) < 0.9 ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0);
    u.addScaledVector(axis, -u.dot(axis));
  }
  u.normalize();
  const v = new THREE.Vector3().crossVectors(axis, u).normalize();
  return { u, v };
}

function childReach(rig) {
  const box = rig.mesh?.geometry?.boundingBox;
  if (!box) return 0.25;
  const centre = box.getCenter(new THREE.Vector3());
  return THREE.MathUtils.clamp(centre.distanceTo(rig.anchor) * 1.05, 0.05, 3);
}

function depthlessMaterial(color, opacity) {
  return new THREE.LineBasicMaterial({
    color,
    transparent: true,
    opacity,
    depthTest: false,
  });
}

function markerMesh(reach) {
  const mesh = new THREE.Mesh(
    new THREE.SphereGeometry(THREE.MathUtils.clamp(reach * 0.05, 0.008, 0.05), 16, 12),
    new THREE.MeshBasicMaterial({ color: COLOR_NOW, depthTest: false })
  );
  mesh.renderOrder = 11;
  return mesh;
}

function updateGizmoMarker(joint) {
  const gizmo = state.gizmos.get(joint.id);
  if (!gizmo) return;
  const { u, v, reach, marker, axis } = gizmo.userData;
  const q = state.jointValue.get(joint.id) ?? 0;
  if (joint.type === "revolute") {
    marker.position.copy(arcPoint(u, v, reach, q));
  } else {
    marker.position.copy(axis).multiplyScalar(q);
  }
}

function applyGizmoVisibility() {
  const showAll = $("toggleGizmoAll").checked;
  const showLabels = $("toggleLabels").checked;
  for (const [jointId, gizmo] of state.gizmos) {
    gizmo.visible = showAll || jointId === state.selectedJointId;
  }
  for (const [jointId, label] of state.labels) {
    const gizmo = state.gizmos.get(jointId);
    label.visible = showLabels && Boolean(gizmo?.visible);
  }
}

// ── mass & centre-of-mass markers ──────────────────────────────────

/* A small sphere plus a weight label at the point a physics engine would
 * balance the part on. Placed only when the file actually authored a centre
 * of mass -- an un-authored one is the engine's to compute from geometry,
 * and guessing at a position here would show a fact the asset never stated. */
function buildMassMarker(part) {
  const rig = state.rig.get(part.id);
  const world = part.mass.center_of_mass_world;
  if (!rig || !world) return;

  const local = new THREE.Vector3(...world).sub(rig.anchor);
  const group = new THREE.Group();
  group.position.copy(local);
  group.renderOrder = 11;

  const radius = THREE.MathUtils.clamp(state.labelHeight * 0.35, 0.006, 0.05);
  const marker = new THREE.Mesh(
    new THREE.SphereGeometry(radius, 12, 10),
    new THREE.MeshBasicMaterial({ color: COLOR_MASS, depthTest: false })
  );
  group.add(marker);

  const label = makeTextSprite(massLabelText(part.mass));
  const height = state.labelHeight * 0.85;
  label.position.set(0, height * 1.3, 0);
  label.scale.set(height * (label.userData.aspect ?? 6), height, 1);
  group.add(label);

  rig.pivot.add(group);
  state.massMarkers.set(part.id, group);
}

function massLabelText(mass) {
  return mass.mass_kg != null ? `${mass.mass_kg.toFixed(2)} kg` : "centre of mass";
}

function applyMassMarkers() {
  const show = $("toggleMass").checked;
  for (const group of state.massMarkers.values()) group.visible = show;
}

// ── joint labels in 3D ──────────────────────────────────────────────

/* The joint table on the right is exact but needs cross-referencing: you read
 * a row, then hunt for the part it names. A label pinned to the joint answers
 * the same question where you are already looking.
 *
 * The name and nothing else. The gizmo already states the type by its shape --
 * an arc for a hinge, a track for a slide -- and states the range by the reach
 * of that arc, which is the entire reason it is drawn. Printing either one
 * beside the graphic that exists to replace it only makes the label long
 * enough to cover the part it is pointing at. */
function buildLabel(joint, group, u, reach) {
  const sprite = makeTextSprite(shortName(joint.name));
  sprite.position.copy(u).multiplyScalar(reach * 1.15);
  // One size for the whole asset, not scaled per joint: a label on a button
  // has to be as readable as one on a door, and sizing each to its own part
  // makes the small ones illegible in exactly the cases you need them.
  const height = state.labelHeight;
  sprite.scale.set(height * (sprite.userData.aspect ?? 6), height, 1);
  group.add(sprite);
  state.labels.set(joint.id, sprite);
}

/* Label height in stage units, from the size of the whole asset. The camera
 * frames the asset, so a fixed fraction of it is a roughly fixed fraction of
 * the screen -- which is what "readable" actually depends on. */
function labelHeightFor(meshes) {
  const bounds = new THREE.Box3();
  for (const mesh of meshes) {
    if (mesh.geometry.boundingBox) bounds.union(mesh.geometry.boundingBox);
  }
  if (bounds.isEmpty()) return 0.05;
  return bounds.getSize(new THREE.Vector3()).length() * 0.028;
}

function makeTextSprite(text) {
  const pad = 12;
  const font = 44;
  const measure = document.createElement("canvas").getContext("2d");
  measure.font = `600 ${font}px system-ui, sans-serif`;
  const width = Math.ceil(measure.measureText(text).width) + pad * 2;
  const height = font + pad * 2;

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  context.font = `600 ${font}px system-ui, sans-serif`;
  context.fillStyle = "rgba(12, 16, 24, 0.82)";
  context.fillRect(0, 0, width, height);
  context.fillStyle = "#f0f3f8";
  context.textBaseline = "middle";
  context.fillText(text, pad, height / 2);

  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true })
  );
  sprite.renderOrder = 12;
  sprite.userData.aspect = width / height;
  return sprite;
}

// ── swept volume ────────────────────────────────────────────────────

/* Ghosts of the part at a handful of poses across its range. It answers "how
 * much room does this need" in one still image, which the animation cannot --
 * you would have to watch the whole cycle and remember it.
 *
 * The ghosts share the part's geometry and only differ by transform, so this
 * costs a few draw calls and no memory worth counting. */
function buildEnvelope(joint) {
  const rig = state.rig.get(joint.child_part);
  if (!rig?.mesh) return;
  const parentRig =
    state.rig.get(joint.parent_part) ?? state.rig.get(state.manifest.root_part);
  if (!parentRig) return;

  const group = new THREE.Group();
  group.visible = false;
  parentRig.pivot.add(group);

  const material = new THREE.MeshBasicMaterial({
    color: COLOR_ENVELOPE,
    transparent: true,
    opacity: 0.08,
    depthWrite: false,
    side: THREE.DoubleSide,
  });

  const { lower, upper } = jointSpan(joint);
  for (let i = 0; i < ENVELOPE_SAMPLES; i += 1) {
    const q = lower + ((upper - lower) * i) / (ENVELOPE_SAMPLES - 1);
    const pivot = new THREE.Group();
    pivot.position.copy(rig.restPosition);
    if (joint.type === "revolute") {
      pivot.quaternion.setFromAxisAngle(rig.axis, q);
    } else {
      pivot.position.addScaledVector(rig.axis, q);
    }
    const ghost = new THREE.Mesh(rig.mesh.geometry, material);
    ghost.position.copy(rig.anchor).negate();
    pivot.add(ghost);
    group.add(pivot);
  }

  state.envelopes.set(joint.id, group);
}

function applyEnvelopeVisibility() {
  const show = $("toggleEnvelope").checked;
  for (const group of state.envelopes.values()) group.visible = show;
}

// ── exploded view ───────────────────────────────────────────────────

/* Parts pushed out along the direction from the asset's centre to their own,
 * which is the plainest possible statement of "this is several rigid bodies,
 * not one shape".
 *
 * Applied to the mesh inside its pivot rather than to the pivot itself, so
 * the kinematics and every anchor stay exactly where they were and the view
 * can be toggled mid-pose without disturbing anything. */
function applyExploded() {
  const factor = $("toggleExploded").checked ? EXPLODE_FRACTION : 0;

  // Unioned from the geometry, which is in stage space, because the offsets
  // below are applied in stage space too. The scene bounding box is not:
  // sceneRoot has already rotated it into three.js's Y-up world.
  const bounds = new THREE.Box3();
  for (const mesh of state.meshes) {
    if (mesh.geometry.boundingBox) bounds.union(mesh.geometry.boundingBox);
  }
  if (bounds.isEmpty()) return;
  const centre = bounds.getCenter(new THREE.Vector3());
  const span = bounds.getSize(new THREE.Vector3()).length();

  for (const rig of state.rig.values()) {
    const meshes = [...movablesOf(rig), ...rig.collisionMeshes];
    for (const mesh of meshes) {
      if (!mesh.userData.restOffset) {
        mesh.userData.restOffset = mesh.position.clone();
      }
      mesh.position.copy(mesh.userData.restOffset);
    }
    if (factor === 0) continue;

    // One offset per part, from the whole part's box -- a textured piece only
    // covers the faces of one material, so its own box sits off-centre within
    // the part, and moving each piece by its own direction would fly the
    // part's pieces apart from each other instead of moving it as one body.
    const box = rig.mesh?.geometry.boundingBox;
    if (!box) continue;
    const direction = box.getCenter(new THREE.Vector3()).sub(centre);
    if (direction.lengthSq() < 1e-9) continue;
    const offset = direction.normalize().multiplyScalar(span * factor * 0.5);
    for (const mesh of meshes) mesh.position.add(offset);
  }
}

// ── selection ───────────────────────────────────────────────────────

/* Selection is a part, always. Every joint has exactly one child part, so a
 * joint selection is a part selection with extra steps -- and only the part
 * form can express what has no joint at all, which is the base, every weld,
 * and every part the file forgot to attach. */
function selectPart(partId) {
  state.selectedPartId = partId;
  const joint = partId ? jointOfPart(partId) : null;
  state.selectedJointId = joint ? joint.id : null;
  refreshSelectionVisuals();
  renderInspector();
}

function selectJoint(jointId) {
  const joint = jointId ? state.jointById.get(jointId) : null;
  selectPart(joint ? joint.child_part : null);
}

function jointOfPart(partId) {
  return state.manifest.joints.find((j) => j.child_part === partId) ?? null;
}

function refreshSelectionVisuals() {
  const isolate = $("toggleIsolate").checked;

  const flagFaults = $("toggleFaults").checked;

  // Parts and Wireframe already colour every part differently so it can be
  // told apart from its neighbours -- tinting the selected one blue on top
  // reads as its colour having changed, not as a mark added to it. Texture
  // mode has no such palette to clash with, so it is the one mode this
  // tint earns its keep in. Selection still shows in the other two: the
  // object list row and the joint's own axis gizmo.
  const tintSelection = surfaceMode() === "materials";

  for (const rig of state.rig.values()) {
    const selected = rig.part.id === state.selectedPartId;
    const ghosted = isolate && !selected;
    for (const mesh of meshesOf(rig)) {
      const strength = mesh === rig.mesh ? SELECT_TINT_FLAT : SELECT_TINT_TEXTURED;
      const tint = selected && tintSelection;
      mesh.material.emissive = new THREE.Color(tint ? COLOR_SELECT : 0x000000);
      mesh.material.emissiveIntensity = tint ? strength : 0;
      mesh.material.transparent = ghosted;
      mesh.material.opacity = ghosted ? 0.12 : 1;
      mesh.material.needsUpdate = true;
    }
    // On its own channel, so selecting a rejected part no longer hides that it
    // is one -- the fill says "this is the one you picked", the edge says "the
    // engine will trip over it", and both can be true at once.
    if (rig.outline) {
      rig.outline.visible = flagFaults;
      rig.outline.material.transparent = ghosted;
      rig.outline.material.opacity = ghosted ? 0.12 : 1;
    }
  }

  for (const element of document.querySelectorAll(".object-row")) {
    element.classList.toggle("selected", element.dataset.partId === state.selectedPartId);
  }
  applyGizmoVisibility();
}

function pickAt(event) {
  const rect = renderer.domElement.getBoundingClientRect();
  const pointer = new THREE.Vector2(
    ((event.clientX - rect.left) / rect.width) * 2 - 1,
    -((event.clientY - rect.top) / rect.height) * 2 + 1
  );
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObjects(state.meshes, false);
  return hits.length ? hits[0].object.userData.partId : null;
}

// ── dragging a joint directly in the viewport ───────────────────────

const drag = { active: false, jointId: null, startQ: 0, startParam: 0, uW: null, vW: null, anchorW: null, axisW: null };

/* The joint frame in world (display) space. Recomputed at drag start because
 * a parent joint may have moved since the rig was built. */
function jointWorldFrame(rig) {
  const parent = rig.pivot.parent;
  parent.updateWorldMatrix(true, false);
  const quaternion = new THREE.Quaternion();
  parent.getWorldQuaternion(quaternion);
  return {
    anchorW: rig.anchorLocal.clone().applyMatrix4(parent.matrixWorld),
    axisW: rig.axis.clone().applyQuaternion(quaternion).normalize(),
  };
}

function beginDrag(event, jointId) {
  const joint = state.jointById.get(jointId);
  const rig = state.rig.get(joint.child_part);
  if (!rig) return false;

  const { anchorW, axisW } = jointWorldFrame(rig);
  const basis = axisBasis(rig.axis, rig);
  const parentQuaternion = new THREE.Quaternion();
  rig.pivot.parent.getWorldQuaternion(parentQuaternion);

  drag.uW = basis.u.clone().applyQuaternion(parentQuaternion).normalize();
  drag.vW = basis.v.clone().applyQuaternion(parentQuaternion).normalize();
  drag.anchorW = anchorW;
  drag.axisW = axisW;

  const param = dragParameter(event, joint);
  if (param === null) return false;

  drag.active = true;
  drag.jointId = jointId;
  drag.startQ = state.jointValue.get(jointId) ?? 0;
  drag.startParam = param;
  controls.enabled = false;
  renderer.domElement.style.cursor = "grabbing";
  return true;
}

/* Revolute: the angle of the pointer around the axis, read on the plane the
 * joint rotates in. Prismatic: how far along the axis the pointer is. Both
 * give a scalar that tracks the mouse in the joint's own coordinate. */
function dragParameter(event, joint) {
  const rect = renderer.domElement.getBoundingClientRect();
  const pointer = new THREE.Vector2(
    ((event.clientX - rect.left) / rect.width) * 2 - 1,
    -((event.clientY - rect.top) / rect.height) * 2 + 1
  );
  raycaster.setFromCamera(pointer, camera);
  const ray = raycaster.ray;

  if (joint.type === "revolute") {
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(drag.axisW, drag.anchorW);
    const hit = new THREE.Vector3();
    if (!ray.intersectPlane(plane, hit)) return null;
    const offset = hit.sub(drag.anchorW);
    return Math.atan2(offset.dot(drag.vW), offset.dot(drag.uW));
  }

  // Closest point between the pointer ray and the joint's axis line.
  const w0 = new THREE.Vector3().subVectors(ray.origin, drag.anchorW);
  const b = drag.axisW.dot(ray.direction);
  const denominator = 1 - b * b;
  if (Math.abs(denominator) < 1e-6) return null;
  return (w0.dot(ray.direction) * b - w0.dot(drag.axisW)) / denominator * -1;
}

function updateDrag(event) {
  const joint = state.jointById.get(drag.jointId);
  const param = dragParameter(event, joint);
  if (param === null) return;

  let delta = param - drag.startParam;
  if (joint.type === "revolute") {
    // Keep the shortest way round so crossing the atan2 seam does not make
    // the part jump a full turn.
    while (delta > Math.PI) delta -= 2 * Math.PI;
    while (delta < -Math.PI) delta += 2 * Math.PI;
  }
  setJointValue(drag.jointId, drag.startQ + delta);
}

function endDrag() {
  if (!drag.active) return;
  drag.active = false;
  drag.jointId = null;
  controls.enabled = true;
  renderer.domElement.style.cursor = "";
}

function installViewportInput() {
  const canvas = renderer.domElement;

  canvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    // Touching the viewport hands control back to the reviewer rather than
    // being ignored until the animation finishes.
    cancelAnimation();
    const partId = pickAt(event);
    if (!partId) return;

    selectPart(partId);
    const joint = state.manifest.joints.find((j) => j.child_part === partId);
    if (joint && beginDrag(event, joint.id)) {
      canvas.setPointerCapture(event.pointerId);
      event.preventDefault();
    }
  });

  canvas.addEventListener("pointermove", (event) => {
    if (drag.active) {
      updateDrag(event);
      return;
    }
    if (state.animating) return;
    const partId = pickAt(event);
    const joint = partId && state.manifest.joints.find((j) => j.child_part === partId);
    canvas.style.cursor = joint ? "grab" : partId ? "pointer" : "";
    $("dragHint").textContent = joint
      ? `drag ${shortName(joint.name)} to ${joint.type === "revolute" ? "swing" : "slide"} it`
      : "";
  });

  for (const type of ["pointerup", "pointercancel", "pointerleave"]) {
    canvas.addEventListener(type, endDrag);
  }
}

// ── left panel: one row per part ─────────────────────────────────────

/* Keyed on parts rather than joints because only the complete list can show
 * an absence: a door that ought to swing and carries no joint does not appear
 * in a list of joints at all, and that is the defect a delivery most often
 * has. The rows that do move carry their slider inline, which is also what
 * makes the number of driveable joints readable without counting tags. */
function renderObjectList() {
  const host = $("objectList");
  host.replaceChildren();

  const parts = state.manifest.parts;
  const driveable = parts.filter((part) => jointOfPart(part.id));
  const inert = parts.filter((part) => !jointOfPart(part.id));

  $("partCount").textContent = parts.length
    ? `· ${driveable.length} of ${parts.length} move`
    : "";

  if (!parts.length) {
    host.append(noteElement("No parts were read from this asset."));
    return;
  }
  if (!driveable.length) {
    host.append(noteElement("No joints. This asset is not articulated."));
  }

  for (const part of driveable) host.append(objectRow(part, jointOfPart(part.id)));

  if (inert.length) {
    const label = document.createElement("div");
    label.className = "object-group";
    label.textContent = `Does not move (${inert.length})`;
    host.append(label);
    for (const part of inert) host.append(objectRow(part, null));
  }

  // Sliders read their position from the DOM, so they have to be in it first.
  for (const joint of state.manifest.joints) syncJointControls(joint.id, false);
}

function objectRow(part, joint) {
  const row = document.createElement("div");
  row.className = "object-row";
  row.dataset.partId = part.id;
  if (joint) row.dataset.jointId = joint.id;
  row.onclick = (event) => {
    if (event.target.tagName === "INPUT") return;
    if (state.selectedPartId === part.id) return;
    selectPart(part.id);
  };

  const head = document.createElement("div");
  head.className = "object-head";

  const rig = state.rig.get(part.id);
  const dot = document.createElement("span");
  dot.className = "dot";
  dot.style.background = rig?.mesh ? partSwatch(rig.mesh) : "#444";
  head.append(dot);

  const name = document.createElement("span");
  name.className = "object-name";
  name.textContent = shortName(part.name);
  name.title = part.id;
  head.append(name);

  // Marked where the part is picked, not only where it is inspected: finding
  // the faulty ones should not mean clicking every row in turn.
  if (state.faultsByPart.has(part.id)) {
    const fault = document.createElement("span");
    fault.className = "fault-dot";
    fault.title = "something is wrong with this part";
    head.append(fault);
  }

  const role = partRole(part, joint);
  head.append(badgeElement(role.kind, role.label, role.title));
  if (joint) {
    const driven = joint.drive.is_active;
    head.append(badgeElement(driven ? "driven" : "free", driven ? "driven" : "free"));
  }
  // Same colour function as the Physics Material surface mode, so a reviewer
  // who has spotted two parts sharing a colour in the viewport can confirm it
  // here without switching modes -- and can tell two materials apart even
  // before ever turning that mode on.
  if (part.physics_material.name) {
    const materialDot = document.createElement("span");
    materialDot.className = "dot material-dot";
    materialDot.style.background = physicsMaterialColor(
      physicsMaterialKey(part)
    ).getStyle();
    materialDot.title = physicsMaterialTitle(part.physics_material);
    head.append(materialDot);
  }
  row.append(head);

  if (joint) appendJointControls(row, joint);
  return row;
}

/* What holds this part in place. "fixed" is a weld the asset authored on
 * purpose; a part with neither a joint nor a weld is attached to nothing at
 * all, which is a different fact and must not wear the same word. */
function partRole(part, joint) {
  if (part.is_root) {
    return {
      kind: "base",
      label: "base",
      title: "the part everything else hangs off",
    };
  }
  if (joint) return { kind: joint.type, label: joint.type, title: joint.type };
  if ((state.manifest.fixed_joints ?? []).some((f) => f.child_part === part.id)) {
    return {
      kind: "fixed",
      label: "fixed",
      title: "welded to its parent — moves with it, no travel of its own",
    };
  }
  return {
    kind: "loose",
    label: "loose",
    title: "no joint and no weld: nothing in the file attaches this part",
  };
}

function badgeElement(kind, label, title, interactive = false) {
  const badge = document.createElement(interactive ? "button" : "span");
  badge.className = `badge ${kind}`;
  if (interactive) badge.type = "button";
  badge.textContent = label;
  if (title) badge.title = title;
  return badge;
}

/* The slider stays on the row rather than moving to the inspector with the
 * rest of the joint's facts: driving four doors in turn should not cost four
 * selections, and the live value belongs next to the control that changes it.
 * What moves right is everything the file states and nothing that moves. */
function appendJointControls(row, joint) {
  const { lower, upper } = jointSpan(joint);

  const controls = document.createElement("div");
  controls.className = "joint-controls";

  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = "0";
  slider.max = String(SLIDER_STEPS);
  slider.value = String(((0 - lower) / (upper - lower || 1)) * SLIDER_STEPS);
  slider.dataset.jointId = joint.id;
  slider.oninput = () => {
    // Keep the value the reviewer just dialled in; resetting here would
    // snap the slider back out from under them.
    cancelAnimation({ reset: false });
    const fraction = Number(slider.value) / SLIDER_STEPS;
    setJointValue(joint.id, lower + fraction * (upper - lower));
    if (state.selectedJointId !== joint.id) selectJoint(joint.id);
  };
  controls.append(slider);

  const readout = document.createElement("span");
  readout.className = "readout";
  readout.dataset.readoutFor = joint.id;
  controls.append(readout);
  row.append(controls);

  const labels = document.createElement("div");
  labels.className = "range-labels";
  // A patched slider has to say so where the slider is, not only in the
  // report: this is the one place someone forms a belief about how far the
  // part travels, and an unlabelled override would plant a wrong one.
  const override = joint.override_limits;
  const middle = override
    ? `overridden, file says ${formatAuthoredRange(joint.limits)}`
    : joint.limits.authored
      ? ""
      : "no authored stop";
  labels.innerHTML =
    `<span>${formatValue(joint, lower)}</span>` +
    `<span${override ? ' class="overridden"' : ""}>${middle}</span>` +
    `<span>${formatValue(joint, upper)}</span>`;
  row.append(labels);
}

/* Read the part's colour straight off the geometry so the tree swatch and the
 * viewport cannot disagree. glTF stores vertex colours linear; CSS wants
 * sRGB, and skipping the conversion leaves every swatch looking near-black. */
function partSwatch(mesh) {
  const colors = mesh.geometry.getAttribute("color");
  if (!colors) return "#9aa6b8";
  const color = new THREE.Color(colors.getX(0), colors.getY(0), colors.getZ(0));
  return `#${color.convertLinearToSRGB().getHexString()}`;
}

function physicsMaterialTitle(material) {
  const numbers = [
    ["static friction", material.static_friction],
    ["dynamic friction", material.dynamic_friction],
    ["restitution", material.restitution],
  ]
    .filter(([, value]) => value != null)
    .map(([label, value]) => `${label} ${value.toFixed(2)}`);
  return numbers.length ? `${material.name} — ${numbers.join(", ")}` : material.name;
}

/* Every part in a delivery tends to be prefixed with the asset name, which
 * pushes the only distinguishing part of the label out of a narrow column and
 * leaves two rows reading identically. The full name stays in the tooltip. */
function shortName(name) {
  const prefix = `${state.assetKey}_`;
  return name.startsWith(prefix) && name.length > prefix.length
    ? name.slice(prefix.length)
    : name;
}

function escapeHtml(text) {
  const holder = document.createElement("span");
  holder.textContent = text;
  return holder.innerHTML;
}

/* A prim path holds no spaces, so a narrow column leaves the layout engine
 * two options: overflow, or break mid-word -- which is how
 * `Cabinet_Door001` ends up split as `Cabinet_Do` and
 * `or001`. Marking the separators as break opportunities means every
 * fragment on screen is still a real path component. */
function breakablePath(path) {
  return escapeHtml(path).replace(/([/_])/g, "$1<wbr>");
}

function syncJointControls(jointId, hitLimit) {
  const joint = state.jointById.get(jointId);
  const q = state.jointValue.get(jointId) ?? 0;
  const { lower, upper } = jointSpan(joint);

  const slider = document.querySelector(`input[data-joint-id="${CSS.escape(jointId)}"]`);
  if (slider && document.activeElement !== slider) {
    slider.value = String(((q - lower) / (upper - lower || 1)) * SLIDER_STEPS);
  }

  const readout = document.querySelector(`[data-readout-for="${CSS.escape(jointId)}"]`);
  if (readout) {
    readout.textContent = formatValue(joint, q);
    if (hitLimit) {
      readout.classList.add("at-limit");
      clearTimeout(readout.dataset.timer);
      readout.dataset.timer = setTimeout(
        () => readout.classList.remove("at-limit"),
        LIMIT_FLASH_MS
      );
    }
  }

}

// ── inspector: everything about the one thing selected ──────────────

/* Keyed on the part, not the joint. Mass, rigid-body status and face count
 * are properties of a part, and hanging them off a joint left them
 * unreachable for the three kinds of part that have none: the base, every
 * weld, and every part the file attached to nothing -- which is the one a
 * reviewer most wants to look at.
 *
 * Rows follow one rule: state the exception, stay quiet on the rule. A unit
 * quaternion, an axis-aligned axis and a joint modelled at its zero are the
 * normal case, and printing them is asking the reader to check four numbers
 * to learn nothing. They are not dropped -- reconciling a delivery against
 * usdview needs the number even when it is the expected one -- they move
 * into the collapsed raw block at the bottom. */
function renderInspector() {
  const host = $("inspector");
  host.replaceChildren();

  const part = state.selectedPartId
    ? state.manifest.parts.find((p) => p.id === state.selectedPartId)
    : null;

  if (!part) {
    const empty = document.createElement("p");
    empty.className = "inspector-empty";
    empty.textContent =
      "Pick a part on the left, or click one in the 3D view.";
    host.append(empty);
    return;
  }

  const joint = jointOfPart(part.id);

  const title = document.createElement("div");
  title.className = "inspector-title";
  const name = document.createElement("span");
  name.className = "object-name";
  name.textContent = shortName(part.name);
  name.title = part.id;
  const role = partRole(part, joint);
  title.append(name, badgeElement(role.kind, role.label, role.title));
  // The Inspector states what this part is, it does not re-litigate what is
  // wrong with it -- that is the report's job. A part with a fault gets one
  // badge pointing there, not a second copy of the finding.
  const badge = faultBadge(part, joint);
  if (badge) title.append(badge);
  host.append(title);

  // Grouped by what each is about, not by how important it is: everything
  // about the part together, everything about the joint together, each
  // ending in its own "more" rather than all the overflow of both meeting at
  // the bottom in one block a reader has to split back apart themselves.
  host.append(subheadElement("part"), kvTable(partRows(part)), partRawDetails(part));
  if (joint) {
    host.append(
      subheadElement("joint"),
      kvTable(jointRows(joint)),
      jointRawDetails(joint)
    );
  }
}

function partRows(part) {
  return [
    // Always shown even when absent: a part a physics engine has to guess the
    // mass of is a defect, and silence here would read as "nothing to say".
    ["mass", part.mass.mass_authored ? `${part.mass.mass_kg} kg` : "not authored"],
    ["faces", part.visual_face_count],
    ["rigid body", part.is_rigid_body ? null : "no — static anchor"],
    ["geometry", geometryNote(part)],
  ];
}

function jointRows(joint) {
  const parent = state.manifest.parts.find((p) => p.id === joint.parent_part);
  const limits = joint.limits;
  const authored = formatAuthoredRange(limits);

  return [
    // Nearly every part hangs off the base, so saying so on every joint says
    // nothing; a joint against anything else is worth a line.
    [
      "relative to",
      parent && parent.id !== state.manifest.root_part
        ? shortName(parent.name)
        : null,
    ],
    ["anchor (m)", vectorText(joint.anchor_world)],
    [
      "orientation (w,x,y,z)",
      isIdentityQuat(joint.frame_quat_world)
        ? null
        : vectorText(joint.frame_quat_world),
    ],
    ["modelled at zero", restPoseFault(joint)],
    ["axis", joint.axis_token],
    [
      "axis (world)",
      isAxisAligned(joint.axis_world) ? null : vectorText(joint.axis_world),
    ],
    [`range (${rawUnitLabel(limits.raw_unit)})`, authored],
    ["drive", driveNote(joint)],
    // A body1 relationship naming a prim with no rigid-body schema cannot be
    // driven by an engine as authored, however this reader resolved it. See
    // Joint.child_attachment_raw_path in app/models.py.
    [
      "body1 names",
      joint.child_attachment_raw_path
        ? pathElement(joint.child_attachment_raw_path)
        : null,
    ],
    ["prim", pathElement(joint.prim_path)],
  ];
}

/* The whole point of the integration: a verdict is only useful next to the
 * thing it is about. Being told five prim paths violate JT.002 is not the
 * same as opening a door and reading why that door will not move. */
/* Both modalities that mean something is wrong, not only the engine's: a
 * body1 relationship naming a prim with no rigid body is a defect this reader
 * found on an asset NVIDIA passes, and an inspector that showed one and not
 * the other would be the same split the report just stopped making. */
/* A pointer, not a copy. The report already states each fault once, in full
 * -- restating it here just because this part happens to be selected made the
 * Inspector a second report with the same content in a plainer typeface.
 * This carries only the count and a way to the original. */
function faultBadge(part, joint) {
  const count = findings().filter(
    (finding) =>
      FAULT_MODALITIES.has(finding.modality) &&
      (finding.part_id === part.id ||
        (joint && finding.joint_id === joint.id))
  ).length;
  if (!count) return null;

  const badge = badgeElement(
    "blocking",
    count === 1 ? "1 error" : `${count} errors`,
    "in the report — click to open it",
    /* interactive */ true
  );
  badge.addEventListener("click", () => jumpToReportFault(part.id));
  return badge;
}

/* The reverse of clicking a part's name inside a report card: open the
 * section that grades the delivery and land on the specific row that names
 * this part, rather than leaving the reader to find it among however many
 * others share the section. */
function jumpToReportFault(partId) {
  setDockOpen(true);
  const section = $("errorsSection");
  if (!section) return;
  section.open = true;
  section.scrollIntoView({ behavior: "smooth", block: "start" });

  const row = [...section.querySelectorAll(".finding-row")].find((el) =>
    (el.dataset.parts ?? "").split(",").includes(partId)
  );
  if (!row) return;
  // After the section itself has settled, not before -- scrolling to both at
  // once fights over where the viewport ends up.
  window.setTimeout(() => {
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.add("jump-target");
    window.setTimeout(() => row.classList.remove("jump-target"), 1600);
  }, 350);
}

/* One "more" per subject, right where that subject's own summary ends, not
 * one shared appendix for both at the bottom of the panel. A reader wanting
 * everything about the joint should not have to split a combined block back
 * into part and joint themselves after opening it.
 *
 * Every row in both is a field the summary never shows, or the exception one
 * only when the summary is *not* the one showing it -- each fact has exactly
 * one home. A field the summary always states regardless of exception (mass,
 * anchor, axis, range, a driven target) is not repeated here at all. */
function rawDetails(rows) {
  const block = document.createElement("details");
  block.className = "raw-values";
  const summary = document.createElement("summary");
  summary.textContent = "more";
  block.append(summary, kvTable(rows));
  return block;
}

function partRawDetails(part) {
  const mass = part.mass;
  return rawDetails([
    ["id", pathElement(part.id)],
    ["glb node", part.node_name],
    // The false case is already the summary's exception line ("no — static
    // anchor"); this states the quiet case the summary stays silent on.
    ["rigid body", part.is_rigid_body ? "yes" : null],
    [
      "centre of mass",
      mass.center_of_mass_authored
        ? vectorText(mass.center_of_mass)
        : "not authored",
    ],
    ["bbox min", part.bbox_min ? vectorText(part.bbox_min) : "—"],
    ["bbox max", part.bbox_max ? vectorText(part.bbox_max) : "—"],
    ["collision", collisionNote(part.collision)],
    ["physics material", physicsMaterialNote(part.physics_material)],
  ]);
}

function physicsMaterialNote(material) {
  if (!material.name) return "not authored";
  const numbers = [
    ["static friction", material.static_friction],
    ["dynamic friction", material.dynamic_friction],
    ["restitution", material.restitution],
  ]
    .filter(([, value]) => value != null)
    .map(([label, value]) => `${label} ${value.toFixed(2)}`);
  return numbers.length ? `${material.name} (${numbers.join(", ")})` : material.name;
}

/* The one case the "Collision geometry" overlay itself cannot show: a part
 * whose collider is its own visual mesh has no second shell to toggle on --
 * what is already on screen *is* what the engine collides against, and that
 * fact only has a home here. */
function collisionNote(collision) {
  if (!collision.has_collision) return "not authored";
  const approx = collision.approximation ? ` (${collision.approximation})` : "";
  return collision.shares_visual_geometry
    ? `same geometry as visual${approx}`
    : `separate hull${approx}`;
}

function jointRawDetails(joint) {
  const drive = joint.drive;
  const limits = joint.limits;
  const gain = (value) => (value === null || value === undefined
    ? "not authored"
    : value);
  // The summary already names the gains once it has a target to report
  // alongside them; short of that, they only ever lived here.
  const driveSummarised = drive.present && drive.is_active;
  // A joint with no drive schema at all has told its whole story in "none —
  // free joint"; six rows of "not authored" under that would repeat the one
  // fact six times rather than add to it. Only once a schema exists does
  // which of its fields were and were not filled in become its own question.
  const driveRow = (value) => (drive.present ? gain(value) : null);

  return rawDetails([
    // Not `id`: `Joint.id` and `Joint.prim_path` are the same string by
    // construction in every reader, and the summary already shows it,
    // labelled `prim`.
    ["type", joint.type],
    ["parent part", pathElement(joint.parent_part)],
    // Not `child part`: the Inspector only ever shows the joint whose
    // child *is* the part on screen, so this would always equal that
    // part's own `id`, in its own "more" just above.
    [
      "orientation (w,x,y,z)",
      isIdentityQuat(joint.frame_quat_world)
        ? vectorText(joint.frame_quat_world)
        : null,
    ],
    [
      "axis (world)",
      isAxisAligned(joint.axis_world) ? vectorText(joint.axis_world) : null,
    ],
    ["limit lower (SI)", limits.lower ?? "unbounded"],
    ["limit upper (SI)", limits.upper ?? "unbounded"],
    ["limits authored", limits.authored ? "yes" : "no"],
    [
      "rest frame offset",
      restPoseFault(joint) === null
        ? `${(joint.rest_frame_offset_m * 1000).toFixed(2)} mm / ` +
          `${joint.rest_frame_offset_deg.toFixed(2)}°`
        : null,
    ],
    ["drive type", driveRow(drive.drive_type)],
    ["stiffness", driveSummarised ? null : driveRow(drive.stiffness)],
    ["damping", driveSummarised ? null : driveRow(drive.damping)],
    ["max force", driveRow(drive.max_force)],
    [
      "target position",
      driveSummarised ? null : driveRow(drive.target_position),
    ],
    ["target velocity", driveRow(drive.target_velocity)],
  ]);
}

function kvTable(rows) {
  const table = document.createElement("table");
  table.className = "kv";
  for (const [label, value] of rows) {
    if (value === null || value === undefined) continue;
    const row = document.createElement("tr");
    const key = document.createElement("td");
    key.textContent = label;
    const cell = document.createElement("td");
    if (value instanceof Node) cell.append(value);
    else cell.textContent = String(value);
    row.append(key, cell);
    table.append(row);
  }
  return table;
}

function subheadElement(text) {
  const head = document.createElement("div");
  head.className = "subhead";
  head.textContent = text;
  return head;
}

function pathElement(path) {
  const cell = document.createElement("span");
  cell.innerHTML = breakablePath(path);
  return cell;
}

function vectorText(values) {
  return values.map((component) => component.toFixed(4)).join(", ");
}

// What one stage unit is worth, for the metres-per-unit a stage is likely to
// declare. Exact float lookups: the same decimal literal parses to the same
// double here as it did in the JSON.
const STAGE_UNIT_NAMES = new Map([
  [1, "m"],
  [0.01, "cm"],
  [0.001, "mm"],
  [0.0254, "in"],
  [0.3048, "ft"],
]);

/* The schema names the unit with an enum token, but "stage units" is not a
 * unit anyone can do anything with -- what one is worth depends on the stage's
 * metres-per-unit. Resolved here, on the row it labels, so the number and the
 * unit it is in arrive together instead of the reader holding a scale factor
 * from elsewhere in their head. */
function rawUnitLabel(unit) {
  if (unit === "degree") return "deg";
  if (unit === "radian") return "rad";
  const scale = state.manifest.stage_meters_per_unit;
  return STAGE_UNIT_NAMES.get(scale) ?? `units of ${scale} m`;
}

function isIdentityQuat(quat) {
  return (
    Math.abs(Math.abs(quat[0]) - 1) < 1e-4 &&
    quat.slice(1).every((component) => Math.abs(component) < 1e-4)
  );
}

/* A free axis of exactly ±X, ±Y or ±Z, which is what the source token already
 * said. Anything else is a joint whose axis was rotated by a transform on the
 * way to world space, and then the vector is the only place that shows it. */
function isAxisAligned(axis) {
  const ones = axis.filter((c) => Math.abs(Math.abs(c) - 1) < 1e-4).length;
  const zeros = axis.filter((c) => Math.abs(c) < 1e-4).length;
  return ones === 1 && zeros === 2;
}

/* Whether the delivered geometry sits at this joint's zero. Silent when it
 * does; when it does not, every position reported for the joint is measured
 * from a different origin than the shape suggests, which the reviewer has to
 * be told rather than left to assume. */
function restPoseFault(joint) {
  const millimetres = joint.rest_frame_offset_m * 1000;
  const degrees = joint.rest_frame_offset_deg;
  if (millimetres <= 1 && degrees <= 0.5) return null;
  return `NO — frames ${millimetres.toFixed(1)} mm / ${degrees.toFixed(1)}° apart`;
}

/* One line. A drive that is applied with zero stiffness and zero damping is a
 * free hinge, and saying so takes fewer words than listing the two zeros that
 * make it one. The gains themselves are in the raw block once this is not the
 * summary that already stated them (see rawValuesBlock).
 *
 * The target leads the gains rather than following them: where a drive
 * commands a part to go is what a reviewer acts on, the PhysX tuning behind
 * it is not. It is flagged, not just quoted, when it sits outside the
 * joint's own authored range -- a drive cannot reach a target its own file
 * says the joint cannot move to. */
function driveNote(joint) {
  const drive = joint.drive;
  if (!drive.present) return "none — free joint";
  if (!drive.is_active) return "applied but inert — behaves as a free hinge";
  const gain = (value) => (value === null || value === undefined
    ? "not authored"
    : value);

  const target = drive.target_position;
  let targetText = "";
  if (target !== null && target !== undefined) {
    const { lower, upper } = joint.limits;
    const outOfRange =
      (lower !== null && lower !== undefined && target < lower) ||
      (upper !== null && upper !== undefined && target > upper);
    targetText = outOfRange
      ? `, targets ${formatValue(joint, target)} — outside its own limit`
      : `, targets ${formatValue(joint, target)}`;
  }

  return (
    `driven${targetText} — stiffness ${gain(drive.stiffness)}, ` +
    `damping ${gain(drive.damping)}`
  );
}

/* The manifest describes the file; the export findings describe the GLB
 * actually on screen. A part decimated past its budget looks authored unless
 * the part itself says otherwise. */
function geometryNote(part) {
  const marker = `part '${part.name}'`;
  const hits = findings()
    .filter((f) => f.id === "mesh.export" && f.detail.includes(marker))
    .map((f) => f.detail.replace(`${marker} `, ""));
  return hits.length ? hits.join(" · ") : null;
}

// ── drop-to-add ─────────────────────────────────────────────────────

/* Taking delivery is copying a folder into the library, so the browser only
 * has to hand the bytes over and re-read the list. Dropping a folder itself
 * is not offered: the browser gives no reliable way to keep a USD together
 * with the textures beside it, and a silently texture-less asset would be
 * worse than asking for a zip. */
function installDropToAdd() {
  // Click-to-browse needs no JS: the label's `for` targets the hidden input
  // natively. Only the drag styling and the drop itself are wired here.
  const input = $("uploadInput");
  input.onchange = async () => {
    const file = input.files[0];
    input.value = "";
    if (file) await uploadAsset(file);
  };

  const dropZone = $("assetDrop");
  // Owns any drop that lands on it -- stopPropagation keeps it from also
  // triggering the whole-window veil below for the same file.
  for (const type of ["dragenter", "dragover"]) {
    dropZone.addEventListener(type, (event) => {
      event.preventDefault();
      event.stopPropagation();
      dropZone.classList.add("drag");
    });
  }
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag"));
  dropZone.addEventListener("drop", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    dropZone.classList.remove("drag");
    const file = event.dataTransfer.files[0];
    if (file) await uploadAsset(file);
  });

  // Whole-window fallback: a delivery dropped anywhere else on the page
  // still lands, for anyone who does not go looking for the zone above.
  const veil = $("dropVeil");
  let depth = 0;

  const hide = () => {
    depth = 0;
    veil.hidden = true;
  };

  window.addEventListener("dragenter", (event) => {
    if (![...event.dataTransfer.types].includes("Files")) return;
    depth += 1;
    veil.hidden = false;
  });
  // dragenter/dragleave fire per element crossed, so count them rather than
  // hiding on the first leave and flickering over every child.
  window.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) veil.hidden = true;
  });
  window.addEventListener("dragover", (event) => event.preventDefault());

  window.addEventListener("drop", async (event) => {
    event.preventDefault();
    hide();
    const file = event.dataTransfer.files[0];
    if (file) await uploadAsset(file);
  });
}

async function uploadAsset(file) {
  setOverlay(`Adding ${file.name}…`);
  document.body.dataset.upload = "busy";

  const body = new FormData();
  body.append("file", file);

  try {
    const response = await fetch("/api/assets", { method: "POST", body });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.detail ?? `upload failed (${response.status})`);
    }
    document.body.dataset.upload = "done";
    await loadAssetList(result.key);
  } catch (error) {
    document.body.dataset.upload = "failed";
    setOverlay(`Could not add ${file.name}.\n\n${error.message}`);
  }
}

/* Say the whole of it in the headline and put the evidence one click away.
 *
 * These banners sit over the viewport, and the evidence is prim paths: four
 * near-identical lines naming four shaders is a paragraph of near-duplicate
 * text covering a fifth of the 3D view, which is the thing the reviewer came
 * for. The count and the consequence are what has to be unmissable; the
 * paths are what you need only once you have decided to go and fix it. */
function fillBanner(banner, headline, tail, items) {
  banner.replaceChildren();

  const lead = document.createElement("b");
  lead.textContent = headline;
  banner.append(lead);
  if (tail) banner.append(document.createTextNode(` ${tail}`));

  const disclosure = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = items.length > 1 ? `${items.length} details` : "details";
  disclosure.append(summary);

  const list = document.createElement("ul");
  for (const text of items) {
    const item = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = text;
    item.append(code);
    list.append(item);
  }
  disclosure.append(list);
  banner.append(disclosure);
  banner.hidden = false;
}

/* A banner promotes a finding; it never originates one. Each of the three
 * below is already in the report under a section of its own, and is repeated
 * here because the report is collapsible and these three change what is on
 * screen: reading them should not depend on having opened anything. Anything
 * that is only ever a banner ends up missing from the downloaded report, which
 * is exactly what happened to the up axis. */
function renderBanners() {
  renderUpAxisBanner();
  renderBannerFor("unreadBanner", "reader.unread_joint", (details) => [
    `${details.length} joint${details.length > 1 ? "s" : ""} in this asset ` +
      `could not be read.`,
    `The parts tree is missing ${details.length > 1 ? "them" : "it"} — a ` +
      `limit of this viewer, not something the asset failed to state.`,
  ]);
  renderBannerFor("geometryBanner", "mesh.export", (details) => [
    "What is on screen is not exactly what was authored.",
    `The 3D view had to change this asset's geometry in ${details.length} ` +
      `place${details.length > 1 ? "s" : ""} to display it.`,
  ]);
}

function renderBannerFor(bannerId, findingId, headlines) {
  const banner = $(bannerId);
  const details = findings()
    .filter((f) => f.id === findingId)
    .map((f) => f.detail);

  if (!details.length) {
    banner.hidden = true;
    banner.replaceChildren();
    return;
  }
  const [headline, tail] = headlines(details);
  fillBanner(banner, headline, tail, details);
}

/* The viewer turns stage space into three.js's Y-up world with one fixed
 * rotation (see the sceneRoot comment in initViewer), so it assumes Z-up
 * unconditionally and a stage that is not Z-up renders lying on its side.
 * Worded here for what it does to the picture; the report states the same
 * fact about the file. */
function renderUpAxisBanner() {
  const banner = $("upAxisBanner");
  const axis = state.manifest.stage_up_axis;

  if (axis === "Z") {
    banner.hidden = true;
    banner.replaceChildren();
    return;
  }

  const lead = document.createElement("b");
  lead.textContent = `This asset declares ${axis}-up.`;
  banner.replaceChildren(
    lead,
    document.createTextNode(
      " The 3D view assumes the Z-up convention Isaac and USD physics use, so " +
        "what is on screen is lying on its side. The numbers in the panels are " +
        "read from the file and are unaffected."
    )
  );
  banner.hidden = false;
}

// ── the report: everything the tool has to say, in one vocabulary ───

/* Sections are what kind of statement a finding is, never which check
 * produced it. Provenance is real and worth showing -- it is how a reader
 * decides how far to trust a line -- but it is not structure: NVIDIA's 66
 * primvar advisories weigh less than one self-contradiction found here, and a
 * layout split by source ranked them the other way round. It rides on the
 * card instead.
 *
 * Coarser than the six modalities behind it, deliberately. Error / warning /
 * note is what every compiler, linter and SARIF consumer already means by
 * those words, and nobody should have to learn a private vocabulary to read a
 * report. The finer grain stays in the data, where export and filtering can
 * still reach it. Declarations sit outside the severity ladder entirely
 * because they are not a severity -- they are what the asset says about
 * itself. See app/findings.py. */
const SECTIONS = [
  {
    key: "errors",
    modalities: ["rejects", "contradicts"],
    label: "source",
    title: "Errors",
    note:
      "A physics engine refuses these, or the file cannot be satisfied as " +
      "written. No agreed bar is needed to call them wrong.",
  },
  {
    key: "warnings",
    modalities: ["advises"],
    label: "source",
    title: "Warnings",
    note: "Authored, workable, and outside what is ordinarily seen.",
  },
  {
    /* The one section whose cards are labelled by modality. Every declaration
       comes from the file itself, so attributing them would print the heading
       again on every row; what separates them is stated from left out. */
    key: "declarations",
    modalities: ["omits", "states"],
    label: "modality",
    title: "Declarations",
    note:
      "One dimension an articulated asset can vary along per line, and what " +
      "this file says on it. Not a verdict — there is no agreed bar yet.",
  },
  {
    key: "notes",
    modalities: ["limits"],
    label: "source",
    title: "Notes",
    note:
      "About this reader and this viewer, not about the asset — so none of " +
      "it counts against the delivery.",
  },
];

const SOURCE_NAMES = {
  nvidia: "NVIDIA's Omni Asset Validator",
  manifest: "what this file declares",
  references: "the reference portability check",
  reader: "this reader",
  mesh: "the geometry exporter",
};

/* The two modalities that mean something is actually wrong. An advisory is
 * survivable by definition and a tool limit is not the asset's doing, so
 * neither one marks a part in the viewport. */
const FAULT_MODALITIES = new Set(["rejects", "contradicts"]);

function findings() {
  return state.report?.findings ?? [];
}

function countOf(modality) {
  return state.report?.counts?.[modality] ?? 0;
}

function groupFaultsByPart(report) {
  const byPart = new Map();
  for (const finding of report.findings ?? []) {
    if (!FAULT_MODALITIES.has(finding.modality) || !finding.part_id) continue;
    if (!byPart.has(finding.part_id)) byPart.set(finding.part_id, []);
    byPart.get(finding.part_id).push(finding);
  }
  return byPart;
}

function renderFindings() {
  const panel = $("findingsPanel");
  panel.replaceChildren();

  /* Nothing found and nothing run look identical in an empty list, and only
   * the first means anything passed. Rules that did not run say so as a
   * finding of their own, so this speaks only for the ones that did. */
  const engine = state.report?.engine ?? {};
  if (engine.ran && !countOf("rejects")) {
    panel.append(pillWrap(pillElement("clean", "Engine rules passed")));
  }

  /* Anything that says the asset is wrong opens. So does the first section of
   * whatever is left when nothing is wrong -- a clean delivery should land on
   * something rather than on a column of shut headings. */
  let opened = false;
  for (const section of SECTIONS) {
    const mine = findings().filter((f) =>
      section.modalities.includes(f.modality)
    );
    if (!mine.length) continue;
    const open =
      section.modalities.some((m) => FAULT_MODALITIES.has(m)) || !opened;
    opened = opened || open;
    panel.append(sectionElement(section, mine, open));
  }
}

function sectionElement(section, sectionFindings, open) {
  const element = document.createElement("details");
  element.className = "section";
  element.id = `${section.key}Section`;
  element.open = open;

  const summary = document.createElement("summary");
  summary.textContent = section.title;
  const count = document.createElement("span");
  count.className = "section-count";
  count.textContent = sectionFindings.length;
  summary.append(count);
  element.append(summary, noteElement(section.note));

  const byDimension = new Map();
  for (const finding of sectionFindings) {
    if (!byDimension.has(finding.dimension)) {
      byDimension.set(finding.dimension, []);
    }
    byDimension.get(finding.dimension).push(finding);
  }

  for (const [dimension, group] of byDimension) {
    const block = document.createElement("div");
    /* A validator rule name is an identifier, not a noun phrase: it is
       CamelCase so it can be grepped against NVIDIA's documentation, and
       uppercasing it the way the other headings are uppercased destroys the
       only word boundaries it has. */
    block.className = group[0].rule ? "dimension identifier" : "dimension";
    const heading = document.createElement("h3");
    heading.textContent = dimension;
    block.append(heading);
    for (const like of groupLikeFindings(group)) {
      block.append(findingCard(like, section.label));
    }
    element.append(block);
  }
  return element;
}

/* Five doors that each swing 90° are one fact with five subjects, not five
 * findings, and a single validator rule fires 66 times on one cabinet. The
 * repetition is not information. The subjects are, so they stay one click
 * away while the dimension still reads at a glance. */
function groupLikeFindings(group) {
  const byKey = new Map();
  for (const finding of group) {
    const key = `${finding.id}|${finding.modality}`;
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(finding);
  }
  return [...byKey.values()];
}

function findingCard(items, labelBy) {
  const [first, ...rest] = items;
  const card = document.createElement("div");
  card.className = `finding-row ${first.modality}`;

  const label = document.createElement("span");
  label.className = "finding";
  label.textContent = labelBy === "source" ? first.source : first.modality;
  // Attribution stays reachable even where the label shows something else.
  label.title = `reported by ${SOURCE_NAMES[first.source] ?? first.source}`;

  const body = document.createElement("div");
  body.className = "finding-body";
  body.append(detailElement(first));
  if (rest.length) {
    const more = document.createElement("details");
    more.className = "finding-more";
    const summary = document.createElement("summary");
    summary.textContent = `${rest.length} more`;
    more.append(summary, ...rest.map(detailElement));
    body.append(more);
  }

  /* The way out of the report is onto the object, so the parts a finding
   * concerns sit inside its own card rather than in a list of their own. A
   * joint is an edge with nothing in the scene to select, which is why every
   * joint finding carries its child part too. */
  const parts = [...new Set(items.map((f) => f.part_id).filter(Boolean))];
  if (parts.length) {
    body.append(affectedPartsRow(parts));
    // The reverse direction: a badge in the Inspector jumps back to whichever
    // card names the part it is looking at.
    card.dataset.parts = parts.join(",");
  }

  card.append(label, body);
  return card;
}

function detailElement(finding) {
  const element = document.createElement("span");
  element.className = "finding-detail";
  element.textContent = finding.detail;
  return element;
}

function affectedPartsRow(partIds) {
  const row = document.createElement("div");
  row.className = "affected-parts";

  if (partIds.length > 1) {
    const lead = document.createElement("span");
    lead.textContent = `${partIds.length} parts: `;
    row.append(lead);
  }

  for (const partId of partIds) {
    const part = state.manifest.parts.find((p) => p.id === partId);
    const button = document.createElement("button");
    button.className = "link";
    // Short, like everywhere else: five full names each wrap onto their own
    // line and the repeated asset prefix is the widest part of every one.
    button.textContent = part ? shortName(part.name) : partId;
    button.title = part ? part.name : partId;
    button.addEventListener("click", () => {
      noteActivity();
      selectPart(partId);
    });
    row.append(button);
  }
  return row;
}

function noteElement(text) {
  const element = document.createElement("p");
  element.className = "panel-note";
  element.textContent = text ?? "";
  return element;
}

function pillWrap(pill) {
  const row = document.createElement("div");
  row.className = "pill-row";
  row.append(pill);
  return row;
}

function pillElement(kind, text) {
  const pill = document.createElement("span");
  pill.className = `pill ${kind}`;
  pill.textContent = text;
  return pill;
}

// Reached without the validator having had its say. Each still names the
// engine, so the bar reads as an answer to the same question either way.
const ENGINE_STALLED = {
  not_applicable: "Engine not checked",
  unavailable: "Engine check unavailable",
  failed: "Engine check failed",
};

/* This is the dock with the report collapsed, so it is the one thing on screen
 * that never scrolls away, and it is read left to right: the verdict, then
 * whatever else is worth a glance without opening anything. Every row is the
 * same shape -- a chip, coloured the way the report already colours that same
 * modality (see `.finding-row.*` in app.css) -- so the strip speaks the
 * report's language rather than inventing a second one for the collapsed
 * state. Each row opens the section it summarises, so the strip is a way in
 * as well as an answer.
 *
 * A row earns its place only by saying something the others do not. A
 * contradiction already turns the verdict red, so "Declarations" stays quiet
 * about it rather than repeating the same fact in a second colour; an asset
 * with nothing left out says nothing here either, since there is no news in
 * confirming a file is as complete as it claims. What is left, an omission,
 * is the one case where this row is the only place that count is visible.
 *
 * The stage's own conventions -- source format, units, up axis -- used to
 * lead this bar and have gone to where each is actually used: the source
 * format already raises a finding of its own when it is not USD, metres-per-
 * unit is the key to the raw rows and now sits with them, and a stage that is
 * not Z-up is a banner over the picture it spoils rather than a grey token at
 * the far end. */
function renderStatusStrip() {
  const strip = $("statusStrip");
  strip.replaceChildren(
    ...[engineVerdict(), warningsRow(), declarationsRow()].filter(Boolean)
  );
}

function engineVerdict() {
  const row = stripRow("errorsSection");
  const stalled = ENGINE_STALLED[state.report?.engine?.status];
  if (stalled) {
    row.append(verdictChip("nochecks", stalled));
    return row;
  }

  const errors = countOf("rejects") + countOf("contradicts");
  row.append(
    errors
      ? verdictChip("blocking", "Errors", errors)
      : verdictChip("clean", "No errors")
  );
  return row;
}

/* Same chip, same shape, coloured the way `.finding-row.advises` already is
 * everywhere else in the report -- worth a look, never a reason to reject. */
function warningsRow() {
  const count = countOf("advises");
  if (!count) return null;
  const row = stripRow("warningsSection");
  row.append(verdictChip("advisory", "Warnings", count));
  return row;
}

/* The one Declarations state that is not already told by the verdict above:
 * a gap the file leaves for a consumer to fill in. Coloured like
 * `.finding-row.omits`, the same fact wearing the same colour wherever it
 * appears. */
function declarationsRow() {
  const count = countOf("omits");
  if (!count) return null;
  const row = stripRow("declarationsSection");
  row.append(verdictChip("partial", "Declarations", count));
  return row;
}

function verdictChip(tone, text, count) {
  const chip = document.createElement("span");
  chip.className = `verdict ${tone}`;
  // Not `.dot`: that class is the per-part colour key on the object rows, and
  // a status light borrowing it would join the palette it is meant to be read
  // against.
  const light = document.createElement("span");
  light.className = "status-dot";
  chip.append(light, text);
  if (count) {
    const evidence = document.createElement("span");
    evidence.className = "verdict-count";
    evidence.textContent = count;
    chip.append(evidence);
  }
  return chip;
}

function stripRow(targetId) {
  const row = document.createElement("button");
  row.className = "strip-row";
  row.type = "button";
  row.addEventListener("click", () => {
    setDockOpen(true);
    // Sections exist only when the asset has findings for them, so the target
    // of a summary can legitimately be absent -- "complete" points at a
    // section of declarations that an asset with none of them never grew.
    const section = $(targetId);
    if (!section) return;
    section.open = true;
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  return row;
}

// ── report dock ─────────────────────────────────────────────────────

/* Open only while the report is what is being read, which is exactly when the
 * 3D view is not -- so the height it takes comes out of a viewport nobody is
 * looking at. The canvas resizes itself: a ResizeObserver watches its host.
 * Collapsed, the status strip stays on the bar, so the verdict survives. */
function setDockOpen(open) {
  $("dockBody").hidden = !open;
  $("btnDockToggle").setAttribute("aria-expanded", String(open));
}

function installDock() {
  $("btnDockToggle").onclick = () => setDockOpen($("dockBody").hidden);
}

/* Served rather than assembled here: the three views answer different
 * questions and someone diffing two deliveries wants them in one document. */
async function downloadInventory() {
  const payload = await fetch(
    `/api/report/${encodeURIComponent(state.assetKey)}`
  ).then(expectJson);
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" })
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `${state.assetKey}-artiscope.json`;
  link.click();
  URL.revokeObjectURL(url);
}

// ── idle behaviour ──────────────────────────────────────────────────

let idleTimer = null;

/* Whether the viewer has asked the OS for less animation. Honouring it costs
 * one line and turns the intro sweep and the idle tour off for people who
 * need them off. */
function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/* Called on anything anyone does. Stops the idle behaviour immediately and
 * restarts the countdown, so the asset only ever moves on its own once the
 * tool has genuinely been left alone. Someone who is using it never sees the
 * tour at all; someone who walked away, or who left this on a screen for the
 * room, gets a demo.
 *
 * The delay is long because reading the joint table for twenty seconds is
 * normal here, and having the thing start moving mid-sentence is not
 * "attract mode", it is an interruption. */
function noteActivity() {
  controls.autoRotate = false;
  cancelAnimation();
  clearTimeout(idleTimer);
  if (prefersReducedMotion()) return;
  idleTimer = setTimeout(goIdle, IDLE_DELAY_MS);
}

/* No switch for this: it only ever runs once the tool has been left alone, and
 * any one thing you do ends it. A permanent control to turn off something you
 * cannot be present for is a line in a menu that nobody has a reason to reach
 * for. `?idle=<ms>` remains for screens meant to be watched, not used. */
function goIdle() {
  controls.autoRotate = true;
  runIdleTour();
}

function installIdleBehaviour() {
  controls.autoRotateSpeed = IDLE_ORBIT_SPEED;
  for (const type of ["pointerdown", "wheel", "keydown", "input"]) {
    document.addEventListener(type, noteActivity, { passive: true });
  }
  noteActivity();
}

// ── camera framing and the sweep ────────────────────────────────────

function frameAsset() {
  if (!assetRoot) return;
  const box = new THREE.Box3().setFromObject(assetRoot);
  if (box.isEmpty()) return;

  const centre = box.getCenter(new THREE.Vector3());
  const radius = Math.max(box.getSize(new THREE.Vector3()).length() * 0.5, 0.1);
  const distance = radius / Math.sin((camera.fov * Math.PI) / 360) * 1.25;

  controls.target.copy(centre);
  camera.position.copy(centre).add(new THREE.Vector3(distance * 0.7, distance * 0.5, distance * 0.7));
  camera.near = Math.max(distance / 1000, 0.001);
  camera.far = distance * 20;
  camera.updateProjectionMatrix();
  controls.update();

  gridHelper.position.y = box.min.y;
}

/* Bumped to invalidate whatever is running. A boolean would not do: a loop
 * has to be able to tell "I was cancelled" from "something newer started", so
 * that only the newest one ever tidies up. */
let animToken = 0;

/* Stop any running animation the instant someone does something. An animation
 * that keeps moving while you are trying to drag a door is worse than no
 * animation at all.
 *
 * `reset` returns the asset to its zero pose, which is what you want when the
 * interruption is a camera drag but not when it is someone pulling a slider
 * -- that would throw away the value they just set. */
/* Published on the body so the running animation is visible in devtools and
 * can be waited on, rather than inferred from whether something happens to be
 * moving at the instant you look. */
function setAnimationState(kind) {
  document.body.dataset.animation = kind;
}

function cancelAnimation({ reset = true } = {}) {
  if (!state.animating) return;
  animToken += 1;
  state.animating = false;
  state.touring = false;
  setAnimationState("");
  $("btnSweep").disabled = false;
  if (reset) resetPose();
}

/* Drive a set of joints out to one limit and back over `seconds`.
 *
 * Resolves `true` if it ran to completion and `false` if it was cancelled, so
 * a caller sequencing several of these knows whether to continue. */
function animateJoints(joints, seconds, token) {
  const start = performance.now();
  const duration = seconds * 1000;

  return new Promise((resolve) => {
    const step = () => {
      if (token !== animToken) {
        resolve(false);
        return;
      }
      const t = (performance.now() - start) / duration;
      if (t >= 1) {
        resolve(true);
        return;
      }
      // Out and back, eased, so both ends of the range are held briefly.
      const phase = (1 - Math.cos(2 * Math.PI * t)) / 2;
      for (const joint of joints) {
        const { lower, upper } = jointSpan(joint);
        setJointValue(joint.id, lower + (upper - lower) * phase);
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}

function sleep(ms, token) {
  return new Promise((resolve) => {
    setTimeout(() => resolve(token === animToken), ms);
  });
}

/* Every joint through its whole range at once. The fastest way to catch a
 * door hinged on the wrong edge, a drawer sliding into the carcass, or an
 * axis pointing 90 degrees off -- all of which look perfectly fine at rest.
 *
 * All at once on purpose: for spotting a defect you want the maximum chance
 * that something visibly collides, and reading each joint individually is the
 * tour's job, not this one's. */
async function sweepAllJoints() {
  if (!state.manifest || !state.manifest.joints.length) return;
  cancelAnimation({ reset: false });

  const token = (animToken += 1);
  state.animating = true;
  setAnimationState("sweep");
  $("btnSweep").disabled = true;

  const finished = await animateJoints(
    state.manifest.joints,
    SWEEP_SECONDS,
    token
  );
  if (!finished) return; // cancelled; whoever cancelled tidied up

  resetPose();
  state.animating = false;
  setAnimationState("");
  $("btnSweep").disabled = false;
}

/* The idle tour: one joint at a time, selected so its gizmo and label are up,
 * back to zero between stops, looping until someone interrupts.
 *
 * One at a time rather than all together because motion is the strongest cue
 * the eye has, and spending it on everything at once spends it on nothing --
 * there is then no way left to say "look at this joint". Moving a single part
 * against a still asset says exactly that, and the label answers what it is
 * while you are already looking at it. Ten seconds of this teaches one
 * concrete thing; ten seconds of everything writhing teaches none. */
async function runIdleTour() {
  if (!state.manifest || !state.manifest.joints.length) return;
  cancelAnimation({ reset: false });

  const token = (animToken += 1);
  state.animating = true;
  state.touring = true;
  setAnimationState("tour");
  resetPose();

  for (let i = 0; token === animToken; i += 1) {
    const joint = state.manifest.joints[i % state.manifest.joints.length];
    selectJoint(joint.id);
    if (!(await animateJoints([joint], TOUR_STOP_SECONDS, token))) return;
    setJointValue(joint.id, 0);
    if (!(await sleep(TOUR_PAUSE_MS, token))) return;
  }
}

// ── viewport: display menu ───────────────────────────────────────────

/* A dropdown, not a panel that merely happens to be hidden: it closes on any
 * click outside it and on Escape. */
function closeDisplayMenu() {
  $("displayPanel").hidden = true;
  $("btnDisplay").setAttribute("aria-expanded", "false");
}

/* Each input states its own default in the markup, so `defaultChecked` is the
 * whole rule -- no second list of defaults to drift out of step. A radio group
 * counts once however many of its members moved: picking a new mode flips two
 * inputs but is one decision. */
function refreshDisplayCount() {
  const inputs = [...$("displayPanel").querySelectorAll("input")];
  const boxes = inputs.filter((el) => el.type === "checkbox");
  const radios = inputs.filter((el) => el.type === "radio");
  const groups = new Set(radios.map((el) => el.name));

  const changed =
    boxes.filter((el) => el.checked !== el.defaultChecked).length +
    [...groups].filter((name) =>
      radios.some((el) => el.name === name && el.checked && !el.defaultChecked)
    ).length;

  const badge = $("displayCount");
  badge.textContent = String(changed);
  badge.hidden = changed === 0;
}

function installDisplayMenu() {
  const trigger = $("btnDisplay");
  const panel = $("displayPanel");

  // Delegated, so a new toggle is counted by being in the panel rather than by
  // remembering to touch this function.
  panel.addEventListener("change", refreshDisplayCount);
  refreshDisplayCount();

  trigger.onclick = (event) => {
    event.stopPropagation();
    const opening = panel.hidden;
    panel.hidden = !opening;
    trigger.setAttribute("aria-expanded", String(opening));
  };
  document.addEventListener("click", (event) => {
    if (panel.hidden) return;
    if (event.target === trigger || panel.contains(event.target)) return;
    closeDisplayMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDisplayMenu();
  });
}

// ── wiring ──────────────────────────────────────────────────────────

function installControls() {
  // Queued behind the document-level handler that has already cancelled the
  // idle tour, so this always starts from rest.
  $("btnSweep").onclick = () => sweepAllJoints();
  $("btnReset").onclick = () => {
    cancelAnimation({ reset: false });
    resetPose();
  };
  $("btnFrame").onclick = () => frameAsset();
  $("btnDownload").onclick = downloadInventory;
  installDisplayMenu();
  installDock();

  $("toggleGizmoAll").onchange = applyGizmoVisibility;
  $("toggleLabels").onchange = applyGizmoVisibility;
  $("toggleEnvelope").onchange = applyEnvelopeVisibility;
  $("toggleExploded").onchange = applyExploded;
  $("toggleIsolate").onchange = refreshSelectionVisuals;
  $("toggleFaults").onchange = refreshSelectionVisuals;
  $("toggleMass").onchange = applyMassMarkers;
  $("toggleCollision").onchange = applyCollisionVisibility;
  for (const radio of document.querySelectorAll("input[name=surfaceMode]")) {
    radio.onchange = applySurfaceMode;
  }
}

/* Published for the same reason the running animation is published on the
 * body: the rig is otherwise unreachable from outside this module, and the
 * defects that matter most here are properties of it rather than of anything
 * in the DOM. A material shared between two parts renders a per-part tint
 * meaningless and looks, on screen, exactly like a deliberate colour. */
window.artiLab = state;

initViewer();
installViewportInput();
installControls();
installIdleBehaviour();
installDropToAdd();
/* ?asset=<key> is the direct link loadAssetList() already accounts for: a key
 * named in the URL is as deliberate as one just dropped, so it survives the
 * jointless filter and opens on arrival. */
loadAssetList(new URLSearchParams(location.search).get("asset"));
