import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type {
  OrganizationRecommendation,
  OrganizationRecommendationListResponse,
  Recommendation,
  RecommendationCategory,
  RecommendationListResponse,
} from "@vault/types";
import { FolderTree, Lightbulb, Search, Sparkles, Wand2 } from "lucide-react";
import { type ReactNode, useState } from "react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { formatBytes } from "@/lib/format-bytes";
import { recommendationKindLabel } from "@/lib/organization-style";
import { categoryBadgeVariant, categoryLabel, riskBadgeVariant } from "@/lib/recommendation-style";
import { pluralFiles, runStorageAction, trackRecommendationApply } from "@/lib/storage-action";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/recommendations/")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: RecommendationsPage,
});

const CATEGORIES: RecommendationCategory[] = [
  "storage_optimization",
  "knowledge_optimization",
  "security",
  "collaboration",
  "productivity",
];

const CARD_CLASS =
  "flex flex-col gap-3 rounded-xl border border-border bg-card p-4 shadow-clay-sm transition-shadow hover:shadow-clay";

function useCanManage(): boolean {
  const user = useAuthStore((state) => state.user);
  return user?.role === "owner" || user?.role === "admin";
}

function RecommendationsPage() {
  return (
    <AppShell title="Recommendations">
      <div className="mx-auto flex max-w-4xl flex-col gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Recommendation Center</h1>
          <p className="text-sm text-muted-foreground">
            Every suggestion explains why. Review it, or let AI Vault do it — each change is
            confirmed with your storage provider and can be undone.
          </p>
        </div>

        <Tabs defaultValue="organize" className="flex flex-col gap-4">
          <TabsList className="w-fit">
            <TabsTrigger value="organize">
              <FolderTree className="size-4" /> Organize
            </TabsTrigger>
            <TabsTrigger value="cleanup">
              <Lightbulb className="size-4" /> Clean up
            </TabsTrigger>
          </TabsList>
          <TabsContent value="organize">
            <OrganizeList />
          </TabsContent>
          <TabsContent value="cleanup">
            <CleanupList />
          </TabsContent>
        </Tabs>
      </div>
    </AppShell>
  );
}

function ListSkeleton() {
  return (
    <div className="flex flex-col gap-3">
      {Array.from({ length: 3 }).map((_, i) => (
        <Skeleton key={i} className="h-28 rounded-xl" />
      ))}
    </div>
  );
}

function OrganizeList() {
  const queryClient = useQueryClient();
  const canManage = useCanManage();
  const recommendationsQuery = useQuery({
    queryKey: ["organization-recommendations", "active"],
    queryFn: () =>
      apiClient.get<OrganizationRecommendationListResponse>(
        "/v1/organization-recommendations?status=active",
      ),
  });

  const analyzeMutation = useMutation({
    mutationFn: () => apiClient.post("/v1/organization/analyze"),
    onSuccess: () => {
      toast.success("Analyzing your files", {
        description: "New suggestions appear here when it finishes.",
      });
      void queryClient.invalidateQueries({ queryKey: ["organization-recommendations"] });
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't start the analysis.");
    },
  });

  const recommendations = recommendationsQuery.data?.items ?? [];

  return (
    <div className="flex flex-col gap-3">
      {canManage && (
        <Card className="flex flex-wrap items-center justify-between gap-3 p-4">
          <p className="text-sm text-muted-foreground">
            AI Vault groups related files into projects, clients, and campaigns from evidence in
            names, folders, and content — never from guesses it can&rsquo;t explain.
          </p>
          <Button
            size="sm"
            variant="outline"
            disabled={analyzeMutation.isPending}
            onClick={() => analyzeMutation.mutate()}
          >
            <Wand2 className="size-4" />
            {analyzeMutation.isPending ? "Starting…" : "Find new suggestions"}
          </Button>
        </Card>
      )}

      {recommendationsQuery.isLoading && <ListSkeleton />}
      {recommendationsQuery.isError && (
        <EmptyState title="Couldn't load suggestions" description="Please try again." />
      )}
      {recommendationsQuery.isSuccess && recommendations.length === 0 && (
        <EmptyState
          icon={FolderTree}
          title="Nothing to organize right now"
          description="Your files look consistently organized, or no analysis has run yet."
        />
      )}

      <ul className="flex flex-col gap-3">
        {recommendations.map((recommendation) => (
          <OrganizeCard
            key={recommendation.id}
            recommendation={recommendation}
            canManage={canManage}
          />
        ))}
      </ul>
    </div>
  );
}

function OrganizeCard({
  recommendation,
  canManage,
}: {
  recommendation: OrganizationRecommendation;
  canManage: boolean;
}) {
  const doItMutation = useMutation({
    mutationFn: () =>
      apiClient.post<OrganizationRecommendation>(
        `/v1/organization-recommendations/${recommendation.id}/apply`,
      ),
    onSuccess: () => {
      void trackRecommendationApply(
        recommendation.id,
        recommendation.affected_file_ids.length,
        recommendation.kind,
      );
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : "Couldn't start — nothing changed.");
    },
  });

  return (
    <li className={CARD_CLASS}>
      <div className="flex items-start justify-between gap-3">
        <p className="min-w-0 font-medium">{recommendation.title}</p>
        <Badge variant="ai" className="shrink-0">
          <Sparkles className="size-3" /> {recommendationKindLabel(recommendation.kind)}
        </Badge>
      </div>
      <p className="text-sm text-muted-foreground">{recommendation.reasoning_summary}</p>
      {recommendation.suggested_destination.length > 0 && (
        <p className="text-sm">
          <span className="text-muted-foreground">
            {recommendation.kind === "rename_file" ? "Rename to " : "Into "}
          </span>
          <span className="font-medium">{recommendation.suggested_destination.join(" / ")}</span>
        </p>
      )}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span>{pluralFiles(recommendation.affected_file_ids.length)}</span>
        <span>from {recommendation.current_locations.length} folders</span>
        {recommendation.estimated_storage_impact_bytes !== null && (
          <span>{formatBytes(recommendation.estimated_storage_impact_bytes)}</span>
        )}
        <span>{Math.round(recommendation.confidence * 100)}% confidence</span>
      </div>
      <CardActions
        reviewLink={
          <Link
            to="/organization-recommendations/$recommendationId"
            params={{ recommendationId: recommendation.id }}
          >
            Review
          </Link>
        }
        canDo={canManage}
        pending={doItMutation.isPending}
        onDo={() => doItMutation.mutate()}
      />
    </li>
  );
}

function CleanupList() {
  const canManage = useCanManage();
  const [category, setCategory] = useState<string>("all");
  const [search, setSearch] = useState("");

  const params = new URLSearchParams({ status: "active" });
  if (category !== "all") params.set("category", category);
  if (search.trim()) params.set("search", search.trim());

  const recommendationsQuery = useQuery({
    queryKey: ["recommendations", category, search],
    queryFn: () =>
      apiClient.get<RecommendationListResponse>(`/v1/recommendations?${params.toString()}`),
  });
  const recommendations = recommendationsQuery.data?.items ?? [];

  return (
    <div className="flex flex-col gap-3">
      <Card className="flex flex-wrap items-center gap-2 p-2">
        <div className="relative min-w-[12rem] flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search suggestions…"
            className="pl-9"
          />
        </div>
        <Select value={category} onValueChange={setCategory}>
          <SelectTrigger className="w-44">
            <SelectValue placeholder="Category" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All categories</SelectItem>
            {CATEGORIES.map((value) => (
              <SelectItem key={value} value={value}>
                {categoryLabel(value)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Card>

      {recommendationsQuery.isLoading && <ListSkeleton />}
      {recommendationsQuery.isError && (
        <EmptyState title="Couldn't load suggestions" description="Please try again." />
      )}
      {recommendationsQuery.isSuccess && recommendations.length === 0 && (
        <EmptyState
          icon={Lightbulb}
          title="Nothing to clean up"
          description="Try a different category, or check back after the next scan."
        />
      )}

      <ul className="flex flex-col gap-3">
        {recommendations.map((recommendation) => (
          <CleanupCard key={recommendation.id} recommendation={recommendation} canManage={canManage} />
        ))}
      </ul>
    </div>
  );
}

function CleanupCard({
  recommendation,
  canManage,
}: {
  recommendation: Recommendation;
  canManage: boolean;
}) {
  const [pending, setPending] = useState(false);

  async function doIt() {
    setPending(true);
    await runStorageAction(
      { recommendation_id: recommendation.id },
      `Working on ${pluralFiles(recommendation.affected_file_ids.length)}…`,
    );
    setPending(false);
  }

  return (
    <li className={CARD_CLASS}>
      <div className="flex items-start justify-between gap-3">
        <p className="min-w-0 font-medium">{recommendation.title}</p>
        <Badge variant={categoryBadgeVariant(recommendation.category)} className="shrink-0">
          {categoryLabel(recommendation.category)}
        </Badge>
      </div>
      <p className="text-sm text-muted-foreground">{recommendation.estimated_impact}</p>
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge variant={riskBadgeVariant(recommendation.risk_level)}>
          {recommendation.risk_level} risk
        </Badge>
        <span>{Math.round(recommendation.confidence * 100)}% confidence</span>
      </div>
      <CardActions
        reviewLink={
          <Link
            to="/recommendations/$recommendationId"
            params={{ recommendationId: recommendation.id }}
          >
            Review
          </Link>
        }
        canDo={canManage && recommendation.actionable}
        pending={pending}
        onDo={() => void doIt()}
      />
    </li>
  );
}

function CardActions({
  reviewLink,
  canDo,
  pending,
  onDo,
}: {
  reviewLink: ReactNode;
  canDo: boolean;
  pending: boolean;
  onDo: () => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <Button asChild size="sm" variant="outline">
        {reviewLink}
      </Button>
      {canDo && (
        <Button size="sm" disabled={pending} onClick={onDo}>
          {pending ? "Starting…" : "Do it"}
        </Button>
      )}
    </div>
  );
}
