import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { EmbeddingJob, EnrichmentJob, IntelligenceJob, ScanJob } from "@vault/types";
import { Cloud } from "lucide-react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchConnectors } from "@/lib/connectors";
import { apiClient } from "@/lib/api-client";
import { embeddingStatusBadgeVariant, isActiveEmbeddingStatus } from "@/lib/embedding-status";
import { enrichmentStatusBadgeVariant, isActiveEnrichmentStatus } from "@/lib/enrichment-status";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { intelligenceStatusBadgeVariant, isActiveIntelligenceStatus } from "@/lib/intelligence-status";
import { isActiveScanStatus, scanStatusBadgeVariant } from "@/lib/scan-status";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/scans")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: ScansPage,
});

function ScansPage() {
  const queryClient = useQueryClient();
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";

  const connectorsQuery = useQuery({
    queryKey: ["connectors"],
    queryFn: fetchConnectors,
  });

  const connector = connectorsQuery.data?.find(
    (candidate) => candidate.provider === "google_workspace" && candidate.status === "connected",
  );

  const scansQuery = useQuery({
    queryKey: ["scans", connector?.id],
    queryFn: () => apiClient.get<ScanJob[]>(`/v1/connectors/${connector?.id}/scans`),
    enabled: connector !== undefined,
    refetchInterval: (query) =>
      query.state.data?.some((job) => isActiveScanStatus(job.status)) ? 3000 : false,
  });

  const startMutation = useMutation({
    mutationFn: () => apiClient.post<ScanJob>(`/v1/connectors/${connector?.id}/scans`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["scans", connector?.id] }),
  });

  const cancelMutation = useMutation({
    mutationFn: (scanJobId: string) => apiClient.post<ScanJob>(`/v1/scans/${scanJobId}/cancel`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["scans", connector?.id] }),
  });

  const enrichmentQuery = useQuery({
    queryKey: ["enrichment", connector?.id],
    queryFn: () => apiClient.get<EnrichmentJob[]>(`/v1/connectors/${connector?.id}/enrichment`),
    enabled: connector !== undefined,
    refetchInterval: (query) =>
      query.state.data?.some((job) => isActiveEnrichmentStatus(job.status)) ? 3000 : false,
  });

  const startEnrichmentMutation = useMutation({
    mutationFn: () => apiClient.post<EnrichmentJob>(`/v1/connectors/${connector?.id}/enrichment`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["enrichment", connector?.id] }),
  });

  const cancelEnrichmentMutation = useMutation({
    mutationFn: (enrichmentJobId: string) =>
      apiClient.post<EnrichmentJob>(`/v1/enrichment/${enrichmentJobId}/cancel`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["enrichment", connector?.id] }),
  });

  const embeddingQuery = useQuery({
    queryKey: ["embedding", connector?.id],
    queryFn: () => apiClient.get<EmbeddingJob[]>(`/v1/connectors/${connector?.id}/embedding`),
    enabled: connector !== undefined,
    refetchInterval: (query) =>
      query.state.data?.some((job) => isActiveEmbeddingStatus(job.status)) ? 3000 : false,
  });

  const startEmbeddingMutation = useMutation({
    mutationFn: () => apiClient.post<EmbeddingJob>(`/v1/connectors/${connector?.id}/embedding`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["embedding", connector?.id] }),
  });

  const cancelEmbeddingMutation = useMutation({
    mutationFn: (embeddingJobId: string) =>
      apiClient.post<EmbeddingJob>(`/v1/embedding/${embeddingJobId}/cancel`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["embedding", connector?.id] }),
  });

  const intelligenceQuery = useQuery({
    queryKey: ["intelligence", connector?.id],
    queryFn: () =>
      apiClient.get<IntelligenceJob[]>(`/v1/connectors/${connector?.id}/intelligence`),
    enabled: connector !== undefined,
    refetchInterval: (query) =>
      query.state.data?.some((job) => isActiveIntelligenceStatus(job.status)) ? 3000 : false,
  });

  const startIntelligenceMutation = useMutation({
    mutationFn: () =>
      apiClient.post<IntelligenceJob>(`/v1/connectors/${connector?.id}/intelligence`),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ["intelligence", connector?.id] }),
  });

  const cancelIntelligenceMutation = useMutation({
    mutationFn: (intelligenceJobId: string) =>
      apiClient.post<IntelligenceJob>(`/v1/intelligence/${intelligenceJobId}/cancel`),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ["intelligence", connector?.id] }),
  });

  const jobs = scansQuery.data ?? [];
  const activeJob = jobs.find((job) => isActiveScanStatus(job.status));
  const lastCompletedJob = jobs.find((job) => job.status === "completed");

  const enrichmentJobs = enrichmentQuery.data ?? [];
  const activeEnrichmentJob = enrichmentJobs.find((job) => isActiveEnrichmentStatus(job.status));
  const lastCompletedEnrichmentJob = enrichmentJobs.find((job) => job.status === "completed");

  const embeddingJobs = embeddingQuery.data ?? [];
  const activeEmbeddingJob = embeddingJobs.find((job) => isActiveEmbeddingStatus(job.status));
  const lastCompletedEmbeddingJob = embeddingJobs.find((job) => job.status === "completed");

  const intelligenceJobs = intelligenceQuery.data ?? [];
  const activeIntelligenceJob = intelligenceJobs.find((job) =>
    isActiveIntelligenceStatus(job.status),
  );
  const lastCompletedIntelligenceJob = intelligenceJobs.find((job) => job.status === "completed");

  return (
    <AppShell title="Scans">
      <div className="mx-auto flex max-w-3xl flex-col gap-4">
        <h1 className="text-xl font-semibold tracking-tight">Scans</h1>

        {connectorsQuery.isLoading && <Skeleton className="h-32 rounded-2xl" />}
        {connectorsQuery.isError && (
          <EmptyState title="Couldn't load storage connections" description="Please try again." />
        )}

        {!connectorsQuery.isLoading && connector === undefined && (
          <EmptyState
            icon={Cloud}
            title="No storage connected yet"
            description="Connect Google Workspace to start scanning."
            action={
              <Button asChild>
                <Link to="/storage-connections">Connect Google Drive</Link>
              </Button>
            }
          />
        )}

        {connector && (
          <>
            <Card>
              <CardHeader>
                <CardTitle>Current status</CardTitle>
              </CardHeader>
              <CardContent>
                {activeJob ? (
                  <div className="flex flex-col gap-2 text-sm">
                    <div className="flex items-center gap-2">
                      <Badge variant={scanStatusBadgeVariant(activeJob.status)} className="capitalize">
                        {activeJob.status}
                      </Badge>
                      {activeJob.progress?.current_source_name && (
                        <span className="text-muted-foreground">
                          scanning {activeJob.progress.current_source_name}
                        </span>
                      )}
                    </div>
                    {activeJob.progress && (
                      <p className="text-muted-foreground">
                        {activeJob.progress.sources_completed}/{activeJob.progress.sources_discovered}{" "}
                        sources · {activeJob.progress.folders_discovered} folders ·{" "}
                        {activeJob.progress.files_discovered} files
                      </p>
                    )}
                    {canManage && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-fit"
                        disabled={cancelMutation.isPending}
                        onClick={() => cancelMutation.mutate(activeJob.id)}
                      >
                        {cancelMutation.isPending ? "Cancelling…" : "Cancel scan"}
                      </Button>
                    )}
                  </div>
                ) : (
                  <div className="flex flex-col gap-3 text-sm">
                    <p className="text-muted-foreground">
                      {lastCompletedJob
                        ? `Last successful scan: ${formatRelativeTime(lastCompletedJob.completed_at ?? lastCompletedJob.created_at)}`
                        : "No scans yet."}
                    </p>
                    {canManage && (
                      <Button
                        className="w-fit"
                        disabled={startMutation.isPending}
                        onClick={() => startMutation.mutate()}
                      >
                        {startMutation.isPending ? "Starting…" : "Start scan"}
                      </Button>
                    )}
                    {startMutation.isError && (
                      <p className="text-sm text-destructive">Couldn&rsquo;t start a scan. Please try again.</p>
                    )}
                  </div>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Scan history</CardTitle>
              </CardHeader>
              <CardContent>
                {scansQuery.isLoading && <Skeleton className="h-4 w-32" />}
                {scansQuery.isError && <p className="text-sm text-destructive">Couldn&rsquo;t load scan history.</p>}
                {jobs.length === 0 && !scansQuery.isLoading && (
                  <p className="text-sm text-muted-foreground">No scans have run yet.</p>
                )}
                <ul className="flex flex-col divide-y divide-border">
                  {jobs.map((job) => (
                    <li key={job.id} className="flex items-center justify-between gap-3 py-2.5 text-sm">
                      <div>
                        <Badge variant={scanStatusBadgeVariant(job.status)} className="capitalize">
                          {job.status}
                        </Badge>
                        <span className="ml-2 text-muted-foreground">{job.scan_type}</span>
                        {job.error && <p className="mt-1 text-destructive">{job.error}</p>}
                      </div>
                      <span className="text-xs text-muted-foreground">{formatRelativeTime(job.created_at)}</span>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
                <CardTitle>Enrichment</CardTitle>
                <Link to="/files" className="text-xs font-medium text-primary hover:underline">
                  Browse files
                </Link>
              </CardHeader>
              <CardContent>
                {activeEnrichmentJob ? (
                  <div className="flex flex-col gap-2 text-sm">
                    <div className="flex items-center gap-2">
                      <Badge
                        variant={enrichmentStatusBadgeVariant(activeEnrichmentJob.status)}
                        className="capitalize"
                      >
                        {activeEnrichmentJob.status}
                      </Badge>
                      {activeEnrichmentJob.progress?.current_file_name && (
                        <span className="text-muted-foreground">
                          enriching {activeEnrichmentJob.progress.current_file_name}
                        </span>
                      )}
                    </div>
                    {activeEnrichmentJob.progress && (
                      <p className="text-muted-foreground">
                        {activeEnrichmentJob.progress.files_processed}/
                        {activeEnrichmentJob.progress.files_pending} files ·{" "}
                        {activeEnrichmentJob.progress.files_failed} failed
                      </p>
                    )}
                    {canManage && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-fit"
                        disabled={cancelEnrichmentMutation.isPending}
                        onClick={() => cancelEnrichmentMutation.mutate(activeEnrichmentJob.id)}
                      >
                        {cancelEnrichmentMutation.isPending ? "Cancelling…" : "Cancel enrichment"}
                      </Button>
                    )}
                  </div>
                ) : (
                  <div className="flex flex-col gap-3 text-sm">
                    <p className="text-muted-foreground">
                      {lastCompletedEnrichmentJob
                        ? `Last enrichment: ${formatRelativeTime(lastCompletedEnrichmentJob.completed_at ?? lastCompletedEnrichmentJob.created_at)}`
                        : "No enrichment has run yet."}
                    </p>
                    {canManage && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-fit"
                        disabled={startEnrichmentMutation.isPending}
                        onClick={() => startEnrichmentMutation.mutate()}
                      >
                        {startEnrichmentMutation.isPending ? "Starting…" : "Re-run enrichment"}
                      </Button>
                    )}
                    {startEnrichmentMutation.isError && (
                      <p className="text-sm text-destructive">Couldn&rsquo;t start enrichment. Please try again.</p>
                    )}
                  </div>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
                <CardTitle>Embedding</CardTitle>
                <Link to="/search" className="text-xs font-medium text-primary hover:underline">
                  Search
                </Link>
              </CardHeader>
              <CardContent>
                {activeEmbeddingJob ? (
                  <div className="flex flex-col gap-2 text-sm">
                    <div className="flex items-center gap-2">
                      <Badge
                        variant={embeddingStatusBadgeVariant(activeEmbeddingJob.status)}
                        className="capitalize"
                      >
                        {activeEmbeddingJob.status}
                      </Badge>
                      {activeEmbeddingJob.progress?.current_file_name && (
                        <span className="text-muted-foreground">
                          embedding {activeEmbeddingJob.progress.current_file_name}
                        </span>
                      )}
                    </div>
                    {activeEmbeddingJob.progress && (
                      <p className="text-muted-foreground">
                        {activeEmbeddingJob.progress.files_processed}/
                        {activeEmbeddingJob.progress.files_pending} files ·{" "}
                        {activeEmbeddingJob.progress.files_failed} failed
                      </p>
                    )}
                    {canManage && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-fit"
                        disabled={cancelEmbeddingMutation.isPending}
                        onClick={() => cancelEmbeddingMutation.mutate(activeEmbeddingJob.id)}
                      >
                        {cancelEmbeddingMutation.isPending ? "Cancelling…" : "Cancel embedding"}
                      </Button>
                    )}
                  </div>
                ) : (
                  <div className="flex flex-col gap-3 text-sm">
                    <p className="text-muted-foreground">
                      {lastCompletedEmbeddingJob
                        ? `Last embedding run: ${formatRelativeTime(lastCompletedEmbeddingJob.completed_at ?? lastCompletedEmbeddingJob.created_at)}`
                        : "No embedding run yet."}
                    </p>
                    {canManage && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-fit"
                        disabled={startEmbeddingMutation.isPending}
                        onClick={() => startEmbeddingMutation.mutate()}
                      >
                        {startEmbeddingMutation.isPending ? "Starting…" : "Re-run embedding"}
                      </Button>
                    )}
                    {startEmbeddingMutation.isError && (
                      <p className="text-sm text-destructive">Couldn&rsquo;t start embedding. Please try again.</p>
                    )}
                  </div>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
                <CardTitle>AI Intelligence</CardTitle>
                <Link to="/files" className="text-xs font-medium text-primary hover:underline">
                  Browse files
                </Link>
              </CardHeader>
              <CardContent>
                {activeIntelligenceJob ? (
                  <div className="flex flex-col gap-2 text-sm">
                    <div className="flex items-center gap-2">
                      <Badge
                        variant={intelligenceStatusBadgeVariant(activeIntelligenceJob.status)}
                        className="capitalize"
                      >
                        {activeIntelligenceJob.status}
                      </Badge>
                      {activeIntelligenceJob.progress?.current_file_name && (
                        <span className="text-muted-foreground">
                          analyzing {activeIntelligenceJob.progress.current_file_name}
                        </span>
                      )}
                    </div>
                    {activeIntelligenceJob.progress && (
                      <p className="text-muted-foreground">
                        {activeIntelligenceJob.progress.files_processed}/
                        {activeIntelligenceJob.progress.files_pending} files ·{" "}
                        {activeIntelligenceJob.progress.files_failed} failed
                      </p>
                    )}
                    {canManage && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-fit"
                        disabled={cancelIntelligenceMutation.isPending}
                        onClick={() => cancelIntelligenceMutation.mutate(activeIntelligenceJob.id)}
                      >
                        {cancelIntelligenceMutation.isPending ? "Cancelling…" : "Cancel analysis"}
                      </Button>
                    )}
                  </div>
                ) : (
                  <div className="flex flex-col gap-3 text-sm">
                    <p className="text-muted-foreground">
                      {lastCompletedIntelligenceJob
                        ? `Last analysis: ${formatRelativeTime(lastCompletedIntelligenceJob.completed_at ?? lastCompletedIntelligenceJob.created_at)}`
                        : "No AI analysis has run yet."}
                    </p>
                    {canManage && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-fit"
                        disabled={startIntelligenceMutation.isPending}
                        onClick={() => startIntelligenceMutation.mutate()}
                      >
                        {startIntelligenceMutation.isPending ? "Starting…" : "Re-run analysis"}
                      </Button>
                    )}
                    {startIntelligenceMutation.isError && (
                      <p className="text-sm text-destructive">
                        Couldn&rsquo;t start AI analysis. Please try again.
                      </p>
                    )}
                  </div>
                )}
              </CardContent>
            </Card>
          </>
        )}
      </div>
    </AppShell>
  );
}
