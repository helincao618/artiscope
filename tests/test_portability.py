"""Whether an asset folder survives being moved.

The distinction these cover is the one the validator cannot make: a reference
that resolves right now but points outside the asset folder is still a defect,
because "deliver one asset" means copying that folder somewhere else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.portability import check_portability

pytest.importorskip("pxr")

TEXTURED_USDA = """#usda 1.0
(
    upAxis = "Z"
    metersPerUnit = 1
)

def Material "Mat"
{
    def Shader "Tex"
    {
        uniform token info:id = "UsdUVTexture"
        asset inputs:file = @TEXTURE_PATH@
    }
}
"""


def _asset(folder: Path, texture_ref: str) -> Path:
    """Write a one-material asset pointing at ``texture_ref``."""
    folder.mkdir(parents=True, exist_ok=True)
    usd = folder / "Asset.usda"
    usd.write_text(
        TEXTURED_USDA.replace("TEXTURE_PATH", texture_ref), encoding="utf-8"
    )
    return usd


class TestSelfContained:
    """The case that needs no warning."""

    def test_a_texture_beside_the_usd_is_portable(self, tmp_path):
        folder = tmp_path / "Asset"
        usd = _asset(folder, "./texture/albedo.png")
        (folder / "texture").mkdir()
        (folder / "texture" / "albedo.png").write_bytes(b"\x89PNG")

        report = check_portability(usd)

        assert report.self_contained
        assert report.broken == []
        assert report.escaping == []


class TestNotPortable:
    """The two ways a folder stops being a deliverable unit."""

    def test_a_reference_into_a_sibling_folder_is_flagged_even_when_it_resolves(
        self, tmp_path
    ):
        # This is the cabinets. Inside the full delivery every reference
        # resolves and the validator says nothing, but copying the folder out
        # on its own silently loses the textures.
        sibling = tmp_path / "OtherAsset" / "texture"
        sibling.mkdir(parents=True)
        (sibling / "shared.png").write_bytes(b"\x89PNG")
        usd = _asset(tmp_path / "Asset", "../OtherAsset/texture/shared.png")

        report = check_portability(usd)

        assert not report.self_contained
        assert report.broken == []
        assert [ref.raw_path for ref in report.escaping] == [
            "../OtherAsset/texture/shared.png"
        ]

    def test_a_reference_that_resolves_nowhere_is_flagged_as_broken(self, tmp_path):
        usd = _asset(tmp_path / "Asset", "./texture/absent.png")

        report = check_portability(usd)

        assert not report.self_contained
        assert [ref.raw_path for ref in report.broken] == ["./texture/absent.png"]

    def test_the_authored_path_is_reported_not_the_resolved_one(self, tmp_path):
        # Whoever fixes this has to find the string in the source asset, so
        # the absolute path it resolved to is the wrong thing to show.
        usd = _asset(tmp_path / "Asset", "../Elsewhere/tex.png")

        report = check_portability(usd)

        assert report.references[0].raw_path == "../Elsewhere/tex.png"


class TestStagedAsSymlinks:
    """A delivery is routinely staged as links into where it was unpacked."""

    def test_a_symlinked_texture_inside_the_folder_is_still_inside(self, tmp_path):
        # Following the link lands outside the folder, so a containment test
        # built on Path.resolve calls every texture in a staged delivery an
        # escape. The question is which folder carries the file, not which
        # disk the bytes ended up on.
        original = tmp_path / "unpacked" / "albedo.png"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"\x89PNG")

        folder = tmp_path / "Asset"
        usd = _asset(folder, "./texture/albedo.png")
        (folder / "texture").mkdir()
        (folder / "texture" / "albedo.png").symlink_to(original)

        report = check_portability(usd)

        assert report.self_contained
        assert report.escaping == []

    def test_a_symlinked_escape_is_still_an_escape(self, tmp_path):
        # The fix must not blind the check: what matters is the authored path
        # leaving the folder, and this one does.
        sibling = tmp_path / "OtherAsset"
        sibling.mkdir()
        (sibling / "shared.png").write_bytes(b"\x89PNG")
        usd = _asset(tmp_path / "Asset", "../OtherAsset/shared.png")

        report = check_portability(usd)

        assert [ref.raw_path for ref in report.escaping] == [
            "../OtherAsset/shared.png"
        ]


class TestEngineSuppliedModules:
    """A module name is not a missing file."""

    def test_a_bare_mdl_module_is_not_counted_as_broken(self, tmp_path):
        # OmniPBR.mdl ships inside Omniverse and resolves through the MDL
        # search path. Off an Omniverse install it cannot resolve, and
        # grading the asset for that would be grading this machine.
        usd = _asset(tmp_path / "Asset", "OmniPBR.mdl")

        report = check_portability(usd)

        assert report.broken == []
        assert [ref.raw_path for ref in report.engine_modules] == ["OmniPBR.mdl"]

    def test_an_mdl_with_a_path_is_a_file_like_any_other(self, tmp_path):
        # Authoring a directory component means naming a file on disk, and a
        # file that is not there is missing however it is spelled.
        usd = _asset(tmp_path / "Asset", "./materials/Custom.mdl")

        report = check_portability(usd)

        assert [ref.raw_path for ref in report.broken] == ["./materials/Custom.mdl"]
        assert report.engine_modules == []


class TestFolderBoundary:
    """What counts as 'the asset' is caller-defined."""

    def test_widening_the_folder_makes_a_sibling_reference_internal(self, tmp_path):
        # Treating the whole delivery as the unit is legitimate -- it is just
        # a different promise about what gets shipped together.
        sibling = tmp_path / "OtherAsset" / "texture"
        sibling.mkdir(parents=True)
        (sibling / "shared.png").write_bytes(b"\x89PNG")
        usd = _asset(tmp_path / "Asset", "../OtherAsset/texture/shared.png")

        report = check_portability(usd, asset_folder=tmp_path)

        assert report.self_contained
