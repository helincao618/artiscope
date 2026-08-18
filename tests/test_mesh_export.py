"""Check that the GLB and the manifest describe the same thing.

These two artefacts are produced by separate code paths and joined in the
browser on node name and world coordinates. Nothing at runtime notices when
they drift apart -- the viewer just shows a door hinged around thin air. So
the join is asserted here instead.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from app.mesh_export import COLLISION_NODE_SEP, MATERIAL_NODE_SEP, export_parts_glb
from app.usd_reader import read_manifest


def _flat_nodes(scene) -> dict:
    """Return only the flat-palette geometries, excluding textured and
    collision-overlay variants."""
    return {
        name: geometry
        for name, geometry in scene.geometry.items()
        if MATERIAL_NODE_SEP not in name and COLLISION_NODE_SEP not in name
    }


def _collision_nodes(scene) -> dict:
    """Return only the collision-overlay geometries."""
    return {
        name: geometry
        for name, geometry in scene.geometry.items()
        if COLLISION_NODE_SEP in name
    }


@pytest.fixture(scope="session")
def cabinet_glb(cabinet_usd, tmp_path_factory):
    """Export the cabinet once and reload it as a trimesh scene."""
    out = tmp_path_factory.mktemp("glb") / "cabinet.glb"
    path, warnings = export_parts_glb(cabinet_usd, out)
    return trimesh.load(str(path)), warnings


@pytest.fixture(scope="session")
def dishwasher_glb(dishwasher_usd, tmp_path_factory):
    """Export the dishwasher once and reload it as a trimesh scene."""
    out = tmp_path_factory.mktemp("glb") / "dishwasher.glb"
    path, warnings = export_parts_glb(dishwasher_usd, out)
    return trimesh.load(str(path)), warnings


def test_cabinet_reports_its_known_missing_textures(cabinet_glb):
    # The cabinet's deliberate appearance defect: two of its materials texture
    # diffuseColor from a sibling asset's folder that does not exist on this
    # disk (see app/portability.py). One of the two is bound by three of the
    # five doors, so this is also the regression test for deduplication --
    # one warning per broken material, not once per part that binds it.
    _, warnings = cabinet_glb
    assert len(warnings) == 2
    assert all("does not resolve on this disk" in w for w in warnings)


def test_dishwasher_exports_without_complaint(dishwasher_glb):
    # Every one of its textures resolves, so this is the control: the
    # textured-view fallback only fires when something is actually missing.
    _, warnings = dishwasher_glb
    assert warnings == []


def test_every_part_becomes_a_named_node(cabinet_usd, cabinet_glb):
    scene, _ = cabinet_glb
    manifest = read_manifest(cabinet_usd)
    assert sorted(_flat_nodes(scene)) == sorted(p.node_name for p in manifest.parts)


def test_dishwasher_parts_survive_the_collider_convention(
    dishwasher_usd, dishwasher_glb
):
    # Its render meshes double as colliders. An exporter that filtered on
    # CollisionAPI would produce an empty file here.
    scene, _ = dishwasher_glb
    manifest = read_manifest(dishwasher_usd)
    flat = _flat_nodes(scene)
    assert sorted(flat) == sorted(p.node_name for p in manifest.parts)
    assert all(len(g.faces) > 0 for g in flat.values())


def test_dishwasher_gets_no_collision_overlay(dishwasher_glb):
    # Every one of its colliders is also its visual mesh, already on screen
    # via the flat/textured pass -- a second, identical node would only
    # double the file for nothing a reviewer could not already see.
    scene, _ = dishwasher_glb
    assert _collision_nodes(scene) == {}


def test_cabinet_gets_a_collision_overlay_for_its_guide_hulls(
    cabinet_usd, cabinet_glb
):
    # Its collision geometry sits apart from the render meshes, in a
    # purpose=guide scope -- the one convention that actually needs a
    # distinct overlay node, since nothing else in the export ever draws it.
    scene, _ = cabinet_glb
    manifest = read_manifest(cabinet_usd)
    collision = _collision_nodes(scene)
    assert collision, "expected at least one part with its own collision hulls"
    for name in collision:
        assert name.endswith(COLLISION_NODE_SEP + "0")
        part_name = name[: -len(COLLISION_NODE_SEP + "0")]
        assert part_name in {p.node_name for p in manifest.parts}
    assert all(len(g.faces) > 0 for g in collision.values())


def test_every_flat_part_has_a_textured_counterpart(cabinet_glb):
    # The textured view is additive, not a replacement: every flat node keeps
    # exactly the name the manifest expects, and gets at least one sibling
    # node carrying the asset's own material instead of the palette.
    scene, _ = cabinet_glb
    flat_names = set(_flat_nodes(scene))
    textured_names = set(scene.geometry) - flat_names
    for name in flat_names:
        assert any(t.startswith(f"{name}{MATERIAL_NODE_SEP}") for t in textured_names)


def test_geometry_stays_in_stage_space(cabinet_usd, cabinet_glb):
    # No Y-up conversion on export: mesh coordinates and manifest anchors have
    # to remain directly comparable, or the viewer's pivots land in the wrong
    # place. The cabinet is taller than it is deep, which only holds in the
    # original Z-up frame.
    scene, _ = cabinet_glb
    extent = scene.bounds[1] - scene.bounds[0]
    assert extent[2] > extent[1]


@pytest.mark.parametrize("asset", ["cabinet", "dishwasher"])
def test_each_joint_anchor_lands_on_the_part_it_moves(
    asset, cabinet_usd, dishwasher_usd, cabinet_glb, dishwasher_glb
):
    # The sharpest available check on frame agreement: a hinge has to sit on
    # or near the door it swings. A frame mismatch throws the anchor metres
    # away, well outside this tolerance.
    usd, (scene, _) = (
        (cabinet_usd, cabinet_glb) if asset == "cabinet" else
        (dishwasher_usd, dishwasher_glb)
    )
    manifest = read_manifest(usd)
    tolerance = 0.2

    for joint in manifest.joints:
        part = manifest.part_by_id(joint.child_part)
        geometry = scene.geometry[part.node_name]
        low, high = geometry.bounds
        anchor = np.asarray(joint.anchor_world)
        assert np.all(anchor >= low - tolerance), (
            f"{joint.name}: anchor {anchor} below part bounds {low}"
        )
        assert np.all(anchor <= high + tolerance), (
            f"{joint.name}: anchor {anchor} above part bounds {high}"
        )


def test_parts_are_visually_distinguishable(cabinet_glb):
    # Per-part colour is how a reviewer sees the part decomposition at all,
    # in the flat view -- the textured nodes carry the asset's own materials
    # instead and are exempt from this by design.
    scene, _ = cabinet_glb
    first_colours = {
        name: tuple(geometry.visual.vertex_colors[0])
        for name, geometry in _flat_nodes(scene).items()
    }
    assert len(set(first_colours.values())) == len(first_colours)
