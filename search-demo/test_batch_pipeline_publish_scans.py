from __future__ import annotations

import sys
from pathlib import Path

import batch_pipeline
import publish_scans


def test_no_publish_flag_preserves_existing_batch_command_sequence(
    monkeypatch, tmp_path: Path
) -> None:
    # Given: one confirmed repository and enough space for exactly one batch.
    commands: list[list[str]] = []
    free_space = iter((batch_pipeline.MIN_FREE_BYTES, 0))
    monkeypatch.setattr(batch_pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(batch_pipeline, "CLEAN_REPOS_SCRIPT", tmp_path / "clean_repos.sh")
    monkeypatch.setattr(batch_pipeline, "free_bytes", lambda: next(free_space))
    monkeypatch.setattr(batch_pipeline, "repo_pairs", lambda source=None: [("owner", "repo")])
    monkeypatch.setattr(batch_pipeline, "load_clone_state", lambda: {"owner/repo": 1})
    monkeypatch.setattr(batch_pipeline, "mark_synced_pairs", lambda pairs: list(pairs))
    monkeypatch.setattr(batch_pipeline, "run", lambda command: commands.append(command))
    monkeypatch.setattr("sys.argv", ["batch_pipeline.py", "--batch-size", "1"])

    # When: the batch pipeline runs without the opt-in publishing flag.
    batch_pipeline.main()

    # Then: its process sequence remains the pre-publication sequence.
    assert commands == [
        [sys.executable, "clone_repos.py", "--offset", "0", "1"],
        [sys.executable, "extract_search_raw.py"],
        ["bash", str(tmp_path / "clean_repos.sh")],
        [sys.executable, "index_qdrant.py"],
    ]


class FakeClient:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("client-close")


class FakePreparedPublisher:
    def __init__(self, events: list[str]) -> None:
        self.client = FakeClient(events)


def _configure_one_batch(
    monkeypatch, tmp_path: Path, events: list[str], *, skip_index: bool = False, stats: bool = False
) -> None:
    monkeypatch.setattr(batch_pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(batch_pipeline, "CLEAN_REPOS_SCRIPT", tmp_path / "clean_repos.sh")
    free_space = iter((batch_pipeline.MIN_FREE_BYTES, 0))
    monkeypatch.setattr(batch_pipeline, "free_bytes", lambda: next(free_space))
    monkeypatch.setattr(batch_pipeline, "repo_pairs", lambda source=None: [("owner", "repo")])
    monkeypatch.setattr(batch_pipeline, "load_clone_state", lambda: {"owner/repo": 1})
    monkeypatch.setattr(batch_pipeline, "mark_synced_pairs", lambda pairs: list(pairs))

    def record(command: list[str]) -> None:
        match command[1]:
            case "clone_repos.py":
                events.append("clone")
            case "extract_search_raw.py":
                events.append("extract")
            case "index_qdrant.py":
                events.append("index")
            case _:
                events.append("cleanup")

    monkeypatch.setattr(batch_pipeline, "run", record)
    arguments = ["batch_pipeline.py", "--batch-size", "1", "--publish-scans"]
    if skip_index:
        arguments.append("--skip-index")
    if stats:
        arguments.append("--stats")
    monkeypatch.setattr("sys.argv", arguments)


def test_publish_scans_publishes_current_batch_skills_before_cleanup_and_closes_client(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    # Given: current and stale extracted repos, each with a skill path.
    (tmp_path / "search-raw" / "owner" / "repo" / "indexed").mkdir(parents=True)
    (tmp_path / "search-raw" / "owner" / "repo" / "indexed" / "SKILL.md").write_text("indexed")
    (tmp_path / "search-raw" / "owner" / "repo" / "unindexed").mkdir()
    (tmp_path / "search-raw" / "owner" / "repo" / "unindexed" / "skill.MD").write_text("unindexed")
    (tmp_path / "search-raw" / "stale" / "repo" / "stale-skill").mkdir(parents=True)
    (tmp_path / "search-raw" / "stale" / "repo" / "stale-skill" / "SKILL.md").write_text("stale")
    events: list[str] = []
    prepared = FakePreparedPublisher(events)
    _configure_one_batch(monkeypatch, tmp_path, events, skip_index=True)
    monkeypatch.setattr(
        publish_scans.PublishConfig,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(publish_scans, "preflight", lambda config: prepared)

    published: list[Path] = []

    def publish(skill_dirs: list[Path], actual_prepared: FakePreparedPublisher) -> publish_scans.PublishSummary:
        assert actual_prepared is prepared
        events.append("publish")
        published.extend(skill_dirs)
        return publish_scans.PublishSummary(2, 1, 1, 0, ())

    monkeypatch.setattr(publish_scans, "publish_skill_directories", publish)

    # When: the opt-in scan publication run skips final indexing.
    batch_pipeline.main()

    # Then: only current batch skill directories publish before cleanup and the client closes.
    assert published == [
        tmp_path / "search-raw" / "owner" / "repo" / "indexed",
        tmp_path / "search-raw" / "owner" / "repo" / "unindexed",
    ]
    assert events == ["clone", "extract", "publish", "cleanup", "client-close"]
    output = capsys.readouterr()
    assert "attempted=2 succeeded=1 skipped=1 failed=0" in output.out
    print(f"manual skip-index events={events}")
    print(f"manual skip-index published={published}")
    print(output.out, end="")


def test_publish_failure_keeps_current_batch_indexing_then_exits_one(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    # Given: a publisher that reports a skill-level failure after one batch.
    (tmp_path / "search-raw" / "owner" / "repo" / "skill").mkdir(parents=True)
    (tmp_path / "search-raw" / "owner" / "repo" / "skill" / "SKILL.md").write_text("skill")
    events: list[str] = []
    prepared = FakePreparedPublisher(events)
    _configure_one_batch(monkeypatch, tmp_path, events)
    monkeypatch.setattr(
        publish_scans.PublishConfig,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(publish_scans, "preflight", lambda config: prepared)
    failure = publish_scans.PublishFailure(tmp_path / "search-raw" / "owner" / "repo" / "skill", "scan failed")
    monkeypatch.setattr(
        publish_scans,
        "publish_skill_directories",
        lambda skill_dirs, actual_prepared: (
            events.append("publish")
            or publish_scans.PublishSummary(1, 0, 0, 1, (failure,))
        ),
    )

    # When: the publisher reports a failure during a normal indexing run.
    try:
        batch_pipeline.main()
    except SystemExit as error:
        exit_code = error.code
    else:
        raise AssertionError("expected publish failures to produce exit code 1")

    # Then: the client closes before current-batch indexing and the failure is reported.
    assert exit_code == 1
    assert events == ["clone", "extract", "client-close", "index", "publish", "cleanup", "client-close"]
    assert "attempted=1 succeeded=0 skipped=0 failed=1" in capsys.readouterr().out


def test_publish_preflight_error_exits_two_before_clone(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    # Given: publishing configuration cannot be loaded.
    events: list[str] = []
    _configure_one_batch(monkeypatch, tmp_path, events)

    def invalid_config(cls):
        raise publish_scans.ConfigurationError("configuration missing")

    monkeypatch.setattr(publish_scans.PublishConfig, "from_env", classmethod(invalid_config))

    # When: scan publication is requested.
    try:
        batch_pipeline.main()
    except SystemExit as error:
        exit_code = error.code
    else:
        raise AssertionError("expected invalid publication preflight to exit")

    # Then: no clone starts and the preflight failure has the documented exit code.
    assert exit_code == 2
    assert events == []
    assert "preflight failed: configuration missing" in capsys.readouterr().err


def test_publish_scans_closes_client_before_per_batch_stats_index(
    monkeypatch, tmp_path: Path
) -> None:
    # Given: one confirmed skill with per-batch indexing enabled.
    (tmp_path / "search-raw" / "owner" / "repo" / "skill").mkdir(parents=True)
    (tmp_path / "search-raw" / "owner" / "repo" / "skill" / "SKILL.md").write_text("skill")
    events: list[str] = []
    prepared = FakePreparedPublisher(events)
    _configure_one_batch(monkeypatch, tmp_path, events, stats=True)
    monkeypatch.setattr(
        publish_scans.PublishConfig,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(publish_scans, "preflight", lambda config: prepared)
    monkeypatch.setattr(
        publish_scans,
        "publish_skill_directories",
        lambda skill_dirs, actual_prepared: (
            events.append("publish")
            or publish_scans.PublishSummary(1, 1, 0, 0, ())
        ),
    )
    monkeypatch.setattr(batch_pipeline, "run_stats_to_log", lambda batch_num: events.append("stats"))

    # When: a stats run publishes its current batch.
    batch_pipeline.main()

    # Then: indexing precedes publication and stats still run after the batch index.
    assert events == ["clone", "extract", "client-close", "index", "publish", "cleanup", "stats", "client-close"]


def test_publish_scans_indexes_before_publishing_and_marks_only_afterward(
    monkeypatch, tmp_path: Path
) -> None:
    # Given: one confirmed extracted skill for a normal publishing run.
    skill_dir = tmp_path / "search-raw" / "owner" / "repo" / "skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("skill")
    events: list[str] = []
    prepared = FakePreparedPublisher(events)
    _configure_one_batch(monkeypatch, tmp_path, events)
    monkeypatch.setattr(batch_pipeline, "mark_synced_pairs", lambda pairs: events.append("mark") or list(pairs))
    preflight_calls = 0

    def preflight(config):
        nonlocal preflight_calls
        preflight_calls += 1
        return prepared

    monkeypatch.setattr(
        publish_scans.PublishConfig,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(publish_scans, "preflight", preflight)
    monkeypatch.setattr(
        publish_scans,
        "publish_skill_directories",
        lambda skill_dirs, actual_prepared: (
            events.append("publish")
            or publish_scans.PublishSummary(1, 1, 0, 0, ())
        ),
    )

    # When: scan publication runs with indexing enabled.
    batch_pipeline.main()

    # Then: current-batch indexing completes before publication and no redundant final index runs.
    assert events == ["clone", "extract", "client-close", "index", "publish", "mark", "cleanup", "client-close"]
    assert preflight_calls == 2
    print(f"manual indexed-publish events={events}")
    print(f"manual indexed-publish preflight_calls={preflight_calls}")


def test_publish_failure_leaves_repo_unsynced_after_indexing(
    monkeypatch, tmp_path: Path
) -> None:
    # Given: a confirmed skill whose publication fails.
    skill_dir = tmp_path / "search-raw" / "owner" / "repo" / "skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("skill")
    events: list[str] = []
    prepared = FakePreparedPublisher(events)
    _configure_one_batch(monkeypatch, tmp_path, events)
    marked: list[list[tuple[str, str]]] = []
    monkeypatch.setattr(batch_pipeline, "mark_synced_pairs", lambda pairs: marked.append(list(pairs)) or list(pairs))
    monkeypatch.setattr(
        publish_scans.PublishConfig,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(publish_scans, "preflight", lambda config: prepared)
    failure = publish_scans.PublishFailure(skill_dir, "scan failed")
    monkeypatch.setattr(
        publish_scans,
        "publish_skill_directories",
        lambda skill_dirs, actual_prepared: (
            events.append("publish")
            or publish_scans.PublishSummary(1, 0, 0, 1, (failure,))
        ),
    )

    # When: indexing succeeds but publication fails.
    try:
        batch_pipeline.main()
    except SystemExit as error:
        exit_code = error.code
    else:
        raise AssertionError("expected publication failure to exit one")

    # Then: the repo is left unsynced for a later retry after its index pass.
    assert exit_code == 1
    assert marked == [[]]
    assert events == ["clone", "extract", "client-close", "index", "publish", "cleanup", "client-close"]


def test_publish_failure_does_not_stop_later_batch_or_per_batch_indexing(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    # Given: two confirmed batches and a publisher that fails only the first one.
    for owner, repo in (("first", "repo"), ("second", "repo")):
        skill_dir = tmp_path / "search-raw" / owner / repo / "skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(owner)
    events: list[str] = []
    prepared = FakePreparedPublisher(events)
    monkeypatch.setattr(batch_pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(batch_pipeline, "CLEAN_REPOS_SCRIPT", tmp_path / "clean_repos.sh")
    free_space = iter((batch_pipeline.MIN_FREE_BYTES, batch_pipeline.MIN_FREE_BYTES, 0))
    monkeypatch.setattr(batch_pipeline, "free_bytes", lambda: next(free_space))
    monkeypatch.setattr(batch_pipeline, "repo_pairs", lambda source=None: [("first", "repo"), ("second", "repo")])
    monkeypatch.setattr(batch_pipeline, "load_clone_state", lambda: {"first/repo": 1, "second/repo": 1})
    monkeypatch.setattr(batch_pipeline, "mark_synced_pairs", lambda pairs: list(pairs))

    def record(command: list[str]) -> None:
        match command[1]:
            case "clone_repos.py":
                events.append("clone")
            case "extract_search_raw.py":
                events.append("extract")
            case "index_qdrant.py":
                events.append("index")
            case _:
                events.append("cleanup")

    monkeypatch.setattr(batch_pipeline, "run", record)
    monkeypatch.setattr("sys.argv", ["batch_pipeline.py", "--batch-size", "1", "--publish-scans"])
    monkeypatch.setattr(
        publish_scans.PublishConfig,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(publish_scans, "preflight", lambda config: prepared)
    first_failure = publish_scans.PublishFailure(
        tmp_path / "search-raw" / "first" / "repo" / "skill", "scan failed"
    )
    summaries = iter((
        publish_scans.PublishSummary(1, 0, 0, 1, (first_failure,)),
        publish_scans.PublishSummary(1, 1, 0, 0, ()),
    ))
    monkeypatch.setattr(
        publish_scans,
        "publish_skill_directories",
        lambda skill_dirs, actual_prepared: (
            events.append("publish")
            or next(summaries)
        ),
    )

    # When: the first batch reports a publication failure.
    try:
        batch_pipeline.main()
    except SystemExit as error:
        exit_code = error.code
    else:
        raise AssertionError("expected aggregate publication failure to exit one")

    # Then: the second batch and each batch index still run before the nonzero exit.
    assert exit_code == 1
    assert events == [
        "clone", "extract", "client-close", "index", "publish", "cleanup",
        "clone", "extract", "client-close", "index", "publish", "cleanup",
        "client-close",
    ]
    output = capsys.readouterr()
    assert "final: attempted=2 succeeded=1 skipped=0 failed=1" in output.out
    assert "failed" in output.err
    print(f"manual failure events={events}")
    print(output.out, end="")
    print(output.err, end="", file=sys.stderr)
