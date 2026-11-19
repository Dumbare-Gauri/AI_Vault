import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { ArchiveJobDetail, ExecutionPlan } from "@vault/types";
import { Archive, ChevronLeft, Download, ExternalLink, ShieldCheck, Trash2 } from "lucide-react";
import { useState } from "react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { trackAction } from "@/lib/storage-action";
import { archiveStatusBadgeVariant, archiveStatusLabel } from "@/lib/archive-style";
import { downloadFile } from "@/lib/download-file";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/archives/$archiveId")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: ArchiveDetailPage,
});

function compressionNote(original: number | null, compressed: number | null): string | null {
  if (original === null || compressed === null || original <= 0) return null;
  const saved = original - compressed;
  if (saved <= 0) return "No space saved — zip overhead outweighs savings for files this small or already compressed";
  return `${formatBytes(saved)} smaller (${Math.round((saved / original) * 100)}%)`;
}

function ArchiveDetailPage() {
  const { archiveId } = Route.useParams();
  const navigate = Route.useNavigate();
  const queryClient = useQueryClient();
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const archiveQuery = useQuery({
    queryKey: ["archives", archiveId],
    queryFn: () => apiClient.get<ArchiveJobDetail>(`/v1/archives/${archiveId}`),
  });

  const downloadMutation = useMutation({
    mutationFn: (archive: ArchiveJobDetail) =>
      downloadFile(`/v1/archives/${archive.id}/download`, `${archive.name}.zip`),
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't download this archive.");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => apiClient.delete(`/v1/archives/${archiveId}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["archives"] });
      toast.success("Archive deleted.");
      void navigate({ to: "/archives" });
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't delete this archive.");
    },
  });

  const removeOriginalsMutation = useMutation({
    mutationFn: (archive: ArchiveJobDetail) =>
      apiClient.post<ExecutionPlan>("/v1/execution-plans", {
        file_ids: archive.manifest.map((entry) => entry.file_id),
        action_type: "archive",
      }),
    onSuccess: (plan) => {
      void trackAction(plan.id, "Moving originals to Trash…");
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't start — nothing was changed.");
    },
  });

  const archive = archiveQuery.data;

  return (
    <AppShell title="Archive">
      <div className="mx-auto flex max-w-3xl flex-col gap-4">
        <Link
          to="/archives"
          className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ChevronLeft className="size-4" /> Back to archives
        </Link>

        {archiveQuery.isLoading && <Skeleton className="h-64 rounded-2xl" />}
        {archiveQuery.isError && <p className="text-sm text-destructive">Couldn&rsquo;t load this archive.</p>}

        {archive && (
          <>
            <Card clay className="p-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="flex items-center gap-3">
                  <span className="flex size-10 items-center justify-center rounded-lg bg-secondary text-muted-foreground">
                    <Archive className="size-5" />
                  </span>
                  <div>
                    <h1 className="text-lg font-semibold">{archive.name}</h1>
                    <p className="text-sm text-muted-foreground">
                      Created {formatRelativeTime(archive.created_at)}
                    </p>
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {archive.verified_at && (
                    <Badge variant="success">
                      <ShieldCheck className="size-3" /> Verified in Google Drive
                    </Badge>
                  )}
                  <Badge variant={archiveStatusBadgeVariant(archive.status)}>
                    {archiveStatusLabel(archive.status)}
                  </Badge>
                </div>
              </div>

              {archive.destination_path && (
                <p className="mt-4 text-sm">
                  <span className="text-muted-foreground">Saved to </span>
                  <span className="font-medium">{archive.destination_path}</span>
                </p>
              )}

              <div className="mt-5 grid grid-cols-2 gap-4 sm:grid-cols-4">
                <div>
                  <p className="text-xs text-muted-foreground">Original size</p>
                  <p className="text-sm font-medium">
                    {archive.original_size_bytes !== null ? formatBytes(archive.original_size_bytes) : "—"}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-muted-foreground">Compressed size</p>
                  <p className="text-sm font-medium">
                    {archive.compressed_size_bytes !== null
                      ? formatBytes(archive.compressed_size_bytes)
                      : "—"}
                  </p>
                  {compressionNote(archive.original_size_bytes, archive.compressed_size_bytes) && (
                    <p className="text-xs text-muted-foreground">
                      {compressionNote(archive.original_size_bytes, archive.compressed_size_bytes)}
                    </p>
                  )}
                </div>
                <div>
                  <p className="text-xs text-muted-foreground">Files</p>
                  <p className="text-sm font-medium">{archive.file_count}</p>
                </div>
                <div>
                  <p className="text-xs text-muted-foreground">Originals removed</p>
                  <p className="text-sm font-medium">
                    {archive.originals_removed_count} of {archive.file_count}
                  </p>
                </div>
              </div>

              <div className="mt-5 flex flex-wrap items-center gap-2">
                {archive.destination_web_view_link && (
                  <Button asChild variant="outline">
                    <a
                      href={archive.destination_web_view_link}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      <ExternalLink className="size-4" /> Open in Google Drive
                    </a>
                  </Button>
                )}
                <Button
                  variant="outline"
                  disabled={
                    archive.status !== "completed" ||
                    !archive.object_storage_key ||
                    downloadMutation.isPending
                  }
                  onClick={() => downloadMutation.mutate(archive)}
                >
                  <Download className="size-4" />
                  {downloadMutation.isPending ? "Downloading…" : "Download Archive"}
                </Button>
                {canManage && archive.originals_removed_count < archive.file_count && (
                  <Button
                    variant="outline"
                    disabled={
                      archive.status !== "completed" ||
                      !archive.verified_at ||
                      removeOriginalsMutation.isPending
                    }
                    onClick={() => removeOriginalsMutation.mutate(archive)}
                  >
                    {removeOriginalsMutation.isPending ? "Starting…" : "Remove Originals"}
                  </Button>
                )}
                {canManage && (
                  <Button
                    variant="destructive"
                    disabled={archive.status === "deleted"}
                    onClick={() => setConfirmingDelete(true)}
                  >
                    <Trash2 className="size-4" />
                    Remove from AI Vault
                  </Button>
                )}
              </div>
              {canManage &&
                archive.status === "completed" &&
                archive.originals_removed_count < archive.file_count && (
                <p className="mt-3 text-xs text-muted-foreground">
                  Creating this archive only made a backup — it hasn&rsquo;t freed any storage yet.
                  Click <strong>Remove Originals</strong> to move the {archive.file_count} original
                  files to Google Drive&rsquo;s Trash and reclaim the space.
                </p>
              )}
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Contents ({archive.manifest.length})</CardTitle>
              </CardHeader>
              <CardContent>
                {archive.manifest.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No files recorded for this archive.</p>
                ) : (
                  <Table>
                    <TableBody>
                      {archive.manifest.map((entry) => (
                        <TableRow key={entry.file_id}>
                          <TableCell>
                            <span className="block truncate font-medium text-foreground/90">
                              {entry.name}
                            </span>
                            <span className="block truncate text-xs text-muted-foreground">
                              {entry.path}
                            </span>
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-right text-muted-foreground">
                            {formatBytes(entry.size_bytes)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          </>
        )}
      </div>

      <Dialog open={confirmingDelete} onOpenChange={setConfirmingDelete}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove this archive from AI Vault?</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            This removes the archive from AI Vault and deletes AI Vault&rsquo;s cached copy. The
            zip saved in your Google Drive stays where it is — delete it there if you no longer
            need it. Your original files are not affected.
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmingDelete(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={deleteMutation.isPending}
              onClick={() => deleteMutation.mutate()}
            >
              {deleteMutation.isPending ? "Removing…" : "Remove"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </AppShell>
  );
}
