import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type {
  DuplicateGroupListResponse,
  EntityType,
  OrganizationEntityListResponse,
  OrganizationRecommendationListResponse,
  StorageFileListResponse,
  StorageOverview,
  StorageStatistics,
} from "@vault/types";
import {
  AlertTriangle,
  Clock,
  Copy,
  Eye,
  FileWarning,
  FolderTree,
  HardDrive,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { entityTypeLabel } from "@/lib/organization-style";
import { analysisStatusBadgeVariant, analysisStatusLabel } from "@/lib/storage-intelligence-style";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/storage-intelligence/")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: StorageIntelligencePage,
});

function StatCard({
  icon: Icon,
  label,
  value,
  sub,
}: {
  icon: LucideIcon;
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <Card clay className="flex flex-col gap-3 p-5">
      <div className="flex size-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
        <Icon className="size-[18px]" aria-hidden="true" />
      </div>
      <div>
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className="mt-0.5 text-2xl font-semibold tracking-tight">{value}</p>
        {sub ? <p className="mt-0.5 text-xs text-muted-foreground">{sub}</p> : null}
      </div>
    </Card>
  );
}

function StorageIntelligencePage() {
  const queryClient = useQueryClient();
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";

  const overviewQuery = useQuery({
    queryKey: ["storage-intelligence", "overview"],
    queryFn: () => apiClient.get<StorageOverview>("/v1/storage/overview"),
  });
  const statisticsQuery = useQuery({
    queryKey: ["storage-intelligence", "statistics"],
    queryFn: () => apiClient.get<StorageStatistics>("/v1/storage/statistics"),
  });
  const duplicatesQuery = useQuery({
    queryKey: ["storage-intelligence", "duplicates", "preview"],
    queryFn: () =>
      apiClient.get<DuplicateGroupListResponse>("/v1/storage/duplicates?limit=5&offset=0"),
  });
  const largeFilesQuery = useQuery({
    queryKey: ["storage-intelligence", "large-files", "preview"],
    queryFn: () =>
      apiClient.get<StorageFileListResponse>("/v1/storage/large-files?limit=5&offset=0"),
  });

  const analyzeMutation = useMutation({
    mutationFn: () => apiClient.post("/v1/storage/analyze"),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["storage-intelligence"] });
    },
  });

  const entitiesQuery = useQuery({
    queryKey: ["organization", "entities"],
    queryFn: () => apiClient.get<OrganizationEntityListResponse>("/v1/organization/entities"),
  });
  const organizationRecommendationsQuery = useQuery({
    queryKey: ["organization-recommendations", "active", "preview"],
    queryFn: () =>
      apiClient.get<OrganizationRecommendationListResponse>(
        "/v1/organization-recommendations?status=active",
      ),
  });
  const analyzeOrganizationMutation = useMutation({
    mutationFn: () => apiClient.post("/v1/organization/analyze"),
    onSuccess: () => {
      toast.success("Analyzing your organization — this can take a few minutes.");
      void queryClient.invalidateQueries({ queryKey: ["organization"] });
      void queryClient.invalidateQueries({ queryKey: ["organization-recommendations"] });
    },
    onError: (error) => {
      toast.error(
        error instanceof ApiError ? error.message : "Couldn't start the organization analysis.",
      );
    },
  });

  const overview = overviewQuery.data;
  const hasAnalysis = overview?.last_analyzed_at != null;
  const entityCountsByType = new Map<EntityType, number>();
  for (const entity of entitiesQuery.data?.items ?? []) {
    const current = entityCountsByType.get(entity.entity_type) ?? 0;
    entityCountsByType.set(entity.entity_type, current + 1);
  }

  return (
    <AppShell title="Storage Intelligence">
      <div className="mx-auto flex max-w-6xl flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Storage Intelligence</h1>
            <p className="text-sm text-muted-foreground">
              Detection and recommendations only — nothing here deletes, moves, or modifies a
              file.
            </p>
          </div>
          {canManage && (
            <Button
              variant="outline"
              onClick={() => analyzeMutation.mutate()}
              disabled={analyzeMutation.isPending || overview?.last_analysis_status === "running"}
            >
              <RefreshCw className="size-4" />
              {analyzeMutation.isPending || overview?.last_analysis_status === "running"
                ? "Analyzing…"
                : "Re-analyze storage"}
            </Button>
          )}
        </div>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="flex items-center gap-2">
              <FolderTree className="size-4 text-primary" /> Organization
            </CardTitle>
            {canManage && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => analyzeOrganizationMutation.mutate()}
                disabled={analyzeOrganizationMutation.isPending}
              >
                <RefreshCw className="size-4" />
                {analyzeOrganizationMutation.isPending ? "Starting…" : "Analyze organization"}
              </Button>
            )}
          </CardHeader>
          <CardContent>
            {entitiesQuery.isLoading ? (
              <Skeleton className="h-10 rounded-lg" />
            ) : entityCountsByType.size === 0 ? (
              <p className="text-sm text-muted-foreground">
                No projects, clients, or campaigns discovered yet — run an analysis to find ones
                scattered across your Drive.
              </p>
            ) : (
              <div className="flex flex-wrap items-center gap-4">
                {Array.from(entityCountsByType.entries()).map(([entityType, count]) => (
                  <div key={entityType} className="flex flex-col">
                    <span className="text-2xl font-semibold tracking-tight">{count}</span>
                    <span className="text-xs text-muted-foreground">
                      {entityTypeLabel(entityType)}
                      {count === 1 ? "" : "s"}
                    </span>
                  </div>
                ))}
                <Link
                  to="/recommendations"
                  className="ml-auto flex items-center gap-1.5 text-sm text-primary hover:underline"
                >
                  {organizationRecommendationsQuery.data?.items.length ?? 0} recommendation
                  {organizationRecommendationsQuery.data?.items.length === 1 ? "" : "s"}
                </Link>
              </div>
            )}
          </CardContent>
        </Card>

        {overviewQuery.isLoading && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-28 rounded-2xl" />
            ))}
          </div>
        )}
        {overviewQuery.isError && (
          <EmptyState title="Couldn't load storage overview" description="Please try again." />
        )}

        {overviewQuery.isSuccess && !hasAnalysis && (
          <EmptyState
            icon={HardDrive}
            title="No storage analysis yet"
            description="Run an analysis to see duplicates, large files, and potential savings across your connected storage."
            action={
              canManage ? (
                <Button onClick={() => analyzeMutation.mutate()} disabled={analyzeMutation.isPending}>
                  {analyzeMutation.isPending ? "Starting…" : "Analyze storage"}
                </Button>
              ) : undefined
            }
          />
        )}

        {overview && hasAnalysis && (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <StatCard
                icon={HardDrive}
                label="Storage used"
                value={formatBytes(overview.total_size_bytes ?? 0)}
                sub={`${(overview.total_files ?? 0).toLocaleString()} files`}
              />
              <StatCard
                icon={Sparkles}
                label="Potential savings"
                value={formatBytes(overview.total_potential_savings_bytes ?? 0)}
              />
              <StatCard
                icon={Copy}
                label="Duplicate files"
                value={formatBytes(overview.duplicate_recoverable_bytes ?? 0)}
                sub={`${(overview.duplicate_group_count ?? 0).toLocaleString()} groups`}
              />
              <StatCard
                icon={Clock}
                label="Inactive files"
                value={formatBytes(overview.inactive_file_bytes ?? 0)}
                sub={`${(overview.inactive_file_count ?? 0).toLocaleString()} files`}
              />
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle>Storage breakdown</CardTitle>
                </CardHeader>
                <CardContent>
                  {statisticsQuery.isLoading && <Skeleton className="h-40 rounded-lg" />}
                  {statisticsQuery.data &&
                  Object.keys(statisticsQuery.data.breakdown_by_type_bytes).length > 0 ? (
                    <ul className="flex flex-col gap-2">
                      {Object.entries(statisticsQuery.data.breakdown_by_type_bytes)
                        .sort(([, a], [, b]) => b - a)
                        .map(([category, bytes]) => (
                          <li key={category} className="flex items-center justify-between text-sm">
                            <span>{category}</span>
                            <span className="text-muted-foreground">{formatBytes(bytes)}</span>
                          </li>
                        ))}
                    </ul>
                  ) : (
                    !statisticsQuery.isLoading && (
                      <p className="text-sm text-muted-foreground">No breakdown available yet.</p>
                    )
                  )}
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Optimization opportunities</CardTitle>
                </CardHeader>
                <CardContent>
                  <ul className="flex flex-col divide-y divide-border text-sm">
                    <li>
                      <Link
                        to="/storage-intelligence/duplicates"
                        className="flex items-center justify-between py-2 hover:text-primary"
                      >
                        <span>Duplicate files</span>
                        <span className="text-muted-foreground">
                          {formatBytes(overview.duplicate_recoverable_bytes ?? 0)}
                        </span>
                      </Link>
                    </li>
                    <li>
                      <Link
                        to="/storage-intelligence/inactive-files"
                        className="flex items-center justify-between py-2 hover:text-primary"
                      >
                        <span>Inactive files</span>
                        <span className="text-muted-foreground">
                          {formatBytes(overview.inactive_file_bytes ?? 0)}
                        </span>
                      </Link>
                    </li>
                    <li>
                      <Link
                        to="/storage-intelligence/large-files"
                        className="flex items-center justify-between py-2 hover:text-primary"
                      >
                        <span>Large files (over 100 MB)</span>
                        <span className="text-muted-foreground">
                          {formatBytes(overview.large_file_bytes ?? 0)}
                        </span>
                      </Link>
                    </li>
                    <li>
                      <Link
                        to="/storage-intelligence/old-files"
                        className="flex items-center justify-between py-2 hover:text-primary"
                      >
                        <span>Old files</span>
                        <span className="text-muted-foreground">
                          {formatBytes(overview.old_file_bytes ?? 0)}
                        </span>
                      </Link>
                    </li>
                    <li>
                      <Link
                        to="/storage-intelligence/candidates"
                        className="flex items-center justify-between py-2 hover:text-primary"
                      >
                        <span>Potential temporary files</span>
                        <span className="text-muted-foreground">
                          {formatBytes(overview.temporary_candidate_bytes ?? 0)}
                        </span>
                      </Link>
                    </li>
                  </ul>
                </CardContent>
              </Card>
            </div>

            <Card>
              <CardHeader className="flex flex-row items-center justify-between">
                <CardTitle>Potential duplicate files</CardTitle>
                <Button asChild variant="ghost" size="sm">
                  <Link to="/storage-intelligence/duplicates">View all</Link>
                </Button>
              </CardHeader>
              <CardContent>
                {duplicatesQuery.isLoading && <Skeleton className="h-24 rounded-lg" />}
                {duplicatesQuery.data?.items.length === 0 && (
                  <p className="text-sm text-muted-foreground">No duplicate files found.</p>
                )}
                {duplicatesQuery.data && duplicatesQuery.data.items.length > 0 && (
                  <ul className="flex flex-col divide-y divide-border">
                    {duplicatesQuery.data.items.map((group) => (
                      <li key={group.id}>
                        <Link
                          to="/storage-intelligence/duplicates/$groupId"
                          params={{ groupId: group.id }}
                          className="flex items-center justify-between gap-3 py-2.5 text-sm hover:text-primary"
                        >
                          <span className="flex items-center gap-2">
                            <Copy className="size-4 text-muted-foreground" />
                            {group.file_count} copies
                          </span>
                          <span className="text-muted-foreground">
                            {formatBytes(group.recoverable_size_bytes)} recoverable
                          </span>
                        </Link>
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex flex-row items-center justify-between">
                <CardTitle>Largest files</CardTitle>
                <Button asChild variant="ghost" size="sm">
                  <Link to="/storage-intelligence/large-files">View all</Link>
                </Button>
              </CardHeader>
              <CardContent>
                {largeFilesQuery.isLoading && <Skeleton className="h-24 rounded-lg" />}
                {largeFilesQuery.data?.items.length === 0 && (
                  <p className="text-sm text-muted-foreground">No large files found.</p>
                )}
                {largeFilesQuery.data && largeFilesQuery.data.items.length > 0 && (
                  <ul className="flex flex-col divide-y divide-border">
                    {largeFilesQuery.data.items.map((file) => (
                      <li key={file.id} className="flex items-center justify-between gap-3 py-2.5 text-sm">
                        <span className="flex min-w-0 items-center gap-2">
                          <FileWarning className="size-4 shrink-0 text-muted-foreground" />
                          <span className="truncate">{file.name}</span>
                        </span>
                        <span className="shrink-0 text-muted-foreground">
                          {formatBytes(file.size_bytes ?? 0)}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
                <p className="mt-3 flex items-center gap-1.5 text-xs text-muted-foreground">
                  <Eye className="size-3.5 shrink-0" />
                  Large doesn&rsquo;t mean unwanted — review before acting on any file here.
                </p>
              </CardContent>
            </Card>

            <Card className="flex flex-wrap items-center justify-between gap-2 p-4">
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <AlertTriangle className="size-4" />
                Last analyzed {formatRelativeTime(overview.last_analyzed_at ?? "")}
              </div>
              {overview.last_analysis_status && (
                <Badge variant={analysisStatusBadgeVariant(overview.last_analysis_status)}>
                  {analysisStatusLabel(overview.last_analysis_status)}
                </Badge>
              )}
            </Card>
          </>
        )}
      </div>
    </AppShell>
  );
}
