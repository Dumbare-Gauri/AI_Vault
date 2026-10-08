import { useQuery } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { DuplicateGroupListResponse } from "@vault/types";
import { ChevronLeft, ChevronRight, Copy } from "lucide-react";
import { useState } from "react";

import { AppShell } from "@/components/app-shell/app-shell";
import { StorageScopeSwitcher, useStorageScope } from "@/components/storage-intelligence/storage-scope-switcher";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { apiClient } from "@/lib/api-client";
import { formatBytes } from "@/lib/format-bytes";
import { useAuthStore } from "@/stores/auth-store";
import { scopedPath } from "@/stores/storage-scope-store";

export const Route = createFileRoute("/storage-intelligence/duplicates/")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: DuplicateGroupsPage,
});

const PAGE_SIZE = 20;

function DuplicateGroupsPage() {
  const [page, setPage] = useState(0);
  const { connectorId } = useStorageScope();

  const groupsQuery = useQuery({
    queryKey: ["storage-intelligence", "duplicates", page, connectorId],
    queryFn: () =>
      apiClient.get<DuplicateGroupListResponse>(
        scopedPath(`/v1/storage/duplicates?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`, connectorId),
      ),
  });

  const total = groupsQuery.data?.total ?? 0;

  return (
    <AppShell title="Duplicate files">
      <div className="mx-auto flex max-w-4xl flex-col gap-4">
        <div>
          <Link
            to="/storage-intelligence"
            className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ChevronLeft className="size-4" /> Back to Storage Intelligence
          </Link>
          <h1 className="mt-2 text-xl font-semibold tracking-tight">Duplicate files</h1>
          <p className="text-sm text-muted-foreground">
            {total > 0
              ? `${total.toLocaleString()} groups of files with identical content.`
              : "Exact-duplicate groups, based on real content checksums."}
          </p>
        </div>
        <StorageScopeSwitcher />

        {groupsQuery.isLoading && (
          <div className="flex flex-col gap-2">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-16 rounded-lg" />
            ))}
          </div>
        )}
        {groupsQuery.isError && (
          <EmptyState title="Couldn't load duplicate files" description="Please try again." />
        )}
        {groupsQuery.data?.items.length === 0 && (
          <EmptyState
            icon={Copy}
            title="No duplicate files found"
            description="Run a storage analysis to check for exact-duplicate files."
          />
        )}

        {groupsQuery.data && groupsQuery.data.items.length > 0 && (
          <Card className="flex flex-col divide-y divide-border p-1">
            {groupsQuery.data.items.map((group) => (
              <Link
                key={group.id}
                to="/storage-intelligence/duplicates/$groupId"
                params={{ groupId: group.id }}
                className="flex items-center justify-between gap-4 rounded-lg px-3 py-3 text-sm transition-colors hover:bg-secondary/60"
              >
                <div className="flex items-center gap-3">
                  <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-secondary text-muted-foreground">
                    <Copy className="size-4" aria-hidden="true" />
                  </span>
                  <div>
                    <p className="font-medium">{group.file_count} copies</p>
                    <p className="text-xs text-muted-foreground">
                      {formatBytes(group.total_size_bytes)} total
                    </p>
                  </div>
                </div>
                <span className="font-medium text-primary">
                  {formatBytes(group.recoverable_size_bytes)} recoverable
                </span>
              </Link>
            ))}
          </Card>
        )}

        {total > PAGE_SIZE && (
          <div className="flex items-center justify-between text-sm text-muted-foreground">
            <span>
              Showing {page * PAGE_SIZE + 1}–{Math.min(total, (page + 1) * PAGE_SIZE)} of{" "}
              {total.toLocaleString()}
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
      </div>
    </AppShell>
  );
}
