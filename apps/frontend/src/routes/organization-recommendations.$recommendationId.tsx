import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { OrganizationRecommendation } from "@vault/types";
import { ChevronLeft, FolderTree } from "lucide-react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { trackRecommendationApply } from "@/lib/storage-action";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { recommendationKindLabel } from "@/lib/organization-style";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/organization-recommendations/$recommendationId")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: OrganizationRecommendationDetailPage,
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

function OrganizationRecommendationDetailPage() {
  const { recommendationId } = Route.useParams();
  const queryClient = useQueryClient();
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";

  const recommendationQuery = useQuery({
    queryKey: ["organization-recommendations", recommendationId],
    queryFn: () =>
      apiClient.get<OrganizationRecommendation>(
        `/v1/organization-recommendations/${recommendationId}`,
      ),
  });

  const applyMutation = useMutation({
    mutationFn: () =>
      apiClient.post<OrganizationRecommendation>(
        `/v1/organization-recommendations/${recommendationId}/apply`,
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["organization-recommendations"] });
      void trackRecommendationApply(
        recommendationId,
        recommendationQuery.data?.affected_file_ids.length ?? 0,
        recommendationQuery.data?.kind,
      );
    },
    onError: (error) => {
      toast.error(
        error instanceof ApiError
          ? error.message
          : "Couldn't apply this organization recommendation.",
      );
    },
  });

  const recommendation = recommendationQuery.data;
  const shownFiles = recommendation?.affected_file_ids.slice(0, MAX_FILES_SHOWN) ?? [];
  const remainingCount = recommendation
    ? recommendation.affected_file_ids.length - shownFiles.length
    : 0;

  return (
    <AppShell title="Organization Recommendation">
      <div className="mx-auto flex max-w-3xl flex-col gap-4">
        <Link
          to="/recommendations"
          className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ChevronLeft className="size-4" /> Back to organization recommendations
        </Link>

        {recommendationQuery.isLoading && <Skeleton className="h-64 rounded-2xl" />}
        {recommendationQuery.isError && (
          <p className="text-sm text-destructive">Couldn&rsquo;t load this recommendation.</p>
        )}

        {recommendation && (
          <>
            <Card clay className="p-6">
              <div className="mb-2 flex items-center gap-2">
                <Badge variant="ai">{recommendationKindLabel(recommendation.kind)}</Badge>
                {recommendation.status !== "active" && (
                  <Badge variant={recommendation.status === "applied" ? "success" : "default"}>
                    {recommendation.status}
                  </Badge>
                )}
              </div>
              <h1 className="text-lg font-semibold">{recommendation.title}</h1>
              <p className="mt-1 text-sm text-muted-foreground">
                {recommendation.reasoning_summary}
              </p>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Why?</CardTitle>
              </CardHeader>
              <CardContent>
                {recommendation.evidence.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No evidence recorded.</p>
                ) : (
                  <ul className="flex flex-col gap-1.5 text-sm">
                    {recommendation.evidence.map((item, index) => (
                      <li key={index} className="flex items-start gap-2">
                        <span className="mt-1.5 size-1 shrink-0 rounded-full bg-muted-foreground" />
                        <span>{item.description}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>

            <div className="grid gap-4 sm:grid-cols-2">
              <Card>
                <CardContent className="divide-y divide-border pt-5">
                  <Field label="Confidence" value={`${Math.round(recommendation.confidence * 100)}%`} />
                  <Field
                    label="Files affected"
                    value={recommendation.affected_file_ids.length.toLocaleString()}
                  />
                  <Field
                    label="Current folders"
                    value={recommendation.current_locations.length}
                  />
                  {recommendation.estimated_storage_impact_bytes !== null && (
                    <Field
                      label="Total size"
                      value={formatBytes(recommendation.estimated_storage_impact_bytes)}
                    />
                  )}
                  <Field label="Generated" value={formatRelativeTime(recommendation.created_at)} />
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <FolderTree className="size-4 text-primary" />{" "}
                    {recommendation.kind === "rename_file" ? "Suggested name" : "Suggested destination"}
                  </CardTitle>
                </CardHeader>
                <CardContent className="flex flex-col gap-3">
                  <p className="text-sm font-medium">
                    {recommendation.suggested_destination.join(" / ")}
                  </p>
                  <p className="rounded-lg bg-secondary p-2.5 text-xs text-muted-foreground">
                    {recommendation.kind === "rename_file"
                      ? "“Do it” renames the file in your storage and confirms the new name with the provider. You can undo it afterwards."
                      : "“Do it” creates this folder in your storage if it doesn’t exist yet, moves every file listed here into it, and confirms each move with the provider. You can undo it afterwards."}
                  </p>

                  {recommendation.status === "active" &&
                    (canManage ? (
                      <Button
                        size="sm"
                        className="w-fit"
                        disabled={applyMutation.isPending}
                        onClick={() => applyMutation.mutate()}
                      >
                        {applyMutation.isPending ? "Starting…" : "Do it"}
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
                <CardTitle>Current locations</CardTitle>
              </CardHeader>
              <CardContent>
                {recommendation.current_locations.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No locations recorded.</p>
                ) : (
                  <ul className="flex flex-col divide-y divide-border">
                    {recommendation.current_locations.map((location) => (
                      <li
                        key={location.folder_id}
                        className="flex items-center justify-between gap-3 py-2.5 text-sm"
                      >
                        <span className="min-w-0 truncate">{location.path ?? "(unresolved path)"}</span>
                        <span className="shrink-0 text-muted-foreground">
                          {location.file_count} {location.file_count === 1 ? "file" : "files"}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>
                  Affected files ({recommendation.affected_file_ids.length.toLocaleString()})
                </CardTitle>
              </CardHeader>
              <CardContent>
                {shownFiles.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No files attached.</p>
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
