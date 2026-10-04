from fastapi import APIRouter

from app.presentation.api.v1.ai import ai_router
from app.presentation.api.v1.approvals import approvals_router
from app.presentation.api.v1.archives import archives_router
from app.presentation.api.v1.auth import auth_router
from app.presentation.api.v1.automation_templates import automation_templates_router
from app.presentation.api.v1.connectors import connectors_router
from app.presentation.api.v1.conversations import conversations_router
from app.presentation.api.v1.dashboard import dashboard_router
from app.presentation.api.v1.embedding import embedding_router
from app.presentation.api.v1.enrichment import enrichment_router
from app.presentation.api.v1.execution_jobs import execution_jobs_router
from app.presentation.api.v1.execution_plans import execution_plans_router
from app.presentation.api.v1.files import files_router
from app.presentation.api.v1.intelligence import intelligence_router
from app.presentation.api.v1.notifications import notifications_router
from app.presentation.api.v1.organizations import organizations_router
from app.presentation.api.v1.recommendations import recommendations_router
from app.presentation.api.v1.scans import scans_router
from app.presentation.api.v1.search import search_router
from app.presentation.api.v1.storage import storage_router
from app.presentation.api.v1.users import users_router
from app.presentation.api.v1.version import version_router
from app.presentation.api.v1.workflow_executions import workflow_executions_router
from app.presentation.api.v1.workflow_policies import workflow_policies_router
from app.presentation.api.v1.workflows import workflows_router

v1_router = APIRouter()
v1_router.include_router(version_router)
v1_router.include_router(auth_router)
v1_router.include_router(users_router)
v1_router.include_router(organizations_router)
v1_router.include_router(ai_router)
v1_router.include_router(connectors_router)
v1_router.include_router(scans_router)
v1_router.include_router(enrichment_router)
v1_router.include_router(files_router)
v1_router.include_router(embedding_router)
v1_router.include_router(intelligence_router)
v1_router.include_router(search_router)
v1_router.include_router(conversations_router)
v1_router.include_router(dashboard_router)
v1_router.include_router(recommendations_router)
v1_router.include_router(execution_plans_router)
v1_router.include_router(approvals_router)
v1_router.include_router(execution_jobs_router)
v1_router.include_router(workflows_router)
v1_router.include_router(workflow_executions_router)
v1_router.include_router(workflow_policies_router)
v1_router.include_router(notifications_router)
v1_router.include_router(automation_templates_router)
v1_router.include_router(storage_router)
v1_router.include_router(archives_router)
