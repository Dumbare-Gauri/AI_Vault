import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { TrashSummary } from "@vault/types";
import { AlertTriangle, ShieldCheck, Trash2 } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { formatBytes } from "@/lib/format-bytes";

// The server also requires this phrase, so a request can only come from this dialog.
const CONFIRM_PHRASE = "EMPTY TRASH";

interface EmptyTrashDialogProps {
  connectorId: string;
  /** Whose Trash this is, as words: "Google Drive" or "this computer". */
  storage?: string;
  onOpenChange: (open: boolean) => void;
}

/** Permanently empties a connected storage's Trash. Shows exactly what
 * will be deleted (read live from Drive) and requires an explicit acknowledgement.
 * Mounted only while open, so the confirmation starts unticked. */
export function EmptyTrashDialog({
  connectorId,
  onOpenChange,
  storage = "Google Drive",
}: EmptyTrashDialogProps) {
  const [acknowledged, setAcknowledged] = useState(false);
  const queryClient = useQueryClient();

  const previewQuery = useQuery({
    queryKey: ["provider-trash", connectorId],
    queryFn: () => apiClient.get<TrashSummary>(`/v1/connectors/${connectorId}/provider-trash`),
    staleTime: 0,
  });

  const emptyMutation = useMutation({
    mutationFn: (expectedCount: number) =>
      apiClient.post<TrashSummary>(`/v1/connectors/${connectorId}/provider-trash/empty`, {
        expected_count: expectedCount,
        confirmation: CONFIRM_PHRASE,
      }),
    onSuccess: (emptied) => {
      if (emptied.file_count === 0) {
        toast.success(`${storage}'s Trash is already empty.`);
      } else if (emptied.still_deleting_count > 0) {
        toast.success(`Permanently deleting ${emptied.file_count} files from ${storage}'s Trash`, {
          description: `${emptied.still_deleting_count} are still being removed — your storage updates within a few minutes.`,
        });
      } else {
        toast.success(
          `${storage}'s Trash is empty — ${emptied.file_count} files permanently deleted, ${formatBytes(emptied.total_bytes)} freed`,
          { description: `Confirmed with ${storage}.` },
        );
      }
      void queryClient.invalidateQueries();
      onOpenChange(false);
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't empty Drive's Trash.");
      void previewQuery.refetch();
    },
  });

  const preview = previewQuery.data;
  const canSubmit =
    preview !== undefined &&
    preview.file_count > 0 &&
    acknowledged &&
    !emptyMutation.isPending;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-destructive">
            <Trash2 className="size-5" /> Empty {storage}&rsquo;s Trash
          </DialogTitle>
          <DialogDescription>
            Everything in {storage}&rsquo;s Trash is deleted permanently. This can&rsquo;t be
            undone.
          </DialogDescription>
        </DialogHeader>

        {previewQuery.isLoading && <Skeleton className="h-40 rounded-lg" />}
        {previewQuery.isError && (
          <p className="text-sm text-destructive">
            Couldn&rsquo;t read {storage}&rsquo;s Trash. Close this and try again.
          </p>
        )}

        {preview && preview.file_count === 0 && (
          <p className="text-sm text-muted-foreground">{storage}&rsquo;s Trash is already empty.</p>
        )}

        {preview && preview.file_count > 0 && (
          <div className="flex flex-col gap-3">
            <p className="text-sm">
              <span className="font-semibold">{preview.file_count.toLocaleString()} files</span>{" "}
              will be deleted, freeing{" "}
              <span className="font-semibold">{formatBytes(preview.total_bytes)}</span>.
            </p>

            {preview.not_backed_up_count > 0 && (
              <div className="flex items-start gap-2 rounded-lg bg-destructive-muted p-3 text-sm text-destructive">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                <span>
                  {preview.not_backed_up_count === preview.file_count
                    ? "None of these files"
                    : `${preview.not_backed_up_count} of these files`}{" "}
                  {preview.not_backed_up_count === 1 && preview.file_count !== 1 ? "has" : "have"}{" "}
                  an AI Vault archive. Once deleted, they&rsquo;re gone for good.
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
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {formatBytes(item.size_bytes)}
                  </span>
                </li>
              ))}
            </ul>
            {preview.file_count > preview.largest.length && (
              <p className="text-xs text-muted-foreground">
                …and {(preview.file_count - preview.largest.length).toLocaleString()} smaller files.
              </p>
            )}

            <label
              htmlFor="empty-trash-confirm"
              className="flex cursor-pointer items-start gap-3 rounded-lg border border-border p-3 text-sm"
            >
              <Checkbox
                id="empty-trash-confirm"
                checked={acknowledged}
                onCheckedChange={(checked) => setAcknowledged(checked === true)}
                className="mt-0.5"
              />
              <span>
                I understand these {preview.file_count.toLocaleString()} files will be permanently
                deleted and can&rsquo;t be recovered.
              </span>
            </label>
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={!canSubmit}
            onClick={() => preview && emptyMutation.mutate(preview.file_count)}
          >
            {emptyMutation.isPending ? "Emptying…" : "Empty Trash permanently"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
