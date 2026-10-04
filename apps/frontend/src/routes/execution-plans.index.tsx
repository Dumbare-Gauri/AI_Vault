import { useQuery } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { ExecutionPlan } from "@vault/types";
import { ClipboardList, History } from "lucide-react";
import { useState } from "react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { apiClient } from "@/lib/api-client";
import { planStatusBadgeVariant, riskLevelBadgeVariant } from "@/lib/execution-style";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/execution-plans/")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: ExecutionCenterPage,
});

const STATUSES = [
  "pending_approval",
  "approved",
  "executing",
  "completed",
  "partially_completed",
  "failed",
  "rejected",
  "changes_requested",
  "expired",
  "rolled_back",
];

function ExecutionCenterPage() {
  const [status, setStatus] = useState<string>("all");

  const params = new URLSearchParams();
  if (status !== "all") params.set("status", status);

  const plansQuery = useQuery({
    queryKey: ["execution-plans", status],
    queryFn: () => apiClient.get<ExecutionPlan[]>(`/v1/execution-plans?${params.toString()}`),
  });

  const plans = plansQuery.data ?? [];

  return (
    <AppShell title="Execution Center">
      <div className="mx-auto flex max-w-3xl flex-col gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Execution Center</h1>
          <p className="text-sm text-muted-foreground">
            Every plan here runs immediately. If a plan failed, its error (e.g. a connector
            needing reconnection) shows on the plan itself.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Select value={status} onValueChange={setStatus}>
            <SelectTrigger className="w-48">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              {STATUSES.map((s) => (
                <SelectItem key={s} value={s} className="capitalize">
                  {s.replace(/_/g, " ")}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button variant="outline" size="sm" asChild className="ml-auto">
            <Link to="/execution-jobs">
              <History className="size-4" /> Execution history
            </Link>
          </Button>
        </div>

        {plansQuery.isLoading && (
          <div className="flex flex-col gap-2">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-20 rounded-xl" />
            ))}
          </div>
        )}
        {plansQuery.isError && (
          <EmptyState title="Couldn't load execution plans" description="Please try again." />
        )}
        {plans.length === 0 && !plansQuery.isLoading && (
          <EmptyState
            icon={ClipboardList}
            title="No execution plans match these filters"
            description="Create one from a recommendation's detail page."
          />
        )}

        <ul className="flex flex-col gap-2">
          {plans.map((plan) => (
            <li key={plan.id}>
              <Link
                to="/execution-plans/$executionPlanId"
                params={{ executionPlanId: plan.id }}
                className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4 text-sm shadow-clay-sm transition-all hover:-translate-y-0.5 hover:shadow-clay"
              >
                <div className="flex items-center justify-between gap-3">
                  <Badge variant={planStatusBadgeVariant(plan.status)} className="capitalize">
                    {plan.status.replace(/_/g, " ")}
                  </Badge>
                  <Badge variant={riskLevelBadgeVariant(plan.risk_level)}>{plan.risk_level} risk</Badge>
                </div>
                <p className="text-muted-foreground">{plan.estimated_impact}</p>
                <div className="flex items-center gap-3 text-xs text-muted-foreground">
                  <span>{plan.target_provider}</span>
                  <span>{plan.rollback_available ? "rollback available" : "not reversible"}</span>
                  <span>{formatRelativeTime(plan.created_at)}</span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      </div>
    </AppShell>
  );
}
