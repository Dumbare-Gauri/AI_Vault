from fastapi import Depends
from sqlalchemy.orm import Session

from app.application.action_service import ActionService
from app.application.ai_provider_config_service import AIProviderConfigService
from app.application.approval_service import ApprovalService
from app.application.archive_service import ArchiveService
from app.application.auth_service import AuthService
from app.application.automation_template_service import AutomationTemplateService
from app.application.connector_service import ConnectorService
from app.application.context_builder_service import ContextBuilderService
from app.application.conversation_service import ConversationService
from app.application.dashboard_service import DashboardService
from app.application.embedding_job_service import EmbeddingJobService
from app.application.enrichment_job_service import EnrichmentJobService
from app.application.execution_job_service import ExecutionJobService
from app.application.execution_plan_service import ExecutionPlanService
from app.application.file_service import FileService
from app.application.intelligence_job_service import IntelligenceJobService
from app.application.notification_service import NotificationService
from app.application.organization_entity_service import OrganizationEntityService
from app.application.organization_recommendation_service import OrganizationRecommendationService
from app.application.organization_service import OrganizationService
from app.application.recommendation_service import RecommendationService
from app.application.scan_service import ScanService
from app.application.search_service import SearchService
from app.application.storage_intelligence_service import StorageIntelligenceService
from app.application.storage_operation_service import StorageOperationService
from app.application.workflow_execution_service import WorkflowExecutionService
from app.application.workflow_policy_service import WorkflowPolicyService
from app.application.workflow_service import WorkflowService
from app.application.workflow_trigger_service import WorkflowTriggerService
from vault_shared.ai_gateway import AIGateway, get_ai_gateway
from vault_shared.connectors.google_workspace import (
    GoogleWorkspaceOAuthClient,
    get_google_workspace_oauth_client,
)
from vault_shared.db.session import get_db
from vault_shared.storage import StorageAdapterRegistry
from vault_shared.storage.default_registry import build_storage_registry


def get_auth_service(db: Session = Depends(get_db)) -> AuthService:
    return AuthService(db)


def get_organization_service(db: Session = Depends(get_db)) -> OrganizationService:
    return OrganizationService(db)


def get_ai_provider_config_service(db: Session = Depends(get_db)) -> AIProviderConfigService:
    return AIProviderConfigService(db)


def get_connector_service(
    db: Session = Depends(get_db),
    oauth_client: GoogleWorkspaceOAuthClient = Depends(get_google_workspace_oauth_client),
) -> ConnectorService:
    return ConnectorService(db, oauth_client=oauth_client)


def get_scan_service(db: Session = Depends(get_db)) -> ScanService:
    return ScanService(db)


def get_enrichment_job_service(db: Session = Depends(get_db)) -> EnrichmentJobService:
    return EnrichmentJobService(db)


def get_intelligence_job_service(db: Session = Depends(get_db)) -> IntelligenceJobService:
    return IntelligenceJobService(db)


def get_storage_registry(
    db: Session = Depends(get_db),
    oauth_client: GoogleWorkspaceOAuthClient = Depends(get_google_workspace_oauth_client),
) -> StorageAdapterRegistry:
    """Per-request, bound to the request's session — deliberately not a
    singleton, so a test overrides this one dependency with a fake."""
    return build_storage_registry(db, oauth_client=oauth_client)


def get_file_service(
    db: Session = Depends(get_db),
    storage: StorageAdapterRegistry = Depends(get_storage_registry),
) -> FileService:
    return FileService(db, storage=storage)


def get_embedding_job_service(db: Session = Depends(get_db)) -> EmbeddingJobService:
    return EmbeddingJobService(db)


def get_search_service(
    db: Session = Depends(get_db), ai_gateway: AIGateway = Depends(get_ai_gateway)
) -> SearchService:
    return SearchService(db, ai_gateway=ai_gateway)


def get_context_builder_service(db: Session = Depends(get_db)) -> ContextBuilderService:
    return ContextBuilderService(db)


def get_conversation_service(
    db: Session = Depends(get_db), ai_gateway: AIGateway = Depends(get_ai_gateway)
) -> ConversationService:
    return ConversationService(db, ai_gateway=ai_gateway)


def get_dashboard_service(db: Session = Depends(get_db)) -> DashboardService:
    return DashboardService(db)


def get_recommendation_service(db: Session = Depends(get_db)) -> RecommendationService:
    return RecommendationService(db)


def get_storage_operation_service(db: Session = Depends(get_db)) -> StorageOperationService:
    return StorageOperationService(db)


def get_organization_entity_service(
    db: Session = Depends(get_db),
) -> OrganizationEntityService:
    return OrganizationEntityService(db)


def get_organization_recommendation_service(
    db: Session = Depends(get_db),
) -> OrganizationRecommendationService:
    return OrganizationRecommendationService(db)


def get_storage_intelligence_service(
    db: Session = Depends(get_db),
) -> StorageIntelligenceService:
    return StorageIntelligenceService(db)


def get_execution_plan_service(db: Session = Depends(get_db)) -> ExecutionPlanService:
    return ExecutionPlanService(db)


def get_archive_service(db: Session = Depends(get_db)) -> ArchiveService:
    return ArchiveService(db)


def get_approval_service(db: Session = Depends(get_db)) -> ApprovalService:
    return ApprovalService(db)


def get_execution_job_service(db: Session = Depends(get_db)) -> ExecutionJobService:
    return ExecutionJobService(db)


def get_action_service(
    db: Session = Depends(get_db),
    job_service: ExecutionJobService = Depends(get_execution_job_service),
) -> ActionService:
    return ActionService(db, job_service=job_service)


def get_workflow_service(db: Session = Depends(get_db)) -> WorkflowService:
    return WorkflowService(db)


def get_workflow_trigger_service(db: Session = Depends(get_db)) -> WorkflowTriggerService:
    return WorkflowTriggerService(db)


def get_workflow_policy_service(db: Session = Depends(get_db)) -> WorkflowPolicyService:
    return WorkflowPolicyService(db)


def get_workflow_execution_service(db: Session = Depends(get_db)) -> WorkflowExecutionService:
    return WorkflowExecutionService(db)


def get_notification_service(db: Session = Depends(get_db)) -> NotificationService:
    return NotificationService(db)


def get_automation_template_service(db: Session = Depends(get_db)) -> AutomationTemplateService:
    return AutomationTemplateService(db)
