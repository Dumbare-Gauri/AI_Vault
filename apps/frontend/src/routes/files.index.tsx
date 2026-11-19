import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, createFileRoute, redirect, useNavigate } from "@tanstack/react-router";
import type { BrowseFolder, ExtractedArchive, FileSummary, FolderListing } from "@vault/types";
import {
  ArrowDownAZ,
  ChevronLeft,
  ChevronRight,
  Cloud,
  Download,
  FileArchive,
  Folder,
  FolderArchive,
  FolderInput,
  LayoutGrid,
  List,
  PencilLine,
  Plus,
  RefreshCw,
  Sparkles,
  Users,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { z } from "zod";

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
import { ArchiveDialog } from "@/components/file-explorer/archive-dialog";
import { Checkbox } from "@/components/ui/checkbox";
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
import { useStorageScope } from "@/components/storage-intelligence/storage-scope-switcher";
import { fetchConnectors } from "@/lib/connectors";
import { ApiError, apiClient } from "@/lib/api-client";
import { suggestArchiveName } from "@/lib/archive-name";
import { downloadFile } from "@/lib/download-file";
import { fileTypeIconElement, fileTypeLabel } from "@/lib/file-icon";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/auth-store";
import { useCopilotPage, useCopilotStore } from "@/stores/copilot-store";

// Where the user is lives in the URL, so Back/Forward, reloads and shared
// links return to the same folder, page, sort and filter.
const filesSearchSchema = z.object({
  folder: z.string().optional(),
  source: z.string().optional(),
  page: z.number().int().min(0).optional(),
  sort: z.enum(["name", "size", "modified"]).optional(),
  q: z.string().optional(),
});

export const Route = createFileRoute("/files/")({
  validateSearch: filesSearchSchema,
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: FilesPage,
});

type SortKey = "name" | "size" | "modified";
type ViewMode = "list" | "grid";

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

function isZip(file: FileSummary): boolean {
  return file.name.toLowerCase().endsWith(".zip");
}

/** Unpacks an archive into a new folder next to it, through the storage
 * itself, and refreshes what's on screen once the storage confirms it. */
function useExtract(file: FileSummary) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiClient.post<ExtractedArchive>(`/v1/files/${file.id}/extract`, {}),
    onSuccess: (result) => {
      void queryClient.invalidateQueries();
      const skipped = result.skipped.length;
      const message = `Extracted ${result.file_count} file${result.file_count === 1 ? "" : "s"} into "${result.name}" — confirmed by the storage`;
      if (skipped > 0) {
        toast.warning(message, { description: `${skipped} entr${skipped === 1 ? "y was" : "ies were"} left out (unsafe or already there).` });
      } else {
        toast.success(message);
      }
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't extract it — nothing was changed.");
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
  const extractMutation = useExtract(file);

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
      {canManage && isZip(file) && (
        <button
          type="button"
          aria-label={`Extract ${file.name}`}
          title="Extract into a new folder"
          disabled={extractMutation.isPending}
          onClick={(e) => stop(e, () => extractMutation.mutate())}
          className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-card hover:text-foreground disabled:opacity-50"
        >
          <FileArchive className="size-4" />
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
  selected: boolean;
  onToggle: (file: FileSummary) => void;
  onRename: (file: FileSummary) => void;
  onMove: (file: FileSummary) => void;
}

/** Ticks a file without opening it — the row around it is a link. */
function SelectBox({ file, selected, onToggle }: Pick<FileEntryProps, "file" | "selected" | "onToggle">) {
  return (
    <span
      className="flex shrink-0 items-center"
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
      }}
    >
      <Checkbox checked={selected} onCheckedChange={() => onToggle(file)} aria-label={`Select ${file.name}`} />
    </span>
  );
}

function FileRow({ file, isOwned, canManage, selected, onToggle, onRename, onMove }: FileEntryProps) {
  return (
    <Link
      to="/files/$fileId"
      params={{ fileId: file.id }}
      className={cn(
        "group grid grid-cols-[1fr_auto_auto_auto_auto] items-center gap-4 rounded-lg px-3 py-2.5 text-sm transition-colors hover:bg-secondary/60",
        selected && "bg-ai-muted/60",
      )}
    >
      <div className="flex min-w-0 items-center gap-3">
        <SelectBox file={file} selected={selected} onToggle={onToggle} />
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

function FileGridCard({ file, isOwned, canManage, selected, onToggle, onRename, onMove }: FileEntryProps) {
  return (
    <Link
      to="/files/$fileId"
      params={{ fileId: file.id }}
      className={cn(
        "group relative flex flex-col gap-3 rounded-xl border bg-card p-4 shadow-clay-sm transition-all hover:-translate-y-0.5 hover:shadow-clay",
        selected ? "border-ai ring-2 ring-ai/30" : "border-border",
      )}
    >
      <span className="absolute left-3 top-3">
        <SelectBox file={file} selected={selected} onToggle={onToggle} />
      </span>
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

interface FolderEntryProps {
  folder: BrowseFolder;
  canManage: boolean;
  onOpen: (folder: BrowseFolder) => void;
  onArchive?: (folder: BrowseFolder) => void;
}

function FolderRow({ folder, canManage, onOpen, onArchive }: FolderEntryProps) {
  return (
    <div className="group grid grid-cols-[1fr_auto] items-center gap-4 rounded-lg px-3 py-2.5 text-sm transition-colors hover:bg-secondary/60">
      <button type="button" onClick={() => onOpen(folder)} className="flex min-w-0 items-center gap-3 text-left">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-secondary text-muted-foreground group-hover:bg-card">
          <Folder className="size-4" />
        </span>
        <span className="min-w-0">
          <span className="block truncate font-medium">{folder.name}</span>
          <span className="block truncate text-xs text-muted-foreground">Folder</span>
        </span>
      </button>
      {canManage && folder.id && (
        <button
          type="button"
          aria-label={`Archive ${folder.name}`}
          title="Zip this folder, keeping its structure"
          onClick={() => onArchive?.(folder)}
          className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-card hover:text-foreground"
        >
          <FolderArchive className="size-4" />
        </button>
      )}
    </div>
  );
}

function FolderCard({ folder, onOpen }: FolderEntryProps) {
  return (
    <button
      type="button"
      onClick={() => onOpen(folder)}
      className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4 text-left shadow-clay-sm transition-all hover:-translate-y-0.5 hover:shadow-clay"
    >
      <div className="flex aspect-video items-center justify-center rounded-lg bg-secondary">
        <Folder className="size-8 text-muted-foreground" />
      </div>
      <p className="truncate text-sm font-medium">{folder.name}</p>
    </button>
  );
}

function Breadcrumbs({
  listing,
  onGo,
}: {
  listing: FolderListing;
  onGo: (place: { source?: string; folder?: string }) => void;
}) {
  return (
    <nav aria-label="Folder path" className="flex flex-wrap items-center gap-1 text-sm">
      {listing.breadcrumbs.map((crumb, index) => {
        const last = index === listing.breadcrumbs.length - 1;
        return (
          <span key={`${crumb.folder_id ?? crumb.source_id ?? "top"}-${index}`} className="flex items-center gap-1">
            {index > 0 && <ChevronRight className="size-3.5 text-muted-foreground" />}
            {last ? (
              <span className="font-medium">{crumb.label}</span>
            ) : (
              <button
                type="button"
                onClick={() =>
                  onGo({ source: crumb.folder_id ? undefined : (crumb.source_id ?? undefined), folder: crumb.folder_id ?? undefined })
                }
                className="text-muted-foreground hover:text-foreground hover:underline"
              >
                {crumb.label}
              </button>
            )}
          </span>
        );
      })}
    </nav>
  );
}

function FilesPage() {
  const search = Route.useSearch();
  const navigate = useNavigate({ from: Route.fullPath });
  const queryClient = useQueryClient();
  const page = search.page ?? 0;
  const sortKey: SortKey = search.sort ?? "name";
  const query = search.q ?? "";
  const [viewMode, setViewMode] = useState<ViewMode>(readViewMode);
  const [renamingFile, setRenamingFile] = useState<FileSummary | null>(null);
  const [movingFile, setMovingFile] = useState<FileSummary | null>(null);
  const [creating, setCreating] = useState<CreateItemKind | null>(null);
  const [archiving, setArchiving] = useState<{ folder?: BrowseFolder; fileIds?: string[] } | null>(null);
  // Kept per screen: moving to another folder or page starts unselected.
  const placeKey = `${search.source ?? ""}|${search.folder ?? ""}|${page}`;
  const [selection, setSelection] = useState<{ key: string; ids: string[] }>({ key: placeKey, ids: [] });
  const selectedIds = selection.key === placeKey ? selection.ids : [];
  const openCopilot = useCopilotStore((state) => state.setOpen);
  const [refreshing, setRefreshing] = useState(false);
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";

  function changeView(mode: ViewMode) {
    setViewMode(mode);
    window.localStorage.setItem("vault-file-view", mode);
  }

  // Each move between folders is its own history entry; sorting and
  // filtering replace the current one.
  function go(place: { source?: string; folder?: string }) {
    void navigate({ search: { source: place.source, folder: place.folder } });
  }
  function update(changes: Partial<z.infer<typeof filesSearchSchema>>) {
    void navigate({ search: (prev) => ({ ...prev, ...changes }), replace: true });
  }

  const connectorsQuery = useQuery({
    queryKey: ["connectors"],
    queryFn: fetchConnectors,
  });
  const { connector } = useStorageScope();

  const listingQuery = useQuery({
    queryKey: ["files", "browse", connector?.id, search.source, search.folder, page],
    queryFn: () => {
      const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(page * PAGE_SIZE) });
      if (search.folder) params.set("folder_id", search.folder);
      else if (search.source) params.set("source_id", search.source);
      return apiClient.get<FolderListing>(`/v1/connectors/${connector?.id}/browse?${params}`);
    },
    enabled: connector !== undefined,
    // While a refresh from the storage runs, keep re-reading the index.
    refetchInterval: refreshing ? 3000 : false,
  });

  const listing = listingQuery.data;
  const total = listing?.total_files ?? 0;
  const folders = listing?.folders ?? [];

  const visibleFiles = useMemo(() => {
    const files = listing?.files ?? [];
    const filtered = query.trim()
      ? files.filter((f) => f.name.toLowerCase().includes(query.trim().toLowerCase()))
      : files;
    return [...filtered].sort((a, b) => {
      if (sortKey === "name") return a.name.localeCompare(b.name);
      if (sortKey === "size") return (b.size_bytes ?? 0) - (a.size_bytes ?? 0);
      return (b.provider_modified_at ?? "").localeCompare(a.provider_modified_at ?? "");
    });
  }, [listing, query, sortKey]);
  const visibleFolders = query.trim()
    ? folders.filter((f) => f.name.toLowerCase().includes(query.trim().toLowerCase()))
    : folders;
  const crumbs = listing?.breadcrumbs ?? [];
  const where =
    crumbs.length > 1 ? crumbs.slice(1).map((crumb) => crumb.label).join(" / ") : (crumbs[0]?.label ?? "Files");
  const visibleIds = new Set(visibleFiles.map((file) => file.id));
  const selectedHere = selectedIds.filter((id) => visibleIds.has(id));
  useCopilotPage({
    page: "files",
    label: where,
    folderId: listing?.folder_id ?? null,
    sourceId: listing?.source_id ?? null,
    fileIds: visibleFiles.map((file) => file.id),
    selectedFileIds: selectedHere,
  });

  function toggle(file: FileSummary) {
    const ids = selectedIds.includes(file.id) ? selectedIds.filter((id) => id !== file.id) : [...selectedIds, file.id];
    setSelection({ key: placeKey, ids });
  }
  function selectAll(all: boolean) {
    setSelection({ key: placeKey, ids: all ? visibleFiles.map((file) => file.id) : [] });
  }

  const refreshMutation = useMutation({
    mutationFn: () =>
      connector?.provider === "local_agent"
        ? apiClient.post("/v1/local-agents/scan", {})
        : apiClient.post(`/v1/connectors/${connector?.id}/scans`, { scan_type: "incremental" }),
    onSuccess: () => {
      setRefreshing(true);
      toast.info("Checking the storage for changes — this folder updates as they come in.");
      window.setTimeout(() => {
        setRefreshing(false);
        void queryClient.invalidateQueries();
      }, 30_000);
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't start a refresh.");
    },
  });

  const isOwned = (file: FileSummary) =>
    connector?.account_email === null || file.owner_email === connector?.account_email;

  return (
    <AppShell title="Files">
      <div className="mx-auto flex max-w-6xl flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold tracking-tight">Files</h1>
            {listing ? (
              <Breadcrumbs listing={listing} onGo={go} />
            ) : (
              <p className="text-sm text-muted-foreground">Folders and files as they are in this storage.</p>
            )}
          </div>
          {connector && (
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={refreshMutation.isPending || refreshing}
                onClick={() => refreshMutation.mutate()}
              >
                <RefreshCw className={cn("size-4", refreshing && "animate-spin")} />
                {refreshing ? "Refreshing…" : "Refresh from storage"}
              </Button>
              {canManage && listing?.source_id && (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button size="sm">
                      <Plus className="size-4" /> New
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem onSelect={() => setCreating("folder")}>Folder</DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => setCreating("text")}>Text file</DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => setCreating("markdown")}>Markdown note</DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
            </div>
          )}
        </div>

        {connectorsQuery.isLoading && (
          <div className="flex flex-col gap-2">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-12 rounded-lg" />
            ))}
          </div>
        )}

        {!connectorsQuery.isLoading && connector === undefined && (
          <EmptyState
            icon={Cloud}
            title="No storage connected yet"
            description="Connect Google Drive or this computer to see its folders and files here."
            action={
              <Button asChild>
                <Link to="/storage-connections">Connect storage</Link>
              </Button>
            }
          />
        )}

        {connector && (
          <>
            <Card className="flex flex-wrap items-center gap-2 p-2">
              <Input
                value={query}
                onChange={(e) => update({ q: e.target.value || undefined })}
                placeholder="Filter this folder…"
                className="w-full max-w-xs"
              />
              <Select value={sortKey} onValueChange={(v) => update({ sort: v as SortKey })}>
                <SelectTrigger className="w-40">
                  <ArrowDownAZ className="size-4 text-muted-foreground" />
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="name">Name</SelectItem>
                  <SelectItem value="modified">Last modified</SelectItem>
                  <SelectItem value="size">Size</SelectItem>
                </SelectContent>
              </Select>

              <div className="ml-auto flex items-center gap-1 rounded-lg bg-secondary p-1">
                <button
                  type="button"
                  onClick={() => changeView("list")}
                  aria-pressed={viewMode === "list"}
                  aria-label="List view"
                  className={cn("rounded-md p-1.5 transition-colors", viewMode === "list" ? "bg-card shadow-clay-sm" : "text-muted-foreground")}
                >
                  <List className="size-4" />
                </button>
                <button
                  type="button"
                  onClick={() => changeView("grid")}
                  aria-pressed={viewMode === "grid"}
                  aria-label="Grid view"
                  className={cn("rounded-md p-1.5 transition-colors", viewMode === "grid" ? "bg-card shadow-clay-sm" : "text-muted-foreground")}
                >
                  <LayoutGrid className="size-4" />
                </button>
              </div>
            </Card>

            {visibleFiles.length > 0 && (
              <div
                className={cn(
                  "flex flex-wrap items-center gap-2 rounded-xl px-3 py-2 text-sm transition-colors",
                  selectedHere.length > 0 ? "border border-ai/30 bg-ai-muted/70" : "text-muted-foreground",
                )}
              >
                <Checkbox
                  checked={
                    selectedHere.length === visibleFiles.length ? true : selectedHere.length > 0 ? "indeterminate" : false
                  }
                  onCheckedChange={(value) => selectAll(value === true)}
                  aria-label="Select every file shown"
                />
                <span className="font-medium">
                  {selectedHere.length > 0
                    ? `${selectedHere.length.toLocaleString()} selected`
                    : "Select files to archive them or ask about them"}
                </span>
                {selectedHere.length > 0 && (
                  <div className="ml-auto flex flex-wrap items-center gap-2">
                    <Button size="sm" variant="ai" onClick={() => openCopilot(true)}>
                      <Sparkles className="size-4" /> Ask about these
                    </Button>
                    {canManage && (
                      <Button size="sm" variant="outline" onClick={() => setArchiving({ fileIds: selectedHere })}>
                        <FolderArchive className="size-4" /> Archive
                      </Button>
                    )}
                    <Button size="sm" variant="ghost" onClick={() => selectAll(false)}>
                      <X className="size-4" /> Clear
                    </Button>
                  </div>
                )}
              </div>
            )}

            {listingQuery.isLoading && (
              <div className="flex flex-col gap-2">
                {Array.from({ length: 8 }).map((_, i) => (
                  <Skeleton key={i} className="h-12 rounded-lg" />
                ))}
              </div>
            )}

            {listingQuery.isError && (
              <EmptyState
                title="Couldn't open this folder"
                description={
                  listingQuery.error instanceof ApiError ? listingQuery.error.message : "Please try again."
                }
                action={
                  <Button variant="outline" onClick={() => go({})}>
                    Go to the top
                  </Button>
                }
              />
            )}

            {listing && visibleFolders.length === 0 && visibleFiles.length === 0 && (
              <EmptyState
                title={query ? "Nothing here matches" : "This folder is empty"}
                description={
                  query
                    ? "Clear the filter to see everything in this folder."
                    : "Nothing has been indexed here. If you just added files, use Refresh from storage."
                }
              />
            )}

            {listing && (visibleFolders.length > 0 || visibleFiles.length > 0) &&
              (viewMode === "list" ? (
                <Card className="flex flex-col divide-y divide-border p-1">
                  {visibleFolders.map((folder) => (
                    <FolderRow
                      key={folder.id ?? folder.source_id}
                      folder={folder}
                      canManage={canManage}
                      onOpen={(f) => go(f.id ? { folder: f.id } : { source: f.source_id })}
                      onArchive={(f) => setArchiving({ folder: f })}
                    />
                  ))}
                  {visibleFiles.map((file) => (
                    <FileRow
                      key={file.id}
                      file={file}
                      isOwned={isOwned(file)}
                      canManage={canManage}
                      selected={selectedIds.includes(file.id)}
                      onToggle={toggle}
                      onRename={setRenamingFile}
                      onMove={setMovingFile}
                    />
                  ))}
                </Card>
              ) : (
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                  {visibleFolders.map((folder) => (
                    <FolderCard
                      key={folder.id ?? folder.source_id}
                      folder={folder}
                      canManage={canManage}
                      onOpen={(f) => go(f.id ? { folder: f.id } : { source: f.source_id })}
                    />
                  ))}
                  {visibleFiles.map((file) => (
                    <FileGridCard
                      key={file.id}
                      file={file}
                      isOwned={isOwned(file)}
                      canManage={canManage}
                      selected={selectedIds.includes(file.id)}
                      onToggle={toggle}
                      onRename={setRenamingFile}
                      onMove={setMovingFile}
                    />
                  ))}
                </div>
              ))}

            {total > PAGE_SIZE && (
              <div className="flex items-center justify-between text-sm text-muted-foreground">
                <span>
                  Files {page * PAGE_SIZE + 1}–{Math.min(total, (page + 1) * PAGE_SIZE)} of {total.toLocaleString()}
                  {query && (
                    <Badge variant="outline" className="ml-2">
                      Filter applies to this page
                    </Badge>
                  )}
                </span>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" disabled={page === 0} onClick={() => void navigate({ search: (prev) => ({ ...prev, page: page - 1 || undefined }) })}>
                    <ChevronLeft className="size-4" /> Previous
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={(page + 1) * PAGE_SIZE >= total}
                    onClick={() => void navigate({ search: (prev) => ({ ...prev, page: page + 1 }) })}
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
      {creating && connector && listing && (
        <CreateItemDialog
          kind={creating}
          connectorId={connector.id}
          parentFolderId={listing.folder_id}
          sourceId={listing.source_id}
          storageLabel={listing.breadcrumbs[0]?.label ?? "your storage"}
          onOpenChange={(open) => !open && setCreating(null)}
        />
      )}
      {archiving && connector && (
        <ArchiveDialog
          onOpenChange={(open) => !open && setArchiving(null)}
          connectorId={connector.id}
          storage={connector.provider === "local_agent" ? "this computer" : "Google Drive"}
          folderIds={archiving.folder?.id ? [archiving.folder.id] : []}
          fileIds={archiving.fileIds ?? []}
          subject={
            archiving.folder
              ? `“${archiving.folder.name}”`
              : `${(archiving.fileIds?.length ?? 0).toLocaleString()} selected files`
          }
          suggestedName={suggestArchiveName(archiving.folder?.name ?? crumbs.at(-1)?.label ?? "Files")}
          here={listing?.folder_id ? { id: listing.folder_id, label: where } : null}
          onStarted={() => selectAll(false)}
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
