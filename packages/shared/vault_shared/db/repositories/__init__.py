from vault_shared.db.repositories.ai_provider_config_repository import (
    AIProviderConfigRepository,
)
from vault_shared.db.repositories.approval_decision_repository import (
    ApprovalDecisionRepository,
)
from vault_shared.db.repositories.approval_request_repository import ApprovalRequestRepository
from vault_shared.db.repositories.archive_job_repository import ArchiveJobRepository
from vault_shared.db.repositories.audit_log_repository import AuditLogRepository
from vault_shared.db.repositories.automation_template_repository import (
    AutomationTemplateRepository,
)
from vault_shared.db.repositories.citation_repository import CitationRepository
from vault_shared.db.repositories.connector_credentials_repository import (
    ConnectorCredentialsRepository,
)
from vault_shared.db.repositories.conversation_message_repository import (
    ConversationMessageRepository,
)
from vault_shared.db.repositories.conversation_repository import ConversationRepository
from vault_shared.db.repositories.dashboard_snapshot_repository import (
    DashboardSnapshotRepository,
)
from vault_shared.db.repositories.duplicate_group_repository import DuplicateGroupRepository
from vault_shared.db.repositories.embedding_event_repository import EmbeddingEventRepository
from vault_shared.db.repositories.embedding_job_repository import EmbeddingJobRepository
from vault_shared.db.repositories.embedding_progress_repository import (
    EmbeddingProgressRepository,
)
from vault_shared.db.repositories.embedding_repository import EmbeddingRepository
from vault_shared.db.repositories.enrichment_event_repository import EnrichmentEventRepository
from vault_shared.db.repositories.enrichment_job_repository import EnrichmentJobRepository
from vault_shared.db.repositories.enrichment_progress_repository import (
    EnrichmentProgressRepository,
)
from vault_shared.db.repositories.execution_audit_repository import ExecutionAuditRepository
from vault_shared.db.repositories.execution_job_repository import ExecutionJobRepository
from vault_shared.db.repositories.execution_plan_repository import ExecutionPlanRepository
from vault_shared.db.repositories.execution_result_repository import ExecutionResultRepository
from vault_shared.db.repositories.execution_step_repository import ExecutionStepRepository
from vault_shared.db.repositories.file_classification_repository import (
    FileClassificationRepository,
)
from vault_shared.db.repositories.file_extraction_repository import FileExtractionRepository
from vault_shared.db.repositories.file_intelligence_repository import (
    FileIntelligenceRepository,
)
from vault_shared.db.repositories.file_lifecycle_repository import FileLifecycleRepository
from vault_shared.db.repositories.file_metadata_repository import FileMetadataRepository
from vault_shared.db.repositories.file_relationship_repository import FileRelationshipRepository
from vault_shared.db.repositories.file_repository import FileRepository
from vault_shared.db.repositories.folder_repository import FolderRepository
from vault_shared.db.repositories.insight_record_repository import InsightRecordRepository
from vault_shared.db.repositories.intelligence_event_repository import (
    IntelligenceEventRepository,
)
from vault_shared.db.repositories.intelligence_job_repository import IntelligenceJobRepository
from vault_shared.db.repositories.intelligence_progress_repository import (
    IntelligenceProgressRepository,
)
from vault_shared.db.repositories.knowledge_attribute_repository import (
    KnowledgeAttributeRepository,
)
from vault_shared.db.repositories.notification_repository import NotificationRepository
from vault_shared.db.repositories.organization_analysis_job_repository import (
    OrganizationAnalysisJobRepository,
)
from vault_shared.db.repositories.organization_entity_repository import (
    FileEntityLinkRepository,
    OrganizationEntityRepository,
)
from vault_shared.db.repositories.organization_memory_repository import (
    OrganizationMemoryRepository,
)
from vault_shared.db.repositories.organization_recommendation_repository import (
    OrganizationRecommendationRepository,
)
from vault_shared.db.repositories.organization_repository import OrganizationRepository
from vault_shared.db.repositories.recommendation_event_repository import (
    RecommendationEventRepository,
)
from vault_shared.db.repositories.recommendation_job_repository import (
    RecommendationJobRepository,
)
from vault_shared.db.repositories.recommendation_repository import RecommendationRepository
from vault_shared.db.repositories.refresh_token_repository import RefreshTokenRepository
from vault_shared.db.repositories.role_repository import RoleRepository
from vault_shared.db.repositories.rollback_record_repository import RollbackRecordRepository
from vault_shared.db.repositories.scan_event_repository import ScanEventRepository
from vault_shared.db.repositories.scan_job_repository import ScanJobRepository
from vault_shared.db.repositories.scan_progress_repository import ScanProgressRepository
from vault_shared.db.repositories.scheduler_job_repository import SchedulerJobRepository
from vault_shared.db.repositories.search_session_repository import SearchSessionRepository
from vault_shared.db.repositories.storage_analysis_event_repository import (
    StorageAnalysisEventRepository,
)
from vault_shared.db.repositories.storage_analysis_job_repository import (
    StorageAnalysisJobRepository,
)
from vault_shared.db.repositories.storage_analysis_snapshot_repository import (
    StorageAnalysisSnapshotRepository,
)
from vault_shared.db.repositories.storage_connector_repository import (
    StorageConnectorRepository,
)
from vault_shared.db.repositories.storage_source_repository import StorageSourceRepository
from vault_shared.db.repositories.user_repository import UserRepository
from vault_shared.db.repositories.workflow_execution_repository import (
    WorkflowExecutionRepository,
)
from vault_shared.db.repositories.workflow_node_execution_repository import (
    WorkflowNodeExecutionRepository,
)
from vault_shared.db.repositories.workflow_node_repository import WorkflowNodeRepository
from vault_shared.db.repositories.workflow_policy_repository import WorkflowPolicyRepository
from vault_shared.db.repositories.workflow_repository import WorkflowRepository
from vault_shared.db.repositories.workflow_trigger_repository import WorkflowTriggerRepository
from vault_shared.db.repositories.workflow_version_repository import WorkflowVersionRepository

__all__ = [
    "AIProviderConfigRepository",
    "ApprovalDecisionRepository",
    "ApprovalRequestRepository",
    "ArchiveJobRepository",
    "AuditLogRepository",
    "AutomationTemplateRepository",
    "CitationRepository",
    "ConnectorCredentialsRepository",
    "ConversationMessageRepository",
    "ConversationRepository",
    "DashboardSnapshotRepository",
    "DuplicateGroupRepository",
    "EmbeddingEventRepository",
    "EmbeddingJobRepository",
    "EmbeddingProgressRepository",
    "EmbeddingRepository",
    "EnrichmentEventRepository",
    "EnrichmentJobRepository",
    "EnrichmentProgressRepository",
    "ExecutionAuditRepository",
    "ExecutionJobRepository",
    "ExecutionPlanRepository",
    "ExecutionResultRepository",
    "ExecutionStepRepository",
    "FileClassificationRepository",
    "FileEntityLinkRepository",
    "FileExtractionRepository",
    "FileIntelligenceRepository",
    "FileLifecycleRepository",
    "FileMetadataRepository",
    "FileRelationshipRepository",
    "FileRepository",
    "FolderRepository",
    "InsightRecordRepository",
    "IntelligenceEventRepository",
    "IntelligenceJobRepository",
    "IntelligenceProgressRepository",
    "KnowledgeAttributeRepository",
    "NotificationRepository",
    "OrganizationAnalysisJobRepository",
    "OrganizationEntityRepository",
    "OrganizationMemoryRepository",
    "OrganizationRecommendationRepository",
    "OrganizationRepository",
    "RecommendationEventRepository",
    "RecommendationJobRepository",
    "RecommendationRepository",
    "RefreshTokenRepository",
    "RoleRepository",
    "RollbackRecordRepository",
    "ScanEventRepository",
    "ScanJobRepository",
    "ScanProgressRepository",
    "SchedulerJobRepository",
    "SearchSessionRepository",
    "StorageAnalysisEventRepository",
    "StorageAnalysisJobRepository",
    "StorageAnalysisSnapshotRepository",
    "StorageConnectorRepository",
    "StorageSourceRepository",
    "UserRepository",
    "WorkflowExecutionRepository",
    "WorkflowNodeExecutionRepository",
    "WorkflowNodeRepository",
    "WorkflowPolicyRepository",
    "WorkflowRepository",
    "WorkflowTriggerRepository",
    "WorkflowVersionRepository",
]
