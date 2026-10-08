import uuid
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, case, func, or_
from sqlalchemy.orm import Query, Session

from vault_shared.db.like import escape_like
from vault_shared.db.models import (
    Embedding,
    ExtractionStatus,
    File,
    FileClassification,
    FileEntityLink,
    FileExtraction,
    FileIntelligence,
    FileMetadata,
    OrganizationEntity,
    StorageConnector,
    StorageSource,
)
from vault_shared.search.file_query import FILE_CATEGORIES, FileQuery


def _owned_by_connection() -> ColumnElement[bool]:
    """Files the connected account itself owns. A connection with no account
    identity (a local disk) has no files owned by anyone else, so everything
    on it counts as the user's."""
    return or_(
        File.owner_email == StorageConnector.account_email,
        StorageConnector.account_email.is_(None),
    )


class FileRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, file_id: uuid.UUID) -> File | None:
        return self._session.get(File, file_id)

    def get_owned_by_organization(
        self, file_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> File | None:
        """Used by the backend's file-detail endpoint, which addresses a
        file directly by id (no connector_id in the URL, unlike the scans
        API) — so ownership has to be checked via a join all the way to
        `storage_connectors.organization_id` rather than a simple
        `connector.organization_id` comparison the caller already has."""
        return (
            self._session.query(File)
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .join(StorageConnector, StorageSource.connector_id == StorageConnector.id)
            .filter(File.id == file_id, StorageConnector.organization_id == organization_id)
            .first()
        )

    def get_owned_with_connector(
        self, file_id: uuid.UUID, *, organization_id: uuid.UUID
    ) -> tuple[File, StorageConnector] | None:
        """Same ownership join as `get_owned_by_organization`, but also
        returns the file's own connector — for direct, read-only Drive
        operations (e.g. download) that need a live access token without
        going through the Execution Engine (nothing is mutated)."""
        row = (
            self._session.query(File, StorageConnector)
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .join(StorageConnector, StorageSource.connector_id == StorageConnector.id)
            .filter(File.id == file_id, StorageConnector.organization_id == organization_id)
            .first()
        )
        return (row[0], row[1]) if row is not None else None

    def list_by_ids(self, file_ids: list[uuid.UUID]) -> list[File]:
        if not file_ids:
            return []
        return self._session.query(File).filter(File.id.in_(file_ids)).all()

    def list_owned_by_organization(
        self, file_ids: list[uuid.UUID], *, organization_id: uuid.UUID
    ) -> list[File]:
        """Org-scoped bulk lookup — unlike `list_by_ids`, safe to use on a
        caller-supplied id list an unrelated organization could otherwise
        smuggle a file id into (Storage Intelligence's ad-hoc execution
        plans, built directly from a user-picked file selection rather
        than a precomputed, already-org-scoped group)."""
        if not file_ids:
            return []
        return self._for_organization(organization_id).filter(File.id.in_(file_ids)).all()

    def list_owned_by_organization_including_trashed(
        self, file_ids: list[uuid.UUID], *, organization_id: uuid.UUID
    ) -> list[File]:
        """Same org-scoped security guarantee as `list_owned_by_organization`
        (never trust a caller-supplied id list), but without
        `_for_organization`'s trashed/ownership exclusions — used only by
        `create_permanent_delete_plan`, whose whole eligibility rule is
        that the candidates ARE already trashed."""
        if not file_ids:
            return []
        return (
            self._session.query(File)
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .join(StorageConnector, StorageSource.connector_id == StorageConnector.id)
            .filter(StorageConnector.organization_id == organization_id, File.id.in_(file_ids))
            .all()
        )

    def get_by_source_and_provider_id(
        self, *, storage_source_id: uuid.UUID, provider_file_id: str
    ) -> File | None:
        return (
            self._session.query(File)
            .filter_by(storage_source_id=storage_source_id, provider_file_id=provider_file_id)
            .first()
        )

    def upsert(
        self,
        *,
        storage_source_id: uuid.UUID,
        provider_file_id: str,
        provider_parent_id: str | None,
        parent_folder_id: uuid.UUID | None,
        name: str,
        path: str,
        mime_type: str | None,
        size_bytes: int | None,
        owner_email: str | None,
        is_shared: bool,
        permissions_summary: str | None,
        version_id: str | None,
        checksum: str | None,
        web_view_link: str | None,
        provider_created_at: datetime | None,
        provider_modified_at: datetime | None,
        provider_viewed_at: datetime | None,
        scanned_at: datetime,
    ) -> File:
        file = self.get_by_source_and_provider_id(
            storage_source_id=storage_source_id, provider_file_id=provider_file_id
        )
        if file is None:
            file = File(storage_source_id=storage_source_id, provider_file_id=provider_file_id)
            self._session.add(file)

        file.provider_parent_id = provider_parent_id
        file.parent_folder_id = parent_folder_id
        # Every caller of upsert() sources from a `trashed = false` Drive
        # listing (see GoogleDriveClient.list_files_page) — if we're
        # upserting it, it isn't trashed, even if a past ExecutionService
        # trash action (later undone outside this platform) had marked it so.
        file.trashed = False
        file.name = name
        file.path = path
        file.mime_type = mime_type
        file.size_bytes = size_bytes
        file.owner_email = owner_email
        file.is_shared = is_shared
        file.permissions_summary = permissions_summary
        file.version_id = version_id
        file.checksum = checksum
        file.web_view_link = web_view_link
        file.provider_created_at = provider_created_at
        file.provider_modified_at = provider_modified_at
        file.provider_viewed_at = provider_viewed_at
        file.scanned_at = scanned_at
        self._session.flush()
        return file

    def mark_trashed(self, file: File, *, trashed: bool) -> None:
        """Called by `ExecutionService` right after a successful (or rolled
        back) ARCHIVE/REMOVE_DUPLICATE trash — the local mirror's only
        acknowledgment that the file left active storage, since the row
        itself is deliberately never deleted here (see `File.trashed`'s
        docstring)."""
        file.trashed = trashed
        self._session.flush()

    def mark_permanently_deleted(self, file: File) -> None:
        """Called by `ExecutionService` right after a successful
        PERMANENT_DELETE — never cleared, since there's no rollback for a
        real Drive deletion (see `File.permanently_deleted_at`'s
        docstring)."""
        file.permanently_deleted_at = datetime.now(UTC)
        self._session.flush()

    def mirror_location(
        self,
        file: File,
        *,
        name: str,
        provider_parent_id: str | None,
        parent_folder_id: uuid.UUID | None,
        path: str,
    ) -> None:
        """Called only by `ExecutionService` with the name/parent the
        provider reported after a rename or move — the mirror follows the
        provider, never the other way round."""
        file.name = name
        file.provider_parent_id = provider_parent_id
        file.parent_folder_id = parent_folder_id
        file.path = path[:4096]
        self._session.flush()

    def record_observed_trash(self, *, storage_source_id: uuid.UUID, provider_file_id: str) -> bool:
        """The scanner saw this file in the provider's Trash. The row is kept
        (not deleted) so its history, Restore and Undo keep working; it is
        excluded from listings by `trashed`. Returns whether a row existed."""
        file = self.get_by_source_and_provider_id(
            storage_source_id=storage_source_id, provider_file_id=provider_file_id
        )
        if file is None:
            return False
        file.trashed = True
        self._session.flush()
        return True

    def count_for_source(self, storage_source_id: uuid.UUID) -> int:
        return self._session.query(File).filter_by(storage_source_id=storage_source_id).count()

    def record_observed_removal(
        self, *, storage_source_id: uuid.UUID, provider_file_id: str
    ) -> bool:
        """The scanner saw this file disappear from the provider for good. The
        row is kept and marked deleted rather than removed, so the actions
        recorded against it (and their history) survive. Returns whether a
        row existed."""
        file = self.get_by_source_and_provider_id(
            storage_source_id=storage_source_id, provider_file_id=provider_file_id
        )
        if file is None:
            return False
        file.trashed = True
        if file.permanently_deleted_at is None:
            file.permanently_deleted_at = datetime.now(UTC)
        self._session.flush()
        return True

    def delete_by_source_and_provider_id(
        self, *, storage_source_id: uuid.UUID, provider_file_id: str
    ) -> bool:
        """Used by incremental sync when Drive reports an item removed or
        trashed. Returns whether a row existed."""
        file = self.get_by_source_and_provider_id(
            storage_source_id=storage_source_id, provider_file_id=provider_file_id
        )
        if file is None:
            return False
        self._session.delete(file)
        self._session.flush()
        return True

    def list_for_source(self, storage_source_id: uuid.UUID) -> list[File]:
        """Used only by the scanner's post-ingest hierarchy-resolution pass
        (`ScannerService._resolve_hierarchy`) to fix up file paths once all
        folder paths are known."""
        return self._session.query(File).filter_by(storage_source_id=storage_source_id).all()

    def _for_connector(
        self, connector_id: uuid.UUID, *, ownership: str | None = None
    ) -> Query[File]:
        """`ownership` backs the Files browser's "All / My files / Shared
        with me" filter — `None` (the default, every other caller) keeps
        the unfiltered-by-ownership behavior every existing caller relies
        on. `"mine"`/`"shared"` compare `File.owner_email` against the
        connector's own `account_email`, same signal already used to keep
        shared-with-me files out of Storage Intelligence's totals
        (`_for_organization`) — here it's a user-chosen view, not an
        always-on exclusion, since Files is meant to be a complete browse
        surface.

        Always excludes `trashed` rows, unconditionally — a file the
        Execution Engine already trashed shouldn't keep showing up in the
        main browse list with working-looking Rename/Move buttons; see
        `list_trashed_for_connector` for the dedicated Trash view."""
        query = (
            self._session.query(File)
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .filter(StorageSource.connector_id == connector_id)
            .filter(File.trashed.is_(False))
        )
        if ownership is not None:
            query = query.join(StorageConnector, StorageSource.connector_id == StorageConnector.id)
            if ownership == "mine":
                query = query.filter(_owned_by_connection())
            elif ownership == "shared":
                query = query.filter(~_owned_by_connection())
        return query

    def list_for_connector(
        self, connector_id: uuid.UUID, *, limit: int, offset: int, ownership: str | None = None
    ) -> list[File]:
        return (
            self._for_connector(connector_id, ownership=ownership)
            .order_by(File.name)
            .limit(limit)
            .offset(offset)
            .all()
        )

    def count_for_connector(self, connector_id: uuid.UUID, *, ownership: str | None = None) -> int:
        return self._for_connector(connector_id, ownership=ownership).count()

    def _trashed_for_connector(self, connector_id: uuid.UUID) -> Query[File]:
        """Deliberately not built on `_for_connector` — that base
        unconditionally excludes trashed rows; this is the one place that
        wants exactly the opposite. Also excludes rows already
        `permanently_deleted_at` — once gone, a file has nothing left to
        show in a "Trash" browse view."""
        return (
            self._session.query(File)
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .filter(StorageSource.connector_id == connector_id)
            .filter(File.trashed.is_(True))
            .filter(File.permanently_deleted_at.is_(None))
        )

    def list_trashed_for_connector(
        self, connector_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[File]:
        return (
            self._trashed_for_connector(connector_id)
            .order_by(File.name)
            .limit(limit)
            .offset(offset)
            .all()
        )

    def count_trashed_for_connector(self, connector_id: uuid.UUID) -> int:
        return self._trashed_for_connector(connector_id).count()

    def list_all_for_connector(self, connector_id: uuid.UUID) -> list[File]:
        """Unpaginated — used only by `RelationshipDiscoveryService`, which
        must compare a connector's *entire* current file set against itself
        (a newly-enriched file can relate to an already-enriched sibling),
        not just whatever was pending in this particular enrichment run."""
        return self._for_connector(connector_id).all()

    def list_pending_enrichment_for_connector(self, connector_id: uuid.UUID) -> list[File]:
        """A file is "pending" if it has never been enriched, or if it was
        rescanned (`File.scanned_at` advanced) since its last enrichment —
        this single query is what makes `EnrichmentJob` resumable and
        automatically cover both "process newly scanned files" and
        "reprocess updated files" (Phase 5 spec) without separate
        bookkeeping of which files a given job/scan touched."""
        return (
            self._for_connector(connector_id)
            .outerjoin(FileMetadata, FileMetadata.file_id == File.id)
            .filter(
                or_(
                    FileMetadata.file_id.is_(None),
                    File.scanned_at > FileMetadata.enriched_at,
                )
            )
            .all()
        )

    def count_pending_enrichment_for_connector(self, connector_id: uuid.UUID) -> int:
        return (
            self._for_connector(connector_id)
            .outerjoin(FileMetadata, FileMetadata.file_id == File.id)
            .filter(
                or_(
                    FileMetadata.file_id.is_(None),
                    File.scanned_at > FileMetadata.enriched_at,
                )
            )
            .count()
        )

    def list_pending_embedding_for_connector(
        self, connector_id: uuid.UUID, *, model_name: str, model_version: str
    ) -> list[File]:
        """A file is "pending" embedding if it has successfully extracted
        text (nothing else is embeddable) and any of: it has no
        `Embedding` row yet; it was re-extracted (`FileExtraction.
        extracted_at` advanced) since its last embedding; or its stored
        embedding was computed by a different model/version than the one
        currently configured (Phase 6 spec: "detect when documents
        require re-embedding" — this is what makes changing
        `LocalEmbeddingProvider`'s vectorization procedure, as ADR-018's
        stopword-filtering fix did, automatically re-embed every file
        rather than silently comparing old and new vectors). Same
        resumable-query shape as `list_pending_enrichment_for_connector`."""
        return (
            self._for_connector(connector_id)
            .join(FileExtraction, FileExtraction.file_id == File.id)
            .outerjoin(Embedding, Embedding.file_id == File.id)
            .filter(
                FileExtraction.status == ExtractionStatus.SUCCESS,
                or_(
                    Embedding.file_id.is_(None),
                    FileExtraction.extracted_at > Embedding.embedded_at,
                    Embedding.model_name != model_name,
                    Embedding.model_version != model_version,
                ),
            )
            .all()
        )

    def list_pending_intelligence_for_connector(
        self, connector_id: uuid.UUID, *, provider: str, model_name: str
    ) -> list[File]:
        """A file is "pending" AI intelligence analysis if it has
        successfully extracted text (nothing else is analyzable) and any
        of: it has no `FileIntelligence` row yet; it was re-extracted
        (`FileExtraction.extracted_at` advanced) since its last analysis;
        or its stored analysis was produced by a different completion
        provider/model than the one currently configured (mirrors
        `list_pending_embedding_for_connector`'s reasoning exactly — a
        provider/model swap, e.g. Kimi to GLM, must auto-requeue every
        file rather than silently leaving stale results in place)."""
        return (
            self._for_connector(connector_id)
            .join(FileExtraction, FileExtraction.file_id == File.id)
            .outerjoin(FileIntelligence, FileIntelligence.file_id == File.id)
            .filter(
                FileExtraction.status == ExtractionStatus.SUCCESS,
                or_(
                    FileIntelligence.file_id.is_(None),
                    FileExtraction.extracted_at > FileIntelligence.processed_at,
                    FileIntelligence.provider != provider,
                    FileIntelligence.model_name != model_name,
                ),
            )
            .all()
        )

    def rank_by_terms(
        self, organization_id: uuid.UUID, terms: list[str], *, limit: int
    ) -> list[tuple[File, StorageConnector, float]]:
        """Files ranked by how many of `terms` they match — in the name (3),
        inside the extracted text (2), in a linked project/client/campaign
        name (2) or in the path (1). Unlike `query_files`, a file needs only
        some of the words, so a question like "what does the payroll file
        say" finds the payroll file, and content counts, so "the contract
        mentioning the payment amount" finds the contract. Every term is a
        bound parameter."""
        if not terms:
            return []
        score_parts = []
        for term in terms:
            pattern = f"%{escape_like(term)}%"
            in_content = (
                self._session.query(FileExtraction.file_id)
                .filter(
                    FileExtraction.file_id == File.id,
                    FileExtraction.extracted_text.ilike(pattern, escape="\\"),
                )
                .exists()
            )
            in_entity = (
                self._session.query(FileEntityLink.id)
                .join(OrganizationEntity, OrganizationEntity.id == FileEntityLink.entity_id)
                .filter(
                    FileEntityLink.file_id == File.id,
                    OrganizationEntity.name.ilike(pattern, escape="\\"),
                )
                .exists()
            )
            score_parts += [
                case((File.name.ilike(pattern, escape="\\"), 3), else_=0),
                case((in_content, 2), else_=0),
                case((in_entity, 2), else_=0),
                case((File.path.ilike(pattern, escape="\\"), 1), else_=0),
            ]
        score = sum(score_parts[1:], score_parts[0]).label("score")
        rows = (
            self._session.query(File, StorageConnector, score)
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .join(StorageConnector, StorageSource.connector_id == StorageConnector.id)
            .filter(
                StorageConnector.organization_id == organization_id,
                File.trashed.is_(False),
                File.permanently_deleted_at.is_(None),
                score > 0,
            )
            .order_by(score.desc(), File.provider_modified_at.desc().nulls_last())
            .limit(limit)
            .all()
        )
        return [(file, connector, float(value)) for file, connector, value in rows]

    def query_files(
        self, organization_id: uuid.UUID, query: FileQuery
    ) -> tuple[list[tuple[File, StorageConnector]], int]:
        """Executes a structured `FileQuery` — every condition is a bound
        parameter, never interpolated SQL. Free text is split into words and
        every word must match the file's name, its path (so folder names
        count), or the name of a project/client/campaign it is linked to.
        Trashed and permanently deleted files are never returned."""
        base = (
            self._session.query(File, StorageConnector)
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .join(StorageConnector, StorageSource.connector_id == StorageConnector.id)
            .filter(
                StorageConnector.organization_id == organization_id,
                File.trashed.is_(False),
                File.permanently_deleted_at.is_(None),
            )
        )
        for token in query.text.split():
            pattern = f"%{escape_like(token)}%"
            entity_match = (
                self._session.query(FileEntityLink.id)
                .join(OrganizationEntity, OrganizationEntity.id == FileEntityLink.entity_id)
                .filter(
                    FileEntityLink.file_id == File.id,
                    OrganizationEntity.name.ilike(pattern, escape="\\"),
                )
                .exists()
            )
            base = base.filter(
                or_(
                    File.name.ilike(pattern, escape="\\"),
                    File.path.ilike(pattern, escape="\\"),
                    entity_match,
                )
            )

        type_conditions = []
        for category in query.categories:
            extensions, mime_prefixes = FILE_CATEGORIES.get(category, (frozenset(), ()))
            type_conditions += [File.name.ilike(f"%.{ext}") for ext in sorted(extensions)]
            type_conditions += [File.mime_type.startswith(prefix) for prefix in mime_prefixes]
        type_conditions += [
            File.name.ilike(f"%.{escape_like(ext)}", escape="\\") for ext in query.extensions
        ]
        if type_conditions:
            base = base.filter(or_(*type_conditions))

        if query.size_min is not None:
            base = base.filter(File.size_bytes >= query.size_min)
        if query.size_max is not None:
            base = base.filter(File.size_bytes <= query.size_max)
        if query.modified_after is not None:
            base = base.filter(File.provider_modified_at >= query.modified_after)
        if query.modified_before is not None:
            base = base.filter(File.provider_modified_at < query.modified_before)
        if query.folder:
            base = base.filter(File.path.ilike(f"%/{escape_like(query.folder)}/%", escape="\\"))
        if query.connector_id:
            base = base.filter(StorageConnector.id == uuid.UUID(query.connector_id))
        if query.ownership == "mine":
            base = base.filter(_owned_by_connection())
        elif query.ownership == "shared":
            base = base.filter(~_owned_by_connection())

        total = base.count()
        if query.sort == "largest":
            base = base.order_by(File.size_bytes.desc().nullslast())
        elif query.sort == "oldest":
            base = base.order_by(File.provider_modified_at.asc().nullslast())
        elif query.sort == "newest" or not query.text:
            base = base.order_by(File.provider_modified_at.desc().nullslast())
        else:
            first_word = f"%{escape_like(query.text.split()[0])}%"
            base = base.order_by(
                File.name.ilike(first_word, escape="\\").desc(),
                File.provider_modified_at.desc().nullslast(),
            )
        rows = base.limit(query.limit).offset(query.offset).all()
        return [(file, connector) for file, connector in rows], total

    def list_recently_modified_for_organization(
        self, organization_id: uuid.UUID, *, limit: int = 10
    ) -> list[File]:
        """The Founder Command Center's "Recent Activity Feed" (Phase 7
        spec) — a plain, efficient ORDER BY/LIMIT query, not a stored
        insight; always reflects the current data, no recompute needed."""
        return (
            self._session.query(File)
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .join(StorageConnector, StorageSource.connector_id == StorageConnector.id)
            .filter(StorageConnector.organization_id == organization_id)
            .order_by(File.provider_modified_at.desc().nulls_last())
            .limit(limit)
            .all()
        )

    def list_for_organization_with_details(
        self, organization_id: uuid.UUID
    ) -> list[tuple[File, FileMetadata | None, FileClassification | None, str | None, str | None]]:
        """The Recommendation Engine's primary data pull (Handbook's
        Recommendation Engine, Phase 7) — every file across *all* of an
        organization's connectors in one query, left-joined with its
        metadata/classification (most rules need several of these fields
        at once), its connector's `workspace_domain` (the orphaned-
        ownership rule's "is this file's owner outside the org" check —
        that rule, and `OwnershipConcentrationRule`, deliberately need
        every file regardless of who owns it, so this query stays
        unfiltered by ownership unlike `_for_organization`), and its
        connector's `account_email` (not for any rule — `RecommendationService.
        _generate` uses it to compute the Dashboard's `total_storage_bytes`/
        `total_files` from only the *owned* subset of these same rows, same
        reasoning as `_for_organization`'s ownership filter: a file shared
        by someone else doesn't count toward this account's real storage).
        Brute-force by design, same acceptance as `EmbeddingRepository.
        list_for_organization` (ADR-018) — fine at reference scale, a
        streaming/paginated version is a future-scale concern."""
        rows = (
            self._session.query(
                File,
                FileMetadata,
                FileClassification,
                StorageConnector.workspace_domain,
                StorageConnector.account_email,
            )
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .join(StorageConnector, StorageSource.connector_id == StorageConnector.id)
            .outerjoin(FileMetadata, FileMetadata.file_id == File.id)
            .outerjoin(FileClassification, FileClassification.file_id == File.id)
            .filter(StorageConnector.organization_id == organization_id, File.trashed.is_(False))
            .all()
        )
        return [
            (file, metadata, classification, domain, account_email)
            for file, metadata, classification, domain, account_email in rows
        ]

    # ------------------------------------------------------------------
    # Storage Intelligence Layer (Phase 1) queries — read-only, org-scoped.
    # ------------------------------------------------------------------

    def _for_organization(
        self, organization_id: uuid.UUID, connector_id: uuid.UUID | None = None
    ) -> Query[File]:
        """Every Storage Intelligence listing (overview totals, large/old/
        inactive/temporary-candidate/duplicate-group queries) builds on
        this one base. Two exclusions here fix all of them at once, not
        just the top-level total:

        - `File.trashed.is_(False)` — already-trashed rows (see that
          column's docstring).
        - `File.owner_email == StorageConnector.account_email` — a file
          shared with the connected account by someone else doesn't count
          against *this* account's real Drive storage quota (only owned
          files do — Google's own quota page never counts it), and
          `ExecutionService.set_trashed` will always fail on it
          (`insufficientFilePermissions`, confirmed in practice) since the
          connected account has no write access to someone else's file.
          Counting it toward "storage used" and offering an Archive button
          that can never succeed are both wrong for the same reason."""
        query = (
            self._session.query(File)
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .join(StorageConnector, StorageSource.connector_id == StorageConnector.id)
            .filter(
                StorageConnector.organization_id == organization_id,
                File.trashed.is_(False),
                _owned_by_connection(),
            )
        )
        if connector_id is not None:
            query = query.filter(StorageConnector.id == connector_id)
        return query

    def list_signal_bearing_for_organization(self, organization_id: uuid.UUID) -> list[File]:
        """Files with at least one real knowledge signal (a deterministic
        classification, or successfully extracted text) — the candidate
        pool `worker.organization.entity_clustering` clusters over. A file
        with neither has nothing for clustering or entity inference to
        reason about, so excluding it up front bounds both the clustering
        graph and the GLM call count for free, without ever concluding
        anything false about it (it simply stays `UNKNOWN`)."""
        return (
            self._for_organization(organization_id)
            .outerjoin(FileClassification, FileClassification.file_id == File.id)
            .outerjoin(FileExtraction, FileExtraction.file_id == File.id)
            .filter(
                or_(
                    FileClassification.file_id.isnot(None),
                    FileExtraction.status == ExtractionStatus.SUCCESS,
                )
            )
            .all()
        )

    def list_all_for_organization(self, organization_id: uuid.UUID) -> list[File]:
        """The Storage Analyzer's primary data pull — every file across all
        of an organization's connectors in one query, aggregated in Python
        afterward (`Counter`/`defaultdict`, an O(n) single pass). This runs
        only inside the background `StorageAnalysisJob`, never on the HTTP
        request path, so it accepts the same "load the org once" tradeoff
        `list_for_organization_with_details` (Recommendation Engine) and
        `EmbeddingRepository.list_for_organization` (ADR-018) already
        made — fine at reference/V1 scale (measured: a real 1,000-file org
        loads and aggregates in well under 100ms), a streaming/paginated
        version is a future-scale concern, not a Phase 1 requirement."""
        return self._for_organization(organization_id).all()

    def list_duplicate_checksum_groups_for_organization(
        self, organization_id: uuid.UUID
    ) -> list[tuple[str, int, int]]:
        """DB-side `GROUP BY` — the O(n²)-avoiding "candidate optimization"
        the spec asks for. Checksum equality already implies size equality
        (identical content ⇒ identical size), so grouping directly by
        checksum is both correct and sufficient on its own; a separate
        size-prefilter stage would only add a redundant pass. Returns
        `(checksum, file_count, total_size_bytes)` for every checksum
        shared by 2+ files — the exact-duplicate groups."""
        rows = (
            self._session.query(File.checksum, func.count(File.id), func.sum(File.size_bytes))
            .join(StorageSource, File.storage_source_id == StorageSource.id)
            .join(StorageConnector, StorageSource.connector_id == StorageConnector.id)
            .filter(
                StorageConnector.organization_id == organization_id,
                File.checksum.isnot(None),
                File.trashed.is_(False),
                _owned_by_connection(),
            )
            .group_by(File.checksum)
            .having(func.count(File.id) > 1)
            .all()
        )
        return [(checksum, count, int(total or 0)) for checksum, count, total in rows]

    def list_for_checksum_in_organization(
        self, organization_id: uuid.UUID, checksum: str
    ) -> list[File]:
        return self._for_organization(organization_id).filter(File.checksum == checksum).all()

    def list_large_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        min_size_bytes: int,
        limit: int,
        offset: int,
        connector_id: uuid.UUID | None = None,
    ) -> tuple[list[File], int]:
        query = self._for_organization(organization_id, connector_id).filter(
            File.size_bytes.isnot(None), File.size_bytes >= min_size_bytes
        )
        total = query.count()
        items = query.order_by(File.size_bytes.desc()).limit(limit).offset(offset).all()
        return items, total

    def list_old_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        older_than: datetime,
        limit: int,
        offset: int,
        connector_id: uuid.UUID | None = None,
    ) -> tuple[list[File], int]:
        """ "Old" = content staleness, judged by `provider_modified_at`
        alone (Phase 1 spec §9) — deliberately distinct from "inactive"
        below, which also considers view activity."""
        query = self._for_organization(organization_id, connector_id).filter(
            File.provider_modified_at.isnot(None), File.provider_modified_at < older_than
        )
        total = query.count()
        items = query.order_by(File.provider_modified_at.asc()).limit(limit).offset(offset).all()
        return items, total

    def list_inactive_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        inactive_since: datetime,
        limit: int,
        offset: int,
        connector_id: uuid.UUID | None = None,
    ) -> tuple[list[File], int]:
        """ "Inactive" = usage staleness, judged by whichever of
        `provider_modified_at`/`provider_viewed_at` is more recent (Phase 1
        spec §10) — a file only qualifies if at least one of those
        timestamps is known and it's older than the cutoff; a file with
        neither timestamp is `UNKNOWN` activity (see
        `list_unknown_activity_for_organization`), never guessed as
        inactive."""
        last_activity = func.greatest(File.provider_modified_at, File.provider_viewed_at)
        query = self._for_organization(organization_id, connector_id).filter(
            or_(File.provider_modified_at.isnot(None), File.provider_viewed_at.isnot(None)),
            last_activity < inactive_since,
        )
        total = query.count()
        items = query.order_by(last_activity.asc()).limit(limit).offset(offset).all()
        return items, total

    def count_unknown_activity_for_organization(self, organization_id: uuid.UUID) -> int:
        return (
            self._for_organization(organization_id)
            .filter(File.provider_modified_at.is_(None), File.provider_viewed_at.is_(None))
            .count()
        )

    def list_temporary_candidates_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        connector_id: uuid.UUID | None = None,
    ) -> tuple[list[File], int]:
        """Deterministic filename/extension heuristics only (Phase 1 spec
        §11) — never a certainty claim; callers must label these as
        "potential" candidates, not "unwanted"/"safe to delete"."""
        patterns = [
            File.name.ilike(pattern)
            for pattern in (
                "%.tmp",
                "%.temp",
                "%.cache",
                "%.log",
                "%.bak",
                "~$%",
                "%copy%",
                "% copy%",
                "%(1)%",
                "%backup%",
                "%.old",
            )
        ]
        query = self._for_organization(organization_id, connector_id).filter(or_(*patterns))
        total = query.count()
        items = (
            query.order_by(File.size_bytes.desc().nulls_last()).limit(limit).offset(offset).all()
        )
        return items, total
