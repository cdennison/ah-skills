import sys
from typing import Self

import pytest
from qdrant_client import QdrantClient, models

import index_qdrant


class _ListVector:
    def __init__(self, values: list[float] | list[int]) -> None:
        self.values = values

    def tolist(self) -> list[float] | list[int]:
        return self.values


class _SparseEmbedding:
    def __init__(self) -> None:
        self.indices = _ListVector([0])
        self.values = _ListVector([1.0])


class _DenseEmbedder:
    def embed(self, texts: list[str], *, batch_size: int) -> list[_ListVector]:
        return [_ListVector([0.0, 0.0]) for _ in texts]


class _SparseEmbedder:
    def embed(self, texts: list[str], *, batch_size: int) -> list[_SparseEmbedding]:
        return [_SparseEmbedding() for _ in texts]


class _Progress:
    def __init__(self, **_: str | float) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def update(self, _: int) -> None:
        return None


def _client_with_collection() -> QdrantClient:
    client = QdrantClient(":memory:")
    client.create_collection(
        index_qdrant.COLLECTION,
        vectors_config={
            index_qdrant.DENSE_VECTOR_NAME: models.VectorParams(
                size=2,
                distance=models.Distance.COSINE,
            ),
        },
        sparse_vectors_config={
            index_qdrant.SPARSE_VECTOR_NAME: models.SparseVectorParams(),
        },
    )
    return client


def _location(path: str) -> index_qdrant.LocationPayload:
    return {
        "owner": "owner",
        "repo": "repo",
        "path": path,
        "repo_url": "https://github.com/owner/repo",
        "skill_url": "https://github.com/owner/repo/blob/HEAD/skills/example/SKILL.md",
        "sources": ["seed"],
        "stars": 1,
        "ranking": "",
        "language": "en",
        "agent_compatibility": [],
    }


def _point(point_id: str, payload: dict[str, index_qdrant.JsonValue]) -> models.PointStruct:
    return models.PointStruct(
        id=point_id,
        vector={
            index_qdrant.DENSE_VECTOR_NAME: [0.0, 0.0],
            index_qdrant.SPARSE_VECTOR_NAME: models.SparseVector(indices=[0], values=[1.0]),
        },
        payload=payload,
    )


def _stored_locations(client: QdrantClient, point_id: str) -> list[index_qdrant.LocationPayload]:
    point = client.retrieve(
        index_qdrant.COLLECTION, ids=[point_id], with_payload=True, with_vectors=False
    )[0]
    return index_qdrant._stored_locations(point.payload)


def test_refresh_metadata_preserves_arbitrary_location_fields(monkeypatch) -> None:
    # Given
    client = _client_with_collection()
    point_id = "00000000-0000-0000-0000-000000000001"
    location: index_qdrant.LocationPayload = {
        **_location("owner/repo/skills/example/SKILL.md"),
        "arbitrary_location_field": {"kept": True},
        "vettd_scan_publications": [{"receipt": "existing"}],
    }
    refresh_payload: dict[str, index_qdrant.JsonValue] = {
        "locations": [location],
        "sources": ["seed"],
        "stars": 1,
        "ranking": "",
        "language": "en",
    }
    client.upsert(
        index_qdrant.COLLECTION,
        points=[_point(point_id, refresh_payload)],
    )
    monkeypatch.setattr(
        index_qdrant.registry,
        "load_registry",
        lambda: [{"owner": "owner", "repo": "repo", "sources": [{"type": "seed"}], "stars": 2}],
    )

    # When
    updated = index_qdrant.refresh_metadata(client)

    # Then
    stored_location = _stored_locations(client, point_id)[0]
    assert updated == 1
    assert stored_location["arbitrary_location_field"] == {"kept": True}
    assert stored_location["vettd_scan_publications"] == [{"receipt": "existing"}]


def test_prune_stale_locations_retains_receipts_for_remaining_paths() -> None:
    # Given
    client = _client_with_collection()
    point_id = "00000000-0000-0000-0000-000000000008"
    kept_path = "owner/repo/skills/kept/SKILL.md"
    stale_path = "owner/repo/skills/stale/SKILL.md"
    client.upsert(
        index_qdrant.COLLECTION,
        points=[
            _point(
                point_id,
                {
                    "locations": [
                        {**_location(kept_path), "vettd_scan_publications": [{"receipt": "kept"}]},
                        {**_location(stale_path), "vettd_scan_publications": [{"receipt": "stale"}]},
                    ]
                },
            )
        ],
    )

    # When
    deleted, updated = index_qdrant.prune_stale_locations(client, {kept_path})

    # Then
    stored_location = _stored_locations(client, point_id)[0]
    assert (deleted, updated) == (0, 1)
    assert stored_location["path"] == kept_path
    assert stored_location["vettd_scan_publications"] == [{"receipt": "kept"}]


def test_upload_preserves_distinct_receipts_for_duplicate_content_locations(monkeypatch) -> None:
    # Given
    client = _client_with_collection()
    point_id = "00000000-0000-0000-0000-000000000002"
    first_path = "owner/repo/skills/first/SKILL.md"
    second_path = "owner/repo/skills/second/SKILL.md"
    old_locations: list[index_qdrant.LocationPayload] = [
        {**_location(first_path), "vettd_scan_publications": [{"receipt": "first"}]},
        {**_location(second_path), "vettd_scan_publications": [{"receipt": "second"}]},
    ]
    duplicate_payload: dict[str, index_qdrant.JsonValue] = {"locations": old_locations}
    client.upsert(index_qdrant.COLLECTION, points=[_point(point_id, duplicate_payload)])
    monkeypatch.setattr(
        index_qdrant,
        "get_embedder",
        lambda _name, sparse, threads: _SparseEmbedder() if sparse else _DenseEmbedder(),
    )
    monkeypatch.setattr(index_qdrant, "tqdm", _Progress)
    replacement: index_qdrant.SkillPayload = {
        "id": point_id,
        "name": "Duplicate content",
        "description": "Two paths share this content.",
        "content": "same content",
        "locations": [_location(first_path), _location(second_path)],
    }

    # When
    index_qdrant.upload_in_batches(client, [replacement], batch_size=1)

    # Then
    stored_locations = _stored_locations(client, point_id)
    receipts_by_path = {location["path"]: location["vettd_scan_publications"] for location in stored_locations}
    assert receipts_by_path == {
        first_path: [{"receipt": "first"}],
        second_path: [{"receipt": "second"}],
    }


def test_upload_preserves_malformed_receipt_values_without_inventing_missing_ones(monkeypatch) -> None:
    # Given
    client = _client_with_collection()
    point_id = "00000000-0000-0000-0000-000000000003"
    missing_path = "owner/repo/skills/missing/SKILL.md"
    malformed_path = "owner/repo/skills/malformed/SKILL.md"
    client.upsert(
        index_qdrant.COLLECTION,
        points=[
            _point(
                point_id,
                {
                    "locations": [
                        None,
                        _location(missing_path),
                        {**_location(malformed_path), "vettd_scan_publications": "not-a-list"},
                    ]
                },
            )
        ],
    )
    monkeypatch.setattr(
        index_qdrant,
        "get_embedder",
        lambda _name, sparse, threads: _SparseEmbedder() if sparse else _DenseEmbedder(),
    )
    monkeypatch.setattr(index_qdrant, "tqdm", _Progress)
    replacement: index_qdrant.SkillPayload = {
        "id": point_id,
        "name": "Malformed receipts",
        "description": "Malformed values are opaque existing data.",
        "content": "same content",
        "locations": [_location(missing_path), _location(malformed_path)],
    }

    # When
    index_qdrant.upload_in_batches(client, [replacement], batch_size=1)

    # Then
    stored_locations = _stored_locations(client, point_id)
    locations_by_path = {location["path"]: location for location in stored_locations}
    assert "vettd_scan_publications" not in locations_by_path[missing_path]
    assert locations_by_path[malformed_path]["vettd_scan_publications"] == "not-a-list"


def test_upload_does_not_copy_receipt_to_content_changed_point(monkeypatch) -> None:
    # Given
    client = _client_with_collection()
    old_point_id = "00000000-0000-0000-0000-000000000004"
    new_point_id = "00000000-0000-0000-0000-000000000005"
    path = "owner/repo/skills/example/SKILL.md"
    client.upsert(
        index_qdrant.COLLECTION,
        points=[
            _point(
                old_point_id,
                {"locations": [{**_location(path), "vettd_scan_publications": [{"receipt": "old"}]}]},
            )
        ],
    )
    monkeypatch.setattr(
        index_qdrant,
        "get_embedder",
        lambda _name, sparse, threads: _SparseEmbedder() if sparse else _DenseEmbedder(),
    )
    monkeypatch.setattr(index_qdrant, "tqdm", _Progress)
    replacement: index_qdrant.SkillPayload = {
        "id": new_point_id,
        "name": "Changed content",
        "description": "This path has a new content-derived point ID.",
        "content": "new content",
        "locations": [_location(path)],
    }

    # When
    index_qdrant.upload_in_batches(client, [replacement], batch_size=1)

    # Then
    new_location = _stored_locations(client, new_point_id)[0]
    assert "vettd_scan_publications" not in new_location


def test_upload_preserves_top_level_cli_security(monkeypatch) -> None:
    # Given a point that already carries a `cli_security` verdict (written
    # post-index by cli-security-scan/build_cli_export.py).
    client = _client_with_collection()
    point_id = "00000000-0000-0000-0000-0000000000c1"
    path = "owner/repo/skills/example/SKILL.md"
    verdict = {"grade": "C", "osv_snapshot_date": "2026-08-30",
               "packages": [{"package": "wrangler", "ecosystem": "npm"}]}
    client.upsert(
        index_qdrant.COLLECTION,
        points=[_point(point_id, {"locations": [_location(path)], "cli_security": verdict})],
    )
    monkeypatch.setattr(
        index_qdrant, "get_embedder",
        lambda _name, sparse, threads: _SparseEmbedder() if sparse else _DenseEmbedder(),
    )
    monkeypatch.setattr(index_qdrant, "tqdm", _Progress)
    # Same content hash -> same point id -> a plain re-index rebuilds the
    # payload from disk with no cli_security.
    reindexed: index_qdrant.SkillPayload = {
        "id": point_id, "name": "Example", "description": "d", "content": "c",
        "locations": [_location(path)],
    }

    # When
    index_qdrant.upload_in_batches(client, [reindexed], batch_size=1)

    # Then the verdict survives.
    stored = client.retrieve(index_qdrant.COLLECTION, ids=[point_id], with_payload=True)[0]
    assert (stored.payload or {})["cli_security"] == verdict


def test_upload_does_not_copy_cli_security_to_content_changed_point(monkeypatch) -> None:
    client = _client_with_collection()
    old_id = "00000000-0000-0000-0000-0000000000c2"
    new_id = "00000000-0000-0000-0000-0000000000c3"
    path = "owner/repo/skills/example/SKILL.md"
    client.upsert(
        index_qdrant.COLLECTION,
        points=[_point(old_id, {"locations": [_location(path)],
                                "cli_security": {"grade": "C", "packages": []}})],
    )
    monkeypatch.setattr(
        index_qdrant, "get_embedder",
        lambda _name, sparse, threads: _SparseEmbedder() if sparse else _DenseEmbedder(),
    )
    monkeypatch.setattr(index_qdrant, "tqdm", _Progress)
    replacement: index_qdrant.SkillPayload = {
        "id": new_id, "name": "Changed", "description": "d", "content": "new",
        "locations": [_location(path)],
    }

    index_qdrant.upload_in_batches(client, [replacement], batch_size=1)

    stored = client.retrieve(index_qdrant.COLLECTION, ids=[new_id], with_payload=True)[0]
    assert "cli_security" not in (stored.payload or {})


def test_filename_fast_index_retains_existing_duplicate_content_locations(monkeypatch) -> None:
    # Given
    client = _client_with_collection()
    point_id = "00000000-0000-0000-0000-000000000009"
    existing_path = "owner/repo/skills/existing/SKILL.md"
    new_path = "owner/repo/skills/new/SKILL.md"
    client.upsert(
        index_qdrant.COLLECTION,
        points=[
            _point(
                point_id,
                {
                    "locations": [
                        {
                            **_location(existing_path),
                            "vettd_scan_publications": [{"receipt": "existing"}],
                        }
                    ]
                },
            )
        ],
    )
    replacement = {
        "id": point_id,
        "name": "Duplicate content",
        "description": "The newly discovered path has existing content.",
        "content": "same content",
        "locations": [_location(new_path)],
    }
    monkeypatch.setattr(index_qdrant, "get_client", lambda: client)
    monkeypatch.setattr(
        index_qdrant,
        "get_embedder",
        lambda _name, sparse, threads: _SparseEmbedder() if sparse else _DenseEmbedder(),
    )
    monkeypatch.setattr(index_qdrant, "tqdm", _Progress)
    monkeypatch.setattr(index_qdrant, "load_skills", lambda skip_paths=None: iter([replacement]))
    monkeypatch.setattr(index_qdrant, "current_paths", lambda: {existing_path, new_path})
    monkeypatch.setattr(index_qdrant, "known_paths", lambda _client: {existing_path})
    monkeypatch.setattr(sys, "argv", ["index_qdrant.py"])

    # When
    index_qdrant.main()

    # Then
    stored_locations = _stored_locations(client, point_id)
    locations_by_path = {location["path"]: location for location in stored_locations}
    assert locations_by_path[existing_path]["vettd_scan_publications"] == [{"receipt": "existing"}]
    assert new_path in locations_by_path


def test_hash_index_rebuild_drops_stale_locations_and_keeps_matching_receipts(monkeypatch) -> None:
    # Given
    client = _client_with_collection()
    point_id = "00000000-0000-0000-0000-000000000010"
    retained_path = "owner/repo/skills/retained/SKILL.md"
    stale_path = "owner/repo/skills/stale/SKILL.md"
    client.upsert(
        index_qdrant.COLLECTION,
        points=[
            _point(
                point_id,
                {
                    "content_hash": "legacy",
                    "locations": [
                        {
                            **_location(retained_path),
                            "stars": 1,
                            "vettd_scan_publications": [{"receipt": "retained"}],
                        },
                        {**_location(stale_path), "vettd_scan_publications": [{"receipt": "stale"}]},
                    ],
                },
            )
        ],
    )
    replacement = {
        "id": point_id,
        "content_hash": "current",
        "name": "Current full rebuild",
        "description": "The full hash scan has no stale path.",
        "content": "same content",
        "locations": [{**_location(retained_path), "stars": 9}],
    }
    monkeypatch.setattr(index_qdrant, "get_client", lambda: client)
    monkeypatch.setattr(
        index_qdrant,
        "get_embedder",
        lambda _name, sparse, threads: _SparseEmbedder() if sparse else _DenseEmbedder(),
    )
    monkeypatch.setattr(index_qdrant, "tqdm", _Progress)
    monkeypatch.setattr(index_qdrant, "load_skills", lambda skip_paths=None: iter([replacement]))
    monkeypatch.setattr(sys, "argv", ["index_qdrant.py", "--hash"])

    # When
    index_qdrant.main()

    # Then
    stored_locations = _stored_locations(client, point_id)
    assert stored_locations == [
        {
            **_location(retained_path),
            "stars": 9,
            "vettd_scan_publications": [{"receipt": "retained"}],
        }
    ]


@pytest.mark.parametrize("index_arguments", [[], ["--hash"]])
def test_main_full_upsert_paths_preserve_duplicate_location_receipts(monkeypatch, index_arguments) -> None:
    # Given
    client = _client_with_collection()
    point_id = "00000000-0000-0000-0000-000000000007"
    first_path = "owner/repo/skills/first/SKILL.md"
    second_path = "owner/repo/skills/second/SKILL.md"
    old_locations: list[index_qdrant.LocationPayload] = [
        {**_location(first_path), "vettd_scan_publications": [{"receipt": "first"}]},
        {**_location(second_path), "vettd_scan_publications": [{"receipt": "second"}]},
    ]
    main_route_payload: dict[str, index_qdrant.JsonValue] = {
        "content_hash": "legacy",
        "locations": old_locations,
    }
    client.upsert(
        index_qdrant.COLLECTION,
        points=[_point(point_id, main_route_payload)],
    )
    replacement = {
        "id": point_id,
        "content_hash": "current",
        "name": "Duplicate content",
        "description": "Two paths share this content.",
        "content": "same content",
        "locations": [_location(first_path), _location(second_path)],
    }
    monkeypatch.setattr(index_qdrant, "get_client", lambda: client)
    monkeypatch.setattr(
        index_qdrant,
        "get_embedder",
        lambda _name, sparse, threads: _SparseEmbedder() if sparse else _DenseEmbedder(),
    )
    monkeypatch.setattr(index_qdrant, "tqdm", _Progress)
    monkeypatch.setattr(index_qdrant, "load_skills", lambda skip_paths=None: iter([replacement]))
    monkeypatch.setattr(index_qdrant, "current_paths", lambda: {first_path, second_path})
    monkeypatch.setattr(index_qdrant, "known_paths", lambda _client: set())
    monkeypatch.setattr(sys, "argv", ["index_qdrant.py", *index_arguments])

    # When
    index_qdrant.main()

    # Then
    stored_locations = _stored_locations(client, point_id)
    assert {
        location["path"]: location["vettd_scan_publications"] for location in stored_locations
    } == {
        first_path: [{"receipt": "first"}],
        second_path: [{"receipt": "second"}],
    }
