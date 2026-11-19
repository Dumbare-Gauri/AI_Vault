import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { CreatedItem } from "@vault/types";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";

export type CreateItemKind = "folder" | "text" | "markdown";

interface CreateItemDialogProps {
  kind: CreateItemKind;
  connectorId: string;
  /** Where to create it: the folder being viewed, or — at the top of an
   * authorized folder on this computer — that top-level folder. */
  parentFolderId?: string | null;
  sourceId?: string | null;
  storageLabel?: string;
  onOpenChange: (open: boolean) => void;
}

const TITLES: Record<CreateItemKind, string> = {
  folder: "New folder",
  text: "New text file",
  markdown: "New Markdown note",
};

const EXTENSIONS: Record<CreateItemKind, string> = { folder: "", text: ".txt", markdown: ".md" };

/** Creates the item directly in Google Drive and waits for the provider to
 * confirm it before reporting success. Mounted only while open. */
export function CreateItemDialog({
  kind,
  connectorId,
  parentFolderId = null,
  sourceId = null,
  storageLabel = "your storage",
  onOpenChange,
}: CreateItemDialogProps) {
  const [name, setName] = useState("");
  const [content, setContent] = useState("");
  const queryClient = useQueryClient();
  const isFile = kind !== "folder";

  const fullName = (() => {
    const trimmed = name.trim();
    if (!isFile || trimmed === "" || trimmed.toLowerCase().endsWith(EXTENSIONS[kind])) return trimmed;
    return `${trimmed}${EXTENSIONS[kind]}`;
  })();

  const createMutation = useMutation({
    mutationFn: () =>
      isFile
        ? apiClient.post<CreatedItem>(`/v1/connectors/${connectorId}/text-files`, {
            name: fullName,
            content,
            format: kind,
            parent_folder_id: parentFolderId,
            source_id: parentFolderId ? null : sourceId,
          })
        : apiClient.post<CreatedItem>(`/v1/connectors/${connectorId}/folders`, {
            name: fullName,
            parent_folder_id: parentFolderId,
            source_id: parentFolderId ? null : sourceId,
          }),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ["files"] });
      toast.success(`"${created.name}" created in ${storageLabel} — confirmed by the storage`, {
        action: created.web_view_link
          ? {
              label: "Open",
              onClick: () => window.open(created.web_view_link ?? "", "_blank", "noopener"),
            }
          : undefined,
      });
      onOpenChange(false);
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't create it — try again.");
    },
  });

  const canSubmit = fullName.length > 0 && !createMutation.isPending;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{TITLES[kind]}</DialogTitle>
          <DialogDescription>Created at the top of your Google Drive.</DialogDescription>
        </DialogHeader>
        <label className="text-sm font-medium" htmlFor="create-item-name">
          Name
        </label>
        <Input
          id="create-item-name"
          autoFocus
          value={name}
          placeholder={isFile ? `notes${EXTENSIONS[kind]}` : "Client work"}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !isFile && canSubmit) createMutation.mutate();
          }}
        />
        {isFile && (
          <>
            <label className="text-sm font-medium" htmlFor="create-item-content">
              Content
            </label>
            <textarea
              id="create-item-content"
              value={content}
              onChange={(event) => setContent(event.target.value)}
              rows={8}
              className="w-full rounded-md border border-input bg-card px-3 py-2 text-sm shadow-clay-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={!canSubmit} onClick={() => createMutation.mutate()}>
            {createMutation.isPending ? "Creating…" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
