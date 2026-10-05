import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ExecutionPlan } from "@vault/types";
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
import { trackAction } from "@/lib/storage-action";

interface RenameDialogProps {
  onOpenChange: (open: boolean) => void;
  fileId: string;
  currentName: string;
  onRenamed?: () => void;
}

/** Mounted only while the rename dialog is open (see call sites) — a fresh
 * mount per open is what resets `name` back to `currentName` each time,
 * deliberately not a `useEffect` keyed on an `open` prop (that pattern
 * flagged "setState synchronously within an effect" and was rejected
 * earlier this project for the same reason on the multi-select page). */
export function RenameDialog({ onOpenChange, fileId, currentName, onRenamed }: RenameDialogProps) {
  const [name, setName] = useState(currentName);
  const queryClient = useQueryClient();

  const renameMutation = useMutation({
    mutationFn: () =>
      apiClient.post<ExecutionPlan>("/v1/execution-plans", {
        file_ids: [fileId],
        action_type: "rename",
        new_name: name.trim(),
      }),
    onSuccess: (plan) => {
      void queryClient.invalidateQueries({ queryKey: ["files"] });
      void trackAction(plan.id, `Renaming to "${name.trim()}"…`);
      onOpenChange(false);
      onRenamed?.();
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't rename this file.");
    },
  });

  const trimmed = name.trim();
  const canSubmit = trimmed.length > 0 && trimmed !== currentName;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Rename file</DialogTitle>
        </DialogHeader>
        <Input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && canSubmit && !renameMutation.isPending) {
              renameMutation.mutate();
            }
          }}
        />
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={!canSubmit || renameMutation.isPending} onClick={() => renameMutation.mutate()}>
            {renameMutation.isPending ? "Renaming…" : "Rename"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
