"""End-to-end test for the ranking/security/index pipeline against one real
registry row, run against live external APIs (GitHub, npm, OSV.dev) and a
live Qdrant collection -- deliberately NOT mocked: the class of bug this
test exists to catch (a field silently staying None, two concurrent writers
clobbering each other's writes, a payload field computed but never actually
reaching Qdrant) lives in the interaction between real upstream data and
this pipeline's own merge/payload logic, not inside any one function in
isolation, so a mocked unit test can't see it. All three of those bugs were
found by hand while building this pipeline (see fetch_mcp_rankings.py,
mcp_registry.py, index_qdrant.py's docstrings/comments) -- this test exists
so the next one is caught automatically instead.

TEST_ID (github:githubscum/lotor) is a fixed, real, small, representative
npm-typed row already in the registry -- not a synthetic fixture -- so a
failure here means something about the actual production data or pipeline
changed shape, not that a fixture drifted from reality. Picked because it's
exactly the row a manual review turned up real problems on: weekly_downloads
missing (the downloads phase had never been run for it), and a description
("A local receipt and approval gate for AI agent sessions. The agent can
act, but it cannot sign.") that turns out to be copy-pasted verbatim from
the README's own opening line by all three sources (official_registry,
glama, and the README itself) -- accurate, but wildly under-representative
of what the tool (cryptographic session receipts, delegation grants,
policy-tiered enforcement hooks) actually does. See test_description_vs_readme
below, which prints that comparison for human judgment rather than asserting
on it -- "poor" is inherently a judgment call, not a machine-checkable
property.

Slow and networked -- not part of a fast default test run
(`pytest test_e2e_pipeline.py` explicitly, not swept up by a bare `pytest`
if this directory ever gets broader default collection). Skipped entirely
if the row isn't in registry.json yet, same as test_scan_mcp.py's
mongodb-mcp-server skip -- a missing fixture is "can't test this yet," not
a failure.

KNOWN LIMITATION: if fetch_mcp_rankings.py's background supervisor job
(supervise.sh start rankings ...) is running concurrently, its next
periodic save can silently overwrite the targeted writes this test makes
for TEST_ID (see mcp_registry.py's set_stars/set_security_scan docstrings
and index_qdrant.py's --ids handling for the two related bugs this exact
failure mode already caused during manual testing). This test does not
work around that -- it's a real, unfixed gap (mcp_registry.save_registry()
does a blind whole-file overwrite, no locking) -- it only warns if that
job appears to be running, so a flaky-looking failure has an explanation.
"""

import datetime
import subprocess
import sys

import pytest

# Import order here is load-bearing, not stylistic. pytest puts this file's
# own directory (mcp-search/) at sys.path[0] to collect it at all (no
# __init__.py here -> default rootdir-relative import). search-demo/ (the
# parent) ALSO has an index_qdrant.py -- the skills pipeline's, a
# completely different module with its own POINT_ID_NAMESPACE/COLLECTION
# constants. Every mcp-search/*.py script (mcp_registry.py, fetch_mcp_*.py,
# index_qdrant.py itself) does its own `sys.path.insert(0, parent)` at
# import time to reach `shared.*` -- so importing ANY of them re-inserts
# search-demo/ at sys.path[0], which would shadow mcp-search/'s
# index_qdrant.py for any *later* `import index_qdrant`. Confirmed this
# exact collision by hand while building this pipeline (an ad hoc debug
# snippet silently computed point ids against the wrong namespace this
# way). The fix: import index_qdrant FIRST, while mcp-search/ is still at
# sys.path[0] -- once imported, it's cached in sys.modules under that name
# for the rest of the process, so later sys.path churn from importing
# mcp_registry/fetch_mcp_rankings/etc. can no longer affect it.
from index_qdrant import get_client, COLLECTION, load_points, point_id

import mcp_registry
from export_mcp_csv import first_descriptor_value
from fetch_mcp_rankings import fetch_downloads, fetch_stars
from fetch_mcp_security import fetch_security
from shared.http import default_limiter, github_limiter

TEST_ID = "github:githubscum/lotor"


def _warn_if_background_job_running() -> None:
    result = subprocess.run(["pgrep", "-f", "fetch_mcp_rankings.py"], capture_output=True, text=True)
    if result.stdout.strip():
        print(
            "\n[WARNING] fetch_mcp_rankings.py appears to be running in the background "
            "(likely under supervise.sh) -- its next periodic save can clobber this test's "
            "targeted writes (known gap, see module docstring). If this test fails in a way "
            "that doesn't match the assertion, stop it first: ./supervise.sh stop rankings",
            file=sys.stderr,
        )


@pytest.fixture(scope="module")
def registry():
    reg = mcp_registry.load_registry()
    if not any(r["id"] == TEST_ID for r in reg):
        pytest.skip(f"{TEST_ID} not in mcp-repo-seeds/registry.json -- pull the pipeline first")
    return reg


def _replay_source_upserts(registry, index) -> None:
    """Re-run the REAL pull_glama.py/pull_official_registry.py upsert_entry()
    functions for TEST_ID only, against the already-cached raw dumps
    (mcp-search-raw/{glama,official_registry}.json) -- no network call, and
    not a test-only shortcut: this exercises the actual production upsert
    path. Needed because mcp_registry.upsert() used to pop each source's
    `description` before storing its descriptor (fixed -- see upsert()'s
    docstring), so any row pulled before that fix has a source descriptor
    missing `description` even though the raw dump on disk has always had
    it. A full re-pull would pick this up naturally (pull_glama.py's own
    upsert is idempotent), but that's a ~19-20h run (see
    fetch_mcp_rankings.py's docstring for the same order-of-magnitude
    figure on a comparable full-registry pass) -- replaying just this one
    row's already-downloaded raw items is the same real code path at test
    speed."""
    import json

    import pull_glama
    import pull_official_registry

    glama_path = mcp_registry.RAW_DIR / "glama.json"
    if glama_path.exists():
        for item in json.loads(glama_path.read_text()):
            repo_url = (item.get("repository") or {}).get("url")
            entry_id = mcp_registry.make_id(repo_url, "glama", item.get("slug") or item.get("id"))
            if entry_id == TEST_ID:
                pull_glama.upsert_entry(registry, item, index)
                break

    official_path = mcp_registry.RAW_DIR / "official_registry.json"
    if official_path.exists():
        for item in json.loads(official_path.read_text()):
            server = item.get("server", {})
            repo_url = (server.get("repository") or {}).get("url")
            entry_id = mcp_registry.make_id(repo_url, "official_registry", server.get("name"))
            if entry_id == TEST_ID:
                pull_official_registry.upsert_entry(registry, item, index)
                break


@pytest.fixture(scope="module")
def refreshed_row(registry):
    """Run the real fetch phases for TEST_ID only (real network calls, real
    registry.json write) and return the resulting row. scope="module" so
    all tests in this file share one fetch instead of hitting GitHub/npm/OSV
    three separate times for the same row."""
    _warn_if_background_job_running()
    index = mcp_registry.build_index(registry)
    only_ids = {TEST_ID}

    fetch_stars(registry, index, github_limiter(), limit=None, rescan=True, stale_days=0, only_ids=only_ids)
    fetch_downloads(registry, index, default_limiter(), limit=None, rescan=True, stale_days=0, only_ids=only_ids)
    fetch_security(registry, index, default_limiter(), limit=None, random_sample=None, rescan=True, stale_days=0,
                    only_ids=only_ids)
    _replay_source_upserts(registry, index)
    mcp_registry.save_registry(registry)

    return mcp_registry.find(registry, TEST_ID)


def _assert_recent_iso_timestamp(value: str | None, field_name: str) -> None:
    assert value is not None, f"{field_name} is missing -- this fetch phase never ran for {TEST_ID}"
    parsed = datetime.datetime.fromisoformat(value)  # raises ValueError -> test failure if malformed
    age = datetime.datetime.now() - parsed
    assert age < datetime.timedelta(hours=1), f"{field_name}={value} is not from this test run (age={age})"


class TestRegistryData:
    """registry.json-level assertions -- does the real fetch pipeline
    actually populate every field it claims to, for a real row."""

    def test_stars_populated(self, refreshed_row):
        assert refreshed_row.get("stars") is not None
        _assert_recent_iso_timestamp(refreshed_row.get("stars_updated"), "stars_updated")

    def test_language_populated(self, refreshed_row):
        # Captured free off the same GitHub call as stars (see
        # mcp_registry.set_stars) -- if stars populates but language
        # doesn't, that call's response shape changed upstream.
        assert refreshed_row.get("language"), "language missing -- GitHub repo API response shape may have changed"

    def test_weekly_downloads_not_zero(self, refreshed_row):
        """The concrete regression this test exists to catch: this row's
        weekly_downloads was found silently None after a supposedly-successful
        pipeline run, because the downloads phase was never actually invoked
        for it (a --stars-only run doesn't touch downloads -- operator error,
        not a code bug, but exactly the kind of silent gap an assertion like
        this is meant to catch going forward)."""
        weekly = refreshed_row.get("weekly_downloads")
        assert weekly is not None, "weekly_downloads is missing -- downloads phase didn't run or found no data"
        assert weekly > 0, (
            f"weekly_downloads={weekly} -- this package has known real installs (verified manually via "
            f"api.npmjs.org), so exactly 0 here means the fetch itself is broken, not that the package is unused"
        )
        _assert_recent_iso_timestamp(refreshed_row.get("downloads_updated"), "downloads_updated")

    def test_security_scan_ran(self, refreshed_row):
        assert refreshed_row.get("security_source") == "osv"
        assert refreshed_row.get("security_vuln_count") is not None
        assert refreshed_row.get("security_vuln_ids") is not None
        _assert_recent_iso_timestamp(refreshed_row.get("security_updated"), "security_updated")

    def test_direct_dependency_scan_ran(self, refreshed_row):
        """Regression guard for the gap found by hand: the top-level OSV
        scan alone completely misses a vulnerable DEPENDENCY of an
        otherwise-clean package. Concretely true for this exact row:
        lotor-mcp itself has 0 known vulns, but its one direct dependency
        (@modelcontextprotocol/sdk) has 3 (GHSA-345p-7cg4-v4c7,
        GHSA-8r9q-7v3j-jr4g, GHSA-w48q-cv73-mx4w; max severity HIGH) --
        confirmed live against OSV.dev while building this. This is
        DIRECT dependencies only, not a full transitive tree -- see
        fetch_mcp_security.py's "DEPENDENCY COVERAGE" docstring section."""
        assert refreshed_row.get("security_direct_deps_scanned") is not None
        assert refreshed_row.get("security_direct_deps_scanned") >= 1, (
            "lotor-mcp declares @modelcontextprotocol/sdk as a dependency -- 0 scanned means "
            "fetch_direct_dependencies() didn't find it (npm manifest fetch broke, or the field's empty)"
        )
        assert refreshed_row.get("security_direct_deps_vuln_count", 0) > 0, (
            "expected >0: @modelcontextprotocol/sdk has known OSV advisories as of when this test was written "
            "(if this now legitimately fails because those were fixed/delisted upstream, that's real news, "
            "not a broken test -- verify at https://osv.dev before assuming this assertion is stale)"
        )
        assert "@modelcontextprotocol/sdk" in (refreshed_row.get("security_direct_deps_with_vulns") or [])
        # Findings 7/8: the dep pass also aggregates severity + ids, kept
        # separate from the package-own security_max_severity above.
        assert refreshed_row.get("security_direct_deps_max_severity") == "HIGH", (
            "@modelcontextprotocol/sdk's advisories were HIGH when this was written "
            "(GHSA-345p-7cg4-v4c7 et al.) -- re-check osv.dev if this legitimately changed"
        )
        assert refreshed_row.get("security_direct_deps_vuln_ids"), "dep advisory ids not collected"

    def test_readme_already_fetched(self, refreshed_row):
        # Not re-fetched by this test (download_readmes.py's job, not
        # fetch_mcp_rankings/fetch_mcp_security's) -- just confirms the
        # third "last updated" clock is present on a real row.
        assert refreshed_row.get("readme_fetched") is not None
        assert refreshed_row.get("readme_path") is not None

    def test_three_last_updated_clocks_are_independent(self, refreshed_row):
        """The three "last updated" timestamps track three genuinely
        different upstream fetches (GitHub readme pull, GitHub stars,
        npm info) on three different schedules -- they should exist as
        three distinct fields, not be collapsed into one, and in this test
        run (readme NOT re-fetched, stars/downloads/security all just
        re-fetched) readme_fetched should predate the other three."""
        readme_ts = datetime.datetime.fromisoformat(refreshed_row["readme_fetched"])
        stars_ts = datetime.datetime.fromisoformat(refreshed_row["stars_updated"])
        downloads_ts = datetime.datetime.fromisoformat(refreshed_row["downloads_updated"])
        security_ts = datetime.datetime.fromisoformat(refreshed_row["security_updated"])
        assert readme_ts < stars_ts, "readme_fetched should predate this run's fresh stars_updated"
        assert readme_ts < downloads_ts, "readme_fetched should predate this run's fresh downloads_updated"
        assert readme_ts < security_ts, "readme_fetched should predate this run's fresh security_updated"


@pytest.fixture(scope="module")
def payload(refreshed_row):
    points = list(load_points([refreshed_row]))
    assert len(points) == 1
    return points[0]


class TestQdrantPayload:
    """Does the data that just landed in registry.json actually make it
    into the Qdrant payload -- this is where the "field computed but never
    added to load_points()" class of bug lives (found twice already:
    stars_updated/downloads_updated/security_updated and readme_fetched
    were all missing from the payload until this test's own review pass)."""

    def test_ranking_fields_in_payload(self, payload):
        for field in ("stars", "weekly_downloads", "monthly_downloads", "language", "package_manager"):
            assert payload.get(field) is not None, f"{field} present in registry.json but missing from Qdrant payload"

    def test_security_fields_in_payload(self, payload):
        assert payload.get("security_source") == "osv"
        assert payload.get("security_vuln_count") is not None

    def test_direct_dep_security_fields_in_payload(self, payload):
        assert payload.get("security_direct_deps_scanned") is not None
        assert payload.get("security_direct_deps_vuln_count") is not None
        assert "@modelcontextprotocol/sdk" in (payload.get("security_direct_deps_with_vulns") or [])
        assert payload.get("security_direct_deps_max_severity") == "HIGH"
        assert payload.get("security_direct_deps_vuln_ids")

    def test_three_timestamps_in_payload(self, payload):
        for field in ("readme_updated", "stars_updated", "downloads_updated", "security_updated"):
            assert payload.get(field) is not None, f"{field} missing from Qdrant payload"

    def test_point_id_is_deterministic(self, refreshed_row, payload):
        assert payload["id"] == point_id(TEST_ID)
        assert payload["mcp_id"] == TEST_ID


@pytest.fixture(scope="module")
def readme_text(refreshed_row):
    readme_path = refreshed_row.get("readme_path")
    assert readme_path, "no readme_path on this row -- download_readmes.py hasn't run for it"
    full_path = mcp_registry.MCP_DIR.parent / readme_path
    assert full_path.exists(), f"readme_path {full_path} set on the row but the file doesn't exist on disk"
    return full_path.read_text(errors="ignore")


class TestDescriptionCapture:
    """Glama alone was found not to be enough -- sometimes it synthesizes a
    genuinely richer summary than any other source (see
    test-data/openzim-mcp-cluster/DESCRIPTION_COMPARISON.md), sometimes it
    just echoes the README's own opening line verbatim, adding nothing
    (this row: confirmed identical). Either way, this pipeline now captures
    BOTH signals distinctly instead of collapsing them into one lossy merged
    `description` -- Glama's own raw text stays on its source descriptor
    (mcp_registry.upsert() no longer pops it), and a README-derived excerpt
    is available via mcp_registry.extract_readme_description(). These tests
    assert both are actually present for this row, not just that the
    machinery exists in the abstract."""

    def test_glama_description_captured_on_source(self, refreshed_row):
        """The regression this guards: mcp_registry.upsert() used to pop
        `description` before building each source's descriptor, so
        sources[].description was always absent -- seeing what Glama
        actually said required manually cross-referencing the raw
        mcp-search-raw/glama.json dump by id. Now it should just be there."""
        glama_source = next((s for s in refreshed_row.get("sources", []) if s["type"] == "glama"), None)
        assert glama_source is not None, f"{TEST_ID} has no glama source at all -- can't test glama capture on it"
        assert glama_source.get("description"), (
            "glama source descriptor has no description -- either the upsert() fix regressed, "
            "or _replay_source_upserts didn't actually find/apply the matching glama.json entry"
        )

    def test_readme_description_extracted(self, readme_text):
        extracted = mcp_registry.extract_readme_description(readme_text)
        assert extracted, "extract_readme_description() found no paragraph-shaped content in a real README"
        assert len(extracted) > 20

    def test_glama_and_readme_are_genuinely_distinct_signals(self, refreshed_row, readme_text):
        """Not asserting they DIFFER (this exact row is a case where they're
        identical -- Glama just echoed the README's tagline verbatim, which
        is itself the point being tested and documented, not a failure).
        Asserting instead that both are independently retrievable without
        going through the lossy merged `description` field -- i.e. the
        capture is real, whether or not the two happen to agree this time."""
        glama_source = next(s for s in refreshed_row.get("sources", []) if s["type"] == "glama")
        glama_description = glama_source.get("description")
        readme_description = mcp_registry.extract_readme_description(readme_text)
        assert glama_description is not None
        assert readme_description is not None
        # both happen to derive from the same maintainer-written tagline for
        # THIS row -- documented, expected, not a bug (see class docstring)
        print(f"\nglama == readme-extracted for {TEST_ID}: {glama_description.strip() == readme_description.strip()}")


def test_description_vs_readme(refreshed_row):
    """Not an assertion -- "the description is poor" is a human judgment
    call, not a machine-checkable property (see module docstring). This
    prints the same side-by-side a human reviewer needs, every run, so it's
    always current instead of a stale one-off doc. The one thing this DOES
    assert is a basic non-emptiness/length sanity floor -- catching "field
    completely blank" (a real bug) without pretending to catch "field is
    technically accurate but undersells the product" (not a bug, a judgment
    call)."""
    description = refreshed_row.get("description") or ""
    assert len(description) > 20, "description is essentially empty -- that IS a bug, not a judgment call"

    readme_path = refreshed_row.get("readme_path")
    readme_text = ""
    if readme_path:
        full_path = mcp_registry.MCP_DIR.parent / readme_path
        if full_path.exists():
            readme_text = full_path.read_text(errors="ignore")

    glama_source = next((s for s in refreshed_row.get("sources", []) if s["type"] == "glama"), None)
    glama_description = glama_source.get("description") if glama_source else None
    readme_description = mcp_registry.extract_readme_description(readme_text) if readme_text else None

    print(f"\n{'=' * 80}\nDESCRIPTION vs README vs GLAMA -- {TEST_ID}\n{'=' * 80}")
    print(f"stored (merged) description ({len(description)} chars):\n  {description}\n")
    if glama_source is None:
        print("glama did not list this server\n")
    elif glama_description is None:
        print("glama listed this server but its source descriptor has no description\n")
    else:
        same = glama_description.strip() == description.strip()
        print(f"glama's own description ({len(glama_description)} chars, {'IDENTICAL to stored' if same else 'DIFFERS from stored'}):\n  {glama_description}\n")
    print(f"readme ({len(readme_text)} chars) -- extracted intro:\n  {readme_description!r}\n")
    if readme_text:
        print(f"stored description is {100 * len(description) / len(readme_text):.1f}% of readme length")
    else:
        print("no readme on disk to compare against")
