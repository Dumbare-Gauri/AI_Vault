import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { ExecutionPlan, StorageFileListResponse } from "@vault/types";
import { Archive, ChevronLeft, ChevronRight, FolderArchive } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useState } from "react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { fileTypeIconElement } from "@/lib/file-icon";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { useAuthStore } from "@/stores/auth-store";

const PAGE_SIZE = 20;

interface StorageFileListPageProps {
  title: string;
  description: string;
  apiPath: string;
  emptyIcon: LucideIcon;
  emptyTitle: string;
  emptyDescription: string;
  /** Shown next to each row's size/date — e.g. "Not modified since" vs
   * "Last opened" — since different listings key off different provider
   * timestamps (Phase 1 spec §9 vs §10: content staleness vs. usage
   * staleness are different questions). */
  dateLabel: string;
  getDate: (file: { provider_modified_at: string | null; provider_viewed_at: string | null }) => string | null;
}

export function StorageFileListPage({
  title,
  description,
  apiPath,
  emptyIcon,
  emptyTitle,
  emptyDescription,
  dateLabel,
  getDate,
}: StorageFileListPageProps) {
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const queryClient = useQueryClient();
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";

  const separator = apiPath.includes("?") ? "&" : "?";
  const listQuery = useQuery({
    queryKey: ["storage-intelligence", apiPath, page],
    queryFn: () =>
      apiClient.get<StorageFileListResponse>(
        `${apiPath}${separator}limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`,
      ),
  });

  const items = listQuery.data?.items ?? [];

  // Selection is scoped to the page currently in view — paging away would
  // otherwise leave a "phantom" selection over files no longer visible.
  // Cleared directly in the pagination handlers below (not via an effect
  // keyed on `page`, which would setState synchronously during render).
  function changePage(next: number) {
    setPage(next);
    setSelected(new Set());
  }

  function toggle(fileId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(fileId)) next.delete(fileId);
      else next.add(fileId);
      return next;
    });
  }

  function toggleAll() {
    setSelected((prev) =>
      prev.size === items.length ? new Set() : new Set(items.map((file) => file.id)),
    );
  }

  const archiveMutation = useMutation({
    mutationFn: (fileIds: string[]) =>
      apiClient.post<ExecutionPlan>("/v1/execution-plans", {
        file_ids: fileIds,
        action_type: "archive",
      }),
    onSuccess: (plan) => {
      void queryClient.invalidateQueries({ queryKey: ["execution-plans"] });
      setSelected(new Set());
      toast.success("Moving to Trash now", {
        description: "Recoverable from Google Drive's Trash.",
        action: {
          label: "View progress",
          onClick: () => {
            window.location.href = `/execution-plans/${plan.id}`;
          },
        },
      });
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't create an archive plan.");
    },
  });

  const createArchiveMutation = useMutation({
    mutationFn: (fileIds: string[]) =>
      apiClient.post<ExecutionPlan>("/v1/execution-plans", {
        file_ids: fileIds,
        action_type: "create_archive",
      }),
    onSuccess: (plan) => {
      void queryClient.invalidateQueries({ queryKey: ["execution-plans"] });
      setSelected(new Set());
      toast.success("Creating archive now", {
        action: {
          label: "View progress",
          onClick: () => {
            window.location.href = `/execution-plans/${plan.id}`;
          },
        },
      });
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't create an archive plan.");
    },
  });

  const total = listQuery.data?.total ?? 0;
  const allOnPageSelected = items.length > 0 && selected.size === items.length;

  return (
    <AppShell title={title}>
      <div className="mx-auto flex max-w-4xl flex-col gap-4">
        <div>
          <Link
            to="/storage-intelligence"
            className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ChevronLeft className="size-4" /> Back to Storage Intelligence
          </Link>
          <h1 className="mt-2 text-xl font-semibold tracking-tight">{title}</h1>
          <p className="text-sm text-muted-foreground">
            {total > 0 ? `${total.toLocaleString()} files found. ${description}` : description}
          </p>
        </div>

        {listQuery.isLoading && (
          <div className="flex flex-col gap-2">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-16 rounded-lg" />
            ))}
          </div>
        )}
        {listQuery.isError && (
          <EmptyState title={`Couldn't load ${title.toLowerCase()}`} description="Please try again." />
        )}
        {listQuery.data?.items.length === 0 && (
          <EmptyState icon={emptyIcon} title={emptyTitle} description={emptyDescription} />
        )}

        {items.length > 0 && (
          <>
            {canManage && (
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-secondary/40 px-3 py-2">
                <label className="flex items-center gap-2 text-sm">
                  <Checkbox checked={allOnPageSelected} onCheckedChange={toggleAll} />
                  {selected.size > 0
                    ? `${selected.size} selected`
                    : `Select all ${items.length} on this page`}
                </label>
                {selected.size > 0 && (
                  <div className="flex items-center gap-1.5">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={createArchiveMutation.isPending}
                      onClick={() => createArchiveMutation.mutate(Array.from(selected))}
                    >
                      <FolderArchive className="size-4" />
                      {createArchiveMutation.isPending ? "Creating…" : "Create Archive"}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={archiveMutation.isPending}
                      onClick={() => archiveMutation.mutate(Array.from(selected))}
                    >
                      <Archive className="size-4" />
                      {archiveMutation.isPending ? "Archiving…" : `Archive ${selected.size}`}
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
                      Clear
                    </Button>
                  </div>
                )}
              </div>
            )}

            <Card className="flex flex-col divide-y divide-border p-1">
              {items.map((file) => {
                const date = getDate(file);
                return (
                  <div
                    key={file.id}
                    className="flex items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-sm"
                  >
                    {canManage && (
                      <Checkbox
                        checked={selected.has(file.id)}
                        onCheckedChange={() => toggle(file.id)}
                        aria-label={`Select ${file.name}`}
                        className="shrink-0"
                      />
                    )}
                    <Link
                      to="/files/$fileId"
                      params={{ fileId: file.id }}
                      className="flex min-w-0 flex-1 items-center gap-3 hover:text-primary"
                    >
                      <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-secondary text-muted-foreground">
                        {fileTypeIconElement(file.mime_type, { className: "size-4" })}
                      </span>
                      <div className="min-w-0">
                        <p className="truncate font-medium">{file.name}</p>
                        <p className="truncate text-xs text-muted-foreground">{file.path}</p>
                      </div>
                    </Link>
                    <div className="shrink-0 text-right text-xs text-muted-foreground">
                      <p>{formatBytes(file.size_bytes ?? 0)}</p>
                      {date && (
                        <p>
                          {dateLabel} {formatRelativeTime(date)}
                        </p>
                      )}
                    </div>
                    {canManage && (
                      <div className="flex shrink-0 items-center gap-1.5">
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={createArchiveMutation.isPending}
                          onClick={() => createArchiveMutation.mutate([file.id])}
                        >
                          <FolderArchive className="size-4" /> Create Archive
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={archiveMutation.isPending}
                          onClick={() => archiveMutation.mutate([file.id])}
                        >
                          <Archive className="size-4" /> Archive
                        </Button>
                      </div>
                    )}
                  </div>
                );
              })}
            </Card>
          </>
        )}

        {total > PAGE_SIZE && (
          <div className="flex items-center justify-between text-sm text-muted-foreground">
            <span>
              Showing {page * PAGE_SIZE + 1}–{Math.min(total, (page + 1) * PAGE_SIZE)} of{" "}
              {total.toLocaleString()}
            </span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page === 0}
                onClick={() => changePage(Math.max(0, page - 1))}
              >
                <ChevronLeft className="size-4" /> Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={(page + 1) * PAGE_SIZE >= total}
                onClick={() => changePage(page + 1)}
              >
                Next <ChevronRight className="size-4" />
              </Button>
            </div>
          </div>
        )}

        <p className="text-xs text-muted-foreground">
          Archiving moves a file to Google Drive&rsquo;s Trash immediately — always reversible from
          there.
        </p>
      </div>
    </AppShell>
  );
}
