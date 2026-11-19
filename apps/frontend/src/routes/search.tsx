import { useMutation } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { SearchRequest, SearchResponse, SearchSort } from "@vault/types";
import { ExternalLink, Search as SearchIcon, SearchX, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { z } from "zod";

import { AppShell } from "@/components/app-shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { apiClient } from "@/lib/api-client";
import { fileTypeIconElement } from "@/lib/file-icon";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/auth-store";

const searchParamsSchema = z.object({
  q: z.string().optional(),
});

export const Route = createFileRoute("/search")({
  validateSearch: searchParamsSchema,
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: SearchPage,
});

const TYPE_CHIPS: { value: string; label: string }[] = [
  { value: "document", label: "Documents" },
  { value: "pdf", label: "PDFs" },
  { value: "spreadsheet", label: "Spreadsheets" },
  { value: "presentation", label: "Presentations" },
  { value: "image", label: "Images" },
  { value: "video", label: "Videos" },
  { value: "design", label: "Design" },
  { value: "code", label: "Code" },
  { value: "archive", label: "Zip & archives" },
];

const EXAMPLES = ["invoices from last year", "PDFs over 10 MB", "presentations in Clients"];

function SearchPage() {
  const { q } = Route.useSearch();
  const [query, setQuery] = useState(q ?? "");
  const [categories, setCategories] = useState<string[]>([]);
  const [sort, setSort] = useState<SearchSort>("relevance");

  const searchMutation = useMutation({
    mutationFn: (request: SearchRequest) => apiClient.post<SearchResponse>("/v1/search", request),
  });

  function run(text: string, nextCategories = categories, nextSort = sort) {
    const hasFilters = nextCategories.length > 0 || nextSort !== "relevance";
    if (text.trim().length === 0 && !hasFilters) return;
    searchMutation.mutate({
      query: text.trim(),
      filters: hasFilters ? { categories: nextCategories, sort: nextSort } : null,
      limit: 100,
    });
  }

  useEffect(() => {
    if (q && q.trim().length > 0) {
      searchMutation.mutate({ query: q.trim(), limit: 100 });
    }
    // Only re-run when the incoming ?q= changes (e.g. from the command palette).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  function toggleCategory(value: string) {
    const next = categories.includes(value)
      ? categories.filter((category) => category !== value)
      : [...categories, value];
    setCategories(next);
    run(query, next);
  }

  function changeSort(value: SearchSort) {
    setSort(value);
    run(query, categories, value);
  }

  const data = searchMutation.data;
  const results = data?.results ?? [];

  return (
    <AppShell title="Search">
      <div className="mx-auto flex max-w-3xl flex-col gap-5">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Search</h1>
          <p className="text-sm text-muted-foreground">
            Search by name, type, folder, client or project, size, or date — or just describe what
            you&rsquo;re looking for.
          </p>
        </div>

        <form
          onSubmit={(event) => {
            event.preventDefault();
            run(query);
          }}
          className="flex gap-2"
        >
          <div className="relative flex-1">
            <SearchIcon className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="PDFs from the Acme project over 5 MB…"
              className="pl-9"
              autoFocus
            />
          </div>
          <Button type="submit" disabled={searchMutation.isPending}>
            {searchMutation.isPending ? "Searching…" : "Search"}
          </Button>
        </form>

        <div className="flex flex-wrap items-center gap-2">
          {TYPE_CHIPS.map((chip) => {
            const active = categories.includes(chip.value);
            return (
              <button
                key={chip.value}
                type="button"
                aria-pressed={active}
                onClick={() => toggleCategory(chip.value)}
                className={cn(
                  "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                  active
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-border bg-card text-muted-foreground hover:text-foreground",
                )}
              >
                {chip.label}
              </button>
            );
          })}
          <Select value={sort} onValueChange={(value) => changeSort(value as SearchSort)}>
            <SelectTrigger className="ml-auto h-8 w-36 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="relevance">Best match</SelectItem>
              <SelectItem value="newest">Newest first</SelectItem>
              <SelectItem value="oldest">Oldest first</SelectItem>
              <SelectItem value="largest">Largest first</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {!searchMutation.isPending && !data && !searchMutation.isError && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            Try:
            {EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                onClick={() => {
                  setQuery(example);
                  run(example);
                }}
                className="rounded-md bg-secondary px-2 py-1 hover:text-foreground"
              >
                {example}
              </button>
            ))}
          </div>
        )}

        {searchMutation.isPending && (
          <div className="flex flex-col gap-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-16 rounded-xl" />
            ))}
          </div>
        )}

        {searchMutation.isError && (
          <EmptyState title="Couldn't run that search" description="Please try again." />
        )}

        {data && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span className="font-medium text-foreground">
              {data.total.toLocaleString()} {data.total === 1 ? "file" : "files"}
            </span>
            {data.understood.length > 0 && <span>·</span>}
            {data.understood.map((part) => (
              <Badge key={part} variant="outline">
                {part}
              </Badge>
            ))}
            {data.interpreted_by_ai && (
              <Badge variant="ai">
                <Sparkles className="size-3" /> Understood with AI
              </Badge>
            )}
          </div>
        )}

        {data && results.length === 0 && (
          <EmptyState
            icon={SearchX}
            title="No matches found"
            description="Nothing in your connected storage matched. Try fewer words or remove a filter."
          />
        )}

        {results.length > 0 && (
          <ul className="flex flex-col gap-2">
            {results.map((result) => (
              <li
                key={result.file_id}
                className="flex items-center gap-2 rounded-xl border border-border bg-card p-3 shadow-clay-sm transition-shadow hover:shadow-clay"
              >
                <Link
                  to="/files/$fileId"
                  params={{ fileId: result.file_id }}
                  className="flex min-w-0 flex-1 items-center gap-3"
                >
                  <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-secondary text-muted-foreground">
                    {fileTypeIconElement(result.mime_type, { className: "size-4" })}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{result.name}</p>
                    <p className="truncate text-xs text-muted-foreground">{result.path}</p>
                  </div>
                  <div className="hidden shrink-0 flex-col items-end text-xs text-muted-foreground sm:flex">
                    <span>{result.size_bytes !== null ? formatBytes(result.size_bytes) : "—"}</span>
                    <span>
                      {result.provider_modified_at
                        ? formatRelativeTime(result.provider_modified_at)
                        : ""}
                      {result.provider ? ` · ${result.provider}` : ""}
                    </span>
                  </div>
                </Link>
                {result.web_view_link && (
                    <a
                      href={result.web_view_link}
                      target="_blank"
                      rel="noopener noreferrer"
                      aria-label={`Open ${result.name} in its storage`}
                      className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground"
                    >
                      <ExternalLink className="size-4" />
                    </a>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </AppShell>
  );
}
