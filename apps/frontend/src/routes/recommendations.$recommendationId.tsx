import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { ExecutionPlan, Recommendation } from "@vault/types";
import { ChevronLeft, ShieldCheck, Sparkles } from "lucide-react";

import { AppShell } from "@/components/app-shell/app-shell";
import { toast } from "@/components/ui/toaster";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, apiClient } from "@/lib/api-client";
import { trackAction } from "@/lib/storage-action";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { categoryBadgeVariant, categoryLabel, riskBadgeVariant } from "@/lib/recommendation-style";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/recommendations/$recommendationId")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: RecommendationDetailPage,
});

const MAX_FILES_SHOWN = 20;

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right font-medium">{value}</span>
    </div>
  );
}

function RecommendationDetailPage() {
  const { recommendationId } = Route.useParams();
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";

  const recommendationQuery = useQuery({
    queryKey: ["recommendations", recommendationId],
    queryFn: () => apiClient.get<Recommendation>(`/v1/recommendations/${recommendationId}`),
  });

  const createPlanMutation = useMutation({
    mutationFn: () =>
      apiClient.post<ExecutionPlan>("/v1/execution-plans", {
        recommendation_id: recommendationId,
      }),
    onSuccess: (plan) => {
      void trackAction(plan.id, "Working on it…");
    },
    onError: (error) => {
      toast.error(
        error instanceof ApiError
          ? error.message
          : "Couldn't start this — nothing was changed.",
      );
    },
  });

  const recommendation = recommendationQuery.data;
  const shownFiles = recommendation?.affected_file_ids.slice(0, MAX_FILES_SHOWN) ?? [];
  const remainingCount = recommendation
    ? recommendation.affected_file_ids.length - shownFiles.length
    : 0;

  return (
    <AppShell title="Recommendation">
      <div className="mx-auto flex max-w-3xl flex-col gap-4">
        <Link
          to="/recommendations"
          className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ChevronLeft className="size-4" /> Back to recommendations
        </Link>

        {recommendationQuery.isLoading && <Skeleton className="h-64 rounded-2xl" />}
        {recommendationQuery.isError && (
          <p className="text-sm text-destructive">
            Couldn&rsquo;t load this recommendation — you may not have permission to view it.
          </p>
        )}

        {recommendation && (
          <>
            <Card clay className="p-6">
              <div className="mb-2 flex items-center gap-2">
                <Badge variant={categoryBadgeVariant(recommendation.category)}>
                  {categoryLabel(recommendation.category)}
                </Badge>
                <Badge variant={riskBadgeVariant(recommendation.risk_level)}>
                  {recommendation.risk_level} risk
                </Badge>
                {recommendation.status === "resolved" && <Badge variant="success">Resolved</Badge>}
              </div>
              <h1 className="text-lg font-semibold">{recommendation.title}</h1>
              <p className="mt-1 text-sm text-muted-foreground">{recommendation.description}</p>
            </Card>

            <div className="grid gap-4 sm:grid-cols-2">
              <Card>
                <CardContent className="divide-y divide-border pt-5">
                  <Field label="Confidence" value={`${Math.round(recommendation.confidence * 100)}%`} />
                  <Field label="Estimated impact" value={recommendation.estimated_impact} />
                  <Field label="Priority score" value={recommendation.priority_score.toFixed(1)} />
                  <Field
                    label="Departments"
                    value={
                      recommendation.related_departments.length > 0
                        ? recommendation.related_departments.join(", ")
                        : "—"
                    }
                  />
                  <Field label="Generated" value={formatRelativeTime(recommendation.created_at)} />
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <ShieldCheck className="size-4 text-primary" /> Suggested action
                  </CardTitle>
                </CardHeader>
                <CardContent className="flex flex-col gap-3">
                  <p className="text-sm">{recommendation.suggested_action}</p>
                  <p className="rounded-lg bg-secondary p-2.5 text-xs text-muted-foreground">
                    &ldquo;Do it&rdquo; makes the change in your storage right away, then checks
                    with the provider that it really happened. You can undo it afterwards.
                  </p>

                  {recommendation.status === "active" &&
                    (canManage ? (
                      <Button
                        size="sm"
                        className="w-fit"
                        disabled={createPlanMutation.isPending}
                        onClick={() => createPlanMutation.mutate()}
                      >
                        <Sparkles className="size-4" />
                        {createPlanMutation.isPending ? "Starting…" : "Do it"}
                      </Button>
                    ) : (
                      <p className="text-xs text-muted-foreground">
                        Only owners and admins can make changes.
                      </p>
                    ))}
                </CardContent>
              </Card>
            </div>

            <Card>
              <CardHeader>
                <CardTitle>
                  Affected files ({recommendation.affected_file_ids.length.toLocaleString()})
                </CardTitle>
              </CardHeader>
              <CardContent>
                {shownFiles.length === 0 ? (
                  <p className="text-sm text-muted-foreground">
                    No specific files are attached to this recommendation.
                  </p>
                ) : (
                  <ul className="flex flex-col gap-1">
                    {shownFiles.map((fileId) => (
                      <li key={fileId}>
                        <Link
                          to="/files/$fileId"
                          params={{ fileId }}
                          className="text-sm text-primary hover:underline"
                        >
                          {fileId}
                        </Link>
                      </li>
                    ))}
                  </ul>
                )}
                {remainingCount > 0 && (
                  <p className="mt-2 text-sm text-muted-foreground">and {remainingCount} more files.</p>
                )}
              </CardContent>
            </Card>
          </>
        )}
      </div>
    </AppShell>
  );
}
