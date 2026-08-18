"""End-to-end checks over the HTTP surface.

Exercised in-process rather than against a running server: the point is that
the three artefacts the browser needs -- manifest, GLB and inventory -- are
consistent with each other. None of that needs a socket.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, main
from conftest import ASSET_ROOT


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """A test client pointed at the reference assets with a scratch cache."""
    workspace = tmp_path_factory.mktemp("service")
    main.settings = config.Settings(
        asset_dir=ASSET_ROOT,
        cache_dir=workspace / "cache",
        face_budget=150_000,
    )
    main.settings.cache_dir.mkdir(parents=True, exist_ok=True)

    if not ASSET_ROOT.is_dir():
        pytest.skip("reference assets not available")
    return TestClient(main.app)


def test_health_sees_the_assets(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["asset_count"] >= 1


def test_asset_keys_disambiguate_identical_file_stems(client):
    # Dishwasher/Dishwasher.usd and
    # Dishwasher_with_drive/Dishwasher.usd share a stem but are two
    # different deliveries; keying on the stem alone would hide one of them.
    keys = {a["key"] for a in client.get("/api/assets").json()["assets"]}
    assert "Dishwasher" in keys
    assert "Dishwasher_with_drive" in keys


def test_the_drive_variant_is_reported_as_driven(client):
    # A copy of the dishwasher with a drive added to the door. It is the
    # control for "does this tool actually notice a drive", so if the two read
    # the same, the drive reporting is not doing anything.
    plain = client.get("/api/manifest/Dishwasher").json()
    with_drive = client.get("/api/manifest/Dishwasher_with_drive").json()

    def door(manifest):
        return next(j for j in manifest["joints"] if j["name"].endswith("Door001"))

    assert not door(plain)["drive"]["is_active"]
    assert door(with_drive)["drive"]["is_active"]


def test_ui_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "artiscope" in response.text


def test_glb_is_served_and_cached(client):
    first = client.get("/api/glb/Cabinet")
    assert first.status_code == 200
    assert first.headers["content-type"] == "model/gltf-binary"
    assert first.content[:4] == b"glTF"

    cached = list(main.settings.cache_dir.glob("Cabinet-*.glb"))
    assert len(cached) == 1

    # Second request must not re-export: a 40 MB stage per page load would
    # make the UI feel broken.
    mtime = cached[0].stat().st_mtime_ns
    assert client.get("/api/glb/Cabinet").status_code == 200
    assert cached[0].stat().st_mtime_ns == mtime


def test_mesh_report_is_clean_for_an_asset_within_its_face_budget(client):
    # Dishwasher is within budget, drops no part, and every one of its
    # textures resolves -- so this is also the regression test that the
    # export path stays quiet when it has nothing to say. (Cabinet
    # is a poor choice here: its doors genuinely texture from a sibling
    # asset's folder that is not present, see test_mesh_export.py.)
    assert client.get("/api/mesh_report/Dishwasher").json() == {"warnings": []}


def test_mesh_report_is_cached_alongside_the_glb(client):
    # Fetching the report before the GLB has ever been requested still has to
    # work -- it triggers the same export and caches the same way.
    client.get("/api/glb/Stovetop")
    cached = list(main.settings.cache_dir.glob("Stovetop-*.warnings.json"))
    assert len(cached) == 1


def test_the_combined_report_includes_mesh_warnings(client):
    report = client.get("/api/report/Cabinet").json()
    assert len(report["mesh"]["warnings"]) == 2


def test_the_findings_endpoint_covers_every_check_at_once(client):
    """The one request the UI makes to know what is wrong with an asset.

    All six checks have to reach it, or a defect is invisible in the only
    place anyone looks for one. The GLB export is among them, so this also
    pins that the endpoint waits for geometry rather than reporting on the
    manifest alone.
    """
    report = client.get("/api/findings/Cabinet").json()
    sources = {f["source"] for f in report["findings"]}
    assert {"nvidia", "manifest", "references", "mesh"} <= sources

    # Rejections and contradictions only; advisories and this tool's own
    # limits are not the delivery's fault.
    assert report["fault_count"] == sum(
        report["counts"][m] for m in ("rejects", "contradicts")
    )
    assert report["engine"]["ran"]


def test_manifest_serialises_the_physics_visualisation_fields(client):
    # The three fields the viewer's physics overlays read straight off the
    # manifest response -- if any drop out of the HTTP contract, the overlay
    # renders nothing and gives no reason why.
    cabinet = client.get("/api/manifest/Cabinet").json()
    carcass = next(p for p in cabinet["parts"] if p["name"] == "Cabinet")
    assert carcass["collision"]["has_collision"] is True
    assert carcass["collision"]["shares_visual_geometry"] is False
    assert carcass["physics_material"]["name"] is None
    # No part in this delivery authors a centre of mass, so the field must
    # come through as absent rather than silently dropped from the schema.
    assert "center_of_mass_world" in carcass["mass"]
    assert carcass["mass"]["center_of_mass_world"] is None

    dishwasher = client.get("/api/manifest/Dishwasher").json()
    body = next(p for p in dishwasher["parts"] if p["name"] == "Dishwasher_Body001")
    assert body["collision"]["has_collision"] is True
    assert body["collision"]["shares_visual_geometry"] is True


def test_manifest_and_inventory_agree_on_size(client):
    manifest = client.get("/api/manifest/Cabinet").json()
    inventory = client.get("/api/inventory/Cabinet").json()
    assert inventory["part_count"] == len(manifest["parts"])
    assert inventory["joint_count"] == len(manifest["joints"])


def test_the_cabinet_reports_its_one_known_inconsistency(client):
    # This asset's only contradiction: all five doors name a body1 one level
    # too deep, a prim with no rigid-body schema at all. Everything else
    # about it is clean, so this is also the test that a false positive
    # elsewhere would break.
    inventory = client.get("/api/inventory/Cabinet").json()
    broken = [o for o in inventory["observations"] if o["finding"] == "inconsistent"]
    assert {o["dimension"] for o in broken} == {"Body attachment"}
    assert len(broken) == 5


def test_a_welded_part_is_attached_not_orphaned(client):
    # WallOven welds its glass panel to the door with a FixedJoint.
    # The parts tree shows it correctly attached, and the description used to
    # say the opposite -- "has no joint, so it cannot move and is not the base
    # either" -- because only driving joints were consulted. Two halves of the
    # same tool contradicting each other about the same part is worse than
    # either answer alone.
    inventory = client.get("/api/inventory/WallOven").json()
    welded = [
        o for o in inventory["observations"] if o["id"] == "structure.welded_part"
    ]
    assert len(welded) == 1
    assert welded[0]["finding"] == "authored"
    assert "Door001_Clear" in welded[0]["subject"]
    assert not [
        o for o in inventory["observations"] if o["id"] == "structure.orphan_part"
    ]


def test_rest_pose_is_reported_as_read_not_assumed(client):
    # The viewer's whole notion of "closed" rests on the delivered geometry
    # being the zero pose. It holds for these assets, but it has to appear in
    # the inventory as something that was actually read off the file.
    inventory = client.get("/api/inventory/Cabinet").json()
    rest = [
        o for o in inventory["observations"] if o["id"] == "rest_pose.frames_coincide"
    ]
    assert len(rest) == 1
    assert rest[0]["finding"] == "authored"


def test_missing_mass_is_described_as_absent_not_wrong(client):
    # No dishwasher part has an authored mass. Whether that disqualifies an
    # asset is not for this tool to decide yet, so it is reported as a thing
    # the file does not say -- not as a defect.
    inventory = client.get("/api/inventory/Dishwasher").json()
    mass = [o for o in inventory["observations"] if o["id"] == "mass.authored"]
    assert len(mass) == 1
    assert mass[0]["finding"] == "absent"


def test_button_return_spring_is_called_out(client):
    # Target -0.05 m against a 0..0.003 m travel. Correct for a button, a bug
    # anywhere else, so it has to be visible rather than pass unremarked.
    inventory = client.get("/api/inventory/Dishwasher").json()
    flagged = [
        o for o in inventory["observations"] if o["id"] == "drive.target_in_range"
    ]
    assert len(flagged) == 1
    assert flagged[0]["finding"] == "unusual"
    assert "Button001" in flagged[0]["subject"]


def test_every_observation_names_a_dimension(client):
    # The list of dimensions is the point of this endpoint: it is what the
    # team reads to learn what an articulated asset can even vary along.
    inventory = client.get("/api/inventory/Cabinet").json()
    assert inventory["observations"]
    assert all(o["dimension"] for o in inventory["observations"])
    dimensions = {o["dimension"] for o in inventory["observations"]}
    assert {"Travel range", "Zero pose", "Mass", "Drive"} <= dimensions


def test_unknown_asset_is_a_404(client):
    assert client.get("/api/manifest/not-a-real-asset").status_code == 404


class TestMeshFidelity:
    """The GLB has to say when it stopped matching the source exactly."""

    @pytest.fixture()
    def tight_budget_client(self, tmp_path):
        """A client whose face budget the cabinet's carcass cannot fit in.

        Restores the module-wide settings afterwards, matching the pattern
        used for the upload tests.
        """
        previous = main.settings
        main.settings = config.Settings(
            asset_dir=ASSET_ROOT,
            cache_dir=tmp_path / "cache",
            face_budget=4,
        )
        main.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            yield TestClient(main.app)
        finally:
            main.settings = previous

    def test_a_decimated_part_is_named_in_the_mesh_report(self, tight_budget_client):
        # Before this was wired up, decimating a part past its budget was
        # visible only in the server log -- invisible to whoever was looking
        # at the viewer and trusting the shape on screen.
        report = tight_budget_client.get("/api/mesh_report/Cabinet").json()
        assert any("decimated" in w for w in report["warnings"])


_STATIC_USDA = """#usda 1.0
(
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "Prop" (
    prepend apiSchemas = ["PhysicsRigidBodyAPI"]
)
{
    def Mesh "geo"
    {
        point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        int[] faceVertexCounts = [3]
        int[] faceVertexIndices = [0, 1, 2]
    }
}
"""

_ARTICULATED_USDA = """#usda 1.0
(
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "Base" (
    prepend apiSchemas = ["PhysicsRigidBodyAPI"]
)
{
    def Mesh "geo"
    {
        point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        int[] faceVertexCounts = [3]
        int[] faceVertexIndices = [0, 1, 2]
    }
}
def Xform "Door" (
    prepend apiSchemas = ["PhysicsRigidBodyAPI"]
)
{
    def Mesh "geo"
    {
        point3f[] points = [(0, 0, 1), (1, 0, 1), (0, 1, 1)]
        int[] faceVertexCounts = [3]
        int[] faceVertexIndices = [0, 1, 2]
    }
    def PhysicsRevoluteJoint "Joint"
    {
        uniform token physics:axis = "Z"
        rel physics:body0 = </Base>
        rel physics:body1 = </Door>
    }
}
"""


class TestPickerLeadsWithArticulation:
    """A directory-sized delivery is mostly parts with no joint at all.

    Listing every one of those beside the handful worth opening would bury
    them, so the default picker omits a readable, zero-joint asset -- reading
    one directly is untouched, this is only about what leads the list.
    """

    @pytest.fixture()
    def mixed_client(self, tmp_path):
        library = tmp_path / "library"
        (library / "Prop").mkdir(parents=True)
        (library / "Prop" / "Prop.usda").write_text(_STATIC_USDA, encoding="utf-8")
        (library / "Cabinet").mkdir()
        (library / "Cabinet" / "Cabinet.usda").write_text(
            _ARTICULATED_USDA, encoding="utf-8"
        )

        previous = main.settings
        main.settings = config.Settings(
            asset_dir=library,
            cache_dir=tmp_path / "cache",
            face_budget=150_000,
        )
        main.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            yield TestClient(main.app)
        finally:
            main.settings = previous

    def test_the_default_list_skips_the_static_asset(self, mixed_client):
        keys = {a["key"] for a in mixed_client.get("/api/assets").json()["assets"]}
        assert keys == {"Cabinet"}

    def test_include_static_brings_it_back(self, mixed_client):
        listing = mixed_client.get("/api/assets?include_static=true").json()
        keys = {a["key"] for a in listing["assets"]}
        assert keys == {"Cabinet", "Prop"}

    def test_the_static_asset_still_reads_directly(self, mixed_client):
        # Filtering the picker must never mean the endpoints stop accepting
        # the asset -- only that it does not lead the list uninvited.
        manifest = mixed_client.get("/api/manifest/Prop").json()
        assert manifest["joints"] == []
        assert len(manifest["parts"]) == 1


class TestUploadEndpoint:
    """Dropping an asset on the page puts it in the picker."""

    @pytest.fixture()
    def upload_client(self, tmp_path):
        """A client over an empty, writable library.

        Restores the module-wide settings afterwards: these tests point the
        service at a scratch directory, and leaving it there would quietly
        blank the asset list for whatever runs next.
        """
        previous = main.settings
        library = tmp_path / "library"
        library.mkdir()
        main.settings = config.Settings(
            asset_dir=library,
            cache_dir=tmp_path / "cache",
            face_budget=150_000,
        )
        main.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            yield TestClient(main.app)
        finally:
            main.settings = previous

    def test_an_uploaded_usd_appears_in_the_asset_list(self, upload_client):
        # Widget.usd has no joints, so it only shows up once asked for by
        # name -- which is exactly what dropping it onto the page is.
        response = upload_client.post(
            "/api/assets",
            files={"file": ("Widget.usd", b"#usda 1.0\n", "model/vnd.usd")},
        )
        assert response.status_code == 201
        assert response.json()["key"] == "Widget"

        listing = upload_client.get("/api/assets?include_static=true").json()
        assert "Widget" in {a["key"] for a in listing["assets"]}

    def test_a_refused_upload_answers_400_with_the_reason(self, upload_client):
        response = upload_client.post(
            "/api/assets",
            files={"file": ("teapot.stl", b"solid teapot", "model/stl")},
        )
        assert response.status_code == 400
        assert "not an asset this viewer reads" in response.json()["detail"]

    def test_a_refused_upload_does_not_enter_the_library(self, upload_client):
        upload_client.post(
            "/api/assets",
            files={"file": ("teapot.stl", b"solid teapot", "model/stl")},
        )
        assert upload_client.get("/api/assets").json()["assets"] == []


class TestValidatorVerdictCache:
    """The picker's verdict column, and the cache that makes it affordable.

    The validator re-reads the whole stage and runs every rule category: a
    fraction of a second on one appliance, three quarters of a minute on an
    assembled room. Computing it while building the picker cost the list its
    whole response rather than making one heavy entry arrive late, so the
    picker reports only verdicts already in hand.
    """

    @pytest.fixture()
    def fresh_client(self, tmp_path):
        """A client over the reference assets with nothing cached yet.

        Restores the module-wide settings afterwards, matching the pattern the
        other scoped clients here use.
        """
        previous = main.settings
        main.settings = config.Settings(
            asset_dir=ASSET_ROOT,
            cache_dir=tmp_path / "cache",
            face_budget=150_000,
        )
        main.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        if not ASSET_ROOT.is_dir():
            pytest.skip("reference assets not available")
        try:
            yield TestClient(main.app)
        finally:
            main.settings = previous

    def test_the_picker_computes_no_verdict_of_its_own(self, fresh_client):
        listing = fresh_client.get("/api/assets?include_static=true").json()
        readable = [a for a in listing["assets"] if not a.get("error")]
        assert readable
        assert all(a["validator_status"] is None for a in readable)
        # Not computing is the whole point, so assert the absence rather than
        # just the reported None: one assembled room in the directory was
        # enough to put the list past the point where the UI gives up.
        assert not list(main.settings.cache_dir.glob("*.validation.json"))

    def test_a_verdict_asked_for_once_is_cached_and_then_listed(self, fresh_client):
        asked = fresh_client.get("/api/validation/Cabinet")
        assert asked.status_code == 200
        assert (
            len(
                list(
                    main.settings.cache_dir.glob("Cabinet-*.validation.json")
                )
            )
            == 1
        )

        listing = fresh_client.get("/api/assets?include_static=true").json()
        entry = next(a for a in listing["assets"] if a["key"] == "Cabinet")
        assert entry["validator_status"] == asked.json()["status"]
        assert entry["blocking_count"] == asked.json()["blocking_count"]

    def test_a_re_delivered_asset_is_not_answered_from_the_old_verdict(
        self, fresh_client
    ):
        # Verdicts are keyed on the source file's modification time, like the
        # GLB cache. A supplier re-delivering under the same filename has to be
        # re-checked: answering from what the previous file scored is worse than
        # not answering at all.
        stale = main.settings.cache_dir / "Cabinet-1.validation.json"
        stale.write_text('{"status":"clean","detail":"stale","issues":[]}')

        listing = fresh_client.get("/api/assets?include_static=true").json()
        entry = next(a for a in listing["assets"] if a["key"] == "Cabinet")
        assert entry["validator_status"] is None

        fresh_client.get("/api/validation/Cabinet")
        assert not stale.exists()

    def test_an_unreadable_cache_file_is_dropped_rather_than_served(self, fresh_client):
        # A verdict half-written by a killed process says nothing about the
        # asset, so it must not be reported as one.
        path = main._validation_cache_path(
            "Cabinet", main._resolve("Cabinet")
        )
        path.write_text('{"status": "cle')

        listing = fresh_client.get("/api/assets?include_static=true").json()
        entry = next(a for a in listing["assets"] if a["key"] == "Cabinet")
        assert entry["validator_status"] is None
        assert not path.exists()

    def test_the_combined_report_validates_once(self, fresh_client, monkeypatch):
        # It used to run the validator for the findings and again for the copy
        # it embeds beside them: on an assembled room, three quarters of a
        # minute spent twice for an answer already in hand.
        calls = []
        real = main.validate_asset

        def counted(usd_path, manifest):
            calls.append(usd_path)
            return real(usd_path, manifest)

        monkeypatch.setattr(main, "validate_asset", counted)
        assert fresh_client.get("/api/report/Cabinet").status_code == 200
        assert len(calls) == 1


def test_health_distinguishes_an_unmounted_directory_from_an_empty_one(tmp_path):
    # Both scan as zero assets, and a bare count of zero reads as "this
    # delivery has nothing in it" rather than "nothing is mounted here" --
    # which is what sent a missing symlink undetected for a week.
    previous = main.settings
    unmounted = tmp_path / "not-mounted"
    main.settings = config.Settings(
        asset_dir=unmounted,
        cache_dir=tmp_path / "cache",
        face_budget=150_000,
    )
    main.settings.cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        body = TestClient(main.app).get("/health").json()
        assert body["asset_count"] == 0
        assert body["asset_dir_exists"] is False

        unmounted.mkdir()
        body = TestClient(main.app).get("/health").json()
        assert body["asset_count"] == 0
        assert body["asset_dir_exists"] is True
    finally:
        main.settings = previous
