import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ExecutionPlan, FolderSummary } from "@vault/types";
import { Folder } from "lucide-react";
import { useEffect, useState } from "react";

import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";

interface MoveDialogProps {
  onOpenChange: (open: boolean) => void;
  connectorId: string;
  fileIds: string[];
  onMoved?: () => void;
}

/** Deliberately a flat name-search picker, not a folder-tree browser —
 * navigating INTO folders is a separate, out-of-scope piece of the file
 * explorer (see the Files browser's own flat listing). Good enough to pick
 * a known destination by name; reuses the Cmd+K palette's `Command`
 * primitive since the shape (typeahead → list → select) is identical.
 *
 * Mounted only while open (see call sites) — a fresh mount per open is
 * what resets `query` back to empty each time, deliberately not a
 * `useEffect` keyed on an `open` prop (flagged "setState synchronously
 * within an effect", same class of lint error hit earlier this project). */
export function MoveDialog({ onOpenChange, connectorId, fileIds, onMoved }: MoveDialogProps) {
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const queryClient = useQueryClient();

  useEffect(() => {
    const timeout = setTimeout(() => setDebouncedQuery(query), 250);
    return () => clearTimeout(timeout);
  }, [query]);

  const foldersQuery = useQuery({
    queryKey: ["folders", connectorId, debouncedQuery],
    queryFn: () =>
      apiClient.get<FolderSummary[]>(
        `/v1/connectors/${connectorId}/folders?query=${encodeURIComponent(debouncedQuery)}`,
      ),
  });

  const moveMutation = useMutation({
    mutationFn: (folder: FolderSummary) =>
      apiClient.post<ExecutionPlan>("/v1/execution-plans", {
        file_ids: fileIds,
        action_type: "move_file",
        new_parent_id: folder.provider_file_id,
      }),
    onSuccess: (_plan, folder) => {
      void queryClient.invalidateQueries({ queryKey: ["files"] });
      toast.success(`Moving to "${folder.name}" now`);
      onOpenChange(false);
      onMoved?.();
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't move to that folder.");
    },
  });

  return (
    <CommandDialog open onOpenChange={onOpenChange}>
      <CommandInput
        placeholder="Search for a destination folder…"
        value={query}
        onValueChange={setQuery}
      />
      <CommandList>
        {foldersQuery.isSuccess && foldersQuery.data.length === 0 && (
          <CommandEmpty>No matching folders.</CommandEmpty>
        )}
        <CommandGroup heading="Folders">
          {foldersQuery.data?.map((folder) => (
            <CommandItem
              key={folder.id}
              disabled={moveMutation.isPending}
              onSelect={() => moveMutation.mutate(folder)}
            >
              <Folder className="size-4 shrink-0 text-muted-foreground" />
              <div className="min-w-0">
                <p className="truncate">{folder.name}</p>
                <p className="truncate text-xs text-muted-foreground">{folder.path}</p>
              </div>
            </CommandItem>
          ))}
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}
