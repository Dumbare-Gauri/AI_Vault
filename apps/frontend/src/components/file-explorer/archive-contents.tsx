import { useQuery } from "@tanstack/react-query";
import type { ArchiveContents as Contents } from "@vault/types";
import { AlertTriangle, FileArchive, Folder } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, apiClient } from "@/lib/api-client";
import { formatBytes } from "@/lib/format-bytes";

const SHOWN = 200;

/** What a zip holds, read on request from the storage — nothing is
 * unpacked. Entries that would land outside their folder are marked; they
 * are skipped on extraction. */
export function ArchiveContents({ fileId }: { fileId: string }) {
  const [requested, setRequested] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const contentsQuery = useQuery({
    queryKey: ["archive-contents", fileId],
    queryFn: () => apiClient.get<Contents>(`/v1/files/${fileId}/archive-contents`),
    enabled: requested,
    staleTime: 60_000,
  });
  const contents = contentsQuery.data;
  const entries = contents ? (showAll ? contents.entries : contents.entries.slice(0, SHOWN)) : [];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FileArchive className="size-4" /> What&rsquo;s inside
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        {!requested && (
          <div className="flex items-center justify-between gap-3">
            <p className="text-muted-foreground">Read the list of files in this archive without unpacking it.</p>
            <Button size="sm" variant="outline" onClick={() => setRequested(true)}>
              Show contents
            </Button>
          </div>
        )}
        {contentsQuery.isLoading && <Skeleton className="h-24 rounded-lg" />}
        {contentsQuery.isError && (
          <p role="alert" className="text-destructive">
            {contentsQuery.error instanceof ApiError ? contentsQuery.error.message : "Couldn't read this archive."}
          </p>
        )}
        {contents && (
          <>
            <p>
              <span className="font-medium">{contents.file_count.toLocaleString()} files</span>,{" "}
              {formatBytes(contents.total_bytes)} unpacked.
            </p>
            {contents.unsafe_count > 0 && (
              <p className="flex items-start gap-2 rounded-lg bg-destructive-muted p-3 text-destructive">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                {contents.unsafe_count.toLocaleString()} entr{contents.unsafe_count === 1 ? "y points" : "ies point"} outside
                the archive&rsquo;s folder. Extracting skips {contents.unsafe_count === 1 ? "it" : "them"}.
              </p>
            )}
            <ul className="max-h-80 divide-y divide-border overflow-y-auto rounded-lg border border-border">
              {entries.map((entry, index) => (
                <li key={`${entry.name}-${index}`} className="flex items-center gap-3 px-3 py-1.5">
                  {entry.is_folder ? (
                    <Folder className="size-4 shrink-0 text-muted-foreground" />
                  ) : (
                    <span className="size-4 shrink-0" />
                  )}
                  <span className={entry.safe ? "min-w-0 flex-1 truncate" : "min-w-0 flex-1 truncate text-destructive"}>
                    {entry.name}
                  </span>
                  {!entry.is_folder && (
                    <span className="shrink-0 text-xs text-muted-foreground">{formatBytes(entry.size_bytes)}</span>
                  )}
                </li>
              ))}
            </ul>
            {contents.entries.length > SHOWN && !showAll && (
              <Button size="sm" variant="ghost" className="self-start" onClick={() => setShowAll(true)}>
                Show all {contents.entries.length.toLocaleString()}
              </Button>
            )}
            {contents.entry_count > contents.entries.length && (
              <p className="text-xs text-muted-foreground">
                Listing the first {contents.entries.length.toLocaleString()} of {contents.entry_count.toLocaleString()}{" "}
                entries; the counts above cover all of them.
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
