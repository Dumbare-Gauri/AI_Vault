import { useMutation } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { Citation, ExecutionPlan } from "@vault/types";
import { Archive, ChevronDown, ChevronUp, FolderArchive } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table";
import { ApiError, apiClient } from "@/lib/api-client";
import { trackAction } from "@/lib/storage-action";
import { assistantToolLabel } from "@/lib/assistant-tool";
import { fileTypeIconElement } from "@/lib/file-icon";
import { formatBytes } from "@/lib/format-bytes";
import { retrievalMethodBadgeVariant, retrievalMethodLabel } from "@/lib/retrieval-method";
import { useAuthStore } from "@/stores/auth-store";

/** Turns a tool answer's cited files into an actionable list — not just a
 * "here's where this came from" link, but somewhere to actually pick files
 * and archive them, the same instant, reversible (Trash, not delete) path
 * every other cleanup action in the app already uses. Generalizes the previous
 * chat-only `CitationFileList` so any tool result with citations (file
 * search, largest/oldest/inactive files, cleanup candidates, duplicate
 * groups) renders the same structured list instead of flattened text. */
export function AIFileResultList({
  citations,
  toolName,
}: {
  citations: Citation[];
  toolName: string | null;
}) {
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Retrieval-method/confidence is debug-ish provenance info, not the
  // result itself — collapsed by default so the file list reads clean.
  const [showDetails, setShowDetails] = useState(false);

  const archiveMutation = useMutation({
    mutationFn: () =>
      apiClient.post<ExecutionPlan>("/v1/execution-plans", {
        file_ids: Array.from(selected),
        action_type: "archive",
      }),
    onSuccess: (plan) => {
      setSelected(new Set());
      void trackAction(plan.id, "Moving to Trash…");
    },
  });

  const createArchiveMutation = useMutation({
    mutationFn: () =>
      apiClient.post<ExecutionPlan>("/v1/execution-plans", {
        file_ids: Array.from(selected),
        action_type: "create_archive",
      }),
    onSuccess: (plan) => {
      setSelected(new Set());
      void trackAction(plan.id, "Creating archive…");
    },
  });

  function toggle(fileId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(fileId)) next.delete(fileId);
      else next.add(fileId);
      return next;
    });
  }

  return (
    <div className="w-full max-w-[85%] rounded-xl border border-border bg-card p-3 shadow-clay-sm">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <p className="text-xs font-medium text-muted-foreground">
            {toolName ? assistantToolLabel(toolName) : "Sources"} ({citations.length})
          </p>
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-1.5 text-xs text-muted-foreground"
            onClick={() => setShowDetails((prev) => !prev)}
          >
            {showDetails ? <ChevronUp className="size-3" /> : <ChevronDown className="size-3" />}
            View match details
          </Button>
        </div>
        {canManage && selected.size > 0 && (
          <div className="flex items-center gap-1.5">
            <Button
              size="sm"
              variant="outline"
              disabled={createArchiveMutation.isPending}
              onClick={() => createArchiveMutation.mutate()}
            >
              <FolderArchive className="size-3.5" />
              {createArchiveMutation.isPending ? "Creating plan…" : "Create Archive"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={archiveMutation.isPending}
              onClick={() => archiveMutation.mutate()}
            >
              <Archive className="size-3.5" />
              {archiveMutation.isPending ? "Creating plan…" : `Archive ${selected.size} selected`}
            </Button>
          </div>
        )}
      </div>

      <Table>
        <TableBody>
          {citations.map((citation) => (
            <TableRow key={citation.id}>
              {canManage && (
                <TableCell className="w-8">
                  <Checkbox
                    checked={selected.has(citation.file_id)}
                    onCheckedChange={() => toggle(citation.file_id)}
                    aria-label={`Select ${citation.file_name ?? "this file"}`}
                  />
                </TableCell>
              )}
              <TableCell className="w-8">
                <span className="flex size-6 shrink-0 items-center justify-center rounded-md bg-secondary text-muted-foreground">
                  {fileTypeIconElement(citation.file_mime_type, { className: "size-3.5" })}
                </span>
              </TableCell>
              <TableCell>
                <Link
                  to="/files/$fileId"
                  params={{ fileId: citation.file_id }}
                  className="block min-w-0"
                >
                  <span className="block truncate font-medium text-foreground/90">
                    {citation.file_name ?? "View file"}
                  </span>
                  {citation.snippet && (
                    <span className="block truncate text-muted-foreground">
                      {citation.snippet.slice(0, 80).replaceAll("\n", " ")}
                    </span>
                  )}
                </Link>
              </TableCell>
              <TableCell className="whitespace-nowrap text-right text-muted-foreground">
                {citation.file_size_bytes !== null && formatBytes(citation.file_size_bytes)}
              </TableCell>
              {showDetails && (
                <TableCell className="whitespace-nowrap text-right">
                  <span className="flex items-center justify-end gap-1.5">
                    <Badge variant={retrievalMethodBadgeVariant(citation.retrieval_method)}>
                      {retrievalMethodLabel(citation.retrieval_method)}
                    </Badge>
                    <span className="text-muted-foreground">
                      {Math.round(citation.confidence * 100)}%
                    </span>
                  </span>
                </TableCell>
              )}
            </TableRow>
          ))}
        </TableBody>
      </Table>

      {(archiveMutation.isError || createArchiveMutation.isError) && (
        <p className="mt-2 text-xs text-destructive">
          {(archiveMutation.error ?? createArchiveMutation.error) instanceof ApiError
            ? (archiveMutation.error ?? createArchiveMutation.error)?.message
            : "Couldn't start that — nothing was changed."}
        </p>
      )}
      {canManage && selected.size === 0 && (
        <p className="mt-2 text-xs text-muted-foreground">
          Select files above to archive them (runs immediately, reversible).
        </p>
      )}
    </div>
  );
}
