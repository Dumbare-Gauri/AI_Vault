import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { Connector, FileDetail } from "@vault/types";
import {
  Archive,
  BrainCircuit,
  ChevronLeft,
  Download,
  ExternalLink,
  FileWarning,
  FolderInput,
  PencilLine,
  Sparkles,
  Tag,
  Trash2,
  Users,
} from "lucide-react";
import { useState } from "react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ArchiveDialog } from "@/components/file-explorer/archive-dialog";
import { MoveDialog } from "@/components/file-explorer/move-dialog";
import { RenameDialog } from "@/components/file-explorer/rename-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { downloadFile } from "@/lib/download-file";
import { fileTypeIconElement, fileTypeLabel } from "@/lib/file-icon";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { runStorageAction } from "@/lib/storage-action";
import { entityTypeLabel, lifecycleBadgeVariant, lifecycleLabel } from "@/lib/organization-style";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/files/$fileId")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: FileDetailPage,
});

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right font-medium">{value}</span>
    </div>
  );
}

function FileDetailPage() {
  const { fileId } = Route.useParams();
  const queryClient = useQueryClient();
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";
  const [renaming, setRenaming] = useState(false);
  const [moving, setMoving] = useState(false);
  const [archiving, setArchiving] = useState(false);

  const fileQuery = useQuery({
    queryKey: ["files", "detail", fileId],
    queryFn: () => apiClient.get<FileDetail>(`/v1/files/${fileId}`),
  });

  const connectorsQuery = useQuery({
    queryKey: ["connectors"],
    queryFn: () => apiClient.get<Connector[]>("/v1/connectors"),
    enabled: canManage,
  });
  const connector = connectorsQuery.data?.find(
    (candidate) => candidate.provider === "google_workspace" && candidate.status === "connected",
  );

  const file = fileQuery.data;
  const isOwned = file !== undefined && file.owner_email === connector?.account_email;

  const downloadMutation = useMutation({
    mutationFn: () => downloadFile(`/v1/files/${fileId}/download`, file?.name ?? "download"),
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't download this file.");
    },
  });

  function refreshAfterFileOp() {
    void queryClient.invalidateQueries({ queryKey: ["files", "detail", fileId] });
  }

  return (
    <AppShell title="File details">
      <div className="mx-auto flex max-w-4xl flex-col gap-4">
        <Link
          to="/files"
          className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ChevronLeft className="size-4" /> Back to files
        </Link>

        {fileQuery.isLoading && (
          <div className="flex flex-col gap-4">
            <Skeleton className="h-24 rounded-2xl" />
            <Skeleton className="h-48 rounded-2xl" />
          </div>
        )}
        {fileQuery.isError && (
          <EmptyState title="Couldn't load this file" description="Please try again." />
        )}

        {file && (
          <>
            <Card clay className="flex flex-col gap-4 p-6 sm:flex-row sm:items-center">
              <div className="flex size-14 shrink-0 items-center justify-center rounded-2xl bg-primary/10 text-primary">
                {fileTypeIconElement(file.mime_type, { className: "size-7" })}
              </div>
              <div className="min-w-0 flex-1">
                <h1 className="truncate text-lg font-semibold">{file.name}</h1>
                <p className="truncate text-sm text-muted-foreground">{file.path}</p>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Badge variant="outline">{fileTypeLabel(file.mime_type)}</Badge>
                  {file.size_bytes !== null && (
                    <Badge variant="outline">{formatBytes(file.size_bytes)}</Badge>
                  )}
                  {file.is_shared ? (
                    <Badge variant="warning">
                      <Users className="size-3" /> Shared
                    </Badge>
                  ) : null}
                  {file.classification ? (
                    <Badge variant="ai">
                      <Sparkles className="size-3" /> {file.classification.document_type}
                    </Badge>
                  ) : null}
                  {file.lifecycle ? (
                    <Badge variant={lifecycleBadgeVariant(file.lifecycle.state)}>
                      {lifecycleLabel(file.lifecycle.state)}
                    </Badge>
                  ) : null}
                  {file.entity_links.map((link) => (
                    <Badge key={link.entity_id} variant="outline">
                      <Tag className="size-3" /> {entityTypeLabel(link.entity_type)}:{" "}
                      {link.entity_name}
                    </Badge>
                  ))}
                </div>
              </div>
              <div className="flex shrink-0 flex-wrap items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={downloadMutation.isPending}
                  onClick={() => downloadMutation.mutate()}
                >
                  <Download className="size-4" />
                  {downloadMutation.isPending ? "Downloading…" : "Download"}
                </Button>
                {canManage && isOwned && (
                  <Button variant="outline" size="sm" onClick={() => setRenaming(true)}>
                    <PencilLine className="size-4" /> Rename
                  </Button>
                )}
                {canManage && isOwned && connector && (
                  <Button variant="outline" size="sm" onClick={() => setMoving(true)}>
                    <FolderInput className="size-4" /> Move
                  </Button>
                )}
                {canManage && isOwned && (
                  <Button variant="outline" size="sm" onClick={() => setArchiving(true)}>
                    <Archive className="size-4" /> Archive
                  </Button>
                )}
                {canManage && isOwned && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      void runStorageAction(
                        { file_ids: [file.id], action_type: "archive" },
                        "Moving to Trash…",
                      )
                    }
                  >
                    <Trash2 className="size-4" /> Move to Trash
                  </Button>
                )}
                {file.web_view_link && (
                  <Button asChild variant="outline" size="sm">
                    <a href={file.web_view_link} target="_blank" rel="noopener noreferrer">
                      <ExternalLink className="size-4" /> Open in Google Drive
                    </a>
                  </Button>
                )}
              </div>
            </Card>

            <div className="grid gap-4 lg:grid-cols-3">
              <div className="flex flex-col gap-4 lg:col-span-2">
                <Card>
                  <CardHeader>
                    <CardTitle className="flex items-center gap-2">
                      <BrainCircuit className="size-4 text-ai" /> AI understanding
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="flex flex-col gap-4">
                    {file.classification ? (
                      <div className="flex items-center justify-between rounded-lg bg-ai-muted p-3 text-sm">
                        <span className="font-medium text-ai">{file.classification.document_type}</span>
                        <span className="text-muted-foreground">
                          {Math.round(file.classification.confidence * 100)}% confidence ·{" "}
                          {file.classification.method}
                        </span>
                      </div>
                    ) : (
                      <p className="text-sm text-muted-foreground">Not classified yet.</p>
                    )}

                    {file.knowledge_attributes.length > 0 ? (
                      <div className="flex flex-wrap gap-2">
                        {file.knowledge_attributes.map((attribute) => (
                          <span
                            key={`${attribute.attribute_type}-${attribute.value}`}
                            className="flex items-center gap-1 rounded-full bg-secondary px-3 py-1 text-xs"
                          >
                            <Tag className="size-3 text-muted-foreground" />
                            {attribute.attribute_type}: <strong>{attribute.value}</strong>
                            <span className="text-muted-foreground">
                              {Math.round(attribute.confidence * 100)}%
                            </span>
                          </span>
                        ))}
                      </div>
                    ) : null}

                    <div className="rounded-lg border border-border p-3 text-sm">
                      {file.extraction ? (
                        <div className="flex items-start gap-2">
                          {file.extraction.status === "failed" ? (
                            <FileWarning className="mt-0.5 size-4 shrink-0 text-destructive" />
                          ) : (
                            <Sparkles className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                          )}
                          <div>
                            <p className="font-medium capitalize">
                              Content extraction: {file.extraction.status.replaceAll("_", " ")}
                            </p>
                            {file.extraction.extractor_name && (
                              <p className="text-muted-foreground">
                                via {file.extraction.extractor_name}
                                {file.extraction.char_count !== null
                                  ? ` · ${file.extraction.char_count.toLocaleString()} characters`
                                  : ""}
                              </p>
                            )}
                            {file.extraction.error && (
                              <p className="text-destructive">{file.extraction.error}</p>
                            )}
                          </div>
                        </div>
                      ) : (
                        <p className="text-muted-foreground">Content not processed yet.</p>
                      )}
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle className="flex items-center gap-2">
                      <BrainCircuit className="size-4 text-ai" /> AI Intelligence
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="flex flex-col gap-4">
                    {!file.intelligence && (
                      <p className="text-sm text-muted-foreground">Not analyzed yet.</p>
                    )}
                    {file.intelligence?.status === "unsupported" && (
                      <p className="text-sm text-muted-foreground">
                        No extracted text to analyze for this file.
                      </p>
                    )}
                    {file.intelligence?.status === "failed" && (
                      <div className="flex items-start gap-2 rounded-lg border border-border p-3 text-sm">
                        <FileWarning className="mt-0.5 size-4 shrink-0 text-destructive" />
                        <div>
                          <p className="font-medium">AI analysis failed</p>
                          {file.intelligence.error && (
                            <p className="text-destructive">{file.intelligence.error}</p>
                          )}
                        </div>
                      </div>
                    )}
                    {file.intelligence?.status === "success" && (
                      <>
                        <div className="flex flex-wrap items-center gap-2">
                          {file.intelligence.document_type && (
                            <Badge variant="ai">
                              <Sparkles className="size-3" /> {file.intelligence.document_type}
                            </Badge>
                          )}
                          {file.intelligence.confidence !== null && (
                            <span className="text-xs text-muted-foreground">
                              {Math.round(file.intelligence.confidence * 100)}% confidence
                            </span>
                          )}
                        </div>

                        {file.intelligence.summary && (
                          <p className="rounded-lg bg-ai-muted p-3 text-sm text-ai">
                            {file.intelligence.summary}
                          </p>
                        )}

                        {file.intelligence.topics.length > 0 && (
                          <div className="flex flex-wrap gap-2">
                            {file.intelligence.topics.map((topic) => (
                              <Badge key={topic} variant="outline">
                                {topic}
                              </Badge>
                            ))}
                          </div>
                        )}

                        {file.intelligence.entities.length > 0 && (
                          <div className="flex flex-wrap gap-2">
                            {file.intelligence.entities.map((entity, index) => (
                              <span
                                // Entity values aren't guaranteed unique
                                // (e.g. two "party" entities can repeat),
                                // unlike knowledge_attributes' constraint.
                                key={`${entity.type}-${entity.value}-${index}`}
                                className="flex items-center gap-1 rounded-full bg-secondary px-3 py-1 text-xs"
                              >
                                <Tag className="size-3 text-muted-foreground" />
                                {entity.type}: <strong>{entity.value}</strong>
                                {entity.confidence !== null && (
                                  <span className="text-muted-foreground">
                                    {Math.round(entity.confidence * 100)}%
                                  </span>
                                )}
                              </span>
                            ))}
                          </div>
                        )}

                        {Object.keys(file.intelligence.structured_metadata).length > 0 && (
                          <div className="rounded-lg border border-border p-3 divide-y divide-border">
                            {Object.entries(file.intelligence.structured_metadata).map(
                              ([key, value]) => (
                                <Field
                                  key={key}
                                  label={key.replaceAll("_", " ")}
                                  value={String(value)}
                                />
                              ),
                            )}
                          </div>
                        )}

                        <p className="text-xs text-muted-foreground">
                          {file.intelligence.provider} · {file.intelligence.model_name} ·{" "}
                          {formatRelativeTime(file.intelligence.processed_at)}
                        </p>
                      </>
                    )}
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle>Related files</CardTitle>
                  </CardHeader>
                  <CardContent>
                    {file.related_files.length === 0 ? (
                      <p className="py-4 text-center text-sm text-muted-foreground">
                        No relationships discovered yet.
                      </p>
                    ) : (
                      <ul className="flex flex-col divide-y divide-border">
                        {file.related_files.map((related) => (
                          <li key={`${related.file_id}-${related.relationship_type}`}>
                            <Link
                              to="/files/$fileId"
                              params={{ fileId: related.file_id }}
                              className="flex items-center justify-between gap-3 py-2.5 text-sm transition-colors hover:text-primary"
                            >
                              <div className="min-w-0">
                                <p className="truncate font-medium">{related.name}</p>
                                <p className="truncate text-xs text-muted-foreground">{related.path}</p>
                              </div>
                              <Badge variant="outline" className="shrink-0">
                                {related.relationship_type.replaceAll("_", " ")} ·{" "}
                                {Math.round(related.confidence * 100)}%
                              </Badge>
                            </Link>
                          </li>
                        ))}
                      </ul>
                    )}
                  </CardContent>
                </Card>
              </div>

              <Card className="h-fit">
                <CardHeader>
                  <CardTitle>File information</CardTitle>
                </CardHeader>
                <CardContent className="divide-y divide-border">
                  <Field
                    label="Modified"
                    value={
                      file.provider_modified_at ? formatRelativeTime(file.provider_modified_at) : "—"
                    }
                  />
                  <Field label="Owner" value={file.owner_email ?? file.metadata?.owner_summary ?? "—"} />
                  <Field label="Sharing" value={file.metadata?.sharing_summary ?? "—"} />
                  <Field label="Extension" value={file.metadata?.normalized_extension ?? "—"} />
                  <Field label="Version" value={file.metadata?.version_label ?? "—"} />
                  <Field label="Language" value={file.metadata?.language ?? "Unknown"} />
                  <Field
                    label="MIME validated"
                    value={
                      file.metadata
                        ? file.metadata.mime_type_validated
                          ? "Yes"
                          : (file.metadata.mime_mismatch_reason ?? "No")
                        : "—"
                    }
                  />
                  <Field
                    label="Enriched"
                    value={file.metadata ? formatRelativeTime(file.metadata.enriched_at) : "Not yet"}
                  />
                </CardContent>
              </Card>
            </div>
          </>
        )}
      </div>

      {file && renaming && (
        <RenameDialog
          onOpenChange={setRenaming}
          fileId={file.id}
          currentName={file.name}
          onRenamed={refreshAfterFileOp}
        />
      )}
      {file && connector && moving && (
        <MoveDialog
          onOpenChange={setMoving}
          connectorId={connector.id}
          fileIds={[file.id]}
          onMoved={refreshAfterFileOp}
        />
      )}
      {file && archiving && <ArchiveDialog onOpenChange={setArchiving} fileIds={[file.id]} />}
    </AppShell>
  );
}
