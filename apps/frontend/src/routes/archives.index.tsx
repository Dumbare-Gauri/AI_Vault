import { useQuery } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { ArchiveJob } from "@vault/types";
import { Archive } from "lucide-react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { apiClient } from "@/lib/api-client";
import { archiveStatusBadgeVariant, archiveStatusLabel } from "@/lib/archive-style";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { useAuthStore } from "@/stores/auth-store";
import { useCopilotPage } from "@/stores/copilot-store";

export const Route = createFileRoute("/archives/")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: ArchivesPage,
});

function ArchivesPage() {
  useCopilotPage({ page: "archives", label: "Archives" });
  const archivesQuery = useQuery({
    queryKey: ["archives"],
    queryFn: () => apiClient.get<ArchiveJob[]>("/v1/archives"),
  });

  return (
    <AppShell title="Archives">
      <div className="mx-auto flex max-w-4xl flex-col gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Archives</h1>
          <p className="text-sm text-muted-foreground">
            Compressed packages created from files selected in Ask Vault or Storage Intelligence.
            Originals are never touched by creating an archive.
          </p>
        </div>

        {archivesQuery.isLoading && (
          <div className="flex flex-col gap-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-16 rounded-xl" />
            ))}
          </div>
        )}
        {archivesQuery.isError && (
          <EmptyState title="Couldn't load archives" description="Please try again." />
        )}
        {archivesQuery.data?.length === 0 && (
          <EmptyState
            icon={Archive}
            title="No archives yet"
            description="Select files in Ask Vault or a Storage Intelligence listing and choose 'Create Archive' to get started."
          />
        )}
        {archivesQuery.data && archivesQuery.data.length > 0 && (
          <div className="flex flex-col gap-2">
            {archivesQuery.data.map((archive) => (
              <Link key={archive.id} to="/archives/$archiveId" params={{ archiveId: archive.id }}>
                <Card className="flex items-center justify-between gap-3 p-4 transition-colors hover:bg-secondary/60">
                  <div className="flex min-w-0 items-center gap-3">
                    <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-secondary text-muted-foreground">
                      <Archive className="size-4" />
                    </span>
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">{archive.name}</p>
                      <p className="text-xs text-muted-foreground">
                        {archive.file_count} {archive.file_count === 1 ? "file" : "files"} ·{" "}
                        {formatRelativeTime(archive.created_at)}
                      </p>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-3">
                    {archive.compressed_size_bytes !== null && (
                      <span className="text-sm text-muted-foreground">
                        {formatBytes(archive.compressed_size_bytes)}
                      </span>
                    )}
                    <Badge variant={archiveStatusBadgeVariant(archive.status)}>
                      {archiveStatusLabel(archive.status)}
                    </Badge>
                  </div>
                </Card>
              </Link>
            ))}
          </div>
        )}
      </div>
    </AppShell>
  );
}
