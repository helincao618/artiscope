"""Browser tests for the viewer.

The viewer is most of this tool. A suite that stopped at the Python would be
checking the half that is easy to get right, and would happily pass while the
page threw on load or hinged every door around the origin.

So these load the real page in a real browser and assert on what actually
reaches the screen. Skipped, not failed, when no browser is available.

The trick for "did the geometry really move" is comparing canvas screenshots:
a still WebGL scene renders deterministically, so the same pose gives byte
-identical output. Asserting that the reset pose matches the rest pose exactly
is what makes the earlier "open pose differs" assertion meaningful -- it rules
out the difference being rendering noise.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

# Idle delay used by the tour tests, via the page's `?idle=` override.
TOUR_IDLE_MS = 1800

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

CHROME_ARGS = [
    "--no-sandbox",
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
]


def _free_port() -> int:
    """Return a port the OS says is currently free."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    """Run the service on a scratch port for the duration of the module."""
    workspace = tmp_path_factory.mktemp("ui")
    port = _free_port()
    python = VENV_PYTHON if VENV_PYTHON.exists() else Path("python3")

    process = subprocess.Popen(
        [
            str(python), "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        env={
            "PATH": "/usr/bin:/bin",
            "ARTISCOPE_CACHE_DIR": str(workspace / "cache"),
        },
    )

    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        if process.poll() is not None:
            pytest.skip("service failed to start")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                break
        except OSError:
            time.sleep(0.4)
    else:
        process.terminate()
        pytest.skip("service did not become reachable")

    yield url
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        # uvicorn does not reliably die on SIGTERM here, and a teardown that
        # raises reports a second error per test, hiding the real one.
        process.kill()
        process.wait(timeout=10)


@pytest.fixture(scope="module")
def page(base_url):
    """A page with the cabinet loaded, collecting console errors as it goes."""
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome", args=CHROME_ARGS)
        except Exception:  # noqa: BLE001 - any launch failure means no browser
            pytest.skip("no usable Chrome for playwright")

        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.on(
            "console",
            lambda m: page.errors.append(m.text) if m.type == "error" else None,
        )

        # `networkidle` is unreliable across Chrome builds and is not the
        # readiness signal anyway: the parts list appearing is.
        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_selector(".object-row", timeout=30_000)
        page.select_option("#assetSelect", "Cabinet")
        _settle_after_load(page)

        yield page
        browser.close()


@pytest.fixture
def tour_page(page, base_url):
    """A page whose idle delay is short, so the tour starts without a wait.

    Its own page rather than the shared one: the tour animates continuously,
    and leaving that running would leak into whatever test came next.
    """
    context = page.context.browser.new_context(viewport={"width": 1280, "height": 860})
    fresh = context.new_page()
    # Short enough not to stall the suite, long enough that a test can observe
    # the pause after an interruption before the tour legitimately resumes.
    fresh.goto(f"{base_url}/?idle={TOUR_IDLE_MS}", wait_until="domcontentloaded")
    fresh.wait_for_selector(".object-row", timeout=30_000)
    yield fresh
    context.close()


def _moving_joints(page) -> list[str]:
    """Return the readouts of every joint currently away from zero."""
    texts = [r.inner_text() for r in page.query_selector_all(".readout")]
    return [t for t in texts if abs(float(re.sub(r"[^\d.\-]", "", t) or 0)) > 0]


def _wait_for_tour(page) -> None:
    """Block until the idle tour specifically is running.

    Not "until something moves": the intro sweep is also motion, and it moves
    every joint at once by design.
    """
    page.wait_for_selector("body[data-animation='tour']", timeout=25_000)


def _settle_after_load(page) -> None:
    """Wait out the intro sweep so the asset is back at its zero pose.

    Loading an asset plays every joint through its range once. Tests that care
    about the pose have to let that finish, or they read a frame of animation.
    """
    page.wait_for_timeout(1500)
    page.wait_for_function(
        "() => [...document.querySelectorAll('.readout')]"
        ".every(r => Number.parseFloat(r.textContent) === 0)",
        timeout=15_000,
    )


def _canvas_png(page) -> bytes:
    """Screenshot the WebGL canvas with the selection pinned and settled.

    Selection has to be pinned because it tints the selected part, and driving
    a slider selects the joint it belongs to. Two images taken in the same
    pose but with different parts highlighted differ for a reason that has
    nothing to do with the pose.
    """
    page.query_selector_all(".object-row")[0].click()
    page.wait_for_timeout(900)
    return page.query_selector("#canvasHost canvas").screenshot()


def _set_all_sliders(page, bound: str) -> None:
    """Drive every joint slider to ``min`` or ``max``."""
    for slider in page.query_selector_all('input[type="range"]'):
        slider.evaluate(
            f"s => {{ s.value = s.{bound};"
            f" s.dispatchEvent(new Event('input', {{bubbles:true}})); }}"
        )
    page.wait_for_timeout(400)


def _set_toggle(page, selector: str, checked: bool) -> None:
    """Check or uncheck a toggle behind the closed-by-default Display menu.

    Opens and closes the menu by setting `.hidden` directly, so it does not
    linger over the canvas for whichever screenshot-based test runs next --
    the `page` fixture is shared across the whole module.
    """
    page.eval_on_selector("#displayPanel", "el => el.hidden = false")
    if checked:
        page.check(selector)
    else:
        page.uncheck(selector)
    page.eval_on_selector("#displayPanel", "el => el.hidden = true")


def _set_surface(page, mode: str) -> None:
    """Pick a surface mode -- ``parts``, ``materials`` or ``wireframe``.

    Same open-and-shut dance as `_set_toggle`. Clicks the visible segment
    rather than the radio: the input itself is a 0x0 hook for the label, which
    is how a segmented control is built and how a reader operates one.
    """
    page.eval_on_selector("#displayPanel", "el => el.hidden = false")
    page.click(f"input[name=surfaceMode][value={mode}] + span")
    page.eval_on_selector("#displayPanel", "el => el.hidden = true")


def _display_count(page) -> int:
    """How many toggles the shut Display menu admits are off their default."""
    return page.eval_on_selector(
        "#displayCount", "el => (el.hidden ? 0 : Number(el.textContent))"
    )


def _open_report(page) -> None:
    """Open the report dock and expand every section it grew.

    Collapsed by default -- on a real delivery the findings run long enough
    that landing on all of them is not the same as being able to get to them.
    Sets the state directly rather than clicking: the `page` fixture is
    module-scoped, so a click would toggle whatever an earlier test left open.
    """
    page.eval_on_selector("#dockBody", "el => el.hidden = false")
    page.eval_on_selector_all(
        "#findingsPanel details.section", "els => els.forEach(e => e.open = true)"
    )
    page.wait_for_timeout(200)


def _close_report(page) -> None:
    """Collapse the dock again, restoring the viewport's full height.

    The canvas is sized by its host, so leaving the dock open would change
    every screenshot taken by a later test in this module.
    """
    page.eval_on_selector("#dockBody", "el => el.hidden = true")
    page.wait_for_timeout(200)


def test_page_loads_without_errors(page):
    assert page.errors == []


def test_the_verdict_survives_the_report_being_collapsed(page):
    """The dock starts shut, but its bar still answers the headline question.

    Whether the engine will take the asset is the one thing nobody should
    have to open anything to read, so the strip is the collapsed dock rather
    than something inside it.
    """
    assert not page.query_selector("#dockBody").is_visible()

    # In the word every problems panel already uses, so it carries its own
    # explanation. An earlier wording spelled the consequence out in a sentence
    # instead, which is what a caption is for, not a headline.
    verdict = page.text_content(".verdict.blocking")
    assert verdict.startswith("Errors")
    assert re.search(r"\d+", verdict)

    # Warnings ride beside it in the same chip shape, not a second visual
    # language for the same bar.
    warnings = page.text_content(".verdict.advisory")
    assert warnings.startswith("Warnings")
    assert re.search(r"\d+", warnings)

    page.click("#btnDockToggle")
    assert page.query_selector("#dockBody").is_visible()
    page.click("#btnDockToggle")
    assert not page.query_selector("#dockBody").is_visible()


def test_every_part_is_listed_and_only_the_moving_ones_get_a_slider(page):
    """One list keyed on parts, so a part with no joint is still visible.

    A list of joints cannot show a door that ought to swing and carries no
    joint, which is the defect a delivery most often has. The carcass is in
    here precisely because it does not move.
    """
    assert len(page.query_selector_all("#objectList .object-row")) == 6
    assert len(page.query_selector_all("#objectList input[type=range]")) == 5
    assert page.inner_text("#partCount") == "· 5 of 6 move"


def test_part_labels_drop_the_repeated_asset_prefix(page):
    labels = [
        element.inner_text()
        for element in page.query_selector_all("#objectList .object-name")
    ]
    assert "Door001" in labels


def test_a_part_with_no_joint_still_reports_its_own_facts(page):
    """Mass, rigid-body status and face count are properties of a part.

    Hanging them off the joint detail left them unreachable for every part
    that has none: the base, every weld, and every part the file attached to
    nothing -- which is the one most worth looking at.
    """
    inert = page.eval_on_selector_all(
        "#objectList .object-row",
        "rows => rows.filter(r => !r.querySelector('input'))"
        ".map(r => r.dataset.partId)",
    )
    assert len(inert) == 1

    page.click(f'#objectList .object-row[data-part-id="{inert[0]}"]')
    page.wait_for_timeout(300)

    inspector = page.text_content("#inspector")
    assert "mass" in inspector
    assert "faces" in inspector
    # It has no joint, so nothing about a joint frame may be claimed for it.
    # Matched on the row label rather than the bare word, which also appears in
    # the "static anchor" note a base with no rigid-body schema carries.
    assert "anchor (m)" not in inspector


def test_the_raw_block_keeps_the_values_the_summary_hides(page):
    """Hidden is not dropped.

    Reconciling a delivery against usdview needs the number even when it is
    the expected one, so the rows the exception rule suppresses -- and the
    schema fields no summary has room for -- collapse rather than vanish.
    """
    page.query_selector_all("#objectList .object-row")[0].click()
    page.wait_for_timeout(300)

    # Everything above the collapsed block. text_content would include the
    # block itself -- a closed `<details>` still holds its text.
    summary = page.eval_on_selector(
        "#inspector",
        "el => [...el.children]"
        ".filter(c => !c.classList.contains('raw-values'))"
        ".map(c => c.textContent).join(' ')",
    )
    # This door hinges on a clean world Z, sits at its authored zero and is a
    # rigid body: three rows that would each cost a read to learn nothing.
    assert "axis (world)" not in summary
    assert "modelled at zero" not in summary
    assert "rigid body" not in summary

    # Two "more" blocks now, one under each subject -- open both and read
    # them together, since a fact can live in either.
    page.eval_on_selector_all(
        "#inspector .raw-values", "els => els.forEach(el => el.open = true)"
    )
    raw = page.eval_on_selector_all(
        "#inspector .raw-values",
        "els => els.map(el => el.textContent).join(' ')",
    )
    for field in (
        "axis (world)",
        "rigid body",
        "rest frame offset",
        "bbox min",
        "centre of mass",
    ):
        assert field in raw


def test_the_inspector_never_prints_the_same_fact_twice(page):
    """The summary states a fact once; the raw block only holds what it didn't.

    Anchor, axis, range, and a driven target used to appear twice: once in
    the summary's own words and again, unrounded, under "All raw values" --
    and the target itself, the one number that says where a drive commands
    a part to go, was never in the summary at all.
    """
    page.select_option("#assetSelect", "Dishwasher")
    _settle_after_load(page)
    page.click('#objectList .object-row[data-part-id="/root/Dishwasher_Button001"]')
    page.wait_for_timeout(300)

    summary_text = page.eval_on_selector(
        "#inspector",
        "el => [...el.children]"
        ".filter(c => !c.classList.contains('raw-values'))"
        ".map(c => c.textContent).join(' ')",
    )
    # This drive's authored target sits outside the travel it authored for
    # itself, and the summary is where a reader has to be told that.
    assert "targets" in summary_text
    assert "outside its own limit" in summary_text

    page.eval_on_selector_all(
        "#inspector .raw-values", "els => els.forEach(el => el.open = true)"
    )
    raw_text = page.eval_on_selector_all(
        "#inspector .raw-values",
        "els => els.map(el => el.textContent).join(' ')",
    )
    # Every one of these is already stated, in full, in the summary above.
    for field in (
        "anchor (m)",
        "axis (source)",
        "limit raw",
        "drive applied",
        "drive active",
        "target position",
    ):
        assert field not in raw_text
    _close_report(page)

    # Every other test in this module assumes the cabinet is loaded.
    page.select_option("#assetSelect", "Cabinet")
    _settle_after_load(page)


def test_the_raw_block_never_restates_a_fact_under_a_second_name(page):
    """Same value, different label, is still the same fact told twice.

    `Joint.id` and `Joint.prim_path` are the same string by construction in
    every reader, and the Inspector only ever shows the joint whose child is
    the part on screen -- so its `child_part` always equals that part's own
    `id`. Neither used to know that about the other.
    """
    page.select_option("#assetSelect", "Dishwasher")
    _settle_after_load(page)
    page.click('#objectList .object-row[data-part-id="/root/Dishwasher_Door001"]')
    page.wait_for_timeout(300)

    page.eval_on_selector_all(
        "#inspector .raw-values", "els => els.forEach(el => el.open = true)"
    )
    labels = [
        td.text_content()
        for td in page.query_selector_all("#inspector .raw-values td:first-child")
    ]
    # Exactly one `id` -- the part's own. The joint's would have been the
    # summary's `prim` row again, and `child part` would have been the part's
    # `id` in its own "more" just above.
    assert labels.count("id") == 1
    assert "child part" not in labels


def test_an_absent_drive_schema_does_not_get_six_empty_rows(page):
    """"None — free joint" is one fact, not six.

    A joint with no drive schema at all has nothing further to say about a
    type, a stiffness, a damping, a max force or a target -- printing "not
    authored" six times over restates the same absence six times, once per
    field it happens to have.
    """
    page.select_option("#assetSelect", "Dishwasher")
    _settle_after_load(page)
    page.click('#objectList .object-row[data-part-id="/root/Dishwasher_Door001"]')
    page.wait_for_timeout(300)

    assert "none — free joint" in page.text_content("#inspector")

    page.eval_on_selector_all(
        "#inspector .raw-values", "els => els.forEach(el => el.open = true)"
    )
    labels = {
        td.text_content()
        for td in page.query_selector_all("#inspector .raw-values td:first-child")
    }
    for field in (
        "drive type",
        "stiffness",
        "damping",
        "max force",
        "target position",
        "target velocity",
    ):
        assert field not in labels

    page.select_option("#assetSelect", "Cabinet")
    _settle_after_load(page)


def test_swatches_are_distinct_per_part(page):
    colours = {
        element.evaluate("e => getComputedStyle(e).backgroundColor")
        for element in page.query_selector_all(".dot")
    }
    assert len(colours) == 6


def test_driving_a_joint_moves_the_geometry(page):
    page.click("#btnReset")
    at_rest = _canvas_png(page)

    _set_all_sliders(page, "min")
    opened = _canvas_png(page)
    assert opened != at_rest, "doors at their limit render the same as closed"

    # The pose is the only thing that changed, so returning to it must return
    # the exact same pixels. Without this the assertion above would also pass
    # on nondeterministic rendering.
    page.click("#btnReset")
    assert _canvas_png(page) == at_rest


def test_authored_materials_toggle_changes_the_render_and_is_reversible(page):
    """The toggle promised in app/mesh_export.py's docstring: both variants
    already sit in the rig, so flipping it is a visibility swap, not a
    reload -- and swapping back has to land on the exact same pixels, or
    something in between is leaking state.
    """
    page.click("#btnReset")
    flat = _canvas_png(page)

    _set_surface(page, "materials")
    page.wait_for_timeout(300)
    materials = _canvas_png(page)
    assert materials != flat, "authored materials render identically to the flat palette"

    _set_surface(page, "parts")
    page.wait_for_timeout(300)
    assert _canvas_png(page) == flat


MATERIAL_OWNERS = """() => {
  const owners = {};
  for (const [id, rig] of window.artiLab.rig) {
    const meshes = rig.mesh ? [rig.mesh, ...rig.texturedMeshes] : rig.texturedMeshes;
    for (const mesh of meshes) {
      (owners[mesh.material.uuid] ??= []).push(id);
    }
  }
  return Object.values(owners);
}"""


def test_no_two_parts_share_a_material(page):
    """Selection and fault flagging are painted onto `material.emissive`.

    GLTFLoader hands every mesh built from one glTF material the same
    THREE.Material, and the exporter gives all six cabinet parts the same
    neutral-grey fallback -- so flagging the five doors the engine rejects
    also lit the carcass, and the authored-materials view came out uniformly
    pink. Nothing in the DOM says so, and on screen it reads as a deliberate
    colour, which is why this asserts on the rig instead.
    """
    page.select_option("#assetSelect", "Cabinet")
    _settle_after_load(page)

    owners = page.evaluate(MATERIAL_OWNERS)
    # The cabinet carries a flat mesh plus textured pieces for all six parts,
    # so an empty result would mean the probe found no rig rather than no
    # sharing -- which is the way this test could pass while asserting
    # nothing at all.
    assert len(owners) > 6
    assert [ids for ids in owners if len(set(ids)) > 1] == []


def test_authored_materials_survive_explode_and_isolate(page):
    # These three toggles touch the same rig entries from different angles
    # (visibility, position, opacity) -- the regression this guards is one of
    # them only updating the flat mesh and leaving a textured piece behind.
    _set_surface(page, "materials")
    _set_toggle(page, "#toggleExploded", True)
    _set_toggle(page, "#toggleIsolate", True)
    page.wait_for_timeout(400)
    assert page.errors == []

    _set_toggle(page, "#toggleIsolate", False)
    _set_toggle(page, "#toggleExploded", False)
    _set_surface(page, "parts")
    page.wait_for_timeout(200)


def test_limits_are_enforced(page):
    _set_all_sliders(page, "min")
    readouts = [r.inner_text() for r in page.query_selector_all(".readout")]
    # Four doors swing to -90; the fifth opens the other way, so its lower
    # bound is 0. Getting one value for all five would mean the per-joint
    # limits are not being applied.
    assert readouts == ["-90.0°", "-90.0°", "-90.0°", "-90.0°", "0.0°"]

    _set_all_sliders(page, "max")
    readouts = [r.inner_text() for r in page.query_selector_all(".readout")]
    assert readouts == ["0.0°", "0.0°", "0.0°", "0.0°", "90.0°"]


def test_a_joint_cannot_be_driven_past_its_stop(page):
    _set_all_sliders(page, "min")
    page.query_selector('input[type="range"]').evaluate(
        "s => { s.value = String(Number(s.min) - 500);"
        " s.dispatchEvent(new Event('input', {bubbles:true})); }"
    )
    page.wait_for_timeout(200)
    assert page.query_selector(".readout").inner_text() == "-90.0°"


def test_reset_returns_every_joint_to_zero(page):
    _set_all_sliders(page, "min")
    page.click("#btnReset")
    page.wait_for_timeout(300)
    readouts = [r.inner_text() for r in page.query_selector_all(".readout")]
    assert set(readouts) == {"0.0°"}


def test_the_report_is_ordered_by_what_kind_of_statement_each_finding_is(page):
    """Sections are modality, never provenance.

    A layout split by which check spoke put NVIDIA's 66 primvar advisories
    above a self-contradiction found here, and captioned the section holding
    the contradiction "not a verdict".
    """
    _open_report(page)
    sections = [
        s.get_attribute("id")
        for s in page.query_selector_all("#findingsPanel details.section")
    ]
    assert sections == [
        s
        for s in [
            "errorsSection",
            "warningsSection",
            "declarationsSection",
            "notesSection",
        ]
        if s in sections
    ]
    assert "errorsSection" in sections

    # Provenance does not get a section of its own: NVIDIA's rejections and the
    # contradictions found here are the same kind of statement, so they share
    # one, and the label on each card is the only place the difference shows.
    labels = {
        el.text_content()
        for el in page.query_selector_all("#errorsSection .finding")
    }
    assert len(labels) > 1
    assert "nvidia" in labels
    _close_report(page)


def test_a_card_label_carries_whichever_fact_its_heading_did_not(page):
    """The label is small print and must not spend itself repeating the heading.

    Under "Warnings" every card once read "advises", which is the heading again
    in the tool's own private word. Declarations are the one section where the
    modality earns the slot: they all come from the file, so what separates
    them is stated versus left out.
    """
    _open_report(page)
    warnings = {
        el.text_content()
        for el in page.query_selector_all("#warningsSection .finding")
    }
    assert warnings
    assert "advises" not in warnings

    declarations = {
        el.text_content()
        for el in page.query_selector_all("#declarationsSection .finding")
    }
    assert declarations
    assert declarations <= {"states", "omits"}
    _close_report(page)


def test_findings_render_grouped_by_dimension(page):
    _open_report(page)
    assert len(page.query_selector_all(".finding-row")) > 0

    # The dimension headings are the part worth reading: they name what an
    # articulated asset can vary along, which is the question this tool is
    # answering for the team right now.
    # text_content, not inner_text: the headings are uppercased in CSS and
    # inner_text would return the rendered casing.
    headings = {h.text_content() for h in page.query_selector_all(".dimension h3")}
    assert {"Travel range", "Zero pose", "Mass"} <= headings
    _close_report(page)


def test_a_tool_limit_is_not_filed_as_something_the_asset_did(page):
    """The reader's parsing log used to arrive as observations about the asset.

    Anything this tool could not do belongs in its own section, and never in
    the count that grades the delivery.
    """
    _open_report(page)
    section = page.query_selector("#notesSection")
    if section is not None:
        assert "not about the asset" in section.text_content()
    # Whatever the reader logged, none of it reaches the declarations.
    declares = page.query_selector("#declarationsSection")
    assert "Source parsing" not in (declares.text_content() if declares else "")
    _close_report(page)


def test_the_validator_verdict_lands_on_the_parts_it_concerns(page):
    """The whole point of the integration.

    NVIDIA's validator reports that five prim paths violate a joint rule.
    Alone that tells a reader nothing about which doors are broken, so the
    panel has to name the parts and clicking one has to select it.
    """
    page.select_option("#assetSelect", "Cabinet")
    _settle_after_load(page)
    _open_report(page)
    page.wait_for_selector("#errorsSection .finding-row", timeout=30_000)

    # PhysicsJointChecker fails all five doors on this asset.
    rule = page.query_selector(
        "#errorsSection .dimension:has(h3:text-is('PhysicsJointChecker'))"
    )
    affected = rule.query_selector_all("button.link")
    assert len(affected) == 5

    affected[0].click()
    page.wait_for_timeout(300)
    # The Inspector points back at the report rather than repeating it: a
    # badge naming the count, not the validator's message a second time.
    badge = page.text_content(".inspector-title button.badge.blocking")
    assert "error" in badge
    assert page.query_selector("#inspector .faults") is None
    _close_report(page)


def test_the_inspector_badge_leads_back_to_the_finding_that_named_it(page):
    """The Inspector is an information panel, not a second report.

    Its only word on a fault is a count and a link — following it has to land
    on the exact card the report already wrote, not just the section.
    """
    page.select_option("#assetSelect", "Cabinet")
    _settle_after_load(page)
    page.query_selector_all("#objectList .object-row")[0].click()
    page.wait_for_timeout(300)

    badge = page.query_selector(".inspector-title button.badge.blocking")
    assert badge is not None
    badge.click()
    page.wait_for_timeout(900)

    assert page.query_selector("#dockBody").is_visible()
    assert page.evaluate("document.getElementById('errorsSection').open")
    assert page.query_selector("#errorsSection .finding-row.jump-target") is not None
    _close_report(page)


def test_a_clean_asset_says_so_rather_than_showing_an_empty_panel(page):
    page.select_option("#assetSelect", "Stovetop")
    _settle_after_load(page)
    _open_report(page)
    page.wait_for_selector("#findingsPanel .pill", timeout=30_000)

    assert "Engine rules passed" in page.text_content("#findingsPanel")
    assert page.query_selector("#errorsSection") is None
    _close_report(page)


def test_a_stage_that_is_not_z_up_says_the_picture_is_wrong(page):
    """The viewer assumes Z-up, so a Y-up asset renders lying on its side.

    Nothing in the sample library is Y-up and the conversion in `initViewer` is
    a fixed rotation, so the asset that would prove this banner still fires is
    one nobody has. The manifest is rewritten in flight instead: the frontend
    is what is under test, and the readers already cover reporting the axis
    they read.
    """
    page.route("**/api/manifest/**", _serve_manifest_as(stage_up_axis="Y"))
    try:
        page.select_option("#assetSelect", "Dishwasher")
        page.wait_for_selector("#upAxisBanner:not([hidden])", timeout=30_000)

        banner = page.text_content("#upAxisBanner")
        assert "Y-up" in banner
        # Says which half of the screen to distrust: the panels read straight
        # from the file, so only the picture is turned the wrong way.
        assert "lying on its side" in banner
    finally:
        page.unroute("**/api/manifest/**")

    # The banner is not sticky -- a Z-up asset loaded after one that was not
    # has to come back clean.
    page.select_option("#assetSelect", "Cabinet")
    _settle_after_load(page)
    assert page.query_selector("#upAxisBanner").is_hidden()


def _serve_manifest_as(**fields):
    """Return a route handler serving the real manifest with `fields` replaced.

    Lets a test reach stage conventions no sample asset declares. The handler
    takes the route alone: playwright passes the request as a second argument
    to anything that will accept one, so a spare parameter on it would silently
    become a `Request`.
    """

    def handler(route) -> None:
        body = route.fetch().json()
        body.update(fields)
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(body)
        )

    return handler


def test_a_raw_range_is_labelled_in_the_unit_the_stage_actually_uses(page):
    """"Stage units" is not a unit anyone can check a number against.

    What one is worth is the stage's metres-per-unit, so the label resolves it.
    Otherwise reconciling the authored `-0.3` against the slider's `-300.0 mm`
    means carrying a scale factor from elsewhere on screen.
    """
    page.select_option("#assetSelect", "Dishwasher")
    _settle_after_load(page)
    # A prismatic joint specifically: a revolute one is labelled in degrees and
    # says nothing about stage units. Chosen by part id rather than by taking
    # the first row, which depends on stage order.
    page.click('#objectList .object-row[data-part-id="/root/Dishwasher_Container001"]')
    # This asset declares metersPerUnit 1, so a stage unit is the metre.
    assert "range (m)" in page.text_content("#inspector")

    # An unnamed scale still has to state itself rather than fall back to the
    # word that says nothing.
    cases = [
        ("Dishwasher_with_drive", 0.01, "range (cm)"),
        ("Dishwasher", 2.5, "range (units of 2.5 m)"),
    ]
    for asset, scale, expected in cases:
        page.route(
            "**/api/manifest/**",
            _serve_manifest_as(stage_meters_per_unit=scale),
        )
        try:
            page.select_option("#assetSelect", asset)
            _settle_after_load(page)
            page.click(
                '#objectList .object-row'
                '[data-part-id="/root/Dishwasher_Container001"]'
            )
            assert expected in page.text_content("#inspector")
        finally:
            page.unroute("**/api/manifest/**")

    page.select_option("#assetSelect", "Cabinet")
    _settle_after_load(page)


def test_a_non_portable_asset_names_the_references_that_would_break(page):
    """A reference can resolve here and still not survive being moved.

    It sits with everything else that is wrong with the file rather than in a
    note appended to NVIDIA's section, which was where it used to read as
    their verdict -- and it is precisely the check they do not make.
    """
    page.select_option("#assetSelect", "Cabinet")
    _settle_after_load(page)
    _open_report(page)
    page.wait_for_selector(
        "#findingsPanel .dimension:has(h3:text-is('External references'))",
        timeout=30_000,
    )

    block = page.query_selector(
        "#findingsPanel .dimension:has(h3:text-is('External references'))"
    )
    page.eval_on_selector_all(
        ".finding-more", "els => els.forEach(e => e.open = true)"
    )
    # The authored path, not the absolute one it resolved to -- that is the
    # string someone has to go and fix in the asset.
    assert "../Cabinet2/" in block.text_content()
    _close_report(page)


def test_the_intro_sweep_runs_and_yields_to_the_first_click(page):
    """Loading shows the ranges, then gets out of the reviewer's way."""
    page.select_option("#assetSelect", "Cabinet")
    page.wait_for_function(
        "() => [...document.querySelectorAll('.readout')]"
        ".some(r => Number.parseFloat(r.textContent) !== 0)",
        timeout=20_000,
    )

    # An empty corner of the viewport: enough to claim control, but it selects
    # nothing, so the only thing under test is that the animation stops.
    box = page.query_selector("#canvasHost canvas").bounding_box()
    page.mouse.click(box["x"] + 6, box["y"] + 6)
    page.wait_for_timeout(600)

    readouts = [r.inner_text() for r in page.query_selector_all(".readout")]
    assert set(readouts) == {"0.0°"}, "clicking did not stop the intro sweep"


def test_the_idle_tour_moves_one_joint_at_a_time(tour_page):
    """The point of the tour: motion has to mean "look here".

    Only one joint may be moving at any moment. Five doors swinging together
    spends the strongest cue the eye has on a constant, and then there is no
    way left to point at anything.
    """
    _wait_for_tour(tour_page)

    for _ in range(8):
        moving = _moving_joints(tour_page)
        assert len(moving) <= 1, f"tour moved {len(moving)} joints at once: {moving}"
        tour_page.wait_for_timeout(220)


def test_the_idle_tour_yields_to_the_first_click(tour_page):
    _wait_for_tour(tour_page)

    # An empty corner of the viewport: enough to claim control, but it selects
    # nothing, so the only thing under test is that the animation stops.
    box = tour_page.query_selector("#canvasHost canvas").bounding_box()
    tour_page.mouse.click(box["x"] + 6, box["y"] + 6)

    # Well inside the idle delay: after that the tour is entitled to come back,
    # and this would be asserting the opposite of what it means to.
    tour_page.wait_for_timeout(TOUR_IDLE_MS // 3)

    assert _moving_joints(tour_page) == [], "clicking did not stop the idle tour"
    assert tour_page.get_attribute("body", "data-animation") == ""


def test_swept_volume_and_exploded_view_toggle_cleanly(page):
    page.errors.clear()
    for toggle in ("#toggleEnvelope", "#toggleExploded"):
        _set_toggle(page, toggle, True)
        page.wait_for_timeout(400)
        _set_toggle(page, toggle, False)
        page.wait_for_timeout(400)
    assert page.errors == []


def _outline_visibility(page) -> list:
    """Whether each rejected part's outline is currently drawn."""
    return page.evaluate(
        """() => [...window.artiLab.rig.values()]
             .filter((r) => r.outline)
             .map((r) => r.outline.visible)"""
    )


def _outline_drift(page) -> float:
    """Furthest any outline sits from the mesh it traces."""
    return page.evaluate(
        """() => Math.max(0, ...[...window.artiLab.rig.values()]
             .filter((r) => r.outline && r.mesh)
             .map((r) => r.outline.position.distanceTo(r.mesh.position)))"""
    )


def test_rejected_parts_are_outlined_and_answer_to_their_switch(page):
    """The outline is built during rig assembly but steered from selection, so
    it can reach the screen with nothing driving it and still look right."""
    faulted = len(page.query_selector_all("#objectList .fault-dot"))
    assert faulted, "no rejected parts in this asset to mark"

    # Off by default: a red outline on every part the engine will eventually
    # refuse is a strong claim to make on first look at a file that opens
    # either way.
    assert len(_outline_visibility(page)) == faulted
    assert not any(_outline_visibility(page))

    _set_toggle(page, "#toggleFaults", True)
    assert all(_outline_visibility(page))

    _set_toggle(page, "#toggleFaults", False)
    assert not any(_outline_visibility(page))


def test_the_outline_travels_with_the_part_it_traces(page):
    """A separate object in the same pivot, so whatever repositions the part
    has to reposition it too or the mark ends up describing empty space."""
    assert _outline_drift(page) < 1e-6

    _set_toggle(page, "#toggleExploded", True)
    page.wait_for_timeout(300)
    assert _outline_drift(page) < 1e-6, "the outline stayed behind when the part moved"

    _set_toggle(page, "#toggleExploded", False)
    page.wait_for_timeout(300)


def test_the_closed_display_menu_admits_what_is_off_default(page):
    """Otherwise a mode left on is a silently wrong picture and the only clue
    to why sits folded inside a menu the reader has closed. Picking a surface
    mode flips two radios but is one decision, so it must count once."""
    before = _display_count(page)

    _set_surface(page, "wireframe")
    assert page.is_visible("#displayCount")
    assert _display_count(page) == before + 1, "one decision counted twice"

    _set_toggle(page, "#toggleEnvelope", True)
    assert _display_count(page) == before + 2

    _set_toggle(page, "#toggleEnvelope", False)
    _set_surface(page, "parts")
    assert _display_count(page) == before


def test_resetting_the_view_costs_one_click_with_the_menu_shut(page):
    """A one-shot camera action, so it must not live behind the dropdown of
    standing preferences: this click times out if it moves back inside."""
    page.errors.clear()
    assert page.is_hidden("#displayPanel")
    page.click("#btnFrame")
    page.wait_for_timeout(300)
    assert page.errors == []
    assert page.is_hidden("#displayPanel"), "a camera action opened the menu"


def test_switching_to_the_dishwasher_reloads_cleanly(page):
    page.errors.clear()
    page.select_option("#assetSelect", "Dishwasher")
    _settle_after_load(page)
    assert page.errors == []
    assert len(page.query_selector_all("#objectList input[type=range]")) == 4

    types = [b.inner_text() for b in page.query_selector_all("#objectList .badge")]
    assert types.count("prismatic") == 3
    assert types.count("revolute") == 1


DRAG_FILES = """() => {
  const dt = new DataTransfer();
  dt.items.add(new File(['x'], 'asset.zip', {type: 'application/zip'}));
  return dt;
}"""


def test_the_drop_veil_stays_out_of_the_way_until_a_file_arrives(page):
    # `.drop-veil` sets `display: flex`, which outranks the user agent's
    # `[hidden] { display: none }`. Getting that wrong leaves a full-window
    # veil over the viewer at all times, and every other test still passes.
    veil = page.query_selector("#dropVeil")
    assert not veil.is_visible()

    page.evaluate(
        f"() => window.dispatchEvent(new DragEvent('dragenter', "
        f"{{dataTransfer: ({DRAG_FILES})(), bubbles: true}}))"
    )
    assert veil.is_visible()

    page.evaluate(
        f"() => window.dispatchEvent(new DragEvent('dragleave', "
        f"{{dataTransfer: ({DRAG_FILES})(), bubbles: true}}))"
    )
    assert not veil.is_visible()


def test_dropping_something_unreadable_says_so_and_adds_nothing(page):
    # No `page.errors` assertion here: the refusal is a 400, which the browser
    # always logs to the console. The refusal reaching the screen is the point.
    before = page.eval_on_selector_all("#assetSelect option", "o => o.length")

    page.evaluate(
        f"() => {{"
        f"  const dt = new DataTransfer();"
        f"  dt.items.add(new File(['solid teapot'], 'teapot.stl'));"
        f"  window.dispatchEvent(new DragEvent('drop', "
        f"    {{dataTransfer: dt, bubbles: true, cancelable: true}}));"
        f"}}"
    )
    page.wait_for_function(
        "() => document.body.dataset.upload === 'failed'", timeout=30_000
    )

    assert "not an asset this viewer reads" in page.query_selector(
        "#viewportOverlay"
    ).text_content()
    after = page.eval_on_selector_all("#assetSelect option", "o => o.length")
    assert after == before
