"""Check what the textured view reads from a USD shader graph, and what it
falls back to when that graph does not cooperate.

Every reference asset's texture-bearing materials wrap the actual
``UsdUVTexture`` in a ``NodeGraph`` (this is what real DCC-to-USD exporters
produce, not a hypothetical), so the synthetic fixture below reproduces that
indirection rather than wiring the texture directly -- a fixture that skips
it would not exercise the code path that matters.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from pxr import UsdShade

from app.mesh_export import MATERIAL_NODE_SEP, _mesh_material_pieces, export_parts_glb
from app.usd_materials import resolve_appearance
from app.usd_scene import discover_part_prims, iter_visual_meshes, open_stage

_MATERIALS_USDA = """#usda 1.0
(
    defaultPrim = "root"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "root"
{{
    def Xform "Part001" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {{
        def Mesh "geo" (
            prepend apiSchemas = ["MaterialBindingAPI"]
        )
        {{
            point3f[] points = [(0,0,0), (1,0,0), (1,1,0), (0,1,0), (2,0,0), (2,1,0)]
            int[] faceVertexCounts = [4, 3]
            int[] faceVertexIndices = [0, 1, 2, 3, 1, 4, 5]
            texCoord2f[] primvars:st = [(0,0), (1,0), (1,1), (0,1), (1,0), (2,0), (2,1)] (
                interpolation = "faceVarying"
            )
            rel material:binding = </root/Looks/Plain>

            def GeomSubset "texturedFace" (
                prepend apiSchemas = ["MaterialBindingAPI"]
            )
            {{
                uniform token elementType = "face"
                uniform token familyName = "materialBind"
                int[] indices = [0]
                rel material:binding = </root/Looks/Wood>
            }}
            def GeomSubset "brokenFace" (
                prepend apiSchemas = ["MaterialBindingAPI"]
            )
            {{
                uniform token elementType = "face"
                uniform token familyName = "materialBind"
                int[] indices = [1]
                rel material:binding = </root/Looks/Broken>
            }}
        }}
    }}

    def Scope "Looks"
    {{
        def Material "Wood"
        {{
            token outputs:surface.connect = </root/Looks/Wood/Shader.outputs:surface>

            def Shader "Shader"
            {{
                uniform token info:id = "UsdPreviewSurface"
                color3f inputs:diffuseColor.connect = </root/Looks/Wood/TexGraph.outputs:rgb>
                float inputs:metallic = 0.1
                float inputs:roughness = 0.6
                token outputs:surface
            }}

            def NodeGraph "TexGraph"
            {{
                token inputs:frame:st = "st"
                color3f outputs:rgb.connect = </root/Looks/Wood/TexGraph/Tex.outputs:rgb>

                def Shader "Tex"
                {{
                    uniform token info:id = "UsdUVTexture"
                    asset inputs:file = @{texture_path}@
                    float2 inputs:st.connect = </root/Looks/Wood/TexGraph/UVReader.outputs:result>
                    color3f outputs:rgb
                }}
                def Shader "UVReader"
                {{
                    uniform token info:id = "UsdPrimvarReader_float2"
                    token inputs:varname = "st"
                    float2 outputs:result
                }}
            }}
        }}

        def Material "Broken"
        {{
            token outputs:surface.connect = </root/Looks/Broken/Shader.outputs:surface>

            def Shader "Shader"
            {{
                uniform token info:id = "UsdPreviewSurface"
                color3f inputs:diffuseColor.connect = </root/Looks/Broken/Tex.outputs:rgb>
                token outputs:surface
            }}
            def Shader "Tex"
            {{
                uniform token info:id = "UsdUVTexture"
                asset inputs:file = @./does_not_exist.png@
                color3f outputs:rgb
            }}
        }}

        def Material "Plain"
        {{
            token outputs:surface.connect = </root/Looks/Plain/Shader.outputs:surface>

            def Shader "Shader"
            {{
                uniform token info:id = "UsdPreviewSurface"
                color3f inputs:diffuseColor = (0.2, 0.4, 0.6)
                float inputs:metallic = 0.0
                float inputs:roughness = 0.8
                token outputs:surface
            }}
        }}
    }}
}}
"""


@pytest.fixture(scope="module")
def materials_usd(tmp_path_factory) -> Path:
    """A one-part asset whose single mesh splits across three materials:
    one textured (through a ``NodeGraph``, matching real exports), one
    textured with a reference that does not resolve, and one untextured.
    """
    workdir = tmp_path_factory.mktemp("materials")
    Image.new("RGB", (4, 4), color=(200, 120, 40)).save(workdir / "wood.png")
    usd_path = workdir / "asset.usda"
    usd_path.write_text(
        _MATERIALS_USDA.format(texture_path="./wood.png"), encoding="utf-8"
    )
    return usd_path


@pytest.fixture(scope="module")
def materials_stage(materials_usd):
    # Kept alive for the whole module: a Usd.Stage's prims become invalid
    # once the stage itself is garbage collected, so a helper that opened
    # and discarded a stage per call would hand back dead references.
    return open_stage(materials_usd)


def _material(stage, name: str) -> UsdShade.Material:
    return UsdShade.Material(stage.GetPrimAtPath(f"/root/Looks/{name}"))


def test_a_texture_behind_a_nodegraph_is_found(materials_stage):
    # Real exports wrap UsdUVTexture in a NodeGraph rather than wiring it
    # directly; a reader that only checks for a direct Shader connection
    # would report this material as untextured.
    appearance = resolve_appearance(_material(materials_stage, "Wood"))
    assert appearance.texture_path is not None
    assert appearance.texture_path.name == "wood.png"
    assert appearance.uv_primvar == "st"
    assert appearance.metallic == pytest.approx(0.1)
    assert appearance.roughness == pytest.approx(0.6)
    assert appearance.notes == []


def test_a_constant_diffuse_color_needs_no_texture(materials_stage):
    appearance = resolve_appearance(_material(materials_stage, "Plain"))
    assert appearance.texture_path is None
    assert appearance.base_color == pytest.approx((0.2, 0.4, 0.6))


def test_an_unresolvable_texture_falls_back_and_says_so(materials_stage):
    appearance = resolve_appearance(_material(materials_stage, "Broken"))
    assert appearance.texture_path is None
    assert len(appearance.notes) == 1
    assert "does not resolve" in appearance.notes[0]


def test_no_material_bound_falls_back_and_says_so():
    appearance = resolve_appearance(None)
    assert appearance.texture_path is None
    assert appearance.notes == ["no material bound; showing neutral grey"]


def test_mesh_material_pieces_split_by_geom_subset(materials_usd):
    stage = open_stage(materials_usd)
    part = discover_part_prims(stage)[0]
    mesh_prim = next(iter_visual_meshes(part))

    pieces = _mesh_material_pieces(mesh_prim, to_metres=1.0)

    # One piece per GeomSubset; the whole mesh's own binding is unused
    # because every face is claimed by a subset.
    assert len(pieces) == 2
    face_counts = sorted(len(mesh.faces) for mesh, _, _ in pieces)
    # The quad (face 0) fans into 2 triangles, the tri (face 1) into 1.
    assert face_counts == [1, 2]

    textured = [p for p in pieces if p[2].texture_path is not None]
    assert len(textured) == 1
    _, uv, _ = textured[0]
    assert uv is not None and uv.shape[1] == 2


def test_export_adds_a_textured_node_per_material(materials_usd, tmp_path):
    glb_path, warnings = export_parts_glb(materials_usd, tmp_path / "asset.glb")
    assert any("does not resolve" in w for w in warnings)

    import trimesh

    scene = trimesh.load(str(glb_path))
    textured_names = [
        n for n in scene.geometry if n.startswith(f"Part001{MATERIAL_NODE_SEP}")
    ]
    assert len(textured_names) == 2

    with_texture = [
        n
        for n in textured_names
        if getattr(scene.geometry[n].visual.material, "baseColorTexture", None)
        is not None
    ]
    assert len(with_texture) == 1
