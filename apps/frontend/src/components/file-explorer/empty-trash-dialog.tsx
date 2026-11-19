import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { EmptyTrashStarted, EmptyTrashStatus, TrashSummary } from "@vault/types";
import { AlertTriangle, CheckCircle2, Loader2, ShieldCheck, Trash2 } from "lucide-react";
import { useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { formatBytes } from "@/lib/format-bytes";

const POLL_MS = 1500;

interface EmptyTrashDialogProps {
  connectorId: string;
  /** Whose Trash this is, as words: "Google Drive" or "this computer". */
  storage?: string;
  onOpenChange: (open: boolean) => void;
}

/** Permanently empties a connected storage's Trash. Shows exactly what will
 * be deleted (read live from the storage) and needs the user's explicit
 * acknowledgement — a box that starts unticked on every open, because the
 * dialog is mounted only while open. The deletion runs in the background:
 * the dialog follows it and reports what the storage confirmed, naming any
 * file that could not be deleted. */
export function EmptyTrashDialog({
  connectorId,
  onOpenChange,
  storage = "Google Drive",
}: EmptyTrashDialogProps) {
  const [understood, setUnderstood] = useState(false);
  const [operationId, setOperationId] = useState<string | null>(null);
  // Set synchronously on the first click, before React re-renders the
  // disabled button — a double click must never send a second deletion.
  const submitted = useRef(false);
  const queryClient = useQueryClient();

  const previewQuery = useQuery({
    queryKey: ["provider-trash", connectorId],
    queryFn: () => apiClient.get<TrashSummary>(`/v1/connectors/${connectorId}/provider-trash`),
    staleTime: 0,
    enabled: operationId === null,
  });

  const startMutation = useMutation({
    mutationFn: (expectedCount: number) =>
      apiClient.post<EmptyTrashStarted>(`/v1/connectors/${connectorId}/provider-trash/empty`, {
        expected_count: expectedCount,
        confirmed: true,
      }),
    onSuccess: (started) => setOperationId(started.operation_id),
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : `Couldn't empty ${storage}'s Trash.`);
      submitted.current = false;
      setUnderstood(false);
      void previewQuery.refetch();
    },
  });

  const statusQuery = useQuery({
    queryKey: ["provider-trash-empty", connectorId, operationId],
    queryFn: async () => {
      const status = await apiClient.get<EmptyTrashStatus>(
        `/v1/connectors/${connectorId}/provider-trash/empty/${operationId}`,
      );
      if (status.status === "done" && status.summary) announce(status.summary);
      if (status.status !== "running") void queryClient.invalidateQueries({ queryKey: ["files"] });
      return status;
    },
    enabled: operationId !== null,
    refetchInterval: (query) => (query.state.data?.status === "running" || !query.state.data ? POLL_MS : false),
  });

  function announce(emptied: TrashSummary) {
    const failed = emptied.not_deleted?.length ?? 0;
    if (emptied.file_count === 0) {
      toast.success(`${storage}'s Trash is already empty.`);
    } else if (failed > 0 || emptied.still_deleting_count > 0) {
      toast.warning(
        `${(emptied.file_count - emptied.still_deleting_count).toLocaleString()} of ${emptied.file_count.toLocaleString()} files permanently deleted from ${storage}'s Trash`,
        { description: `${emptied.still_deleting_count.toLocaleString()} are still there — see the list.` },
      );
    } else {
      toast.success(
        `${storage}'s Trash is empty — ${emptied.file_count.toLocaleString()} files permanently deleted, ${formatBytes(emptied.total_bytes)} freed`,
        { description: `Confirmed with ${storage}.` },
      );
    }
  }

  const preview = previewQuery.data;
  const status = statusQuery.data;
  const running = startMutation.isPending || (operationId !== null && (!status || status.status === "running"));
  const finished = status && status.status !== "running" ? status : null;
  const canSubmit = preview !== undefined && preview.file_count > 0 && understood && !running && operationId === null;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-destructive">
            <Trash2 className="size-5" /> Empty {storage}&rsquo;s Trash
          </DialogTitle>
          <DialogDescription>
            Moving to Trash can be undone; this can&rsquo;t. Everything in {storage}&rsquo;s Trash is
            deleted permanently.
          </DialogDescription>
        </DialogHeader>

        {operationId === null && previewQuery.isLoading && <Skeleton className="h-40 rounded-lg" />}
        {operationId === null && previewQuery.isError && (
          <p className="text-sm text-destructive">
            Couldn&rsquo;t read {storage}&rsquo;s Trash. Close this and try again.
          </p>
        )}

        {preview && preview.file_count === 0 && operationId === null && (
          <p className="text-sm text-muted-foreground">{storage}&rsquo;s Trash is already empty.</p>
        )}

        {preview && preview.file_count > 0 && !finished && (
          <div className="flex flex-col gap-3">
            <p className="text-sm">
              <span className="font-semibold">{preview.file_count.toLocaleString()} files</span> will be
              deleted, freeing <span className="font-semibold">{formatBytes(preview.total_bytes)}</span>.
            </p>

            {preview.not_backed_up_count > 0 && (
              <div className="flex items-start gap-2 rounded-lg bg-destructive-muted p-3 text-sm text-destructive">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                <span>
                  {preview.not_backed_up_count === preview.file_count
                    ? "None of these files has"
                    : `${preview.not_backed_up_count} of these files ${preview.not_backed_up_count === 1 ? "has" : "have"} no`}{" "}
                  AI Vault archive. Once deleted, they&rsquo;re gone for good.
                </span>
              </div>
            )}

            <ul className="max-h-56 divide-y divide-border overflow-y-auto rounded-lg border border-border text-sm">
              {preview.largest.map((item, index) => (
                <li key={`${item.name}-${index}`} className="flex items-center gap-3 px-3 py-2">
                  <span className="min-w-0 flex-1 truncate">{item.name}</span>
                  {item.backed_up && (
                    <Badge variant="success" className="shrink-0">
                      <ShieldCheck className="size-3" /> Archived
                    </Badge>
                  )}
                  <span className="shrink-0 text-xs text-muted-foreground">{formatBytes(item.size_bytes)}</span>
                </li>
              ))}
            </ul>
            {preview.file_count > preview.largest.length && (
              <p className="text-xs text-muted-foreground">
                …and {(preview.file_count - preview.largest.length).toLocaleString()} smaller files.
              </p>
            )}

            {running ? (
              <div role="status" className="flex items-center gap-3 rounded-lg border border-border bg-secondary/60 p-3 text-sm">
                <Loader2 className="size-4 shrink-0 animate-spin" />
                <span>
                  Permanently deleting {preview.file_count.toLocaleString()} files and confirming with {storage}… A
                  large Trash can take a minute. You can close this — it carries on.
                </span>
              </div>
            ) : (
              <label htmlFor="empty-trash-understood" className="flex cursor-pointer items-start gap-3 rounded-lg border border-border p-3 text-sm">
                <Checkbox
                  id="empty-trash-understood"
                  checked={understood}
                  onCheckedChange={(value) => setUnderstood(value === true)}
                  className="mt-0.5"
                  aria-label="I understand these files will be permanently deleted and cannot be recovered."
                />
                <span aria-hidden>I understand these files will be permanently deleted and cannot be recovered.</span>
              </label>
            )}
          </div>
        )}

        {finished?.status === "failed" && (
          <p role="alert" className="text-sm text-destructive">
            {finished.error ?? `Couldn't empty ${storage}'s Trash.`} Nothing more was deleted.
          </p>
        )}

        {finished?.status === "done" && finished.summary && (
          <div className="flex flex-col gap-3 text-sm">
            <p className="flex items-center gap-2 font-medium">
              <CheckCircle2 className="size-4 text-success" />
              {(finished.summary.file_count - finished.summary.still_deleting_count).toLocaleString()} of{" "}
              {finished.summary.file_count.toLocaleString()} files permanently deleted — confirmed with {storage}.
            </p>
            {(finished.summary.not_deleted?.length ?? 0) > 0 && (
              <>
                <p className="text-muted-foreground">These were still in the Trash when AI Vault checked:</p>
                <ul className="max-h-48 divide-y divide-border overflow-y-auto rounded-lg border border-border">
                  {finished.summary.not_deleted?.map((item, index) => (
                    <li key={`${item.name}-${index}`} className="flex items-center gap-3 px-3 py-2">
                      <span className="min-w-0 flex-1 truncate">{item.name}</span>
                      <span className="shrink-0 text-xs text-muted-foreground">{formatBytes(item.size_bytes)}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {finished || running ? "Close" : "Cancel"}
          </Button>
          {!finished && (
            <Button
              variant="destructive"
              disabled={!canSubmit}
              onClick={() => {
                if (!preview || submitted.current) return;
                submitted.current = true;
                startMutation.mutate(preview.file_count);
              }}
            >
              {running ? (
                <>
                  <Loader2 className="size-4 animate-spin" /> Deleting…
                </>
              ) : (
                "Delete permanently"
              )}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
