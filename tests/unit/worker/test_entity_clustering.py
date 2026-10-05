import uuid

from vault_shared.db.models import File
from worker.organization.entity_clustering import (
    MAX_CLUSTER_SIZE_FOR_AI,
    build_clusters,
)


def _make_file(name: str, *, folder: uuid.UUID | None = None) -> File:
    file = File(storage_source_id=uuid.uuid4(), provider_file_id=str(uuid.uuid4()))
    file.id = uuid.uuid4()
    file.name = name
    file.parent_folder_id = folder
    return file


def _cluster_ids(clusters) -> list[set[uuid.UUID]]:
    return [{file.id for file in cluster.files} for cluster in clusters]


def test_files_in_the_same_folder_form_one_cluster() -> None:
    folder = uuid.uuid4()
    a, b = _make_file("brief.docx", folder=folder), _make_file("notes.txt", folder=folder)

    assert _cluster_ids(build_clusters([a, b], [])) == [{a.id, b.id}]


def test_matching_base_names_link_files_across_folders() -> None:
    v1 = _make_file("Acme Pitch_v1.pptx", folder=uuid.uuid4())
    v2 = _make_file("Acme Pitch_v2.pptx", folder=uuid.uuid4())

    assert _cluster_ids(build_clusters([v1, v2], [])) == [{v1.id, v2.id}]


def test_explicit_relationship_edges_link_otherwise_unrelated_files() -> None:
    a = _make_file("alpha.pdf", folder=uuid.uuid4())
    b = _make_file("beta.pdf", folder=uuid.uuid4())

    assert _cluster_ids(build_clusters([a, b], [(a.id, b.id)])) == [{a.id, b.id}]


def test_an_unconnected_file_is_never_its_own_cluster() -> None:
    lonely = _make_file("lonely.pdf", folder=uuid.uuid4())

    assert build_clusters([lonely], []) == []


def test_an_edge_to_a_file_outside_the_pool_is_ignored() -> None:
    a = _make_file("alpha.pdf", folder=uuid.uuid4())

    assert build_clusters([a], [(a.id, uuid.uuid4())]) == []


def test_an_oversized_component_is_split_rather_than_skipped() -> None:
    folder = uuid.uuid4()
    files = [_make_file(f"file{i}.txt", folder=folder) for i in range(MAX_CLUSTER_SIZE_FOR_AI + 5)]

    clusters = build_clusters(files, [])

    assert len(clusters) == 2
    assert all(len(cluster.files) <= MAX_CLUSTER_SIZE_FOR_AI for cluster in clusters)
    assert sum(len(cluster.files) for cluster in clusters) == len(files)
