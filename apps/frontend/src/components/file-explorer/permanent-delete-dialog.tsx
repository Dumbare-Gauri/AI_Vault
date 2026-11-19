import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ExecutionPlan } from "@vault/types";
import { ShieldAlert } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { pluralFiles, trackAction } from "@/lib/storage-action";

/** The server checks this exact phrase too — it names the count, so a
 * confirmation for one file can't be replayed for many. */
function confirmPhrase(count: number): string {
  return `DELETE ${count} FILE${count === 1 ? "" : "S"}`;
}

interface PermanentDeleteDialogProps {
  onOpenChange: (open: boolean) => void;
  fileIds: string[];
  /** Names of the files being deleted, shown so the user sees exactly what goes. */
  fileNames?: string[];
  onDeleted?: () => void;
}

/** The one destructive action in this app that's genuinely unrecoverable
 * (real Drive `files.delete`, not Trash) — it runs immediately like every
 * other action, but only after this dialog's own type-to-confirm gate,
 * on the theory that an irreversible action deserves more friction than a
 * Trash/Archive one. Mounted only while open (see call site), same
 * conditional-mount pattern as RenameDialog/MoveDialog — resets
 * `confirmText` for free on each open. */
export function PermanentDeleteDialog({ onOpenChange, fileIds, fileNames = [], onDeleted }: PermanentDeleteDialogProps) {
  const [confirmText, setConfirmText] = useState("");
  const phrase = confirmPhrase(fileIds.length);
  const queryClient = useQueryClient();

  const deleteMutation = useMutation({
    mutationFn: () =>
      apiClient.post<ExecutionPlan>("/v1/execution-plans/permanent-delete", {
        file_ids: fileIds,
        confirmation: confirmText.trim(),
      }),
    onSuccess: (plan) => {
      void queryClient.invalidateQueries({ queryKey: ["trash"] });
      void trackAction(plan.id, `Permanently deleting ${pluralFiles(fileIds.length)}…`);
      onOpenChange(false);
      onDeleted?.();
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't delete — nothing was changed.");
    },
  });

  const canSubmit = confirmText.trim() === phrase;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-destructive">
            <ShieldAlert className="size-5" />
            Permanently delete {fileIds.length} {fileIds.length === 1 ? "file" : "files"}?
          </DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-3 text-sm">
          <p className="text-muted-foreground">
            This removes {fileIds.length === 1 ? "this file" : "these files"} from Google Drive
            entirely — not to Trash, gone for good. There is no undo, no rollback, and Vault has no
            way to recover it once this completes.
          </p>
          {fileNames.length > 0 && (
            <ul className="max-h-48 divide-y divide-border overflow-y-auto rounded-lg border border-border">
              {fileNames.map((name, index) => (
                <li key={`${name}-${index}`} className="truncate px-3 py-1.5">
                  {name}
                </li>
              ))}
            </ul>
          )}
          <p className="text-muted-foreground">
            Type <strong className="text-foreground">{phrase}</strong> to continue.
          </p>
          <Input
            autoFocus
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            placeholder={phrase}
            aria-label="Confirmation phrase"
          />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={!canSubmit || deleteMutation.isPending}
            onClick={() => deleteMutation.mutate()}
          >
            {deleteMutation.isPending ? "Sending…" : "Permanently delete"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
