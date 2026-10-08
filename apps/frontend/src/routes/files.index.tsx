import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { Connector, FileListResponse, FileSummary } from "@vault/types";
import {
  ArrowDownAZ,
  ChevronLeft,
  ChevronRight,
  Cloud,
  Download,
  FolderInput,
  LayoutGrid,
  List,
  PencilLine,
  Plus,
  Users,
} from "lucide-react";
import { useMemo, useState } from "react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { EmptyState } from "@/components/ui/empty-state";
import { type CreateItemKind, CreateItemDialog } from "@/components/file-explorer/create-item-dialog";
import { MoveDialog } from "@/components/file-explorer/move-dialog";
import { RenameDialog } from "@/components/file-explorer/rename-dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { downloadFile } from "@/lib/download-file";
import { fileTypeIconElement, fileTypeLabel } from "@/lib/file-icon";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/files/")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: FilesPage,
});

type SortKey = "name" | "size" | "modified";
type ViewMode = "list" | "grid";
type OwnershipFilter = "all" | "mine" | "shared";

const PAGE_SIZE = 50;

function readViewMode(): ViewMode {
  if (typeof window === "undefined") return "list";
  return window.localStorage.getItem("vault-file-view") === "grid" ? "grid" : "list";
}

function folderOf(path: string, name: string): string {
  const withoutFile = path.endsWith(name) ? path.slice(0, path.length - name.length) : path;
  const trimmed = withoutFile.replace(/\/+$/, "").replace(/^\/+/, "");
  return trimmed.length > 0 ? trimmed : "Root";
}

function useFileDownload(file: FileSummary) {
  return useMutation({
    mutationFn: () => downloadFile(`/v1/files/${file.id}/download`, file.name),
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't download this file.");
    },
  });
}

interface FileRowActionsProps {
  file: FileSummary;
  canManage: boolean;
  onRename: (file: FileSummary) => void;
  onMove: (file: FileSummary) => void;
  className?: string;
}

function FileRowActions({ file, canManage, onRename, onMove, className }: FileRowActionsProps) {
  const downloadMutation = useFileDownload(file);

  function stop<E extends { preventDefault: () => void; stopPropagation: () => void }>(
    e: E,
    action: () => void,
  ) {
    e.preventDefault();
    e.stopPropagation();
    action();
  }

  return (
    <div className={cn("flex shrink-0 items-center gap-0.5", className)}>
      {canManage && (
        <button
          type="button"
          aria-label={`Rename ${file.name}`}
          onClick={(e) => stop(e, () => onRename(file))}
          className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-card hover:text-foreground"
        >
          <PencilLine className="size-4" />
        </button>
      )}
      {canManage && (
        <button
          type="button"
          aria-label={`Move ${file.name}`}
          onClick={(e) => stop(e, () => onMove(file))}
          className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-card hover:text-foreground"
        >
          <FolderInput className="size-4" />
        </button>
      )}
      <button
        type="button"
        aria-label={`Download ${file.name}`}
        disabled={downloadMutation.isPending}
        onClick={(e) => stop(e, () => downloadMutation.mutate())}
        className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-card hover:text-foreground disabled:opacity-50"
      >
        <Download className="size-4" />
      </button>
    </div>
  );
}

function fileSubtitle(file: FileSummary, isOwned: boolean): string {
  if (!isOwned) return `Shared by ${file.owner_email ?? "someone else"}`;
  return folderOf(file.path, file.name);
}

interface FileEntryProps {
  file: FileSummary;
  isOwned: boolean;
  canManage: boolean;
  onRename: (file: FileSummary) => void;
  onMove: (file: FileSummary) => void;
}

function FileRow({ file, isOwned, canManage, onRename, onMove }: FileEntryProps) {
  return (
    <Link
      to="/files/$fileId"
      params={{ fileId: file.id }}
      className="group grid grid-cols-[1fr_auto_auto_auto_auto] items-center gap-4 rounded-lg px-3 py-2.5 text-sm transition-colors hover:bg-secondary/60"
    >
      <div className="flex min-w-0 items-center gap-3">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-secondary text-muted-foreground group-hover:bg-card">
          {fileTypeIconElement(file.mime_type, { className: "size-4" })}
        </span>
        <div className="min-w-0">
          <p className="truncate font-medium">{file.name}</p>
          <p className="truncate text-xs text-muted-foreground">{fileSubtitle(file, isOwned)}</p>
        </div>
      </div>
      <span className="hidden w-24 shrink-0 truncate text-xs text-muted-foreground sm:block">
        {fileTypeLabel(file.mime_type)}
      </span>
      <span className="hidden w-20 shrink-0 text-right text-xs text-muted-foreground md:block">
        {file.size_bytes !== null ? formatBytes(file.size_bytes) : "—"}
      </span>
      <span className="w-24 shrink-0 text-right text-xs text-muted-foreground">
        {file.provider_modified_at ? formatRelativeTime(file.provider_modified_at) : "—"}
      </span>
      <FileRowActions
        file={file}
        canManage={canManage && isOwned}
        onRename={onRename}
        onMove={onMove}
      />
    </Link>
  );
}

function FileGridCard({ file, isOwned, canManage, onRename, onMove }: FileEntryProps) {
  return (
    <Link
      to="/files/$fileId"
      params={{ fileId: file.id }}
      className="group relative flex flex-col gap-3 rounded-xl border border-border bg-card p-4 shadow-clay-sm transition-all hover:-translate-y-0.5 hover:shadow-clay"
    >
      <FileRowActions
        file={file}
        canManage={canManage && isOwned}
        onRename={onRename}
        onMove={onMove}
        className="absolute right-3 top-3 rounded-md bg-card opacity-0 shadow-clay-sm group-hover:opacity-100"
      />
      <div className="flex aspect-video items-center justify-center rounded-lg bg-secondary">
        {fileTypeIconElement(file.mime_type, { className: "size-8 text-muted-foreground" })}
      </div>
      <div className="min-w-0">
        <p className="truncate text-sm font-medium">{file.name}</p>
        <p className="truncate text-xs text-muted-foreground">{fileSubtitle(file, isOwned)}</p>
      </div>
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>{file.size_bytes !== null ? formatBytes(file.size_bytes) : "—"}</span>
        {file.is_shared ? (
          <span className="flex items-center gap-1">
            <Users className="size-3" /> Shared
          </span>
        ) : null}
      </div>
    </Link>
  );
}

function FilesPage() {
  const [viewMode, setViewMode] = useState<ViewMode>(readViewMode);
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("modified");
  const [page, setPage] = useState(0);
  const [renamingFile, setRenamingFile] = useState<FileSummary | null>(null);
  const [movingFile, setMovingFile] = useState<FileSummary | null>(null);
  const [ownership, setOwnership] = useState<OwnershipFilter>("all");
  const [creating, setCreating] = useState<CreateItemKind | null>(null);
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";

  function changeView(mode: ViewMode) {
    setViewMode(mode);
    window.localStorage.setItem("vault-file-view", mode);
  }

  function changeOwnership(next: OwnershipFilter) {
    setOwnership(next);
    setPage(0);
  }

  const connectorsQuery = useQuery({
    queryKey: ["connectors"],
    queryFn: () => apiClient.get<Connector[]>("/v1/connectors"),
  });

  const connected = (connectorsQuery.data ?? []).filter(
    (candidate) => candidate.status === "connected",
  );
  const [chosenConnectorId, setChosenConnectorId] = useState<string | null>(null);
  const connector =
    connected.find((candidate) => candidate.id === chosenConnectorId) ?? connected[0];

  const filesQuery = useQuery({
    queryKey: ["files", connector?.id, page, ownership],
    queryFn: () =>
      apiClient.get<FileListResponse>(
        `/v1/connectors/${connector?.id}/files?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}` +
          (ownership === "all" ? "" : `&ownership=${ownership}`),
      ),
    enabled: connector !== undefined,
  });

  const files = filesQuery.data?.items ?? [];
  const total = filesQuery.data?.total ?? 0;

  const visibleFiles = useMemo(() => {
    const filtered = query.trim()
      ? files.filter((f) => f.name.toLowerCase().includes(query.trim().toLowerCase()))
      : files;
    return [...filtered].sort((a, b) => {
      if (sortKey === "name") return a.name.localeCompare(b.name);
      if (sortKey === "size") return (b.size_bytes ?? 0) - (a.size_bytes ?? 0);
      return (b.provider_modified_at ?? "").localeCompare(a.provider_modified_at ?? "");
    });
  }, [files, query, sortKey]);

  return (
    <AppShell title="Files">
      <div className="mx-auto flex max-w-6xl flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Files</h1>
            <p className="text-sm text-muted-foreground">
              Everything Vault has scanned across your connected storage.
            </p>
          </div>
          {connector && canManage && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="sm">
                  <Plus className="size-4" /> New
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => setCreating("folder")}>Folder</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setCreating("text")}>Text file</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setCreating("markdown")}>
                  Markdown note
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>

        {connectorsQuery.isLoading && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-24 rounded-xl" />
            ))}
          </div>
        )}

        {!connectorsQuery.isLoading && connector === undefined && (
          <EmptyState
            icon={Cloud}
            title="No storage connected yet"
            description="Connect Google Workspace to let Vault scan and understand your organization's files."
            action={
              <Button asChild>
                <Link to="/storage-connections">Connect Google Drive</Link>
              </Button>
            }
          />
        )}

        {connector && (
          <>
            {connected.length > 1 && (
              <div className="flex w-fit items-center gap-1 rounded-lg bg-secondary p-1">
                {connected.map((candidate) => (
                  <button
                    key={candidate.id}
                    type="button"
                    onClick={() => {
                      setChosenConnectorId(candidate.id);
                      setPage(0);
                    }}
                    aria-pressed={candidate.id === connector.id}
                    className={cn(
                      "rounded-md px-3 py-1.5 text-sm transition-colors",
                      candidate.id === connector.id
                        ? "bg-card shadow-clay-sm"
                        : "text-muted-foreground",
                    )}
                  >
                    {candidate.display_name ?? candidate.provider_name}
                  </button>
                ))}
              </div>
            )}
            <div className="flex items-center gap-1 rounded-lg bg-secondary p-1 w-fit">
              {(
                [
                  ["all", "All files"],
                  ["mine", "My files"],
                  ["shared", "Shared with me"],
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => changeOwnership(value)}
                  aria-pressed={ownership === value}
                  className={cn(
                    "rounded-md px-3 py-1.5 text-sm transition-colors",
                    ownership === value ? "bg-card shadow-clay-sm" : "text-muted-foreground",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>

            <Card className="flex flex-wrap items-center gap-2 p-2">
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Filter files on this page…"
                className="w-full max-w-xs"
              />
              <Select value={sortKey} onValueChange={(v) => setSortKey(v as SortKey)}>
                <SelectTrigger className="w-40">
                  <ArrowDownAZ className="size-4 text-muted-foreground" />
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="modified">Last modified</SelectItem>
                  <SelectItem value="name">Name</SelectItem>
                  <SelectItem value="size">Size</SelectItem>
                </SelectContent>
              </Select>

              <div className="ml-auto flex items-center gap-1 rounded-lg bg-secondary p-1">
                <button
                  type="button"
                  onClick={() => changeView("list")}
                  aria-pressed={viewMode === "list"}
                  aria-label="List view"
                  className={cn(
                    "rounded-md p-1.5 transition-colors",
                    viewMode === "list" ? "bg-card shadow-clay-sm" : "text-muted-foreground",
                  )}
                >
                  <List className="size-4" />
                </button>
                <button
                  type="button"
                  onClick={() => changeView("grid")}
                  aria-pressed={viewMode === "grid"}
                  aria-label="Grid view"
                  className={cn(
                    "rounded-md p-1.5 transition-colors",
                    viewMode === "grid" ? "bg-card shadow-clay-sm" : "text-muted-foreground",
                  )}
                >
                  <LayoutGrid className="size-4" />
                </button>
              </div>
            </Card>

            {filesQuery.isLoading && (
              <div className="flex flex-col gap-2">
                {Array.from({ length: 8 }).map((_, i) => (
                  <Skeleton key={i} className="h-12 rounded-lg" />
                ))}
              </div>
            )}

            {filesQuery.isError && (
              <EmptyState title="Couldn't load files" description="Please try again." />
            )}

            {filesQuery.isSuccess && files.length === 0 && ownership === "all" && (
              <EmptyState
                title="No files yet"
                description={
                  connector?.provider === "local_agent"
                    ? "Add a folder or drive on this computer, or wait for the agent to finish scanning."
                    : "Run a scan to let Vault index this connector's storage."
                }
                action={
                  <Button asChild variant="outline">
                    {connector?.provider === "local_agent" ? (
                      <Link to="/storage-connections">Add a folder or drive</Link>
                    ) : (
                      <Link to="/scans">Go to Scans</Link>
                    )}
                  </Button>
                }
              />
            )}

            {filesQuery.isSuccess && files.length === 0 && ownership !== "all" && (
              <EmptyState
                title={ownership === "mine" ? "No files owned by you" : "Nothing shared with you"}
                description={
                  ownership === "mine"
                    ? "Every scanned file here is shared by someone else."
                    : "No one else has shared a file with this account yet."
                }
              />
            )}

            {visibleFiles.length > 0 &&
              (viewMode === "list" ? (
                <Card className="flex flex-col divide-y divide-border p-1">
                  {visibleFiles.map((file) => (
                    <FileRow
                      key={file.id}
                      file={file}
                      isOwned={connector.account_email === null || file.owner_email === connector.account_email}
                      canManage={canManage}
                      onRename={setRenamingFile}
                      onMove={setMovingFile}
                    />
                  ))}
                </Card>
              ) : (
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                  {visibleFiles.map((file) => (
                    <FileGridCard
                      key={file.id}
                      file={file}
                      isOwned={connector.account_email === null || file.owner_email === connector.account_email}
                      canManage={canManage}
                      onRename={setRenamingFile}
                      onMove={setMovingFile}
                    />
                  ))}
                </div>
              ))}

            {total > PAGE_SIZE && (
              <div className="flex items-center justify-between text-sm text-muted-foreground">
                <span>
                  Showing {page * PAGE_SIZE + 1}–{Math.min(total, (page + 1) * PAGE_SIZE)} of{" "}
                  {total.toLocaleString()}
                  <Badge variant="outline" className="ml-2">
                    Page-local filter/sort only
                  </Badge>
                </span>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={page === 0}
                    onClick={() => setPage((p) => Math.max(0, p - 1))}
                  >
                    <ChevronLeft className="size-4" /> Previous
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={(page + 1) * PAGE_SIZE >= total}
                    onClick={() => setPage((p) => p + 1)}
                  >
                    Next <ChevronRight className="size-4" />
                  </Button>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {renamingFile && (
        <RenameDialog
          onOpenChange={(open) => !open && setRenamingFile(null)}
          fileId={renamingFile.id}
          currentName={renamingFile.name}
        />
      )}
      {creating && connector && (
        <CreateItemDialog
          kind={creating}
          connectorId={connector.id}
          onOpenChange={(open) => !open && setCreating(null)}
        />
      )}
      {movingFile && connector && (
        <MoveDialog
          onOpenChange={(open) => !open && setMovingFile(null)}
          connectorId={connector.id}
          fileIds={[movingFile.id]}
        />
      )}
    </AppShell>
  );
}
