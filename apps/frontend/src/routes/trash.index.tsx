import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { Connector, ExecutionPlan, FileListResponse } from "@vault/types";
import {
  ChevronLeft,
  ChevronRight,
  Cloud,
  FolderArchive,
  RotateCcw,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useState } from "react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { EmptyState } from "@/components/ui/empty-state";
import { PermanentDeleteDialog } from "@/components/file-explorer/permanent-delete-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { runStorageAction, trackAction } from "@/lib/storage-action";
import { fileTypeIconElement } from "@/lib/file-icon";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/trash/")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: TrashPage,
});

const PAGE_SIZE = 50;

function TrashPage() {
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const queryClient = useQueryClient();
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";

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
      prev.size === archivedFiles.length ? new Set() : new Set(archivedFiles.map((f) => f.id)),
    );
  }

  const connectorsQuery = useQuery({
    queryKey: ["connectors"],
    queryFn: () => apiClient.get<Connector[]>("/v1/connectors"),
  });
  const connector = connectorsQuery.data?.find(
    (candidate) => candidate.provider === "google_workspace" && candidate.status === "connected",
  );

  const trashQuery = useQuery({
    queryKey: ["trash", connector?.id, page],
    queryFn: () =>
      apiClient.get<FileListResponse>(
        `/v1/connectors/${connector?.id}/trash?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`,
      ),
    enabled: connector !== undefined,
  });

  const createArchiveMutation = useMutation({
    mutationFn: (fileId: string) =>
      apiClient.post<ExecutionPlan>("/v1/execution-plans", {
        file_ids: [fileId],
        action_type: "create_archive",
      }),
    onSuccess: (plan) => {
      void queryClient.invalidateQueries({ queryKey: ["trash"] });
      void trackAction(plan.id, "Creating a backup…");
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't start archiving — nothing was changed.");
    },
  });

  const files = trashQuery.data?.items ?? [];
  const total = trashQuery.data?.total ?? 0;
  // Only an already-archived file is eligible for permanent deletion (the
  // backend enforces this too) — bulk-select never includes anything else.
  const archivedFiles = files.filter((f) => f.is_archived);
  const allSelected = archivedFiles.length > 0 && selected.size === archivedFiles.length;

  return (
    <AppShell title="Trash">
      <div className="mx-auto flex max-w-4xl flex-col gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Trash</h1>
          <p className="text-sm text-muted-foreground">
            Files already moved to Google Drive&rsquo;s Trash (via Archive or Remove Duplicate).
            Google keeps counting these against your storage quota until they&rsquo;re permanently
            deleted here or emptied from Drive&rsquo;s own Trash. A file must be backed up with
            Create Archive before it can be permanently deleted — nothing is ever deleted with no
            copy left anywhere.
          </p>
        </div>

        {connectorsQuery.isLoading && (
          <div className="flex flex-col gap-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-16 rounded-xl" />
            ))}
          </div>
        )}

        {!connectorsQuery.isLoading && connector === undefined && (
          <EmptyState
            icon={Cloud}
            title="No storage connected yet"
            description="Connect Google Workspace to see what's in Trash."
            action={
              <Button asChild>
                <Link to="/storage-connections">Connect Google Drive</Link>
              </Button>
            }
          />
        )}

        {connector && (
          <>
            {trashQuery.isLoading && (
              <div className="flex flex-col gap-2">
                {Array.from({ length: 6 }).map((_, i) => (
                  <Skeleton key={i} className="h-16 rounded-lg" />
                ))}
              </div>
            )}

            {trashQuery.isError && (
              <EmptyState title="Couldn't load Trash" description="Please try again." />
            )}

            {trashQuery.isSuccess && files.length === 0 && (
              <EmptyState
                icon={Trash2}
                title="Trash is empty"
                description="Nothing here — files you archive or remove as duplicates will show up in this list."
              />
            )}

            {files.length > 0 && (
              <>
                {canManage && archivedFiles.length > 0 && (
                  <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-secondary/40 px-3 py-2">
                    <label className="flex items-center gap-2 text-sm">
                      <Checkbox checked={allSelected} onCheckedChange={toggleAll} />
                      {selected.size > 0
                        ? `${selected.size} selected`
                        : `Select all ${archivedFiles.length} backed-up file${archivedFiles.length === 1 ? "" : "s"}`}
                    </label>
                    {selected.size > 0 && (
                      <div className="flex items-center gap-1.5">
                        <Button variant="destructive" size="sm" onClick={() => setConfirming(true)}>
                          <Trash2 className="size-4" />
                          Permanently delete {selected.size}
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
                          Clear
                        </Button>
                      </div>
                    )}
                  </div>
                )}

                <Card className="flex flex-col divide-y divide-border p-1">
                  {files.map((file) => (
                    <div
                      key={file.id}
                      className="flex items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-sm"
                    >
                      {canManage && file.is_archived && (
                        <Checkbox
                          checked={selected.has(file.id)}
                          onCheckedChange={() => toggle(file.id)}
                          aria-label={`Select ${file.name}`}
                          className="shrink-0"
                        />
                      )}
                      <div className="flex min-w-0 flex-1 items-center gap-3">
                        <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-secondary text-muted-foreground">
                          {fileTypeIconElement(file.mime_type, { className: "size-4" })}
                        </span>
                        <div className="min-w-0">
                          <p className="truncate font-medium">{file.name}</p>
                          <p className="truncate text-xs text-muted-foreground">{file.path}</p>
                        </div>
                        {file.is_archived ? (
                          <Badge variant="success" className="shrink-0">
                            <ShieldCheck className="size-3" /> Backed up
                          </Badge>
                        ) : (
                          <Badge variant="outline" className="shrink-0">
                            Not backed up
                          </Badge>
                        )}
                      </div>
                      <div className="shrink-0 text-right text-xs text-muted-foreground">
                        <p>{file.size_bytes !== null ? formatBytes(file.size_bytes) : "—"}</p>
                        {file.provider_modified_at && (
                          <p>Modified {formatRelativeTime(file.provider_modified_at)}</p>
                        )}
                      </div>
                      {canManage && (
                        <Button
                          variant="outline"
                          size="sm"
                          className="shrink-0"
                          onClick={() =>
                            void runStorageAction(
                              { file_ids: [file.id], action_type: "restore" },
                              `Restoring "${file.name}"…`,
                            )
                          }
                        >
                          <RotateCcw className="size-4" /> Restore
                        </Button>
                      )}
                      {canManage &&
                        (file.is_archived ? (
                          <Button
                            variant="outline"
                            size="sm"
                            className="shrink-0"
                            onClick={() => {
                              setSelected(new Set([file.id]));
                              setConfirming(true);
                            }}
                          >
                            <Trash2 className="size-4" /> Delete forever
                          </Button>
                        ) : (
                          <Button
                            variant="outline"
                            size="sm"
                            className="shrink-0"
                            disabled={createArchiveMutation.isPending}
                            onClick={() => createArchiveMutation.mutate(file.id)}
                          >
                            <FolderArchive className="size-4" /> Create Archive
                          </Button>
                        ))}
                    </div>
                  ))}
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
          </>
        )}
      </div>

      {confirming && selected.size > 0 && (
        <PermanentDeleteDialog
          onOpenChange={(open) => !open && setConfirming(false)}
          fileIds={Array.from(selected)}
          onDeleted={() => setSelected(new Set())}
        />
      )}
    </AppShell>
  );
}
