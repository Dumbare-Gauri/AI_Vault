import uuid
from collections import defaultdict
from dataclasses import dataclass

from vault_shared.db.models import File
from worker.enrichment.naming import normalize_base_name

# One AI call's worth of files — kept small deliberately (spec's "candidate
# clusters → targeted reasoning," not "send everything to GLM"). An
# oversized connected component is split by folder into sub-batches of this
# size rather than skipped outright: unlike `RelationshipDiscoveryService`'s
# oversized-group skip, silently producing zero entity inference for a
# large real project is the worse failure mode here.
MAX_CLUSTER_SIZE_FOR_AI = 15

# Bounds one "Analyze Organization" run's total GLM call count, so a single
# click has a predictable cost/latency ceiling. Remaining clusters are
# simply left for the next run — safe, since `FileEntityLink.
# inference_version` means an already-linked file is never reprocessed.
MAX_CLUSTERS_PER_RUN = 200


@dataclass(frozen=True)
class EntityCluster:
    """A candidate group of files that plausibly share a Project/Client/
    Campaign — connected-component membership, not a claim that they
    actually do. A lone, unconnected file never becomes its own
    single-file cluster: with no co-occurrence signal at all there is
    nothing for the AI step to reason about, and the file simply stays
    unlinked (spec's "Unknown is always allowed")."""

    files: list[File]


def build_clusters(
    files: list[File], relationship_edges: list[tuple[uuid.UUID, uuid.UUID]]
) -> list[EntityCluster]:
    """Connected components over three edge sources: explicit
    `FileRelationship` pairs (`relationship_edges`, supplied by the caller
    since discovering them needs the database), files sharing a
    `parent_folder_id`, and files whose `normalize_base_name` matches
    across folders (reusing `worker.enrichment.naming`, the same notion of
    "same document" `RelationshipDiscoveryService` already uses for
    version grouping)."""
    file_ids = {file.id for file in files}
    parent: dict[uuid.UUID, uuid.UUID] = {file.id: file.id for file in files}

    def find(node: uuid.UUID) -> uuid.UUID:
        while parent[node] != node:
            node = parent[node]
        return node

    def union(a: uuid.UUID, b: uuid.UUID) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_a] = root_b

    for file_id, related_file_id in relationship_edges:
        if file_id in file_ids and related_file_id in file_ids:
            union(file_id, related_file_id)

    by_folder: dict[uuid.UUID | None, list[uuid.UUID]] = defaultdict(list)
    by_base_name: dict[str, list[uuid.UUID]] = defaultdict(list)
    for file in files:
        by_folder[file.parent_folder_id].append(file.id)
        by_base_name[normalize_base_name(file.name)].append(file.id)

    for group in by_folder.values():
        for other in group[1:]:
            union(group[0], other)
    for group in by_base_name.values():
        for other in group[1:]:
            union(group[0], other)

    components: dict[uuid.UUID, list[File]] = defaultdict(list)
    for file in files:
        components[find(file.id)].append(file)

    clusters: list[EntityCluster] = []
    for component_files in components.values():
        if len(component_files) < 2:
            continue
        if len(component_files) <= MAX_CLUSTER_SIZE_FOR_AI:
            clusters.append(EntityCluster(files=component_files))
        else:
            clusters.extend(_split_oversized_component(component_files))

    return clusters[:MAX_CLUSTERS_PER_RUN]


def _split_oversized_component(component_files: list[File]) -> list[EntityCluster]:
    by_folder: dict[uuid.UUID | None, list[File]] = defaultdict(list)
    for file in component_files:
        by_folder[file.parent_folder_id].append(file)

    sub_batches: list[EntityCluster] = []
    for folder_files in by_folder.values():
        for start in range(0, len(folder_files), MAX_CLUSTER_SIZE_FOR_AI):
            batch = folder_files[start : start + MAX_CLUSTER_SIZE_FOR_AI]
            if len(batch) >= 2:
                sub_batches.append(EntityCluster(files=batch))
    return sub_batches
