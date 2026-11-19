import type {
  Action,
  CreateExecutionPlanRequest,
  ExecutionPlan,
  OrganizationRecommendation,
} from "@vault/types";

import { toast } from "@/components/ui/toaster";

import { ApiError, apiClient } from "./api-client";
import { queryClient } from "./query-client";

const POLL_INTERVAL_MS = 1500;
const MAX_WAIT_MS = 10 * 60 * 1000;

export type OutcomeTone = "success" | "warning" | "error" | "info";

export interface ActionOutcome {
  tone: OutcomeTone;
  title: string;
  description: string | undefined;
}

export function describeOutcome(action: Action): ActionOutcome {
  const problems = action.problems.map((problem) => `${problem.file_name}: ${problem.reason}`);
  const description = problems.length > 0 ? problems.slice(0, 3).join("\n") : undefined;
  switch (action.status) {
    case "done":
      return { tone: "success", title: action.message, description };
    case "partial":
      return { tone: "warning", title: action.message, description };
    case "failed":
      return { tone: "error", title: action.message, description };
    default:
      return { tone: "info", title: action.message, description };
  }
}

function showOutcome(toastId: string | number, action: Action): void {
  const outcome = describeOutcome(action);
  const options = {
    id: toastId,
    description: outcome.description,
    duration: 8000,
    action: action.can_undo
      ? { label: "Undo", onClick: () => void undoAction(action.id) }
      : undefined,
  };
  if (outcome.tone === "success") toast.success(outcome.title, options);
  else if (outcome.tone === "warning") toast.warning(outcome.title, options);
  else if (outcome.tone === "error") toast.error(outcome.title, options);
  else toast.info(outcome.title, options);
}

/** Follows one storage operation until the provider has confirmed it, keeping a
 * single toast up to date — "Renaming 3 files…" → "3 files renamed in Google
 * Drive — confirmed". */
export async function trackAction(actionId: string, progress: string): Promise<Action | null> {
  const toastId = toast.loading(progress);
  const deadline = Date.now() + MAX_WAIT_MS;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    let action: Action;
    try {
      action = await apiClient.get<Action>(`/v1/actions/${actionId}`);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        toast.error("Lost track of this change — check Recent activity on the dashboard.", {
          id: toastId,
        });
        return null;
      }
      continue;
    }
    if (action.status === "in_progress") {
      toast.loading(action.message, { id: toastId });
      continue;
    }
    showOutcome(toastId, action);
    void queryClient.invalidateQueries();
    return action;
  }
  toast.info("Still working in the background — check Recent Activity on the dashboard.", {
    id: toastId,
  });
  return null;
}

export async function runStorageAction(
  request: CreateExecutionPlanRequest,
  progress: string,
): Promise<Action | null> {
  let plan: ExecutionPlan;
  try {
    plan = await apiClient.post<ExecutionPlan>("/v1/execution-plans", request);
  } catch (error) {
    toast.error(error instanceof ApiError ? error.message : "Couldn't start that — try again.");
    return null;
  }
  return trackAction(plan.id, progress);
}

/** Applying an organization recommendation first creates the destination folders,
 * then moves the files; the move is an ordinary action once it exists. */
export async function trackRecommendationApply(
  recommendationId: string,
  fileCount: number,
  kind = "group",
): Promise<Action | null> {
  const isRename = kind === "rename_file";
  const toastId = toast.loading(isRename ? "Renaming in Google Drive…" : "Creating folders in Google Drive…");
  const deadline = Date.now() + 2 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    let recommendation: OrganizationRecommendation;
    try {
      recommendation = await apiClient.get<OrganizationRecommendation>(
        `/v1/organization-recommendations/${recommendationId}`,
      );
    } catch {
      continue;
    }
    if (recommendation.execution_plan_id) {
      toast.dismiss(toastId);
      return trackAction(
        recommendation.execution_plan_id,
        isRename ? "Renaming…" : `Organizing ${pluralFiles(fileCount)}…`,
      );
    }
  }
  toast.error("Couldn't start organizing — nothing was moved. Try again.", { id: toastId });
  return null;
}

export async function undoAction(actionId: string): Promise<void> {
  try {
    await apiClient.post<Action>(`/v1/actions/${actionId}/undo`, {});
  } catch (error) {
    toast.error(error instanceof ApiError ? error.message : "Couldn't undo that.");
    return;
  }
  await trackAction(actionId, "Undoing…");
}

export function pluralFiles(count: number): string {
  return count === 1 ? "1 file" : `${count} files`;
}
