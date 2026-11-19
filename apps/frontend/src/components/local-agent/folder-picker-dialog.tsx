import { type UseQueryResult, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { AgentBrowseResult, AgentVolume, LocalAgent } from "@vault/types";
import { ArrowLeft, Check, ChevronRight, Folder, FolderPlus, HardDrive, Usb } from "lucide-react";
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
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { formatBytes } from "@/lib/format-bytes";

const EXTERNAL_KINDS = new Set(["removable", "external"]);

function driveName(volume: AgentVolume) {
  const kind = EXTERNAL_KINDS.has(volume.kind) ? "External drive" : volume.kind === "network" ? "Network drive" : "Drive";
  return volume.label ? `${volume.label} (${volume.mount})` : `${kind} ${volume.mount}`;
}

function DriveList({ volumes, onOpen }: { volumes: AgentVolume[]; onOpen: (path: string) => void }) {
  if (volumes.length === 0) {
    return <p className="py-6 text-center text-sm text-muted-foreground">The agent hasn&rsquo;t reported any drives yet.</p>;
  }
  return (
    <ul className="flex flex-col gap-2">
      {volumes.map((volume) => {
        const external = EXTERNAL_KINDS.has(volume.kind);
        const Icon = external ? Usb : HardDrive;
        return (
          <li key={volume.mount}>
            <button
              type="button"
              onClick={() => onOpen(volume.mount)}
              className="flex w-full items-center gap-3 rounded-lg border border-border p-3 text-left hover:bg-secondary"
            >
              <Icon className="size-5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{driveName(volume)}</span>
                <span className="block text-xs text-muted-foreground">
                  {formatBytes(volume.free_bytes)} free of {formatBytes(volume.total_bytes)}
                </span>
              </span>
              {external && <Badge variant="outline">External</Badge>}
              <ChevronRight className="size-4 text-muted-foreground" />
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function FolderList({
  browse,
  onOpen,
  onAdd,
  adding,
}: {
  browse: UseQueryResult<AgentBrowseResult>;
  onOpen: (path: string) => void;
  onAdd: (path: string) => void;
  adding: boolean;
}) {
  if (browse.isPending) {
    return (
      <div className="flex flex-col gap-2">
        {[0, 1, 2, 3].map((row) => (
          <Skeleton key={row} className="h-10 w-full" />
        ))}
      </div>
    );
  }
  if (browse.isError) {
    return (
      <p className="py-6 text-center text-sm text-destructive">
        {browse.error instanceof ApiError ? browse.error.message : "Couldn't open that folder."}
      </p>
    );
  }
  const { folders, truncated } = browse.data;
  if (folders.length === 0) {
    return <p className="py-6 text-center text-sm text-muted-foreground">No folders inside.</p>;
  }
  return (
    <>
      <ul className="flex max-h-80 flex-col overflow-y-auto rounded-lg border border-border">
        {folders.map((folder) => (
          <li key={folder.path} className="flex items-center gap-2 border-b border-border px-2 py-1 last:border-b-0">
            <button
              type="button"
              onClick={() => onOpen(folder.path)}
              className="flex min-w-0 flex-1 items-center gap-2 rounded px-1 py-1.5 text-left text-sm hover:bg-secondary"
            >
              <Folder className="size-4 shrink-0 text-muted-foreground" />
              <span className="truncate">{folder.name}</span>
            </button>
            {folder.added ? (
              <Badge variant="outline">
                <Check className="size-3" /> Added
              </Badge>
            ) : (
              <Button
                size="sm"
                variant="ghost"
                disabled={!folder.can_add || adding}
                title={folder.reason ?? `Give the agent ${folder.name}`}
                onClick={() => onAdd(folder.path)}
              >
                <FolderPlus className="size-4" /> Add
              </Button>
            )}
          </li>
        ))}
      </ul>
      {truncated && <p className="text-xs text-muted-foreground">Showing the first 1,000 folders.</p>}
    </>
  );
}

/** Picks a folder or a whole external drive on the connected computer and
 * gives it to the agent. The agent checks every choice against its own safety
 * rules; system folders and the whole system drive are never accepted. */
export function FolderPickerDialog({
  agent,
  onOpenChange,
}: {
  agent: LocalAgent;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [path, setPath] = useState<string | null>(null);
  const browseQuery = useQuery({
    queryKey: ["local-agent-browse", path],
    queryFn: () => apiClient.get<AgentBrowseResult>(`/v1/local-agents/browse?path=${encodeURIComponent(path ?? "")}`),
    enabled: path !== null,
    retry: false,
  });
  const browse = browseQuery.data;

  const addMutation = useMutation({
    mutationFn: (folder: string) => apiClient.post<{ roots: string[] }>("/v1/local-agents/roots", { path: folder }),
    onSuccess: (_, folder) => {
      toast.success(`Added ${folder} — scanning it now`);
      void queryClient.invalidateQueries({ queryKey: ["local-agents"] });
      void queryClient.invalidateQueries({ queryKey: ["connectors"] });
      void queryClient.invalidateQueries({ queryKey: ["files"] });
      onOpenChange(false);
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't add that folder.");
    },
  });

  const goUp = () => setPath(browse?.parent && browse.parent !== path ? browse.parent : null);

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <FolderPlus className="size-5" /> Add a folder or drive
          </DialogTitle>
          <DialogDescription>
            Choose what AI Vault may organize on {agent.device_name ?? "this computer"}. Only folder names are shown
            here; the agent never works outside what you add.
          </DialogDescription>
        </DialogHeader>

        <div className="flex items-center gap-2 text-sm">
          {path !== null && (
            <Button size="sm" variant="ghost" onClick={goUp} aria-label="Up one folder">
              <ArrowLeft className="size-4" />
            </Button>
          )}
          <span className="min-w-0 truncate font-medium">{path ?? "Drives"}</span>
        </div>

        {path === null ? (
          <DriveList volumes={agent.volumes} onOpen={setPath} />
        ) : (
          <FolderList browse={browseQuery} onOpen={setPath} onAdd={(folder) => addMutation.mutate(folder)} adding={addMutation.isPending} />
        )}

        <DialogFooter className="items-center gap-2">
          {path !== null && browse && !browse.can_add && browse.reason && (
            <p className="mr-auto text-xs text-muted-foreground">{browse.reason}</p>
          )}
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          {path !== null && (
            <Button disabled={!browse?.can_add || addMutation.isPending} onClick={() => addMutation.mutate(path)}>
              <FolderPlus className="size-4" />
              {addMutation.isPending ? "Adding…" : "Add this folder"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
