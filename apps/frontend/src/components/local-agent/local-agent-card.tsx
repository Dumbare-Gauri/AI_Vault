import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { LocalAgent } from "@vault/types";
import { Copy, FolderPlus, HardDrive, Laptop, Power, Trash2, X } from "lucide-react";
import { useState } from "react";

import { EmptyTrashDialog } from "@/components/file-explorer/empty-trash-dialog";
import { FolderPickerDialog } from "@/components/local-agent/folder-picker-dialog";
import { AgentNotInstalledError, useStartAgent } from "@/components/local-agent/use-start-agent";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
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
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { cn } from "@/lib/utils";

const INSTALL_COMMAND = "py -m aivault_agent install";

export function useLocalAgents(fast = false) {
  return useQuery({
    queryKey: ["local-agents"],
    queryFn: () => apiClient.get<LocalAgent[]>("/v1/local-agents"),
    refetchInterval: fast ? 2_000 : 15_000,
  });
}

function copy(text: string) {
  void navigator.clipboard.writeText(text).then(() => toast.success("Copied"));
}

function InstallOnce() {
  return (
    <div className="flex flex-col gap-2 rounded-lg bg-warning-muted p-3 text-sm">
      <p className="font-medium">One-time setup on this computer</p>
      <p className="text-muted-foreground">
        The agent isn&rsquo;t installed here yet. Open PowerShell in AI Vault&rsquo;s <code>apps\local-agent</code>{" "}
        folder and run this once — from then on it starts with Windows and this button does everything:
      </p>
      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 truncate rounded bg-card px-2 py-1 text-xs">{INSTALL_COMMAND}</code>
        <Button size="sm" variant="outline" onClick={() => copy(INSTALL_COMMAND)}>
          <Copy className="size-3.5" />
        </Button>
      </div>
    </div>
  );
}

function startError(error: unknown) {
  if (error instanceof AgentNotInstalledError) return null;
  return error instanceof ApiError || error instanceof Error ? error.message : "Couldn't start the agent.";
}

/** Connects this computer with one click: starts the agent if needed, pairs
 * it with a fresh key behind the scenes, then opens the folder picker.
 * Mounted only while open. */
export function ConnectComputerDialog({ onOpenChange }: { onOpenChange: (open: boolean) => void }) {
  const [name, setName] = useState("My computer");
  const startAgent = useStartAgent();
  const agents = useLocalAgents(startAgent.isSuccess);
  const agent = agents.data?.find((item) => item.connector_id === startAgent.data?.connector_id);

  if (agent?.online) {
    return <FolderPickerDialog agent={agent} onOpenChange={onOpenChange} />;
  }
  const notInstalled = startAgent.error instanceof AgentNotInstalledError;
  const problem = startAgent.isError ? startError(startAgent.error) : null;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Laptop className="size-5" /> Connect this computer
          </DialogTitle>
          <DialogDescription>
            The AI Vault agent runs on your computer and only works inside the folders you choose. It connects out
            to AI Vault — nothing can reach into your machine.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3 text-sm">
          <label className="font-medium" htmlFor="agent-name">
            Name for this computer
          </label>
          <Input id="agent-name" value={name} onChange={(e) => setName(e.target.value)} />
          {notInstalled && <InstallOnce />}
          {problem && <p className="text-destructive">{problem}</p>}
          {startAgent.isSuccess && (
            <p className="text-muted-foreground">Connected — waiting for the agent to report in…</p>
          )}
        </div>

        <DialogFooter>
          <Button
            disabled={startAgent.isPending || startAgent.isSuccess || !name.trim()}
            onClick={() => startAgent.mutate({ name })}
          >
            <Power className="size-4" />
            {startAgent.isPending ? "Starting the agent…" : notInstalled ? "Try again" : "Connect this computer"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RemoveFolderDialog({ path, onOpenChange }: { path: string; onOpenChange: (open: boolean) => void }) {
  const queryClient = useQueryClient();
  const removeMutation = useMutation({
    mutationFn: () => apiClient.post<{ roots: string[] }>("/v1/local-agents/roots/remove", { path }),
    onSuccess: () => {
      toast.success(`Removed ${path}`);
      void queryClient.invalidateQueries({ queryKey: ["local-agents"] });
      void queryClient.invalidateQueries({ queryKey: ["connectors"] });
      void queryClient.invalidateQueries({ queryKey: ["files"] });
      onOpenChange(false);
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't remove that folder.");
    },
  });
  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Stop using this folder?</DialogTitle>
          <DialogDescription>
            AI Vault forgets <span className="font-medium text-foreground">{path}</span> and its files. Nothing on
            your computer is moved or deleted.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="destructive" disabled={removeMutation.isPending} onClick={() => removeMutation.mutate()}>
            {removeMutation.isPending ? "Removing…" : "Remove folder"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function FolderRow({
  path,
  offline,
  onRemove,
}: {
  path: string;
  offline: boolean;
  onRemove: (() => void) | null;
}) {
  return (
    <li className={cn("flex items-center gap-2", offline && "text-muted-foreground")}>
      <HardDrive className="size-3.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate">{path}</span>
      {offline && <Badge variant="outline">Drive not connected</Badge>}
      {onRemove && (
        <Button size="sm" variant="ghost" aria-label={`Remove ${path}`} onClick={onRemove}>
          <X className="size-3.5" />
        </Button>
      )}
    </li>
  );
}

export function LocalAgentCard({ agent, canManage }: { agent: LocalAgent; canManage: boolean }) {
  const [connecting, setConnecting] = useState(false);
  const [emptying, setEmptying] = useState(false);
  const [picking, setPicking] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const startAgent = useStartAgent();
  const canChange = canManage && agent.online;
  const folders = [
    ...agent.roots.map((path) => ({ path, offline: false })),
    ...agent.offline_roots.map((path) => ({ path, offline: true })),
  ];

  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex items-center gap-2 font-medium">
            <Laptop className="size-4 text-muted-foreground" />
            {agent.name ?? "This computer"}
          </p>
          <p className="text-sm text-muted-foreground">
            {agent.device_name ? `${agent.device_name} · ${agent.platform ?? ""}` : "Agent not started yet"}
          </p>
          <p className="text-xs text-muted-foreground">
            {agent.last_seen_at ? `Last seen ${formatRelativeTime(agent.last_seen_at)}` : "Never seen"}
          </p>
        </div>
        <span className="flex shrink-0 items-center gap-1.5 text-xs">
          <span className={cn("size-2 rounded-full", agent.online ? "bg-success" : "bg-muted-foreground")} />
          {agent.online ? "Agent online" : "Agent offline"}
        </span>
      </div>

      {folders.length > 0 ? (
        <ul className="mt-3 flex flex-col gap-1 text-sm">
          {folders.map((folder) => (
            <FolderRow
              key={folder.path}
              path={folder.path}
              offline={folder.offline}
              onRemove={canChange ? () => setRemoving(folder.path) : null}
            />
          ))}
        </ul>
      ) : (
        agent.online && <p className="mt-3 text-sm text-muted-foreground">No folders added yet.</p>
      )}
      {agent.volumes.length > 0 && (
        <p className="mt-2 text-xs text-muted-foreground">
          {agent.volumes
            .map((v) => `${v.mount} ${formatBytes(v.free_bytes)} free of ${formatBytes(v.total_bytes)}`)
            .join(" · ")}
        </p>
      )}

      {canManage && (
        <div className="mt-3 flex flex-wrap gap-2">
          {agent.online ? (
            <>
              <Button size="sm" onClick={() => setPicking(true)}>
                <FolderPlus className="size-4" /> Add folder or drive
              </Button>
              <Button variant="outline" size="sm" onClick={() => setEmptying(true)}>
                <Trash2 className="size-4" /> Empty agent Trash
              </Button>
            </>
          ) : (
            <Button
              size="sm"
              disabled={startAgent.isPending}
              onClick={() =>
                startAgent.mutate(
                  { name: agent.name ?? undefined },
                  {
                    onError: (error) => {
                      if (error instanceof AgentNotInstalledError) setConnecting(true);
                      else toast.error(startError(error) ?? "Couldn't start the agent.");
                    },
                  },
                )
              }
            >
              <Power className="size-4" /> {startAgent.isPending ? "Starting…" : "Start agent"}
            </Button>
          )}
        </div>
      )}
      {(picking || (startAgent.isSuccess && agent.online)) && (
        <FolderPickerDialog
          agent={agent}
          onOpenChange={(open) => {
            if (open) return;
            setPicking(false);
            startAgent.reset();
          }}
        />
      )}
      {removing && <RemoveFolderDialog path={removing} onOpenChange={(open) => !open && setRemoving(null)} />}
      {connecting && <ConnectComputerDialog onOpenChange={(open) => !open && setConnecting(false)} />}
      {emptying && (
        <EmptyTrashDialog
          connectorId={agent.connector_id}
          storage="this computer"
          onOpenChange={(open) => !open && setEmptying(false)}
        />
      )}
    </Card>
  );
}
