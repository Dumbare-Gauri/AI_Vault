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

const CONFIRM_PHRASE = "DELETE";

interface PermanentDeleteDialogProps {
  onOpenChange: (open: boolean) => void;
  fileIds: string[];
  onDeleted?: () => void;
}

/** The one destructive action in this app that's genuinely unrecoverable
 * (real Drive `files.delete`, not Trash) — it runs immediately like every
 * other action, but only after this dialog's own type-to-confirm gate,
 * on the theory that an irreversible action deserves more friction than a
 * Trash/Archive one. Mounted only while open (see call site), same
 * conditional-mount pattern as RenameDialog/MoveDialog — resets
 * `confirmText` for free on each open. */
export function PermanentDeleteDialog({ onOpenChange, fileIds, onDeleted }: PermanentDeleteDialogProps) {
  const [confirmText, setConfirmText] = useState("");
  const queryClient = useQueryClient();

  const deleteMutation = useMutation({
    mutationFn: () =>
      apiClient.post<ExecutionPlan>("/v1/execution-plans/permanent-delete", { file_ids: fileIds }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["trash"] });
      void queryClient.invalidateQueries({ queryKey: ["execution-plans"] });
      toast.success(
        fileIds.length === 1 ? "Permanently deleting file now" : `Permanently deleting ${fileIds.length} files now`,
      );
      onOpenChange(false);
      onDeleted?.();
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't create a deletion plan.");
    },
  });

  const canSubmit = confirmText.trim().toUpperCase() === CONFIRM_PHRASE;

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
          <p className="text-muted-foreground">
            Type <strong className="text-foreground">{CONFIRM_PHRASE}</strong> to continue.
          </p>
          <Input
            autoFocus
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            placeholder={CONFIRM_PHRASE}
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
