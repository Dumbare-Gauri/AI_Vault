import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type {
  Action,
  ActionProposalBlock,
  ActionResultBlock,
  ActionResultStep,
  AnswerBlock,
  AnswerDuplicateGroup,
  AnswerFile,
  ConversationMessage,
  DuplicateGroupsBlock,
  FileListBlock,
  NoticeBlock,
  ResultPage,
  StorageSummaryBlock,
} from "@vault/types";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Copy,
  Eye,
  Folder,
  FolderTree,
  HardDrive,
  Info,
  Laptop,
  Loader2,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { useState } from "react";

import { FilePreviewDialog } from "@/components/file-preview/file-preview-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { fileTypeIconElement } from "@/lib/file-icon";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/auth-store";

const PAGE = 25;

interface BlockContext {
  conversationId: string;
  messageId: string;
  index: number;
}

function DuplicateOf({ original, onPreview }: { original: AnswerFile; onPreview: (file: AnswerFile) => void }) {
  return (
    <span className="mt-0.5 flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
      <Copy className="size-3 shrink-0" />
      <span className="shrink-0">Same content as</span>
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          onPreview(original);
        }}
        className="max-w-[60%] shrink-0 truncate font-medium text-foreground/80 underline-offset-2 hover:underline"
        title={[original.storage, original.path].filter(Boolean).join(" · ")}
      >
        {original.name}
      </button>
      {original.path && <span className="min-w-0 truncate">· {original.path}</span>}
    </span>
  );
}

function FileRow({ file, onPreview }: { file: AnswerFile; onPreview: (file: AnswerFile) => void }) {
  const meta = [file.storage, file.path && file.path !== `/${file.name}` ? file.path : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <li className="group flex items-center gap-3 rounded-lg px-2 py-2 hover:bg-secondary/60">
      <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-secondary text-muted-foreground">
        {fileTypeIconElement(file.mime_type, { className: "size-4" })}
      </span>
      <div className="min-w-0 flex-1">
        <button type="button" onClick={() => onPreview(file)} className="block w-full min-w-0 text-left">
          <span className="flex items-center gap-2">
            <span className="truncate text-sm font-medium">{file.name}</span>
            {file.keep && <Badge variant="success">Keep</Badge>}
            {file.trashed && <Badge variant="outline">In Trash</Badge>}
          </span>
          {meta && <span className="block truncate text-xs text-muted-foreground">{meta}</span>}
        </button>
        {file.duplicate_of && <DuplicateOf original={file.duplicate_of} onPreview={onPreview} />}
      </div>
      <span className="hidden shrink-0 text-xs text-muted-foreground sm:block">
        {file.modified_at ? formatRelativeTime(file.modified_at) : ""}
      </span>
      <span className="w-16 shrink-0 text-right text-xs text-muted-foreground">
        {file.size_bytes != null ? formatBytes(file.size_bytes) : ""}
      </span>
      <Button
        size="icon"
        variant="ghost"
        className="size-7 shrink-0"
        aria-label={`Preview ${file.name}`}
        onClick={() => onPreview(file)}
      >
        <Eye />
      </Button>
    </li>
  );
}

function usePreview() {
  const [file, setFile] = useState<AnswerFile | null>(null);
  const dialog = file ? <FilePreviewDialog file={file} onOpenChange={(open) => !open && setFile(null)} /> : null;
  return { open: setFile, dialog };
}

function useMorePages<T>(ctx: BlockContext, key: "items" | "groups", initial: T[], total: number) {
  const [extra, setExtra] = useState<T[]>([]);
  const loaded = initial.length + extra.length;
  const more = useMutation({
    mutationFn: () =>
      apiClient.get<ResultPage>(
        `/v1/conversations/${ctx.conversationId}/messages/${ctx.messageId}/blocks/${ctx.index}?offset=${loaded}&limit=${PAGE}`,
      ),
    onSuccess: (page) => setExtra((prev) => [...prev, ...(page[key] as T[])]),
    onError: (error) => toast.error(error instanceof ApiError ? error.message : "Couldn't load more."),
  });
  return { all: [...initial, ...extra], remaining: Math.max(0, total - loaded), more };
}

function MoreButton({ remaining, pending, onClick }: { remaining: number; pending: boolean; onClick: () => void }) {
  if (remaining === 0) return null;
  return (
    <Button variant="ghost" size="sm" className="mt-1 w-full" disabled={pending} onClick={onClick}>
      {pending ? <Loader2 className="animate-spin" /> : <ChevronDown />}
      Show {Math.min(PAGE, remaining).toLocaleString()} more of {remaining.toLocaleString()}
    </Button>
  );
}

function BlockCard({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("w-full rounded-xl border border-border bg-card p-3 shadow-clay-sm", className)}>{children}</div>;
}

function FileListView({ block, ctx }: { block: FileListBlock; ctx: BlockContext }) {
  const preview = usePreview();
  const { all, remaining, more } = useMorePages<AnswerFile>(ctx, "items", block.items, block.total);
  return (
    <BlockCard>
      <div className="mb-1 flex items-center justify-between px-2">
        <p className="text-sm font-semibold">{block.title}</p>
        <span className="text-xs text-muted-foreground">{block.total.toLocaleString()} files</span>
      </div>
      {block.note && <p className="px-2 pb-1 text-xs text-muted-foreground">{block.note}</p>}
      <ul className="flex flex-col">
        {all.map((file) => (
          <FileRow key={file.id} file={file} onPreview={preview.open} />
        ))}
      </ul>
      <MoreButton remaining={remaining} pending={more.isPending} onClick={() => more.mutate()} />
      {preview.dialog}
    </BlockCard>
  );
}

function DuplicateGroupView({ group, onPreview }: { group: AnswerDuplicateGroup; onPreview: (f: AnswerFile) => void }) {
  const [open, setOpen] = useState(false);
  const keep = group.files.find((file) => file.keep) ?? group.files[0];
  return (
    <li className="rounded-lg border border-border">
      <button type="button" onClick={() => setOpen((v) => !v)} className="flex w-full items-center gap-2 px-3 py-2 text-left">
        {open ? <ChevronDown className="size-4 shrink-0" /> : <ChevronRight className="size-4 shrink-0" />}
        <Copy className="size-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{keep?.name}</span>
        <span className="shrink-0 text-xs text-muted-foreground">
          {group.file_count} copies · {formatBytes(group.recoverable_bytes ?? 0)} to free
        </span>
      </button>
      {open && (
        <ul className="flex flex-col border-t border-border px-1 py-1">
          <li className="px-2 py-1 text-xs text-muted-foreground">
            Identical content: every copy is{" "}
            {group.size_bytes != null ? formatBytes(group.size_bytes) : "the same size"} with the same content
            fingerprint{group.fingerprint ? ` (${group.fingerprint}…)` : ""}. Open any two to compare.
          </li>
          {group.files.map((file) => (
            <FileRow key={file.id} file={file} onPreview={onPreview} />
          ))}
          {group.keep_reason && <p className="px-2 py-1 text-xs text-muted-foreground">Keeping: {group.keep_reason}</p>}
        </ul>
      )}
    </li>
  );
}

function DuplicateGroupsView({ block, ctx }: { block: DuplicateGroupsBlock; ctx: BlockContext }) {
  const preview = usePreview();
  const { all, remaining, more } = useMorePages<AnswerDuplicateGroup>(ctx, "groups", block.groups, block.total_groups);
  return (
    <BlockCard>
      <div className="mb-2 flex items-center justify-between px-1">
        <p className="text-sm font-semibold">{block.title}</p>
        <span className="text-xs text-muted-foreground">
          {block.total_groups.toLocaleString()} groups · {formatBytes(block.recoverable_bytes)} to free
        </span>
      </div>
      <ul className="flex flex-col gap-1.5">
        {all.map((group) => (
          <DuplicateGroupView key={group.id} group={group} onPreview={preview.open} />
        ))}
      </ul>
      <MoreButton remaining={remaining} pending={more.isPending} onClick={() => more.mutate()} />
      {preview.dialog}
    </BlockCard>
  );
}

function StorageSummaryView({ block }: { block: StorageSummaryBlock }) {
  return (
    <div className="grid w-full gap-2 sm:grid-cols-2">
      {block.storages.map((storage) => {
        const used = storage.provider_used_bytes;
        const total = storage.provider_total_bytes;
        const percent = used != null && total ? Math.min(100, (used / total) * 100) : null;
        const topBytes = storage.breakdown[0]?.bytes ?? 0;
        return (
          <BlockCard key={storage.label}>
            <p className="flex items-center gap-2 text-sm font-semibold">
              {storage.is_local ? <Laptop className="size-4 text-muted-foreground" /> : <HardDrive className="size-4 text-muted-foreground" />}
              {storage.label}
            </p>
            <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs">
              <dt className="text-muted-foreground">Indexed by AI Vault</dt>
              <dd className="text-right font-medium">
                {storage.indexed_bytes != null ? `${formatBytes(storage.indexed_bytes)} · ${storage.indexed_files?.toLocaleString()} files` : "Not analyzed"}
              </dd>
              {used != null && (
                <>
                  <dt className="text-muted-foreground">{storage.is_local ? "Drive usage" : "Account usage"}</dt>
                  <dd className="text-right font-medium">
                    {formatBytes(used)}
                    {total ? ` of ${formatBytes(total)}` : ""}
                  </dd>
                </>
              )}
              {storage.potential_savings_bytes != null && (
                <>
                  <dt className="text-muted-foreground">Could free</dt>
                  <dd className="text-right font-medium text-warning">{formatBytes(storage.potential_savings_bytes)}</dd>
                </>
              )}
              <dt className="text-muted-foreground">Last synced</dt>
              <dd className="text-right" title={storage.last_synced_at ?? undefined}>
                {storage.last_synced_at ? formatRelativeTime(storage.last_synced_at) : "Never"}
              </dd>
            </dl>
            {percent != null && (
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-secondary" role="img" aria-label={`${Math.round(percent)}% used`}>
                <div className="h-full bg-primary" style={{ width: `${percent}%` }} />
              </div>
            )}
            {storage.breakdown.length > 0 && (
              <ul className="mt-3 flex flex-col gap-1">
                {storage.breakdown.slice(0, 6).map((row) => (
                  <li key={row.category} className="flex items-center gap-2 text-xs">
                    <span className="w-24 shrink-0 truncate">{row.category}</span>
                    <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-secondary">
                      <span className="block h-full bg-ai" style={{ width: `${topBytes ? (row.bytes / topBytes) * 100 : 0}%` }} />
                    </span>
                    <span className="w-16 shrink-0 text-right text-muted-foreground">{formatBytes(row.bytes)}</span>
                  </li>
                ))}
              </ul>
            )}
          </BlockCard>
        );
      })}
    </div>
  );
}

const RISK_LABEL = { low: "Low risk", medium: "Review first", high: "High impact" } as const;

function ActionProposalView({ block, ctx }: { block: ActionProposalBlock; ctx: BlockContext }) {
  const queryClient = useQueryClient();
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";
  const preview = usePreview();
  const [showFiles, setShowFiles] = useState(false);
  const decide = useMutation({
    mutationFn: (decision: "confirm" | "cancel") =>
      apiClient.post(`/v1/conversations/${ctx.conversationId}/actions/${block.action_id}/${decision}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["conversations", ctx.conversationId] }),
    onError: (error) => toast.error(error instanceof ApiError ? error.message : "That didn't go through."),
  });
  const plan = block.preview;
  return (
    <BlockCard className={cn("border-ai/30", block.status !== "pending" && "opacity-80")}>
      <div className="flex items-start justify-between gap-3 px-1">
        <div className="min-w-0">
          <p className="flex items-center gap-2 text-sm font-semibold">
            <ShieldCheck className="size-4 shrink-0 text-ai" /> {block.title}
          </p>
          <p className="mt-1 whitespace-pre-line text-sm text-muted-foreground">{block.description}</p>
        </div>
        <Badge variant={block.risk === "high" ? "destructive" : block.risk === "medium" ? "warning" : "outline"} className="shrink-0">
          {RISK_LABEL[block.risk]}
        </Badge>
      </div>

      {plan && (
        <div className="mt-3 rounded-lg bg-secondary/50 p-3 text-sm">
          <p className="flex items-center gap-2 font-medium">
            <FolderTree className="size-4 text-ai" /> {plan.root}
          </p>
          <ul className="ml-2 mt-1 flex flex-col gap-1 border-l border-border pl-3">
            {plan.folders.map((folder) => (
              <li key={folder.name}>
                <span className="flex items-center gap-2">
                  <Folder className="size-3.5 text-muted-foreground" />
                  <span className="font-medium">{folder.name}</span>
                  <span className="text-xs text-muted-foreground">
                    {folder.count} file{folder.count === 1 ? "" : "s"}
                    {folder.confidence != null ? ` · ${Math.round(folder.confidence * 100)}% sure` : ""}
                  </span>
                </span>
                {folder.reason && <span className="ml-5 block text-xs text-muted-foreground">{folder.reason}</span>}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-muted-foreground">
            {plan.folders_created} folders created · {plan.files_moved} files moved · {plan.files_renamed} renamed ·{" "}
            {plan.files_deleted} deleted
            {plan.unsure_count > 0 ? ` · ${plan.unsure_count} left where they are (not sure)` : ""}
          </p>
        </div>
      )}

      {block.items.length > 0 && (
        <div className="mt-2">
          <Button variant="ghost" size="sm" onClick={() => setShowFiles((v) => !v)}>
            {showFiles ? <ChevronDown /> : <ChevronRight />}
            {block.affected_count.toLocaleString()} file{block.affected_count === 1 ? "" : "s"} affected
          </Button>
          {showFiles && (
            <ul className="flex flex-col">
              {block.items.map((file) => (
                <FileRow key={file.id} file={file} onPreview={preview.open} />
              ))}
              {block.affected_count > block.items.length && (
                <p className="px-2 text-xs text-muted-foreground">…and {(block.affected_count - block.items.length).toLocaleString()} more</p>
              )}
            </ul>
          )}
        </div>
      )}

      <div className="mt-3 flex items-center justify-end gap-2">
        {block.status === "pending" && canManage && (
          <>
            <Button variant="outline" size="sm" disabled={decide.isPending} onClick={() => decide.mutate("cancel")}>
              Cancel
            </Button>
            <Button
              variant={block.risk === "high" ? "destructive" : "ai"}
              size="sm"
              disabled={decide.isPending}
              onClick={() => decide.mutate("confirm")}
            >
              {decide.isPending && <Loader2 className="animate-spin" />}
              {block.confirm_label}
            </Button>
          </>
        )}
        {block.status === "pending" && !canManage && (
          <p className="text-xs text-muted-foreground">Only an owner or admin can confirm changes.</p>
        )}
        {block.status === "confirmed" && <Badge variant="success">Confirmed</Badge>}
        {block.status === "cancelled" && <Badge variant="outline">Cancelled</Badge>}
      </div>
      {preview.dialog}
    </BlockCard>
  );
}

function StepRow({ step }: { step: ActionResultStep }) {
  const actionQuery = useQuery({
    queryKey: ["actions", step.plan_id],
    queryFn: () => apiClient.get<Action>(`/v1/actions/${step.plan_id}`),
    enabled: Boolean(step.plan_id),
    refetchInterval: (query) => (query.state.data && query.state.data.status !== "in_progress" ? false : 1500),
  });
  const action = actionQuery.data;
  const status = action
    ? action.status === "done"
      ? "done"
      : action.status === "in_progress"
        ? "running"
        : action.status === "partial"
          ? "partial"
          : "failed"
    : step.status;
  const message = action?.message ?? step.message;
  return (
    <li className="flex items-start gap-2.5 py-1.5 text-sm">
      {status === "done" && <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />}
      {status === "running" && <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin text-ai" />}
      {status === "partial" && <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" />}
      {status === "failed" && <XCircle className="mt-0.5 size-4 shrink-0 text-destructive" />}
      <span className="min-w-0">
        <span className="block font-medium">{step.label}</span>
        {message && <span className="block text-xs text-muted-foreground">{message}</span>}
        {action?.problems.slice(0, 3).map((problem) => (
          <span key={problem.file_name} className="block text-xs text-destructive">
            {problem.file_name}: {problem.reason}
          </span>
        ))}
      </span>
    </li>
  );
}

function ActionResultView({ block }: { block: ActionResultBlock }) {
  return (
    <BlockCard>
      <p className="px-1 text-sm font-semibold">{block.title}</p>
      <ul className="mt-1 flex flex-col divide-y divide-border px-1">
        {block.steps.map((step, index) => (
          <StepRow key={`${step.label}-${index}`} step={step} />
        ))}
      </ul>
      <p className="mt-2 px-1 text-xs text-muted-foreground">
        Each step is checked against the storage itself. <Link to="/dashboard" className="underline">Activity</Link> keeps the full record.
      </p>
    </BlockCard>
  );
}

function NoticeView({ block }: { block: NoticeBlock }) {
  const Icon = block.tone === "warning" ? AlertTriangle : Info;
  return (
    <div
      className={cn(
        "flex w-full items-start gap-2 rounded-lg px-3 py-2 text-xs",
        block.tone === "warning" ? "bg-warning-muted" : "bg-secondary",
      )}
    >
      <Icon className={cn("mt-0.5 size-3.5 shrink-0", block.tone === "warning" && "text-warning")} />
      <span>{block.text}</span>
    </div>
  );
}

function Block({ block, ctx }: { block: AnswerBlock; ctx: BlockContext }) {
  switch (block.type) {
    case "file_list":
      return <FileListView block={block} ctx={ctx} />;
    case "duplicate_groups":
      return <DuplicateGroupsView block={block} ctx={ctx} />;
    case "storage_summary":
      return <StorageSummaryView block={block} />;
    case "action_proposal":
      return <ActionProposalView block={block} ctx={ctx} />;
    case "action_result":
      return <ActionResultView block={block} />;
    case "notice":
      return <NoticeView block={block} />;
  }
}

/** The structured parts of an answer, in order. */
export function AnswerBlocks({ message, conversationId }: { message: ConversationMessage; conversationId: string }) {
  if (!message.blocks?.length) return null;
  return (
    <div className="flex w-full flex-col gap-2">
      {message.blocks.map((block, index) => (
        <Block key={index} block={block} ctx={{ conversationId, messageId: message.id, index }} />
      ))}
    </div>
  );
}
