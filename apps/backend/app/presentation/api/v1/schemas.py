import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.application.conversation_service import AssistantTurn, ConversationDetail
from app.application.dashboard_service import DashboardOverview
from app.application.execution_job_service import ExecutionJobDetail
from app.application.execution_plan_service import ExecutionPlanDetail
from app.application.file_service import FileDetail
from app.application.organization_entity_service import OrganizationEntityDetail
from app.application.search_service import SearchResult
from app.application.vault_ai.blocks import public as public_blocks
from app.application.workflow_execution_service import WorkflowExecutionDetail
from app.application.workflow_service import WorkflowDetail
from vault_shared.ai_gateway.provider_catalog import AI_PROVIDERS, DEFAULT_AI_PROVIDER
from vault_shared.db.models import (
    AIProviderConfig,
    ApprovalRequest,
    ArchiveJob,
    AutomationTemplate,
    Citation,
    Conversation,
    ConversationMessage,
    DashboardSnapshot,
    DuplicateGroup,
    EmbeddingJob,
    EmbeddingProgress,
    EnrichmentJob,
    EnrichmentProgress,
    ExecutionJob,
    ExecutionPlan,
    ExecutionResult,
    ExecutionStep,
    File,
    FileClassification,
    FileEntityLink,
    FileExtraction,
    FileIntelligence,
    FileLifecycle,
    FileMetadata,
    Folder,
    InsightRecord,
    IntelligenceJob,
    IntelligenceProgress,
    KnowledgeAttribute,
    LocalAgent,
    Notification,
    Organization,
    OrganizationAnalysisJob,
    OrganizationEntity,
    OrganizationRecommendation,
    Recommendation,
    RecommendationJob,
    ScanJob,
    ScanProgress,
    StorageAnalysisJob,
    StorageAnalysisSnapshot,
    StorageConnector,
    User,
    Workflow,
    WorkflowExecution,
    WorkflowNode,
    WorkflowNodeExecution,
    WorkflowPolicy,
    WorkflowTrigger,
    WorkflowVersion,
    provider_display_name,
)
from vault_shared.execution.plan_service import is_executable_rule


class UserProfileResponse(BaseModel):
    id: str
    email: str
    name: str
    avatar_url: str | None
    role: str
    organization_id: str
    created_at: datetime
    last_login_at: datetime | None

    @classmethod
    def from_model(cls, user: User) -> "UserProfileResponse":
        return cls(
            id=str(user.id),
            email=user.email,
            name=user.name,
            avatar_url=user.avatar_url,
            role=user.role.name,
            organization_id=str(user.organization_id),
            created_at=user.created_at,
            last_login_at=user.last_login_at,
        )


class OrganizationResponse(BaseModel):
    id: str
    name: str
    slug: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, organization: Organization) -> "OrganizationResponse":
        return cls(
            id=str(organization.id),
            name=organization.name,
            slug=organization.slug,
            created_at=organization.created_at,
            updated_at=organization.updated_at,
        )


class OrganizationUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class AIProviderOptionResponse(BaseModel):
    id: str
    label: str
    model_hint: str


class AIProviderConfigResponse(BaseModel):
    """Never includes the API key, encrypted or otherwise — mirrors
    `ConnectorResponse`'s "never expose tokens" rule."""

    configured: bool
    model_name: str | None
    provider: str | None
    available_providers: list[AIProviderOptionResponse]

    @classmethod
    def from_model(cls, config: AIProviderConfig | None) -> "AIProviderConfigResponse":
        options = [
            AIProviderOptionResponse(id=spec.id, label=spec.label, model_hint=spec.model_hint)
            for spec in AI_PROVIDERS.values()
        ]
        if config is None:
            return cls(
                configured=False, model_name=None, provider=None, available_providers=options
            )
        return cls(
            configured=True,
            model_name=config.model_name,
            provider=config.provider,
            available_providers=options,
        )


class AIStatusResponse(BaseModel):
    """Configuration state only — never carries key material, and reporting
    it never calls the provider."""

    mode: str
    source: str
    provider: str
    model: str | None


class AIProviderConfigUpdateRequest(BaseModel):
    """`api_key` blank/omitted keeps the organization's existing stored
    key — only `model_name` is required on every update."""

    api_key: str | None = Field(default=None, min_length=1)
    model_name: str = Field(min_length=1, max_length=200)
    provider: str = Field(default=DEFAULT_AI_PROVIDER, max_length=30)


class AIProviderConfigTestRequest(BaseModel):
    api_key: str | None = Field(default=None, min_length=1)
    model_name: str = Field(min_length=1, max_length=200)
    provider: str = Field(default=DEFAULT_AI_PROVIDER, max_length=30)


class AIProviderConfigTestResponse(BaseModel):
    success: bool
    error: str | None = None


class GoogleLoginRequest(BaseModel):
    id_token: str = Field(min_length=1)


class SessionResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserProfileResponse


class ConnectorResponse(BaseModel):
    """Never includes token values — Phase 3 spec's "never expose refresh
    tokens" — those live only in ConnectorCredentials, which has no
    response schema of its own."""

    id: str
    provider: str
    status: str
    account_email: str | None
    workspace_domain: str | None
    last_verified_at: datetime | None
    last_failed_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    provider_name: str
    display_name: str | None
    last_synced_at: datetime | None
    storage_used_bytes: int | None
    storage_total_bytes: int | None
    storage_trash_bytes: int | None
    quota_checked_at: datetime | None

    @classmethod
    def from_model(cls, connector: StorageConnector) -> "ConnectorResponse":
        return cls(
            id=str(connector.id),
            provider=connector.provider,
            status=connector.status,
            account_email=connector.account_email,
            workspace_domain=connector.workspace_domain,
            last_verified_at=connector.last_verified_at,
            last_failed_at=connector.last_failed_at,
            last_error=connector.last_error,
            created_at=connector.created_at,
            updated_at=connector.updated_at,
            provider_name=provider_display_name(connector.provider),
            display_name=connector.display_name,
            last_synced_at=connector.last_synced_at,
            storage_used_bytes=connector.storage_used_bytes,
            storage_total_bytes=connector.storage_total_bytes,
            storage_trash_bytes=connector.storage_trash_bytes,
            quota_checked_at=connector.quota_checked_at,
        )


class InitiateConnectResponse(BaseModel):
    authorize_url: str


class CompleteConnectRequest(BaseModel):
    code: str = Field(min_length=1)
    state: str = Field(min_length=1)


class ScanProgressResponse(BaseModel):
    sources_discovered: int
    sources_completed: int
    folders_discovered: int
    files_discovered: int
    current_source_name: str | None
    updated_at: datetime

    @classmethod
    def from_model(cls, progress: ScanProgress) -> "ScanProgressResponse":
        return cls(
            sources_discovered=progress.sources_discovered,
            sources_completed=progress.sources_completed,
            folders_discovered=progress.folders_discovered,
            files_discovered=progress.files_discovered,
            current_source_name=progress.current_source_name,
            updated_at=progress.updated_at,
        )


class ScanJobResponse(BaseModel):
    id: str
    connector_id: str
    scan_type: str
    status: str
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    progress: ScanProgressResponse | None = None

    @classmethod
    def from_model(cls, job: ScanJob, *, progress: ScanProgress | None = None) -> "ScanJobResponse":
        return cls(
            id=str(job.id),
            connector_id=str(job.connector_id),
            scan_type=job.scan_type,
            status=job.status,
            error=job.error,
            started_at=job.started_at,
            completed_at=job.completed_at,
            created_at=job.created_at,
            progress=ScanProgressResponse.from_model(progress) if progress else None,
        )


class StartScanRequest(BaseModel):
    scan_type: str = Field(default="full", pattern="^(full|incremental)$")


class FileSummaryResponse(BaseModel):
    id: str
    name: str
    path: str
    mime_type: str | None
    size_bytes: int | None
    is_shared: bool
    owner_email: str | None
    provider_modified_at: datetime | None
    # Only ever populated by the Trash listing endpoint (whether a
    # completed archive already backs this file) — every other caller
    # leaves it unset, since ownership of "is this backed up" doesn't
    # apply outside the permanent-delete eligibility question.
    is_archived: bool | None = None

    @classmethod
    def from_model(cls, file: File, *, is_archived: bool | None = None) -> "FileSummaryResponse":
        return cls(
            id=str(file.id),
            name=file.name,
            path=file.path,
            mime_type=file.mime_type,
            size_bytes=file.size_bytes,
            is_shared=file.is_shared,
            owner_email=file.owner_email,
            provider_modified_at=file.provider_modified_at,
            is_archived=is_archived,
        )


class FileListResponse(BaseModel):
    items: list[FileSummaryResponse]
    total: int


class FolderSummaryResponse(BaseModel):
    """Backs the Files browser's "Move to folder" picker — `provider_file_id`
    is the value a `MOVE_FILE` plan's `new_parent_id` must carry (the
    provider's own id, not this platform's `id`), since that's what
    `StorageAdapter.move` is called with at execution time."""

    id: str
    provider_file_id: str
    name: str
    path: str

    @classmethod
    def from_model(cls, folder: Folder) -> "FolderSummaryResponse":
        return cls(
            id=str(folder.id),
            provider_file_id=folder.provider_file_id,
            name=folder.name,
            path=folder.path,
        )


class CreateFolderRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_folder_id: str | None = None


class CreateTextFileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=1_000_000)
    format: Literal["text", "markdown"] = "text"
    parent_folder_id: str | None = None


class TrashItemResponse(BaseModel):
    name: str
    size_bytes: int
    backed_up: bool


class TrashSummaryResponse(BaseModel):
    """What is (or was) in the provider's Trash."""

    file_count: int
    total_bytes: int
    not_backed_up_count: int
    largest: list[TrashItemResponse]
    still_deleting_count: int = 0


class EmptyTrashRequest(BaseModel):
    expected_count: int = Field(ge=0)
    confirmation: str = Field(max_length=50)


class CopyFileRequest(BaseModel):
    new_name: str | None = Field(default=None, max_length=255)


class CreatedItemResponse(BaseModel):
    id: str
    name: str
    path: str
    web_view_link: str | None = None


class FileMetadataResponse(BaseModel):
    normalized_extension: str | None
    mime_type_validated: bool
    mime_mismatch_reason: str | None
    naming_pattern: str | None
    version_label: str | None
    owner_summary: str | None
    sharing_summary: str | None
    duplicate_group_key: str | None
    language: str | None
    enriched_at: datetime

    @classmethod
    def from_model(cls, metadata: FileMetadata) -> "FileMetadataResponse":
        return cls(
            normalized_extension=metadata.normalized_extension,
            mime_type_validated=metadata.mime_type_validated,
            mime_mismatch_reason=metadata.mime_mismatch_reason,
            naming_pattern=metadata.naming_pattern,
            version_label=metadata.version_label,
            owner_summary=metadata.owner_summary,
            sharing_summary=metadata.sharing_summary,
            duplicate_group_key=metadata.duplicate_group_key,
            language=metadata.language,
            enriched_at=metadata.enriched_at,
        )


class FileClassificationResponse(BaseModel):
    document_type: str
    confidence: float
    method: str
    classified_at: datetime

    @classmethod
    def from_model(cls, classification: FileClassification) -> "FileClassificationResponse":
        return cls(
            document_type=classification.document_type,
            confidence=classification.confidence,
            method=classification.method,
            classified_at=classification.classified_at,
        )


class FileExtractionResponse(BaseModel):
    """Never includes the extracted text itself — only its status/size —
    to keep the file-detail payload small; the text exists to feed a future
    Embedding Engine (Phase 5 spec's Search Preparation), not to be
    rendered directly in this phase's UI."""

    status: str
    extractor_name: str | None
    char_count: int | None
    error: str | None
    extracted_at: datetime

    @classmethod
    def from_model(cls, extraction: FileExtraction) -> "FileExtractionResponse":
        return cls(
            status=extraction.status,
            extractor_name=extraction.extractor_name,
            char_count=extraction.char_count,
            error=extraction.error,
            extracted_at=extraction.extracted_at,
        )


class FileIntelligenceEntityResponse(BaseModel):
    type: str
    value: str
    confidence: float | None


class FileIntelligenceResponse(BaseModel):
    """Unlike `FileExtractionResponse`'s deliberate exclusion of raw
    extracted text, `summary` here *is* meant to render directly — it's a
    distilled, human-facing summary produced by the completion provider,
    not raw extracted text. Don't "fix" this by removing it to match the
    other precedent."""

    status: str
    document_type: str | None
    summary: str | None
    entities: list[FileIntelligenceEntityResponse]
    structured_metadata: dict
    topics: list[str]
    confidence: float | None
    provider: str
    model_name: str
    error: str | None
    processed_at: datetime

    @classmethod
    def from_model(cls, intelligence: FileIntelligence) -> "FileIntelligenceResponse":
        return cls(
            status=intelligence.status,
            document_type=intelligence.document_type,
            summary=intelligence.summary,
            entities=[
                FileIntelligenceEntityResponse(
                    type=entity.get("type", ""),
                    value=entity.get("value", ""),
                    confidence=entity.get("confidence"),
                )
                for entity in intelligence.entities
            ],
            structured_metadata=intelligence.structured_metadata,
            topics=intelligence.topics,
            confidence=intelligence.confidence,
            provider=intelligence.provider,
            model_name=intelligence.model_name,
            error=intelligence.error,
            processed_at=intelligence.processed_at,
        )


class KnowledgeAttributeResponse(BaseModel):
    attribute_type: str
    value: str
    confidence: float
    source: str

    @classmethod
    def from_model(cls, attribute: KnowledgeAttribute) -> "KnowledgeAttributeResponse":
        return cls(
            attribute_type=attribute.attribute_type,
            value=attribute.value,
            confidence=attribute.confidence,
            source=attribute.source,
        )


class RelatedFileResponse(BaseModel):
    file_id: str
    name: str
    path: str
    relationship_type: str
    confidence: float
    metadata: dict


class FileEntityLinkResponse(BaseModel):
    entity_id: str
    entity_type: str
    entity_name: str
    confidence: float
    evidence: list[dict]
    is_user_confirmed: bool
    source: str

    @classmethod
    def from_model(
        cls, link: FileEntityLink, entity: OrganizationEntity
    ) -> "FileEntityLinkResponse":
        return cls(
            entity_id=str(entity.id),
            entity_type=entity.entity_type,
            entity_name=entity.name,
            confidence=link.confidence,
            evidence=link.evidence,
            is_user_confirmed=link.is_user_confirmed,
            source=link.source,
        )


class FileLifecycleResponse(BaseModel):
    state: str
    confidence: float
    evidence: list[str]
    analyzed_at: datetime

    @classmethod
    def from_model(cls, lifecycle: FileLifecycle) -> "FileLifecycleResponse":
        return cls(
            state=lifecycle.state,
            confidence=lifecycle.confidence,
            evidence=lifecycle.evidence,
            analyzed_at=lifecycle.analyzed_at,
        )


class FileDetailResponse(BaseModel):
    id: str
    name: str
    path: str
    mime_type: str | None
    size_bytes: int | None
    is_shared: bool
    owner_email: str | None
    provider_modified_at: datetime | None
    web_view_link: str | None
    metadata: FileMetadataResponse | None
    classification: FileClassificationResponse | None
    extraction: FileExtractionResponse | None
    intelligence: FileIntelligenceResponse | None
    knowledge_attributes: list[KnowledgeAttributeResponse]
    related_files: list[RelatedFileResponse]
    entity_links: list[FileEntityLinkResponse]
    lifecycle: FileLifecycleResponse | None
    connector_id: str | None = None
    provider: str | None = None

    @classmethod
    def from_detail(cls, detail: FileDetail) -> "FileDetailResponse":
        file = detail.file
        return cls(
            id=str(file.id),
            name=file.name,
            path=file.path,
            mime_type=file.mime_type,
            size_bytes=file.size_bytes,
            is_shared=file.is_shared,
            owner_email=file.owner_email,
            provider_modified_at=file.provider_modified_at,
            web_view_link=file.web_view_link,
            connector_id=str(detail.connector_id) if detail.connector_id else None,
            provider=detail.provider,
            metadata=FileMetadataResponse.from_model(detail.metadata) if detail.metadata else None,
            classification=(
                FileClassificationResponse.from_model(detail.classification)
                if detail.classification
                else None
            ),
            extraction=(
                FileExtractionResponse.from_model(detail.extraction) if detail.extraction else None
            ),
            intelligence=(
                FileIntelligenceResponse.from_model(detail.intelligence)
                if detail.intelligence
                else None
            ),
            knowledge_attributes=[
                KnowledgeAttributeResponse.from_model(attribute)
                for attribute in detail.knowledge_attributes
            ],
            related_files=[
                RelatedFileResponse(
                    file_id=str(related.file.id),
                    name=related.file.name,
                    path=related.file.path,
                    relationship_type=related.relationship.relationship_type,
                    confidence=related.relationship.confidence,
                    metadata=related.relationship.metadata_,
                )
                for related in detail.related_files
            ],
            entity_links=[
                FileEntityLinkResponse.from_model(linked.link, linked.entity)
                for linked in detail.entity_links
            ],
            lifecycle=(
                FileLifecycleResponse.from_model(detail.lifecycle) if detail.lifecycle else None
            ),
        )


class EnrichmentProgressResponse(BaseModel):
    files_pending: int
    files_processed: int
    files_failed: int
    current_file_name: str | None
    updated_at: datetime

    @classmethod
    def from_model(cls, progress: EnrichmentProgress) -> "EnrichmentProgressResponse":
        return cls(
            files_pending=progress.files_pending,
            files_processed=progress.files_processed,
            files_failed=progress.files_failed,
            current_file_name=progress.current_file_name,
            updated_at=progress.updated_at,
        )


class EnrichmentJobResponse(BaseModel):
    id: str
    connector_id: str
    triggered_by: str
    status: str
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    progress: EnrichmentProgressResponse | None = None

    @classmethod
    def from_model(
        cls, job: EnrichmentJob, *, progress: EnrichmentProgress | None = None
    ) -> "EnrichmentJobResponse":
        return cls(
            id=str(job.id),
            connector_id=str(job.connector_id),
            triggered_by=job.triggered_by,
            status=job.status,
            error=job.error,
            started_at=job.started_at,
            completed_at=job.completed_at,
            created_at=job.created_at,
            progress=EnrichmentProgressResponse.from_model(progress) if progress else None,
        )


class IntelligenceProgressResponse(BaseModel):
    files_pending: int
    files_processed: int
    files_failed: int
    current_file_name: str | None
    updated_at: datetime

    @classmethod
    def from_model(cls, progress: IntelligenceProgress) -> "IntelligenceProgressResponse":
        return cls(
            files_pending=progress.files_pending,
            files_processed=progress.files_processed,
            files_failed=progress.files_failed,
            current_file_name=progress.current_file_name,
            updated_at=progress.updated_at,
        )


class IntelligenceJobResponse(BaseModel):
    id: str
    connector_id: str
    triggered_by: str
    status: str
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    progress: IntelligenceProgressResponse | None = None

    @classmethod
    def from_model(
        cls, job: IntelligenceJob, *, progress: IntelligenceProgress | None = None
    ) -> "IntelligenceJobResponse":
        return cls(
            id=str(job.id),
            connector_id=str(job.connector_id),
            triggered_by=job.triggered_by,
            status=job.status,
            error=job.error,
            started_at=job.started_at,
            completed_at=job.completed_at,
            created_at=job.created_at,
            progress=IntelligenceProgressResponse.from_model(progress) if progress else None,
        )


class EmbeddingProgressResponse(BaseModel):
    files_pending: int
    files_processed: int
    files_failed: int
    current_file_name: str | None
    updated_at: datetime

    @classmethod
    def from_model(cls, progress: EmbeddingProgress) -> "EmbeddingProgressResponse":
        return cls(
            files_pending=progress.files_pending,
            files_processed=progress.files_processed,
            files_failed=progress.files_failed,
            current_file_name=progress.current_file_name,
            updated_at=progress.updated_at,
        )


class EmbeddingJobResponse(BaseModel):
    id: str
    connector_id: str
    triggered_by: str
    status: str
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    progress: EmbeddingProgressResponse | None = None

    @classmethod
    def from_model(
        cls, job: EmbeddingJob, *, progress: EmbeddingProgress | None = None
    ) -> "EmbeddingJobResponse":
        return cls(
            id=str(job.id),
            connector_id=str(job.connector_id),
            triggered_by=job.triggered_by,
            status=job.status,
            error=job.error,
            started_at=job.started_at,
            completed_at=job.completed_at,
            created_at=job.created_at,
            progress=EmbeddingProgressResponse.from_model(progress) if progress else None,
        )


class SearchFilters(BaseModel):
    categories: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    size_min: int | None = Field(default=None, ge=0)
    size_max: int | None = Field(default=None, ge=0)
    modified_after: datetime | None = None
    modified_before: datetime | None = None
    folder: str | None = Field(default=None, max_length=255)
    connector_id: str | None = None
    ownership: Literal["mine", "shared"] | None = None
    sort: Literal["relevance", "largest", "newest", "oldest"] = "relevance"


class SearchRequest(BaseModel):
    query: str = Field(default="", max_length=1024)
    filters: SearchFilters | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _query_or_filters(self) -> "SearchRequest":
        if not self.query.strip() and self.filters is None:
            raise ValueError("Type something to search for, or choose a filter.")
        return self


class SearchResultResponse(BaseModel):
    file_id: str
    name: str
    path: str
    mime_type: str | None
    size_bytes: int | None
    provider_modified_at: datetime | None
    web_view_link: str | None
    provider: str | None
    score: float
    retrieval_method: str

    @classmethod
    def from_result(cls, result: SearchResult) -> "SearchResultResponse":
        return cls(
            file_id=str(result.file.id),
            name=result.file.name,
            path=result.file.path,
            mime_type=result.file.mime_type,
            size_bytes=result.file.size_bytes,
            provider_modified_at=result.file.provider_modified_at,
            web_view_link=result.file.web_view_link,
            provider=(
                provider_display_name(result.connector.provider) if result.connector else None
            ),
            score=result.score,
            retrieval_method=result.retrieval_method,
        )


class SearchResponse(BaseModel):
    query: str
    total: int
    understood: list[str]
    interpreted_by_ai: bool
    results: list[SearchResultResponse]


class CitationResponse(BaseModel):
    id: str
    file_id: str
    snippet: str | None
    confidence: float
    retrieval_method: str
    page_number: int | None = None
    passage_index: int | None = None
    file_name: str | None = None
    file_size_bytes: int | None = None
    file_mime_type: str | None = None

    @classmethod
    def from_model(cls, citation: Citation, *, file: File | None = None) -> "CitationResponse":
        return cls(
            id=str(citation.id),
            file_id=str(citation.file_id),
            snippet=citation.snippet,
            confidence=citation.confidence,
            retrieval_method=citation.retrieval_method,
            page_number=citation.page_number,
            passage_index=citation.passage_index,
            file_name=file.name if file else None,
            file_size_bytes=file.size_bytes if file else None,
            file_mime_type=file.mime_type if file else None,
        )


class ConversationMessageResponse(BaseModel):
    id: str
    role: str
    content: str
    retrieval_method: str | None
    provider: str | None
    token_usage: int | None
    tool_name: str | None
    created_at: datetime
    citations: list[CitationResponse] = []
    # Structured answer parts (file lists, duplicate groups, proposals…).
    blocks: list[dict] = []

    @classmethod
    def from_model(
        cls,
        message: ConversationMessage,
        *,
        citations: list[Citation] | None = None,
        files_by_id: dict[uuid.UUID, File] | None = None,
    ) -> "ConversationMessageResponse":
        files_by_id = files_by_id or {}
        return cls(
            id=str(message.id),
            role=message.role,
            content=message.content,
            retrieval_method=message.retrieval_method,
            provider=message.provider,
            token_usage=message.token_usage,
            tool_name=message.tool_name,
            created_at=message.created_at,
            blocks=public_blocks(message.blocks or []),
            citations=[
                CitationResponse.from_model(c, file=files_by_id.get(c.file_id))
                for c in citations or []
            ],
        )


class ConversationResponse(BaseModel):
    id: str
    title: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, conversation: Conversation) -> "ConversationResponse":
        return cls(
            id=str(conversation.id),
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )


class ConversationDetailResponse(ConversationResponse):
    messages: list[ConversationMessageResponse]

    @classmethod
    def from_detail(cls, detail: ConversationDetail) -> "ConversationDetailResponse":
        return cls(
            id=str(detail.conversation.id),
            title=detail.conversation.title,
            created_at=detail.conversation.created_at,
            updated_at=detail.conversation.updated_at,
            messages=[
                ConversationMessageResponse.from_model(
                    message,
                    citations=detail.citations_by_message_id.get(message.id),
                    files_by_id=detail.files_by_id,
                )
                for message in detail.messages
            ],
        )


class ResultPageResponse(BaseModel):
    total: int
    items: list[dict] = []
    groups: list[dict] = []


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4096)


class AskResponse(BaseModel):
    conversation: ConversationResponse
    user_message: ConversationMessageResponse
    assistant_message: ConversationMessageResponse

    @classmethod
    def from_turn(cls, turn: AssistantTurn) -> "AskResponse":
        return cls(
            conversation=ConversationResponse.from_model(turn.conversation),
            user_message=ConversationMessageResponse.from_model(turn.user_message),
            assistant_message=ConversationMessageResponse.from_model(
                turn.assistant_message, citations=turn.citations, files_by_id=turn.files_by_id
            ),
        )


class DashboardSnapshotResponse(BaseModel):
    connected_providers: int
    total_files: int
    total_folders: int
    total_storage_bytes: int
    classified_files: int
    unclassified_files: int
    pending_enrichment_files: int
    embedded_files: int
    relationship_count: int
    active_recommendations: int
    knowledge_completeness_score: float
    created_at: datetime

    @classmethod
    def from_model(cls, snapshot: DashboardSnapshot) -> "DashboardSnapshotResponse":
        return cls(
            connected_providers=snapshot.connected_providers,
            total_files=snapshot.total_files,
            total_folders=snapshot.total_folders,
            total_storage_bytes=snapshot.total_storage_bytes,
            classified_files=snapshot.classified_files,
            unclassified_files=snapshot.unclassified_files,
            pending_enrichment_files=snapshot.pending_enrichment_files,
            embedded_files=snapshot.embedded_files,
            relationship_count=snapshot.relationship_count,
            active_recommendations=snapshot.active_recommendations,
            knowledge_completeness_score=snapshot.knowledge_completeness_score,
            created_at=snapshot.created_at,
        )


class InsightRecordResponse(BaseModel):
    id: str
    insight_type: str
    title: str
    description: str
    confidence: float
    related_file_ids: list[str]
    created_at: datetime

    @classmethod
    def from_model(cls, insight: InsightRecord) -> "InsightRecordResponse":
        return cls(
            id=str(insight.id),
            insight_type=insight.insight_type,
            title=insight.title,
            description=insight.description,
            confidence=insight.confidence,
            related_file_ids=insight.related_file_ids,
            created_at=insight.created_at,
        )


class IntelligenceSummaryResponse(BaseModel):
    files_analyzed: int
    entities_by_type: dict[str, int]
    lifecycle_by_state: dict[str, int]
    organize_suggestions: int


class DashboardResponse(BaseModel):
    connector_count: int
    latest_snapshot: DashboardSnapshotResponse | None
    snapshot_history: list[DashboardSnapshotResponse]
    recent_insights: list[InsightRecordResponse]
    recent_activity: list[FileSummaryResponse]
    latest_scan_status: str | None
    latest_enrichment_status: str | None
    latest_embedding_status: str | None
    latest_recommendation_status: str | None
    intelligence: IntelligenceSummaryResponse

    @classmethod
    def from_overview(cls, overview: DashboardOverview) -> "DashboardResponse":
        return cls(
            connector_count=len(overview.connectors),
            latest_snapshot=(
                DashboardSnapshotResponse.from_model(overview.latest_snapshot)
                if overview.latest_snapshot
                else None
            ),
            snapshot_history=[
                DashboardSnapshotResponse.from_model(snapshot)
                for snapshot in overview.snapshot_history
            ],
            recent_insights=[
                InsightRecordResponse.from_model(insight) for insight in overview.recent_insights
            ],
            recent_activity=[
                FileSummaryResponse.from_model(file) for file in overview.recent_activity
            ],
            intelligence=IntelligenceSummaryResponse(
                files_analyzed=overview.intelligence.files_analyzed,
                entities_by_type=overview.intelligence.entities_by_type,
                lifecycle_by_state=overview.intelligence.lifecycle_by_state,
                organize_suggestions=overview.intelligence.organize_suggestions,
            ),
            latest_scan_status=overview.latest_scan_status,
            latest_enrichment_status=overview.latest_enrichment_status,
            latest_embedding_status=overview.latest_embedding_status,
            latest_recommendation_status=overview.latest_recommendation_status,
        )


class RecommendationResponse(BaseModel):
    id: str
    category: str
    rule_name: str
    title: str
    description: str
    confidence: float
    estimated_impact: str
    impact_value: float | None
    risk_level: str
    suggested_action: str
    requires_approval: bool
    related_departments: list[str]
    affected_file_ids: list[str]
    status: str
    priority_score: float
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    actionable: bool

    @classmethod
    def from_model(cls, recommendation: Recommendation) -> "RecommendationResponse":
        return cls(
            actionable=is_executable_rule(recommendation.rule_name),
            id=str(recommendation.id),
            category=recommendation.category,
            rule_name=recommendation.rule_name,
            title=recommendation.title,
            description=recommendation.description,
            confidence=recommendation.confidence,
            estimated_impact=recommendation.estimated_impact,
            impact_value=recommendation.impact_value,
            risk_level=recommendation.risk_level,
            suggested_action=recommendation.suggested_action,
            requires_approval=recommendation.requires_approval,
            related_departments=recommendation.related_departments,
            affected_file_ids=recommendation.affected_file_ids,
            status=recommendation.status,
            priority_score=recommendation.priority_score,
            created_at=recommendation.created_at,
            updated_at=recommendation.updated_at,
            resolved_at=recommendation.resolved_at,
        )


class RecommendationListResponse(BaseModel):
    items: list[RecommendationResponse]


class RecommendationJobResponse(BaseModel):
    id: str
    organization_id: str
    triggered_by: str
    status: str
    error: str | None
    recommendations_active: int | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, job: RecommendationJob) -> "RecommendationJobResponse":
        return cls(
            id=str(job.id),
            organization_id=str(job.organization_id),
            triggered_by=job.triggered_by,
            status=job.status,
            error=job.error,
            recommendations_active=job.recommendations_active,
            started_at=job.started_at,
            completed_at=job.completed_at,
            created_at=job.created_at,
        )


class ExecutionStepResponse(BaseModel):
    id: str
    step_order: int
    action_type: str
    target_file_id: str
    pre_state: dict
    planned_change: dict
    status: str

    @classmethod
    def from_model(cls, step: ExecutionStep) -> "ExecutionStepResponse":
        return cls(
            id=str(step.id),
            step_order=step.step_order,
            action_type=step.action_type,
            target_file_id=str(step.target_file_id),
            pre_state=step.pre_state,
            planned_change=step.planned_change,
            status=step.status,
        )


class ExecutionPlanResponse(BaseModel):
    id: str
    organization_id: str
    recommendation_id: str | None
    duplicate_group_id: str | None
    status: str
    target_provider: str
    estimated_impact: str
    estimated_storage_savings_bytes: int | None
    risk_level: str
    rollback_available: bool
    required_permissions: list[str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, plan: ExecutionPlan) -> "ExecutionPlanResponse":
        return cls(
            id=str(plan.id),
            organization_id=str(plan.organization_id),
            recommendation_id=str(plan.recommendation_id) if plan.recommendation_id else None,
            duplicate_group_id=(str(plan.duplicate_group_id) if plan.duplicate_group_id else None),
            status=plan.status,
            target_provider=plan.target_provider,
            estimated_impact=plan.estimated_impact,
            estimated_storage_savings_bytes=plan.estimated_storage_savings_bytes,
            risk_level=plan.risk_level,
            rollback_available=plan.rollback_available,
            required_permissions=plan.required_permissions,
            created_at=plan.created_at,
            updated_at=plan.updated_at,
        )


class ExecutionPlanDetailResponse(ExecutionPlanResponse):
    steps: list[ExecutionStepResponse]

    @classmethod
    def from_detail(cls, detail: ExecutionPlanDetail) -> "ExecutionPlanDetailResponse":
        base = ExecutionPlanResponse.from_model(detail.plan)
        return cls(
            **base.model_dump(),
            steps=[ExecutionStepResponse.from_model(step) for step in detail.steps],
        )


class ArchiveManifestEntry(BaseModel):
    file_id: str
    name: str
    path: str
    size_bytes: int
    mime_type: str | None
    checksum_sha256: str
    # Absent on archives created before archives were stored in the user's
    # own connected storage.
    provider_file_id: str | None = None
    original_size_bytes: int | None = None
    original_modified_at: str | None = None
    exported_as: str | None = None
    zip_entry_name: str | None = None
    original_removed: bool = False


class ArchiveJobResponse(BaseModel):
    id: str
    organization_id: str
    execution_plan_id: str
    name: str
    status: str
    object_storage_key: str | None
    original_size_bytes: int | None
    compressed_size_bytes: int | None
    file_count: int
    created_by_user_id: str
    created_at: datetime
    completed_at: datetime | None
    destination_path: str | None
    destination_web_view_link: str | None
    archive_sha256: str | None
    verified_at: datetime | None
    remove_originals: bool
    originals_removed_count: int

    @classmethod
    def from_model(cls, archive_job: ArchiveJob) -> "ArchiveJobResponse":
        return cls(
            id=str(archive_job.id),
            organization_id=str(archive_job.organization_id),
            execution_plan_id=str(archive_job.execution_plan_id),
            name=archive_job.name,
            status=archive_job.status,
            object_storage_key=archive_job.object_storage_key,
            original_size_bytes=archive_job.original_size_bytes,
            compressed_size_bytes=archive_job.compressed_size_bytes,
            file_count=archive_job.file_count,
            created_by_user_id=str(archive_job.created_by_user_id),
            created_at=archive_job.created_at,
            completed_at=archive_job.completed_at,
            destination_path=archive_job.destination_path,
            destination_web_view_link=archive_job.destination_web_view_link,
            archive_sha256=archive_job.archive_sha256,
            verified_at=archive_job.verified_at,
            remove_originals=archive_job.remove_originals,
            originals_removed_count=archive_job.originals_removed_count,
        )


class ArchiveJobDetailResponse(ArchiveJobResponse):
    manifest: list[ArchiveManifestEntry]

    @classmethod
    def from_model_detail(cls, archive_job: ArchiveJob) -> "ArchiveJobDetailResponse":
        base = ArchiveJobResponse.from_model(archive_job)
        return cls(**base.model_dump(), manifest=archive_job.manifest)


class CreateExecutionPlanRequest(BaseModel):
    """Exactly one origin must be set — validated in the router, which
    calls the matching `ExecutionPlanService` method. `file_ids` (with
    `action_type`) is Storage Intelligence's ad-hoc origin: a user-picked
    file selection from a large/old/inactive/temporary-candidate listing,
    rather than a precomputed `Recommendation`/`DuplicateGroup`."""

    recommendation_id: str | None = None
    duplicate_group_id: str | None = None
    file_ids: list[str] | None = None
    action_type: str | None = None
    new_name: str | None = None
    new_parent_id: str | None = None
    # CREATE_ARCHIVE only: move originals to Trash once the archive is verified.
    remove_originals: bool = False


class PermanentDeleteRequest(BaseModel):
    """Its own request shape, its own endpoint — never folded into
    `CreateExecutionPlanRequest`'s `action_type` allowlist, so a
    real, unrecoverable Drive deletion can never be reached through the
    generic ad-hoc plan path."""

    file_ids: list[str]


class ApprovalRequestResponse(BaseModel):
    id: str
    execution_plan_id: str
    organization_id: str
    status: str
    expires_at: datetime
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, request: ApprovalRequest) -> "ApprovalRequestResponse":
        return cls(
            id=str(request.id),
            execution_plan_id=str(request.execution_plan_id),
            organization_id=str(request.organization_id),
            status=request.status,
            expires_at=request.expires_at,
            created_at=request.created_at,
            updated_at=request.updated_at,
        )


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject|request_changes)$")
    comments: str | None = Field(default=None, max_length=4096)


class BulkApprovalDecisionRequest(BaseModel):
    approval_request_ids: list[str] = Field(min_length=1, max_length=100)
    decision: str = Field(pattern="^(approve|reject|request_changes)$")
    comments: str | None = Field(default=None, max_length=4096)


class ExecutionJobResponse(BaseModel):
    id: str
    execution_plan_id: str
    organization_id: str
    status: str
    is_rollback: bool
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, job: ExecutionJob) -> "ExecutionJobResponse":
        return cls(
            id=str(job.id),
            execution_plan_id=str(job.execution_plan_id),
            organization_id=str(job.organization_id),
            status=job.status,
            is_rollback=job.is_rollback,
            error=job.error,
            started_at=job.started_at,
            completed_at=job.completed_at,
            created_at=job.created_at,
        )


class ExecutionResultResponse(BaseModel):
    id: str
    execution_step_id: str
    status: str
    verification_status: str
    error: str | None
    executed_at: datetime
    verified_at: datetime | None

    @classmethod
    def from_model(cls, result: ExecutionResult) -> "ExecutionResultResponse":
        return cls(
            id=str(result.id),
            execution_step_id=str(result.execution_step_id),
            status=result.status,
            verification_status=result.verification_status,
            error=result.error,
            executed_at=result.executed_at,
            verified_at=result.verified_at,
        )


class ExecutionJobDetailResponse(ExecutionJobResponse):
    results: list[ExecutionResultResponse]

    @classmethod
    def from_detail(cls, detail: ExecutionJobDetail) -> "ExecutionJobDetailResponse":
        base = ExecutionJobResponse.from_model(detail.job)
        return cls(
            **base.model_dump(),
            results=[ExecutionResultResponse.from_model(result) for result in detail.results],
        )


# ---------------------------------------------------------------------------
# Phase 9 — Automation Engine (ADR-021)
# ---------------------------------------------------------------------------


class WorkflowResponse(BaseModel):
    id: str
    organization_id: str
    created_by_user_id: str
    name: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, workflow: Workflow) -> "WorkflowResponse":
        return cls(
            id=str(workflow.id),
            organization_id=str(workflow.organization_id),
            created_by_user_id=str(workflow.created_by_user_id),
            name=workflow.name,
            description=workflow.description,
            status=workflow.status,
            created_at=workflow.created_at,
            updated_at=workflow.updated_at,
        )


class WorkflowVersionResponse(BaseModel):
    id: str
    workflow_id: str
    version_number: int
    status: str
    published_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, version: WorkflowVersion) -> "WorkflowVersionResponse":
        return cls(
            id=str(version.id),
            workflow_id=str(version.workflow_id),
            version_number=version.version_number,
            status=version.status,
            published_at=version.published_at,
            created_at=version.created_at,
        )


class WorkflowNodeResponse(BaseModel):
    id: str
    node_type: str
    name: str
    config: dict
    next_nodes: dict
    position_x: int
    position_y: int

    @classmethod
    def from_model(cls, node: WorkflowNode) -> "WorkflowNodeResponse":
        return cls(
            id=str(node.id),
            node_type=node.node_type,
            name=node.name,
            config=node.config,
            next_nodes=node.next_nodes,
            position_x=node.position_x,
            position_y=node.position_y,
        )


class WorkflowDetailResponse(WorkflowResponse):
    published_version: WorkflowVersionResponse | None
    draft_version: WorkflowVersionResponse | None

    @classmethod
    def from_detail(cls, detail: WorkflowDetail) -> "WorkflowDetailResponse":
        base = WorkflowResponse.from_model(detail.workflow)
        return cls(
            **base.model_dump(),
            published_version=(
                WorkflowVersionResponse.from_model(detail.published_version)
                if detail.published_version
                else None
            ),
            draft_version=(
                WorkflowVersionResponse.from_model(detail.draft_version)
                if detail.draft_version
                else None
            ),
        )


class CreateWorkflowRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)


class SetWorkflowStatusRequest(BaseModel):
    status: str = Field(pattern="^(active|paused|disabled)$")


class WorkflowNodeInput(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    node_type: str
    name: str = Field(min_length=1, max_length=255)
    config: dict = Field(default_factory=dict)
    next_nodes: dict[str, str] = Field(default_factory=dict)
    position_x: int = 0
    position_y: int = 0


class ReplaceWorkflowNodesRequest(BaseModel):
    nodes: list[WorkflowNodeInput] = Field(min_length=1, max_length=200)


class WorkflowDraftResponse(BaseModel):
    version: WorkflowVersionResponse
    nodes: list[WorkflowNodeResponse]


class WorkflowTriggerResponse(BaseModel):
    id: str
    workflow_id: str
    trigger_type: str
    config: dict
    enabled: bool
    created_at: datetime

    @classmethod
    def from_model(cls, trigger: WorkflowTrigger) -> "WorkflowTriggerResponse":
        return cls(
            id=str(trigger.id),
            workflow_id=str(trigger.workflow_id),
            trigger_type=trigger.trigger_type,
            config=trigger.config,
            enabled=trigger.enabled,
            created_at=trigger.created_at,
        )


class CreateWorkflowTriggerRequest(BaseModel):
    trigger_type: str = Field(pattern="^(scheduled|event|manual)$")
    config: dict = Field(default_factory=dict)


class SetWorkflowTriggerEnabledRequest(BaseModel):
    enabled: bool


class WorkflowNodeExecutionResponse(BaseModel):
    id: str
    workflow_node_id: str
    status: str
    output_context: dict
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def from_model(cls, node_execution: WorkflowNodeExecution) -> "WorkflowNodeExecutionResponse":
        return cls(
            id=str(node_execution.id),
            workflow_node_id=str(node_execution.workflow_node_id),
            status=node_execution.status,
            output_context=node_execution.output_context,
            error=node_execution.error,
            started_at=node_execution.started_at,
            completed_at=node_execution.completed_at,
        )


class WorkflowExecutionResponse(BaseModel):
    id: str
    workflow_id: str
    workflow_version_id: str
    organization_id: str
    status: str
    trigger_type: str
    current_node_id: str | None
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, execution: WorkflowExecution) -> "WorkflowExecutionResponse":
        return cls(
            id=str(execution.id),
            workflow_id=str(execution.workflow_id),
            workflow_version_id=str(execution.workflow_version_id),
            organization_id=str(execution.organization_id),
            status=execution.status,
            trigger_type=execution.trigger_type,
            current_node_id=str(execution.current_node_id) if execution.current_node_id else None,
            error=execution.error,
            started_at=execution.started_at,
            completed_at=execution.completed_at,
            created_at=execution.created_at,
        )


class WorkflowExecutionDetailResponse(WorkflowExecutionResponse):
    node_executions: list[WorkflowNodeExecutionResponse]

    @classmethod
    def from_detail(cls, detail: WorkflowExecutionDetail) -> "WorkflowExecutionDetailResponse":
        base = WorkflowExecutionResponse.from_model(detail.execution)
        return cls(
            **base.model_dump(),
            node_executions=[
                WorkflowNodeExecutionResponse.from_model(ne) for ne in detail.node_executions
            ],
        )


class WorkflowPolicyResponse(BaseModel):
    id: str
    organization_id: str
    policy_key: str
    version: int
    name: str
    description: str | None
    status: str
    effect: str
    conditions: dict
    published_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, policy: WorkflowPolicy) -> "WorkflowPolicyResponse":
        return cls(
            id=str(policy.id),
            organization_id=str(policy.organization_id),
            policy_key=policy.policy_key,
            version=policy.version,
            name=policy.name,
            description=policy.description,
            status=policy.status,
            effect=policy.effect,
            conditions=policy.conditions,
            published_at=policy.published_at,
            created_at=policy.created_at,
        )


class CreateWorkflowPolicyRequest(BaseModel):
    policy_key: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    effect: str = Field(pattern="^(auto_execute|require_approval|skip)$")
    conditions: dict = Field(default_factory=dict)


class NotificationResponse(BaseModel):
    id: str
    workflow_execution_id: str | None
    channel: str
    subject: str
    body: str
    status: str
    sent_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, notification: Notification) -> "NotificationResponse":
        return cls(
            id=str(notification.id),
            workflow_execution_id=(
                str(notification.workflow_execution_id)
                if notification.workflow_execution_id
                else None
            ),
            channel=notification.channel,
            subject=notification.subject,
            body=notification.body,
            status=notification.status,
            sent_at=notification.sent_at,
            created_at=notification.created_at,
        )


class AutomationTemplateResponse(BaseModel):
    id: str
    name: str
    description: str | None
    category: str
    node_definitions: list

    @classmethod
    def from_model(cls, template: AutomationTemplate) -> "AutomationTemplateResponse":
        return cls(
            id=str(template.id),
            name=template.name,
            description=template.description,
            category=template.category,
            node_definitions=template.node_definitions,
        )


class StorageFileResponse(BaseModel):
    """A file's shape as it appears in Storage Intelligence's large/old/
    inactive/candidate listings — deliberately not `FileSummaryResponse`
    (it lacks `provider_viewed_at`/`storage_source_id`, both needed here:
    Phase 1 spec §8's "last_accessed_at"/"storage_source" columns)."""

    id: str
    name: str
    path: str
    mime_type: str | None
    size_bytes: int | None
    provider_modified_at: datetime | None
    provider_viewed_at: datetime | None
    storage_source_id: str

    @classmethod
    def from_model(cls, file: File) -> "StorageFileResponse":
        return cls(
            id=str(file.id),
            name=file.name,
            path=file.path,
            mime_type=file.mime_type,
            size_bytes=file.size_bytes,
            provider_modified_at=file.provider_modified_at,
            provider_viewed_at=file.provider_viewed_at,
            storage_source_id=str(file.storage_source_id),
        )


class StorageFileListResponse(BaseModel):
    items: list[StorageFileResponse]
    total: int


class DuplicateGroupResponse(BaseModel):
    id: str
    checksum: str
    file_count: int
    total_size_bytes: int
    recoverable_size_bytes: int
    recommended_keep_file_id: str | None
    recommended_keep_reason: str | None
    recommended_keep_confidence: float | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, group: DuplicateGroup) -> "DuplicateGroupResponse":
        return cls(
            id=str(group.id),
            checksum=group.checksum,
            file_count=group.file_count,
            total_size_bytes=group.total_size_bytes,
            recoverable_size_bytes=group.recoverable_size_bytes,
            recommended_keep_file_id=(
                str(group.recommended_keep_file_id) if group.recommended_keep_file_id else None
            ),
            recommended_keep_reason=group.recommended_keep_reason,
            recommended_keep_confidence=group.recommended_keep_confidence,
            created_at=group.created_at,
            updated_at=group.updated_at,
        )


class DuplicateGroupListResponse(BaseModel):
    items: list[DuplicateGroupResponse]
    total: int


class DuplicateGroupMemberResponse(BaseModel):
    file: StorageFileResponse
    is_recommended_keep: bool


class DuplicateGroupDetailResponse(DuplicateGroupResponse):
    members: list[DuplicateGroupMemberResponse]

    @classmethod
    def from_model_with_members(
        cls, group: DuplicateGroup, members: list[tuple[File, bool]]
    ) -> "DuplicateGroupDetailResponse":
        base = DuplicateGroupResponse.from_model(group)
        return cls(
            **base.model_dump(),
            members=[
                DuplicateGroupMemberResponse(
                    file=StorageFileResponse.from_model(file), is_recommended_keep=is_keep
                )
                for file, is_keep in members
            ],
        )


class StorageAnalysisJobResponse(BaseModel):
    id: str
    organization_id: str
    triggered_by: str
    status: str
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, job: StorageAnalysisJob) -> "StorageAnalysisJobResponse":
        return cls(
            id=str(job.id),
            organization_id=str(job.organization_id),
            triggered_by=job.triggered_by,
            status=job.status,
            error=job.error,
            started_at=job.started_at,
            completed_at=job.completed_at,
            created_at=job.created_at,
        )


class StorageOverviewResponse(BaseModel):
    """`GET /v1/storage/overview` — the headline numbers (Phase 1 spec
    §21's stat-card row): total size/files, duplicate savings, large/old/
    inactive/temporary summary counts, total potential savings, and when
    the org was last analyzed. `None` fields mean no analysis has run yet
    for this organization — the frontend shows an empty state, never a
    fabricated zero (Phase 1 spec's "never fabricate data" discipline,
    carried over from the Search Performance work)."""

    total_size_bytes: int | None
    total_files: int | None
    total_folders: int | None
    duplicate_group_count: int | None
    duplicate_file_count: int | None
    duplicate_recoverable_bytes: int | None
    large_file_count: int | None
    large_file_bytes: int | None
    old_file_count: int | None
    old_file_bytes: int | None
    inactive_file_count: int | None
    inactive_file_bytes: int | None
    temporary_candidate_count: int | None
    temporary_candidate_bytes: int | None
    total_potential_savings_bytes: int | None
    last_analyzed_at: datetime | None
    last_analysis_status: str | None

    @classmethod
    def from_models(
        cls,
        snapshot: StorageAnalysisSnapshot | None,
        latest_job: StorageAnalysisJob | None,
    ) -> "StorageOverviewResponse":
        return cls(
            total_size_bytes=snapshot.total_size_bytes if snapshot else None,
            total_files=snapshot.total_files if snapshot else None,
            total_folders=snapshot.total_folders if snapshot else None,
            duplicate_group_count=snapshot.duplicate_group_count if snapshot else None,
            duplicate_file_count=snapshot.duplicate_file_count if snapshot else None,
            duplicate_recoverable_bytes=(
                snapshot.duplicate_recoverable_bytes if snapshot else None
            ),
            large_file_count=snapshot.large_file_count if snapshot else None,
            large_file_bytes=snapshot.large_file_bytes if snapshot else None,
            old_file_count=snapshot.old_file_count if snapshot else None,
            old_file_bytes=snapshot.old_file_bytes if snapshot else None,
            inactive_file_count=snapshot.inactive_file_count if snapshot else None,
            inactive_file_bytes=snapshot.inactive_file_bytes if snapshot else None,
            temporary_candidate_count=snapshot.temporary_candidate_count if snapshot else None,
            temporary_candidate_bytes=snapshot.temporary_candidate_bytes if snapshot else None,
            total_potential_savings_bytes=(
                snapshot.total_potential_savings_bytes if snapshot else None
            ),
            last_analyzed_at=snapshot.created_at if snapshot else None,
            last_analysis_status=latest_job.status if latest_job else None,
        )


class StorageStatisticsResponse(BaseModel):
    """`GET /v1/storage/statistics` — the breakdown data (Phase 1 spec
    §7's "where is the user's storage going" charts), separate from the
    headline numbers above so a client that only needs one doesn't fetch
    the other."""

    breakdown_by_type_bytes: dict[str, int]
    breakdown_by_size_bucket_bytes: dict[str, int]
    breakdown_by_source_bytes: dict[str, dict]
    computed_at: datetime | None

    @classmethod
    def from_model(cls, snapshot: StorageAnalysisSnapshot | None) -> "StorageStatisticsResponse":
        if snapshot is None:
            return cls(
                breakdown_by_type_bytes={},
                breakdown_by_size_bucket_bytes={},
                breakdown_by_source_bytes={},
                computed_at=None,
            )
        return cls(
            breakdown_by_type_bytes=snapshot.breakdown_by_type_bytes,
            breakdown_by_size_bucket_bytes=snapshot.breakdown_by_size_bucket_bytes,
            breakdown_by_source_bytes=snapshot.breakdown_by_source_bytes,
            computed_at=snapshot.created_at,
        )


class ApplyAutomationTemplateRequest(BaseModel):
    workflow_name: str | None = Field(default=None, max_length=255)


class OrganizationEntityResponse(BaseModel):
    id: str
    entity_type: str
    name: str
    confidence: float
    evidence: list[dict]
    status: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, entity: OrganizationEntity) -> "OrganizationEntityResponse":
        return cls(
            id=str(entity.id),
            entity_type=entity.entity_type,
            name=entity.name,
            confidence=entity.confidence,
            evidence=entity.evidence,
            status=entity.status,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


class OrganizationEntityListResponse(BaseModel):
    items: list[OrganizationEntityResponse]


class OrganizationEntityFileResponse(BaseModel):
    id: str
    name: str
    path: str


class OrganizationEntityDetailResponse(BaseModel):
    entity: OrganizationEntityResponse
    files: list[OrganizationEntityFileResponse]

    @classmethod
    def from_detail(cls, detail: OrganizationEntityDetail) -> "OrganizationEntityDetailResponse":
        return cls(
            entity=OrganizationEntityResponse.from_model(detail.entity),
            files=[
                OrganizationEntityFileResponse(id=str(file.id), name=file.name, path=file.path)
                for file in detail.files
            ],
        )


class OrganizationRecommendationResponse(BaseModel):
    id: str
    kind: str
    entity_id: str | None
    title: str
    reasoning_summary: str
    evidence: list[dict]
    confidence: float
    affected_file_ids: list[str]
    current_locations: list[dict]
    suggested_destination: list[str]
    estimated_storage_impact_bytes: int | None
    status: str
    execution_plan_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(
        cls, recommendation: OrganizationRecommendation
    ) -> "OrganizationRecommendationResponse":
        return cls(
            id=str(recommendation.id),
            kind=recommendation.kind,
            entity_id=str(recommendation.entity_id) if recommendation.entity_id else None,
            title=recommendation.title,
            reasoning_summary=recommendation.reasoning_summary,
            evidence=recommendation.evidence,
            confidence=recommendation.confidence,
            affected_file_ids=recommendation.affected_file_ids,
            current_locations=recommendation.current_locations,
            suggested_destination=recommendation.suggested_destination,
            estimated_storage_impact_bytes=recommendation.estimated_storage_impact_bytes,
            status=recommendation.status,
            execution_plan_id=(
                str(recommendation.execution_plan_id) if recommendation.execution_plan_id else None
            ),
            created_at=recommendation.created_at,
            updated_at=recommendation.updated_at,
        )


class OrganizationRecommendationListResponse(BaseModel):
    items: list[OrganizationRecommendationResponse]


class OrganizationAnalysisJobResponse(BaseModel):
    id: str
    organization_id: str
    status: str
    clusters_found: int
    entities_created: int
    recommendations_generated: int
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, job: OrganizationAnalysisJob) -> "OrganizationAnalysisJobResponse":
        return cls(
            id=str(job.id),
            organization_id=str(job.organization_id),
            status=job.status,
            clusters_found=job.clusters_found,
            entities_created=job.entities_created,
            recommendations_generated=job.recommendations_generated,
            error=job.error,
            started_at=job.started_at,
            completed_at=job.completed_at,
            created_at=job.created_at,
        )


class ActionProblemResponse(BaseModel):
    file_name: str
    reason: str


class ActionArchiveResponse(BaseModel):
    destination_path: str | None
    destination_web_view_link: str | None
    original_size_bytes: int | None
    compressed_size_bytes: int | None
    originals_removed_count: int
    verified: bool


class ActionResponse(BaseModel):
    """What the user asked AI Vault to do and what the provider confirmed —
    deliberately no plan/job/step vocabulary."""

    id: str
    kind: str
    status: str
    message: str
    total: int
    succeeded: int
    failed: int
    verified: int
    provider: str
    created_at: datetime
    can_undo: bool
    problems: list[ActionProblemResponse]
    archive: ActionArchiveResponse | None


class ActivityItemResponse(BaseModel):
    id: str
    kind: str
    status: str
    message: str
    at: datetime


class ActivityResponse(BaseModel):
    items: list[ActivityItemResponse]


class CreateLocalAgentRequest(BaseModel):
    name: str = Field(default="This computer", max_length=255)


class LocalAgentKeyResponse(BaseModel):
    """The agent key is in this response only — AI Vault keeps just a hash."""

    connector_id: str
    agent_key: str


class AgentVolume(BaseModel):
    mount: str = Field(max_length=4096)
    label: str | None = Field(default=None, max_length=255)
    filesystem: str | None = Field(default=None, max_length=64)
    kind: str = Field(max_length=32)
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)


class LocalAgentResponse(BaseModel):
    connector_id: str
    name: str | None
    device_name: str | None
    platform: str | None
    online: bool
    last_seen_at: datetime | None
    roots: list[str]
    offline_roots: list[str]
    volumes: list[AgentVolume]

    @classmethod
    def from_models(
        cls, connector: StorageConnector, agent: LocalAgent | None, *, online: bool
    ) -> "LocalAgentResponse":
        return cls(
            connector_id=str(connector.id),
            name=connector.display_name,
            device_name=agent.device_name if agent else None,
            platform=agent.platform if agent else None,
            online=online,
            last_seen_at=agent.last_seen_at if agent else None,
            roots=list(agent.roots) if agent else [],
            offline_roots=list(agent.offline_roots or []) if agent else [],
            volumes=[AgentVolume(**volume) for volume in agent.volumes] if agent else [],
        )


class AgentHeartbeatRequest(BaseModel):
    device_name: str = Field(max_length=255)
    platform: str = Field(max_length=255)
    roots: list[str] = Field(max_length=50)
    offline_roots: list[str] = Field(default_factory=list, max_length=50)
    volumes: list[AgentVolume] = Field(max_length=100)


class AgentFolderRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class AgentBrowseFolder(BaseModel):
    name: str
    path: str
    can_add: bool
    reason: str | None
    added: bool


class AgentBrowseResponse(BaseModel):
    """Folder names inside one folder on the connected computer — never files."""

    path: str
    parent: str | None
    can_add: bool
    reason: str | None
    folders: list[AgentBrowseFolder]
    truncated: bool


class AgentRootsResponse(BaseModel):
    roots: list[str]


class AgentScanBatchRequest(BaseModel):
    root: str = Field(max_length=4096)
    root_id: str = Field(max_length=100)
    entries: list[dict] = Field(max_length=2000)
    first: bool = False
    final: bool = False
    errors: list[dict] = Field(default_factory=list, max_length=10000)


class AgentScanBatchResponse(BaseModel):
    stored: int


class AgentCommandResponse(BaseModel):
    id: str
    op: str
    params: dict


class AgentCommandResultRequest(BaseModel):
    """What the agent saw after running a command: `ok`, and either the
    resulting state or the reason it failed."""

    model_config = ConfigDict(extra="allow")

    ok: bool


class AgentCommandsResponse(BaseModel):
    commands: list[AgentCommandResponse]
