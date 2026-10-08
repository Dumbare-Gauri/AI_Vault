import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Download, ExternalLink, FileQuestion } from "lucide-react";
import { useEffect, useMemo } from "react";

import { MarkdownMessage } from "@/components/markdown-message";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, apiClient } from "@/lib/api-client";
import { formatBytes } from "@/lib/format-bytes";

/** The least a viewer needs to know about a file. */
export interface PreviewableFile {
  id: string;
  name: string;
  mime_type: string | null;
  size_bytes: number | null;
  path?: string | null;
  storage?: string | null;
}

type PreviewKind = "pdf" | "image" | "video" | "audio" | "markdown" | "text" | "none";

const MAX_PREVIEW_BYTES = 50 * 1024 * 1024;
const TEXT_EXTENSIONS = new Set([
  "txt", "csv", "tsv", "json", "log", "xml", "yaml", "yml", "toml", "ini", "py", "js", "jsx",
  "ts", "tsx", "java", "go", "rs", "rb", "php", "c", "cpp", "h", "cs", "swift", "kt", "sql",
  "sh", "ps1", "html", "css", "scss", "vue",
]); // prettier-ignore

function previewKind(file: PreviewableFile): PreviewKind {
  const mime = file.mime_type ?? "";
  const extension = file.name.includes(".") ? file.name.split(".").pop()!.toLowerCase() : "";
  if (mime === "application/pdf" || extension === "pdf") return "pdf";
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("video/")) return "video";
  if (mime.startsWith("audio/")) return "audio";
  if (extension === "md" || mime === "text/markdown") return "markdown";
  if (mime.startsWith("text/") || TEXT_EXTENSIONS.has(extension)) return "text";
  return "none";
}

function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** Views a file wherever it appears — Ask Vault, search, lists — without
 * leaving the page. Formats the browser can't show get details and a download. */
export function FilePreviewDialog({
  file,
  onOpenChange,
}: {
  file: PreviewableFile;
  onOpenChange: (open: boolean) => void;
}) {
  const kind = previewKind(file);
  const tooLarge = (file.size_bytes ?? 0) > MAX_PREVIEW_BYTES;
  const canPreview = kind !== "none" && !tooLarge;

  const contentQuery = useQuery({
    queryKey: ["file-preview", file.id],
    queryFn: () => apiClient.blob(`/v1/files/${file.id}/download`),
    enabled: canPreview,
    staleTime: 5 * 60 * 1000,
    retry: 1,
  });
  const blob = contentQuery.data;
  const url = useMemo(() => (blob && kind !== "text" && kind !== "markdown" ? URL.createObjectURL(blob) : null), [blob, kind]);
  useEffect(() => () => (url ? URL.revokeObjectURL(url) : undefined), [url]);

  const textQuery = useQuery({
    queryKey: ["file-preview-text", file.id],
    queryFn: () => blob!.text(),
    enabled: Boolean(blob) && (kind === "text" || kind === "markdown"),
  });

  async function download() {
    saveBlob(blob ?? (await apiClient.blob(`/v1/files/${file.id}/download`)), file.name);
  }

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90vh] w-[min(960px,95vw)] max-w-none flex-col">
        <DialogHeader>
          <DialogTitle className="truncate pr-8">{file.name}</DialogTitle>
          <DialogDescription className="truncate">
            {[file.storage, file.path, file.size_bytes != null ? formatBytes(file.size_bytes) : null]
              .filter(Boolean)
              .join(" · ")}
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-secondary/40">
          {!canPreview && (
            <div className="flex flex-col items-center gap-2 p-10 text-center text-sm text-muted-foreground">
              <FileQuestion className="size-8" />
              {tooLarge
                ? "This file is too large to preview here — download it instead."
                : "This type of file can't be previewed in the browser."}
            </div>
          )}
          {canPreview && contentQuery.isPending && <Skeleton className="h-[60vh] w-full" />}
          {contentQuery.isError && (
            <div className="flex flex-col items-start gap-3 p-6 text-sm">
              <p className="text-destructive">
                {contentQuery.error instanceof ApiError
                  ? contentQuery.error.message
                  : "Couldn't reach AI Vault to open this file — the connection dropped or the server was restarting."}
              </p>
              <Button variant="outline" size="sm" onClick={() => void contentQuery.refetch()}>
                Try again
              </Button>
            </div>
          )}
          {url && kind === "pdf" && <iframe title={file.name} src={url} className="h-[70vh] w-full" />}
          {url && kind === "image" && (
            <img src={url} alt={file.name} className="mx-auto max-h-[70vh] object-contain" />
          )}
          {url && kind === "video" && <video src={url} controls className="mx-auto max-h-[70vh]" />}
          {url && kind === "audio" && <audio src={url} controls className="m-6 w-[calc(100%-3rem)]" />}
          {textQuery.data !== undefined && kind === "markdown" && (
            <div className="p-4">
              <MarkdownMessage content={textQuery.data} />
            </div>
          )}
          {textQuery.data !== undefined && kind === "text" && (
            <pre className="whitespace-pre-wrap break-words p-4 font-mono text-xs leading-relaxed">
              {textQuery.data}
            </pre>
          )}
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="outline" asChild>
            <Link to="/files/$fileId" params={{ fileId: file.id }}>
              <ExternalLink className="size-4" /> Details
            </Link>
          </Button>
          <Button onClick={() => void download()}>
            <Download className="size-4" /> Download
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
