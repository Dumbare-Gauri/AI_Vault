import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { DuplicateGroupDetail, ExecutionPlan } from "@vault/types";
import { CheckCircle2, ChevronLeft, Sparkles, Trash2 } from "lucide-react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { trackAction } from "@/lib/storage-action";
import { fileTypeIconElement } from "@/lib/file-icon";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { confidenceLabel } from "@/lib/storage-intelligence-style";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/storage-intelligence/duplicates/$groupId")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: DuplicateGroupDetailPage,
});

function DuplicateGroupDetailPage() {
  const { groupId } = Route.useParams();
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";

  const groupQuery = useQuery({
    queryKey: ["storage-intelligence", "duplicates", groupId],
    queryFn: () => apiClient.get<DuplicateGroupDetail>(`/v1/storage/duplicates/${groupId}`),
  });

  const removeDuplicatesMutation = useMutation({
    mutationFn: () =>
      apiClient.post<ExecutionPlan>("/v1/execution-plans", { duplicate_group_id: groupId }),
    onSuccess: (plan) => {
      void trackAction(plan.id, "Cleaning up duplicates…");
    },
    onError: (error) => {
      toast.error(
        error instanceof ApiError
          ? error.message
          : "Couldn't start — nothing was changed.",
      );
    },
  });

  const group = groupQuery.data;

  return (
    <AppShell title="Duplicate group">
      <div className="mx-auto flex max-w-3xl flex-col gap-4">
        <Link
          to="/storage-intelligence/duplicates"
          className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ChevronLeft className="size-4" /> Back to duplicate files
        </Link>

        {groupQuery.isLoading && (
          <div className="flex flex-col gap-4">
            <Skeleton className="h-24 rounded-2xl" />
            <Skeleton className="h-64 rounded-2xl" />
          </div>
        )}
        {groupQuery.isError && (
          <EmptyState title="Couldn't load this duplicate group" description="Please try again." />
        )}

        {group && (
          <>
            <Card clay className="flex flex-col gap-4 p-6 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h1 className="text-lg font-semibold">{group.file_count} identical copies</h1>
                <p className="text-sm text-muted-foreground">
                  {formatBytes(group.total_size_bytes)} total &middot;{" "}
                  <span className="font-medium text-primary">
                    {formatBytes(group.recoverable_size_bytes)} potentially recoverable
                  </span>
                </p>
              </div>
              {canManage && (
                <Button
                  variant="destructive"
                  className="shrink-0"
                  disabled={removeDuplicatesMutation.isPending}
                  onClick={() => removeDuplicatesMutation.mutate()}
                >
                  <Trash2 className="size-4" />
                  {removeDuplicatesMutation.isPending
                    ? "Creating plan…"
                    : "Remove duplicate copies"}
                </Button>
              )}
            </Card>
            <p className="-mt-2 text-xs text-muted-foreground">
              Moves the non-kept copies to Google Drive&rsquo;s Trash immediately — never
              a permanent delete, and reversible from Trash or Vault&rsquo;s rollback.
            </p>

            {group.recommended_keep_reason && (
              <Card className="flex items-start gap-3 bg-ai-muted p-4">
                <Sparkles className="mt-0.5 size-4 shrink-0 text-ai" />
                <div className="text-sm">
                  <p className="font-medium text-ai">Recommended: keep one copy</p>
                  <p className="mt-1 text-muted-foreground">{group.recommended_keep_reason}</p>
                  {group.recommended_keep_confidence != null && (
                    <Badge variant="outline" className="mt-2">
                      {confidenceLabel(group.recommended_keep_confidence)}
                    </Badge>
                  )}
                  <p className="mt-2 text-xs text-muted-foreground">
                    Advisory only — no file has been moved, renamed, or deleted.
                  </p>
                </div>
              </Card>
            )}

            <Card>
              <CardHeader>
                <CardTitle>All copies</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col divide-y divide-border p-0">
                {group.members.map((member) => (
                  <Link
                    key={member.file.id}
                    to="/files/$fileId"
                    params={{ fileId: member.file.id }}
                    className="flex items-center justify-between gap-3 px-4 py-3 text-sm hover:bg-secondary/60"
                  >
                    <div className="flex min-w-0 items-center gap-3">
                      <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-secondary text-muted-foreground">
                        {fileTypeIconElement(member.file.mime_type, { className: "size-4" })}
                      </span>
                      <div className="min-w-0">
                        <p className="truncate font-medium">{member.file.name}</p>
                        <p className="truncate text-xs text-muted-foreground">{member.file.path}</p>
                      </div>
                      {member.is_recommended_keep && (
                        <Badge variant="success" className="shrink-0">
                          <CheckCircle2 className="size-3" /> Keep
                        </Badge>
                      )}
                    </div>
                    <div className="shrink-0 text-right text-xs text-muted-foreground">
                      <p>{formatBytes(member.file.size_bytes ?? 0)}</p>
                      {member.file.provider_modified_at && (
                        <p>{formatRelativeTime(member.file.provider_modified_at)}</p>
                      )}
                    </div>
                  </Link>
                ))}
              </CardContent>
            </Card>
          </>
        )}
      </div>
    </AppShell>
  );
}
