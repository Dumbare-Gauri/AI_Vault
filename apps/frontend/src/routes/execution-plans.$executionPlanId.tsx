import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { ExecutionJob, ExecutionPlanDetail } from "@vault/types";
import { ChevronLeft, RotateCcw, ShieldAlert } from "lucide-react";

import { AppShell } from "@/components/app-shell/app-shell";
import { toast } from "@/components/ui/toaster";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, apiClient } from "@/lib/api-client";
import { actionTypeLabel, planStatusBadgeVariant, riskLevelBadgeVariant } from "@/lib/execution-style";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/execution-plans/$executionPlanId")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: ExecutionPlanDetailPage,
});

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right font-medium">{value}</span>
    </div>
  );
}

function ExecutionPlanDetailPage() {
  const { executionPlanId } = Route.useParams();
  const queryClient = useQueryClient();
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";

  const planQuery = useQuery({
    queryKey: ["execution-plans", executionPlanId],
    queryFn: () => apiClient.get<ExecutionPlanDetail>(`/v1/execution-plans/${executionPlanId}`),
  });

  const rollbackMutation = useMutation({
    mutationFn: () =>
      apiClient.post<ExecutionJob>(`/v1/execution-plans/${executionPlanId}/rollback`),
    onSuccess: (job) => {
      void queryClient.invalidateQueries({ queryKey: ["execution-plans", executionPlanId] });
      void queryClient.invalidateQueries({ queryKey: ["execution-jobs"] });
      toast.success("Rollback job started", {
        action: {
          label: "View progress",
          onClick: () => {
            window.location.href = `/execution-jobs/${job.id}`;
          },
        },
      });
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't start rollback.");
    },
  });

  const plan = planQuery.data;
  const isPermanentDelete = plan?.steps.some((step) => step.action_type === "permanent_delete");

  return (
    <AppShell title="Execution plan">
      <div className="mx-auto flex max-w-3xl flex-col gap-4">
        <Link
          to="/execution-plans"
          className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ChevronLeft className="size-4" /> Back to Execution Center
        </Link>

        {planQuery.isLoading && <Skeleton className="h-64 rounded-2xl" />}
        {planQuery.isError && (
          <p className="text-sm text-destructive">
            Couldn&rsquo;t load this execution plan — you may not have permission to view it.
          </p>
        )}

        {plan && (
          <>
            {isPermanentDelete && (
              <Card className="flex items-start gap-3 border-destructive/40 bg-destructive/10 p-4">
                <ShieldAlert className="mt-0.5 size-5 shrink-0 text-destructive" />
                <div>
                  <p className="text-sm font-semibold text-destructive">
                    This plan permanently deletes files from Google Drive
                  </p>
                  <p className="text-sm text-muted-foreground">
                    Not a Trash action — this ran immediately once confirmed and cannot be undone.
                  </p>
                </div>
              </Card>
            )}

            <Card clay className="p-6">
              <div className="mb-2 flex items-center gap-2">
                <Badge variant={planStatusBadgeVariant(plan.status)} className="capitalize">
                  {plan.status.replace(/_/g, " ")}
                </Badge>
                <Badge variant={riskLevelBadgeVariant(plan.risk_level)}>{plan.risk_level} risk</Badge>
              </div>
              <h1 className="text-lg font-semibold">Execution plan</h1>
              <p className="text-sm text-muted-foreground">{plan.estimated_impact}</p>
            </Card>

            <div className="grid gap-4 sm:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle>Risk summary</CardTitle>
                </CardHeader>
                <CardContent className="divide-y divide-border">
                  <Field label="Steps" value={plan.steps.length} />
                  <Field
                    label="Estimated storage savings"
                    value={
                      plan.estimated_storage_savings_bytes !== null
                        ? formatBytes(plan.estimated_storage_savings_bytes)
                        : "—"
                    }
                  />
                  <Field label="Rollback" value={plan.rollback_available ? "Available" : "Not reversible"} />
                  <Field label="Target provider" value={plan.target_provider} />
                  <Field label="Required permissions" value={plan.required_permissions.join(", ")} />
                  <Field label="Created" value={formatRelativeTime(plan.created_at)} />
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Rollback</CardTitle>
                </CardHeader>
                <CardContent>
                  {!plan.rollback_available && (
                    <p className="text-sm text-muted-foreground">
                      This plan cannot be rolled back.
                    </p>
                  )}

                  {canManage && plan.rollback_available && (
                    <div className="mt-4 flex flex-col gap-2 border-t border-border pt-4">
                      <p className="flex items-start gap-2 text-xs text-muted-foreground">
                        <ShieldAlert className="mt-0.5 size-3.5 shrink-0" />
                        Reverses every executed step that hasn&rsquo;t already been rolled back —
                        moved files move back, renamed files restore, archived files un-trash.
                      </p>
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-fit"
                        disabled={rollbackMutation.isPending}
                        onClick={() => rollbackMutation.mutate()}
                      >
                        <RotateCcw className="size-4" />
                        {rollbackMutation.isPending ? "Starting rollback…" : "Start rollback"}
                      </Button>
                    </div>
                  )}
                </CardContent>
              </Card>
            </div>

            <Card>
              <CardHeader>
                <CardTitle>Steps ({plan.steps.length})</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col divide-y divide-border">
                {plan.steps.map((step) => (
                  <div key={step.id} className="flex items-center justify-between gap-3 py-2.5 text-sm">
                    <div>
                      <p className="font-medium">{actionTypeLabel(step.action_type)}</p>
                      <Link
                        to="/files/$fileId"
                        params={{ fileId: step.target_file_id }}
                        className="text-xs text-primary hover:underline"
                      >
                        {step.target_file_id}
                      </Link>
                    </div>
                    <Badge variant="outline" className="shrink-0 capitalize">
                      {step.status}
                    </Badge>
                  </div>
                ))}
              </CardContent>
            </Card>
          </>
        )}
      </div>
    </AppShell>
  );
}
