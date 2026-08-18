"""Rules for taking delivery of an uploaded asset.

The library is a directory, so intake decides where untrusted bytes are
allowed to land. Most of what is worth testing here is refusal.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.intake import IntakeError, accept_upload

MINIMAL_USDA = b'#usda 1.0\n(\n    upAxis = "Z"\n)\n'


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    """Write a zip holding ``entries`` and return its path."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


class TestAcceptedShapes:
    """What intake is for."""

    def test_a_bare_usd_becomes_its_own_asset_folder(self, tmp_path):
        payload = tmp_path / "upload"
        payload.write_bytes(MINIMAL_USDA)
        library = tmp_path / "library"

        result = accept_upload("Toaster003.usd", payload, library)

        assert result.key == "Toaster003"
        assert (library / "Toaster003" / "Toaster003.usd").is_file()
        assert result.usd_count == 1

    def test_a_usdz_package_travels_as_a_single_opaque_file(self, tmp_path):
        # .usdz already carries its textures inside the package, so -- unlike
        # a .zip of a folder -- it must land untouched, not be unpacked.
        payload = tmp_path / "upload"
        payload.write_bytes(b"PK\x03\x04fake usdz payload")
        library = tmp_path / "library"

        result = accept_upload("Toaster003.usdz", payload, library)

        assert result.key == "Toaster003"
        assert (library / "Toaster003" / "Toaster003.usdz").is_file()
        assert result.usd_count == 1

    def test_a_zipped_folder_keeps_its_textures_beside_the_usd(self, tmp_path):
        payload = _zip(
            tmp_path / "upload.zip",
            {
                "Toaster003/Toaster003.usd": MINIMAL_USDA,
                "Toaster003/texture/albedo.png": b"\x89PNG",
            },
        )
        library = tmp_path / "library"

        result = accept_upload("Toaster003.zip", payload, library)

        root = library / "Toaster003"
        assert (root / "Toaster003" / "Toaster003.usd").is_file()
        assert (root / "Toaster003" / "texture" / "albedo.png").is_file()
        assert result.usd_count == 1


class TestRefusals:
    """Everything intake must not do."""

    def test_a_zip_entry_cannot_escape_the_asset_folder(self, tmp_path):
        # The classic zip-slip: an entry whose path climbs out of the
        # extraction root and overwrites something else on disk.
        payload = _zip(
            tmp_path / "upload.zip",
            {"../../escaped.usd": MINIMAL_USDA},
        )
        library = tmp_path / "library"

        with pytest.raises(IntakeError, match="outside the asset folder"):
            accept_upload("evil.zip", payload, library)

        assert not (tmp_path / "escaped.usd").exists()

    def test_a_filename_cannot_climb_out_of_the_library(self, tmp_path):
        payload = tmp_path / "upload"
        payload.write_bytes(MINIMAL_USDA)
        library = tmp_path / "library"

        result = accept_upload("../../pwned.usd", payload, library)

        assert result.directory.parent == library
        assert not (tmp_path / "pwned.usd").exists()

    def test_a_urdf_is_accepted_alongside_usd(self, tmp_path):
        # The shallow path: readable and drivable, never graded. Intake has no
        # opinion about the tier -- it just must not refuse the file.
        payload = tmp_path / "upload"
        payload.write_bytes(b'<?xml version="1.0"?><robot name="R"/>')

        result = accept_upload("Robot.urdf", payload, tmp_path / "library")

        assert (result.directory / "Robot.urdf").is_file()

    def test_an_unsupported_extension_is_named_in_the_refusal(self, tmp_path):
        payload = tmp_path / "upload"
        payload.write_bytes(b"solid teapot")

        with pytest.raises(IntakeError, match="not an asset this viewer reads"):
            accept_upload("teapot.stl", payload, tmp_path / "library")

    def test_a_zip_without_any_asset_is_rejected(self, tmp_path):
        payload = _zip(tmp_path / "upload.zip", {"notes/readme.txt": b"hi"})

        with pytest.raises(IntakeError, match="contains no readable asset"):
            accept_upload("notes.zip", payload, tmp_path / "library")

    def test_a_rejected_upload_leaves_no_folder_behind(self, tmp_path):
        # A half-written folder would show up in the picker as a broken asset
        # that nobody added.
        payload = _zip(tmp_path / "upload.zip", {"notes/readme.txt": b"hi"})
        library = tmp_path / "library"

        with pytest.raises(IntakeError):
            accept_upload("notes.zip", payload, library)

        assert not (library / "notes").exists()

    def test_an_existing_asset_is_never_overwritten(self, tmp_path):
        payload = tmp_path / "upload"
        payload.write_bytes(MINIMAL_USDA)
        library = tmp_path / "library"
        accept_upload("Toaster003.usd", payload, library)

        payload.write_bytes(b"#usda 1.0\n# different\n")
        with pytest.raises(IntakeError, match="already in the library"):
            accept_upload("Toaster003.usd", payload, library)

        kept = (library / "Toaster003" / "Toaster003.usd").read_bytes()
        assert kept == MINIMAL_USDA
