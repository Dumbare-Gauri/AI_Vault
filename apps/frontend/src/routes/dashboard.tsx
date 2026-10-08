import { useQuery } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type {
  ActivityItem,
  ActivityResponse,
  Connector,
  Dashboard,
  OrganizationRecommendationListResponse,
  RecommendationListResponse,
  StorageOverview,
} from "@vault/types";
import {
  Archive,
  ArrowUpRight,
  BrainCircuit,
  Copy,
  FolderKanban,
  FolderTree,
  Plus,
  ScanSearch,
  Sparkles,
  Trash2,
  Users,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useState } from "react";

import { AppShell } from "@/components/app-shell/app-shell";
import { EmptyTrashDialog } from "@/components/file-explorer/empty-trash-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { apiClient } from "@/lib/api-client";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { summarizeStorage } from "@/lib/storage-summary";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/dashboard")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: DashboardPage,
});

const DONE_TONE = { dot: "bg-success", label: "Done" };
const ACTIVITY_TONE: Record<string, { dot: string; label: string }> = {
  done: DONE_TONE,
  partial: { dot: "bg-warning", label: "Partly done" },
  failed: { dot: "bg-destructive", label: "Failed" },
  undone: { dot: "bg-muted-foreground", label: "Undone" },
  in_progress: { dot: "bg-primary animate-pulse", label: "In progress" },
};

function percent(part: number, whole: number): number {
  if (whole <= 0) return 0;
  return Math.min(100, Math.max(0, (part / whole) * 100));
}

function Timestamp({ iso }: { iso: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <time dateTime={iso} className="shrink-0 text-xs text-muted-foreground">
          {formatRelativeTime(iso)}
        </time>
      </TooltipTrigger>
      <TooltipContent>{new Date(iso).toLocaleString()}</TooltipContent>
    </Tooltip>
  );
}

function StorageHero({
  connectors,
  dashboard,
  overview,
}: {
  connectors: Connector[];
  dashboard: Dashboard;
  overview: StorageOverview | undefined;
}) {
  const summary = summarizeStorage(
    connectors,
    dashboard.latest_snapshot?.total_storage_bytes ?? 0,
    overview?.total_potential_savings_bytes ?? null,
  );
  const usedPercent = summary.totalBytes ? percent(summary.usedBytes, summary.totalBytes) : 0;
  const recoverablePercent = summary.totalBytes
    ? percent(Math.min(summary.recoverableBytes, summary.usedBytes), summary.totalBytes)
    : 0;

  return (
    <Card clay className="relative min-w-0 overflow-hidden p-6 lg:col-span-2 lg:p-8">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -right-24 -top-24 size-72 rounded-full bg-primary/10 blur-3xl"
      />
      <p className="text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">
        Storage
      </p>
      <div className="mt-3 flex flex-wrap items-end gap-x-3 gap-y-1">
        <p className="text-5xl font-semibold leading-none tracking-tight">
          {formatBytes(summary.usedBytes)}
        </p>
        <p className="pb-1 text-sm text-muted-foreground">
          {summary.totalBytes !== null
            ? `used of ${formatBytes(summary.totalBytes)}`
            : "used across connected storage"}
        </p>
      </div>
      {summary.accountsWithQuota > 0 && overview?.total_size_bytes != null && (
        <p className="mt-3 max-w-xl text-sm text-muted-foreground">
          Account storage as Google reports it — Gmail and Google Photos count too. Your own
          Drive files take {formatBytes(overview.total_size_bytes)}
          {overview.total_files != null ? ` (${overview.total_files.toLocaleString()} files)` : ""}.
        </p>
      )}

      {summary.totalBytes !== null && (
        <div
          className="mt-6 h-2.5 w-full overflow-hidden rounded-full bg-secondary"
          role="img"
          aria-label={`${Math.round(usedPercent)}% of capacity used`}
        >
          <div className="flex h-full" style={{ width: `${usedPercent}%` }}>
            <div className="h-full flex-1 bg-primary" />
            {recoverablePercent > 0 && (
              <div
                className="h-full bg-warning"
                style={{ width: `${(recoverablePercent / usedPercent) * 100}%` }}
              />
            )}
          </div>
        </div>
      )}

      <dl className="mt-6 grid grid-cols-3 gap-4 border-t border-border pt-5">
        <div>
          <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="size-2 rounded-full bg-primary" /> Used
          </dt>
          <dd className="mt-1 text-lg font-semibold">{formatBytes(summary.usedBytes)}</dd>
        </div>
        <div>
          <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="size-2 rounded-full bg-secondary ring-1 ring-border" /> Free
          </dt>
          <dd className="mt-1 text-lg font-semibold">
            {summary.freeBytes !== null ? formatBytes(summary.freeBytes) : "—"}
          </dd>
        </div>
        <div>
          <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="size-2 rounded-full bg-warning" /> Potential savings
          </dt>
          <dd className="mt-1 text-lg font-semibold">
            {overview?.total_potential_savings_bytes != null
              ? formatBytes(summary.recoverableBytes)
              : "—"}
          </dd>
        </div>
      </dl>
      {overview?.total_potential_savings_bytes == null && (
        <p className="mt-3 text-xs text-muted-foreground">
          Potential savings appear after the first storage analysis.
        </p>
      )}
    </Card>
  );
}

/** Google keeps counting trashed files against the quota until the Trash is
 * emptied — shown on its own so it isn't mistaken for part of the total. */
function DriveTrashCard({ connectors, canManage }: { connectors: Connector[]; canManage: boolean }) {
  const [emptying, setEmptying] = useState(false);
  const trashConnector = connectors.find(
    (connector) => connector.status === "connected" && (connector.storage_trash_bytes ?? 0) > 0,
  );
  if (!trashConnector) return null;
  return (
    <Card className="flex flex-wrap items-center gap-4 p-5">
      <span className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-warning-muted">
        <Trash2 className="size-5 text-warning" aria-hidden="true" />
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">Google Drive Trash</p>
        <p className="mt-1 text-2xl font-semibold">{formatBytes(trashConnector.storage_trash_bytes ?? 0)}</p>
        <p className="text-sm text-muted-foreground">
          Still counts against your Google storage until the Trash is emptied.
        </p>
      </div>
      {canManage && (
        <Button variant="destructive" className="shrink-0" onClick={() => setEmptying(true)}>
          <Trash2 className="size-4" /> Empty Drive Trash
        </Button>
      )}
      {emptying && (
        <EmptyTrashDialog connectorId={trashConnector.id} onOpenChange={(open) => !open && setEmptying(false)} />
      )}
    </Card>
  );
}

function AccountsCard({ connectors }: { connectors: Connector[] }) {
  return (
    <Card className="flex min-w-0 flex-col">
      <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle>Connected storage</CardTitle>
        <Link
          to="/storage-connections"
          className="flex items-center gap-1 text-xs font-medium text-primary hover:underline"
        >
          <Plus className="size-3.5" /> Add storage
        </Link>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-3">
        {connectors.length === 0 ? (
          <p className="text-sm text-muted-foreground">No storage connected yet.</p>
        ) : (
          connectors.map((connector) => {
            const connected = connector.status === "connected";
            const capacity = connector.storage_total_bytes;
            const used = connector.storage_used_bytes;
            return (
              <div key={connector.id} className="flex flex-col gap-2 rounded-lg bg-secondary/50 p-3">
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">
                      {connector.display_name ?? connector.provider_name}
                    </p>
                    <p className="truncate text-xs text-muted-foreground">
                      {connector.account_email ?? "—"}
                    </p>
                  </div>
                  <span className="flex shrink-0 items-center gap-1.5 text-xs">
                    <span
                      className={cn(
                        "size-2 rounded-full",
                        connected ? "bg-success" : "bg-warning",
                      )}
                    />
                    {connected ? "Connected" : connector.status.replace("_", " ")}
                  </span>
                </div>
                {used !== null && (
                  <div className="flex flex-col gap-1">
                    {capacity !== null && (
                      <div className="h-1.5 w-full overflow-hidden rounded-full bg-card">
                        <div
                          className="h-full rounded-full bg-primary"
                          style={{ width: `${percent(used, capacity)}%` }}
                        />
                      </div>
                    )}
                    <p className="text-xs text-muted-foreground">
                      {formatBytes(used)}
                      {capacity !== null ? ` of ${formatBytes(capacity)}` : " used"}
                      {connector.last_synced_at && (
                        <> · synced {formatRelativeTime(connector.last_synced_at)}</>
                      )}
                    </p>
                  </div>
                )}
              </div>
            );
          })
        )}
      </CardContent>
    </Card>
  );
}

function Metric({
  icon: Icon,
  label,
  value,
  to,
  detail,
  tone = "default",
}: {
  icon: LucideIcon;
  label: string;
  value: number | null;
  to: string;
  detail?: string;
  tone?: "default" | "ai" | "warning";
}) {
  return (
    <Link
      to={to}
      className="group flex flex-col gap-3 rounded-xl border border-border bg-card p-4 transition-all hover:-translate-y-0.5 hover:shadow-clay"
    >
      <span
        className={cn(
          "flex size-8 items-center justify-center rounded-lg",
          tone === "ai" && "bg-ai-muted text-ai",
          tone === "warning" && "bg-warning-muted text-warning",
          tone === "default" && "bg-primary/10 text-primary",
        )}
      >
        <Icon className="size-4" aria-hidden="true" />
      </span>
      <div>
        <p className="text-2xl font-semibold tracking-tight">
          {value === null ? "—" : value.toLocaleString()}
        </p>
        <p className="text-xs text-muted-foreground">{label}</p>
        {detail && <p className="mt-0.5 text-xs text-muted-foreground/80">{detail}</p>}
      </div>
    </Link>
  );
}

function ActivityRow({ item }: { item: ActivityItem }) {
  const tone = ACTIVITY_TONE[item.status] ?? DONE_TONE;
  return (
    <li className="flex items-start justify-between gap-3 py-3 text-sm">
      <div className="flex min-w-0 items-start gap-3">
        <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", tone.dot)} aria-hidden="true" />
        <div className="min-w-0">
          <p className="font-medium">{item.message}</p>
          <p className="text-xs text-muted-foreground">{tone.label}</p>
        </div>
      </div>
      <Timestamp iso={item.at} />
    </li>
  );
}

function DashboardPage() {
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";

  const dashboardQuery = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => apiClient.get<Dashboard>("/v1/dashboard"),
  });
  const connectorsQuery = useQuery({
    queryKey: ["connectors"],
    queryFn: () => apiClient.get<Connector[]>("/v1/connectors"),
  });
  const overviewQuery = useQuery({
    queryKey: ["storage-intelligence", "overview"],
    queryFn: () => apiClient.get<StorageOverview>("/v1/storage/overview"),
  });
  const activityQuery = useQuery({
    queryKey: ["activity"],
    queryFn: () => apiClient.get<ActivityResponse>("/v1/activity"),
    refetchInterval: 15_000,
  });
  const organizeQuery = useQuery({
    queryKey: ["organization-recommendations", "active"],
    queryFn: () =>
      apiClient.get<OrganizationRecommendationListResponse>(
        "/v1/organization-recommendations?status=active",
      ),
  });
  const aiStatusQuery = useQuery({
    queryKey: ["ai-status"],
    queryFn: () => apiClient.get<{ mode: string }>("/v1/ai/status"),
  });
  const cleanupQuery = useQuery({
    queryKey: ["recommendations", "preview"],
    queryFn: () => apiClient.get<RecommendationListResponse>("/v1/recommendations?status=active"),
  });

  const dashboard = dashboardQuery.data;
  const connectors = connectorsQuery.data ?? [];
  const overview = overviewQuery.data;
  const intelligence = dashboard?.intelligence;
  const aiReady = aiStatusQuery.data?.mode === "ai";
  const entities = intelligence?.entities_by_type ?? {};

  const suggestions = [
    ...(organizeQuery.data?.items ?? []).slice(0, 3).map((item) => ({
      id: item.id,
      title: item.title,
      detail: item.reasoning_summary,
      to: "/organization-recommendations/$recommendationId" as const,
      ai: true,
    })),
    ...(cleanupQuery.data?.items ?? []).slice(0, 3).map((item) => ({
      id: item.id,
      title: item.title,
      detail: item.estimated_impact,
      to: "/recommendations/$recommendationId" as const,
      ai: false,
    })),
  ];

  const today = new Date().toLocaleDateString(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
  });

  return (
    <AppShell title="Dashboard">
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        <header className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <p className="text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">
              {today}
            </p>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight">
              Welcome back{user?.name ? `, ${user.name.split(" ")[0]}` : ""}
            </h1>
          </div>
          <Button asChild variant="outline" size="sm">
            <Link to="/search">
              <ScanSearch className="size-4" /> Find a file
            </Link>
          </Button>
        </header>

        {dashboardQuery.isLoading && (
          <div className="grid gap-4 lg:grid-cols-3">
            <Skeleton className="h-72 rounded-2xl lg:col-span-2" />
            <Skeleton className="h-72 rounded-2xl" />
          </div>
        )}

        {dashboardQuery.isError && (
          <EmptyState
            title="Couldn't load the dashboard"
            description="There was a problem reaching AI Vault. Try refreshing the page."
          />
        )}

        {dashboard && (
          <>
            <div className="grid gap-4 lg:grid-cols-3">
              <StorageHero connectors={connectors} dashboard={dashboard} overview={overview} />
              <AccountsCard connectors={connectors} />
            </div>
            <DriveTrashCard connectors={connectors} canManage={canManage} />

            <section className="flex flex-col gap-3">
              <div className="flex items-center justify-between">
                <h2 className="text-base font-semibold">What AI Vault understands</h2>
                <Link
                  to="/storage-intelligence"
                  className="flex items-center gap-1 text-xs font-medium text-primary hover:underline"
                >
                  Storage Intelligence <ArrowUpRight className="size-3.5" />
                </Link>
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                <Metric
                  icon={BrainCircuit}
                  label="Files read by AI"
                  value={intelligence?.files_analyzed ?? null}
                  detail={aiReady ? undefined : "AI not set up yet"}
                  to={aiReady ? "/files" : "/organization"}
                  tone="ai"
                />
                <Metric
                  icon={FolderKanban}
                  label="Projects"
                  value={entities.project ?? 0}
                  to="/storage-intelligence"
                  tone="ai"
                />
                <Metric
                  icon={Users}
                  label="Clients"
                  value={entities.client ?? 0}
                  to="/storage-intelligence"
                  tone="ai"
                />
                <Metric
                  icon={Copy}
                  label="Duplicate files"
                  value={overview?.duplicate_file_count ?? null}
                  detail={
                    overview?.duplicate_recoverable_bytes != null
                      ? `${formatBytes(overview.duplicate_recoverable_bytes)} in ${overview.duplicate_group_count ?? 0} groups`
                      : undefined
                  }
                  to="/storage-intelligence/duplicates"
                  tone="warning"
                />
                <Metric
                  icon={Archive}
                  label="Inactive files"
                  value={overview?.inactive_file_count ?? null}
                  detail={
                    overview?.inactive_file_bytes != null
                      ? formatBytes(overview.inactive_file_bytes)
                      : undefined
                  }
                  to="/storage-intelligence/inactive-files"
                />
                <Metric
                  icon={FolderTree}
                  label="To organize"
                  value={intelligence?.organize_suggestions ?? 0}
                  to="/recommendations"
                />
              </div>
            </section>

            <div className="grid gap-4 lg:grid-cols-5">
              <Card className="min-w-0 lg:col-span-3">
                <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
                  <CardTitle>Recommended for you</CardTitle>
                  <Link
                    to="/recommendations"
                    className="flex items-center gap-1 text-xs font-medium text-primary hover:underline"
                  >
                    All recommendations <ArrowUpRight className="size-3.5" />
                  </Link>
                </CardHeader>
                <CardContent>
                  {suggestions.length === 0 ? (
                    <p className="py-6 text-center text-sm text-muted-foreground">
                      Nothing to do right now — your storage looks in good shape.
                    </p>
                  ) : (
                    <ul className="flex flex-col gap-1">
                      {suggestions.map((suggestion) => (
                        <li key={suggestion.id}>
                          <Link
                            to={suggestion.to}
                            params={{ recommendationId: suggestion.id }}
                            className="flex items-start gap-3 rounded-lg p-2.5 text-sm transition-colors hover:bg-secondary/60"
                          >
                            <span
                              className={cn(
                                "mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md",
                                suggestion.ai ? "bg-ai-muted text-ai" : "bg-primary/10 text-primary",
                              )}
                            >
                              {suggestion.ai ? (
                                <Sparkles className="size-3.5" />
                              ) : (
                                <Archive className="size-3.5" />
                              )}
                            </span>
                            <div className="min-w-0">
                              <p className="truncate font-medium">{suggestion.title}</p>
                              <p className="line-clamp-2 text-xs text-muted-foreground">
                                {suggestion.detail}
                              </p>
                            </div>
                          </Link>
                        </li>
                      ))}
                    </ul>
                  )}
                </CardContent>
              </Card>

              <Card className="min-w-0 lg:col-span-2">
                <CardHeader>
                  <CardTitle>Recent activity</CardTitle>
                </CardHeader>
                <CardContent>
                  {activityQuery.isLoading && <Skeleton className="h-40 rounded-lg" />}
                  {activityQuery.data && activityQuery.data.items.length === 0 && (
                    <p className="py-6 text-center text-sm text-muted-foreground">
                      Changes you make through AI Vault appear here.
                    </p>
                  )}
                  {activityQuery.data && activityQuery.data.items.length > 0 && (
                    <ul className="flex flex-col divide-y divide-border">
                      {activityQuery.data.items.slice(0, 8).map((item) => (
                        <ActivityRow key={`${item.kind}-${item.id}`} item={item} />
                      ))}
                    </ul>
                  )}
                </CardContent>
              </Card>
            </div>
          </>
        )}
      </div>
    </AppShell>
  );
}
