import socket
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest
from sqlalchemy.orm import Session

from app.application.auth_service import AuthService
from app.application.search_service import SearchService
from app.infrastructure.auth.google_identity import GoogleUserInfo
from vault_shared import get_settings
from vault_shared.ai_gateway import AIGateway
from vault_shared.ai_gateway.interfaces import EmbeddingResult
from vault_shared.ai_gateway.providers import ExtractiveCompletionProvider
from vault_shared.db.models import ConnectorProvider, DriveType
from vault_shared.db.repositories import (
    EmbeddingRepository,
    FileExtractionRepository,
    FileRepository,
    StorageConnectorRepository,
    StorageSourceRepository,
)
from vault_shared.db.session import get_session_factory


def _reachable(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.port:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=1):
            return True
    except OSError:
        return False


def _infra_available() -> bool:
    settings = get_settings()
    return _reachable(settings.database_url) and _reachable(settings.redis_url)


requires_infra = pytest.mark.skipif(
    not _infra_available(),
    reason="Postgres/Redis not reachable — run against `docker compose up` or CI service containers.",
)


class _FixedVectorEmbeddingProvider:
    """A test double standing in for `LocalEmbeddingProvider` — returns a
    fixed vector for every text embedded, regardless of content, so a
    test can control exactly what `rank_by_similarity` sees without
    depending on gensim's real (slow, network-fetched) GloVe vectors."""

    name = "fixed_vector_test_double"
    model_name = "test"
    model_version = "1"

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        return [
            EmbeddingResult(
                vector=self._vector,
                model_name=self.model_name,
                model_version=self.model_version,
                dimensions=len(self._vector),
            )
            for _ in texts
        ]


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _provision_user(db: Session):
    unique = uuid.uuid4().hex[:12]
    google_user = GoogleUserInfo(
        sub=f"sub-{unique}",
        email=f"founder-{unique}@example.com",
        email_verified=True,
        name="Ada Founder",
        picture=None,
    )
    session = AuthService(db).complete_google_login(google_user=google_user, ip_address=None)
    return session.user


def _provision_connector(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID):
    return StorageConnectorRepository(db).upsert_connected(
        organization_id=organization_id,
        provider=ConnectorProvider.GOOGLE_WORKSPACE,
        connected_by_user_id=user_id,
        account_email="founder@acme.com",
        workspace_domain="acme.com",
    )


def _provision_file(db: Session, *, connector_id: uuid.UUID, name: str):
    source = StorageSourceRepository(db).upsert(
        connector_id=connector_id,
        provider_drive_id="root",
        name="My Drive",
        drive_type=DriveType.MY_DRIVE,
    )
    now = datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=source.id,
        provider_file_id=str(uuid.uuid4()),
        provider_parent_id=None,
        parent_folder_id=None,
        name=name,
        path=f"/{name}",
        mime_type="application/pdf",
        size_bytes=1024,
        owner_email="founder@acme.com",
        is_shared=False,
        permissions_summary=None,
        version_id=None,
        checksum=None,
        web_view_link=None,
        provider_created_at=now,
        provider_modified_at=now,
        provider_viewed_at=None,
        scanned_at=now,
    )
    db.commit()
    return file


@requires_infra
def test_search_finds_a_file_by_name_substring(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    _provision_file(db, connector_id=connector.id, name="Q3 Board Deck.pdf")
    gateway = AIGateway(
        embedding_provider=_FixedVectorEmbeddingProvider([0.0, 0.0, 0.0]),
        completion_provider=ExtractiveCompletionProvider(),
    )
    service = SearchService(db, ai_gateway=gateway)

    results = service.search("Board Deck", organization_id=user.organization_id, user_id=user.id)

    assert len(results) == 1
    assert results[0].retrieval_method == "metadata"
    assert results[0].score == 1.0


@requires_infra
def test_search_ranks_semantic_matches_by_similarity(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    aligned = _provision_file(db, connector_id=connector.id, name="Alpha.pdf")
    opposite = _provision_file(db, connector_id=connector.id, name="Beta.pdf")

    embeddings = EmbeddingRepository(db)
    embeddings.upsert(
        file_id=aligned.id,
        model_name="test",
        model_version="1",
        dimensions=4,
        vector=[1.0, 0.0, 0.0, 0.0],
        content_hash="hash-a",
        embedded_at=datetime.now(UTC),
    )
    embeddings.upsert(
        file_id=opposite.id,
        model_name="test",
        model_version="1",
        dimensions=4,
        vector=[-1.0, 0.0, 0.0, 0.0],
        content_hash="hash-b",
        embedded_at=datetime.now(UTC),
    )
    db.commit()

    gateway = AIGateway(
        embedding_provider=_FixedVectorEmbeddingProvider([1.0, 0.0, 0.0, 0.0]),
        completion_provider=ExtractiveCompletionProvider(),
    )
    service = SearchService(db, ai_gateway=gateway)

    results = service.search(
        "content that means the same thing",
        organization_id=user.organization_id,
        user_id=user.id,
    )

    assert [r.file.id for r in results] == [aligned.id]
    assert results[0].retrieval_method == "semantic"


@requires_infra
def test_search_marks_a_file_found_by_both_methods(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    file = _provision_file(db, connector_id=connector.id, name="Alpha.pdf")
    # A second, unrelated file with a distinct vector — without it, the
    # candidate pool would be a single file identical to the query, and
    # `rank_by_similarity`'s mean-centering (query and candidate collapse
    # to the same point) would strip the semantic signal to exactly zero.
    # A real organization always has more than one embedded file, so this
    # mirrors realistic usage rather than that degenerate edge case.
    unrelated = _provision_file(db, connector_id=connector.id, name="Unrelated.pdf")

    embeddings = EmbeddingRepository(db)
    embeddings.upsert(
        file_id=file.id,
        model_name="test",
        model_version="1",
        dimensions=4,
        vector=[1.0, 0.0, 0.0, 0.0],
        content_hash="hash-a",
        embedded_at=datetime.now(UTC),
    )
    embeddings.upsert(
        file_id=unrelated.id,
        model_name="test",
        model_version="1",
        dimensions=4,
        vector=[0.0, 1.0, 0.0, 0.0],
        content_hash="hash-b",
        embedded_at=datetime.now(UTC),
    )
    db.commit()

    gateway = AIGateway(
        embedding_provider=_FixedVectorEmbeddingProvider([1.0, 0.0, 0.0, 0.0]),
        completion_provider=ExtractiveCompletionProvider(),
    )
    service = SearchService(db, ai_gateway=gateway)

    results = service.search("Alpha", organization_id=user.organization_id, user_id=user.id)

    assert len(results) == 1
    assert results[0].retrieval_method == "both"


def _embed(db: Session, file_id, vector: list[float], content_hash: str) -> None:
    EmbeddingRepository(db).upsert(
        file_id=file_id,
        model_name="test",
        model_version="1",
        dimensions=4,
        vector=vector,
        content_hash=content_hash,
        embedded_at=datetime.now(UTC),
    )


@requires_infra
def test_exact_matches_are_not_padded_with_similar_content(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    exact = _provision_file(db, connector_id=connector.id, name="Acme invoice.pdf")
    similar = _provision_file(db, connector_id=connector.id, name="Quarterly statement.pdf")
    _embed(db, exact.id, [0.0, 1.0, 0.0, 0.0], "hash-exact")
    _embed(db, similar.id, [1.0, 0.0, 0.0, 0.0], "hash-similar")
    db.commit()
    gateway = AIGateway(
        embedding_provider=_FixedVectorEmbeddingProvider([1.0, 0.0, 0.0, 0.0]),
        completion_provider=ExtractiveCompletionProvider(),
    )

    outcome = SearchService(db, ai_gateway=gateway).find(
        "acme invoice", organization_id=user.organization_id, user_id=user.id
    )

    assert [r.file.id for r in outcome.results] == [exact.id]


@requires_infra
def test_similar_content_is_offered_when_nothing_matches_exactly(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    similar = _provision_file(db, connector_id=connector.id, name="Quarterly statement.pdf")
    other = _provision_file(db, connector_id=connector.id, name="Holiday photo.jpg")
    _embed(db, similar.id, [1.0, 0.0, 0.0, 0.0], "hash-similar")
    _embed(db, other.id, [-1.0, 0.0, 0.0, 0.0], "hash-other")
    db.commit()
    gateway = AIGateway(
        embedding_provider=_FixedVectorEmbeddingProvider([1.0, 0.0, 0.0, 0.0]),
        completion_provider=ExtractiveCompletionProvider(),
    )

    outcome = SearchService(db, ai_gateway=gateway).find(
        "billing", organization_id=user.organization_id, user_id=user.id
    )

    assert [r.file.id for r in outcome.results] == [similar.id]
    assert any("similar content" in part for part in outcome.understood)


@requires_infra
def test_a_file_is_found_by_words_inside_it_when_no_name_matches(db: Session) -> None:
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    contract = _provision_file(db, connector_id=connector.id, name="Blarrow_Contract_2026.pdf")
    FileExtractionRepository(db).upsert(
        file_id=contract.id,
        status="success",
        extractor_name="pdf_text",
        extracted_text="Payment Terms. The client will pay INR 2,50,000.",
        char_count=48,
        error=None,
        extracted_at=datetime.now(UTC),
    )
    db.commit()
    gateway = AIGateway(
        embedding_provider=_FixedVectorEmbeddingProvider([1.0, 0.0, 0.0, 0.0]),
        completion_provider=ExtractiveCompletionProvider(),
    )

    outcome = SearchService(db, ai_gateway=gateway).find(
        "contract mentioning the payment amount",
        organization_id=user.organization_id,
        user_id=user.id,
    )

    assert [r.file.id for r in outcome.results] == [contract.id]


@requires_infra
def test_search_never_returns_another_organizations_files(db: Session) -> None:
    user = _provision_user(db)
    other_user = _provision_user(db)
    other_connector = _provision_connector(
        db, organization_id=other_user.organization_id, user_id=other_user.id
    )
    _provision_file(db, connector_id=other_connector.id, name="Confidential Salary Data.pdf")
    gateway = AIGateway(
        embedding_provider=_FixedVectorEmbeddingProvider([0.0, 0.0, 0.0]),
        completion_provider=ExtractiveCompletionProvider(),
    )
    service = SearchService(db, ai_gateway=gateway)

    results = service.search(
        "Confidential Salary Data", organization_id=user.organization_id, user_id=user.id
    )

    assert results == []


def _file(
    db: Session,
    *,
    connector_id: uuid.UUID,
    name: str,
    path: str | None = None,
    size: int = 1024,
    modified: datetime | None = None,
    mime: str = "application/octet-stream",
    trashed: bool = False,
):
    source = StorageSourceRepository(db).upsert(
        connector_id=connector_id,
        provider_drive_id="root",
        name="My Drive",
        drive_type=DriveType.MY_DRIVE,
    )
    when = modified or datetime.now(UTC)
    file = FileRepository(db).upsert(
        storage_source_id=source.id,
        provider_file_id=str(uuid.uuid4()),
        provider_parent_id=None,
        parent_folder_id=None,
        name=name,
        path=path or f"/{name}",
        mime_type=mime,
        size_bytes=size,
        owner_email="founder@acme.com",
        is_shared=False,
        permissions_summary=None,
        version_id=None,
        checksum=None,
        web_view_link=None,
        provider_created_at=when,
        provider_modified_at=when,
        provider_viewed_at=None,
        scanned_at=when,
    )
    if trashed:
        FileRepository(db).mark_trashed(file, trashed=True)
    db.commit()
    return file


def _find(db: Session, user, text: str) -> list[str]:  # noqa: ANN001
    gateway = AIGateway(
        embedding_provider=_FixedVectorEmbeddingProvider([0.0, 0.0, 0.0]),
        completion_provider=ExtractiveCompletionProvider(),
    )
    outcome = SearchService(db, ai_gateway=gateway).find(
        text, organization_id=user.organization_id, user_id=user.id
    )
    return sorted(result.file.name for result in outcome.results)


@pytest.fixture
def messy_drive(db: Session):
    user = _provision_user(db)
    connector = _provision_connector(db, organization_id=user.organization_id, user_id=user.id)
    c = connector.id
    _file(db, connector_id=c, name="train.py", path="/Code/ml/train.py")
    _file(db, connector_id=c, name="analysis.ipynb", path="/Code/analysis.ipynb")
    _file(
        db,
        connector_id=c,
        name="Blarrow_Business_Plan.xlsx",
        path="/Docs/Blarrow_Business_Plan.xlsx",
    )
    _file(
        db,
        connector_id=c,
        name="hero.png",
        path="/Clients/Blarrow/Website/hero.png",
        mime="image/png",
    )
    _file(db, connector_id=c, name="big-video.mp4", size=900 * 1024**2, mime="video/mp4")
    _file(db, connector_id=c, name="old-report.pdf", modified=datetime(2021, 6, 1, tzinfo=UTC))
    _file(db, connector_id=c, name="trashed.py", trashed=True)
    return user


@requires_infra
def test_python_files_are_found_by_type(db: Session, messy_drive) -> None:  # noqa: ANN001
    assert _find(db, messy_drive, "Find all Python files") == ["analysis.ipynb", "train.py"]


@requires_infra
def test_size_filter(db: Session, messy_drive) -> None:  # noqa: ANN001
    assert _find(db, messy_drive, "files larger than 500 MB") == ["big-video.mp4"]


@requires_infra
def test_date_filter(db: Session, messy_drive) -> None:  # noqa: ANN001
    assert _find(db, messy_drive, "files modified before 2023") == ["old-report.pdf"]


@requires_infra
def test_a_client_name_matches_file_names_and_folder_paths(db: Session, messy_drive) -> None:  # noqa: ANN001
    assert _find(db, messy_drive, "Find Blarrow files") == [
        "Blarrow_Business_Plan.xlsx",
        "hero.png",
    ]


@requires_infra
def test_images_for_a_client(db: Session, messy_drive) -> None:  # noqa: ANN001
    assert _find(db, messy_drive, "Blarrow images") == ["hero.png"]


@requires_infra
def test_trashed_files_are_never_returned(db: Session, messy_drive) -> None:  # noqa: ANN001
    assert "trashed.py" not in _find(db, messy_drive, "python")


@requires_infra
def test_another_organization_never_sees_these_files(db: Session, messy_drive) -> None:  # noqa: ANN001
    stranger = _provision_user(db)
    assert _find(db, stranger, "Find all Python files") == []
