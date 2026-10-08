import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, redirect } from "@tanstack/react-router";
import type { Connector, InitiateConnectResponse } from "@vault/types";
import { AlertTriangle, Cloud, Link2, Plus } from "lucide-react";
import { useState } from "react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { apiClient } from "@/lib/api-client";
import { connectorStatusBadgeVariant } from "@/lib/connector-status";
import {
  ConnectComputerDialog,
  LocalAgentCard,
  useLocalAgents,
} from "@/components/local-agent/local-agent-card";
import { formatBytes } from "@/lib/format-bytes";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/storage-connections")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: StorageConnectionsPage,
});

const UPCOMING_PROVIDERS = ["Microsoft OneDrive", "Dropbox"];

function StorageConnectionsPage() {
  const queryClient = useQueryClient();
  const user = useAuthStore((state) => state.user);
  const canManage = user?.role === "owner" || user?.role === "admin";
  const [confirmingDisconnectId, setConfirmingDisconnectId] = useState<string | null>(null);

  const connectorsQuery = useQuery({
    queryKey: ["connectors"],
    queryFn: () => apiClient.get<Connector[]>("/v1/connectors"),
  });

  const connectMutation = useMutation({
    mutationFn: () => apiClient.post<InitiateConnectResponse>("/v1/connectors/google/connect"),
    onSuccess: (data) => {
      // A real top-level navigation, not a fetch — Google must redirect the
      // whole browser tab, and our SPA state is deliberately gone until the
      // /connectors/google/callback route reloads it (see that route's note).
      window.location.href = data.authorize_url;
    },
  });

  const disconnectMutation = useMutation({
    mutationFn: (connectorId: string) =>
      apiClient.post<Connector>(`/v1/connectors/${connectorId}/disconnect`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["connectors"] });
      setConfirmingDisconnectId(null);
    },
  });

  const verifyMutation = useMutation({
    mutationFn: (connectorId: string) =>
      apiClient.post<Connector>(`/v1/connectors/${connectorId}/verify`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["connectors"] }),
  });

  const localAgentsQuery = useLocalAgents();
  const [connectingComputer, setConnectingComputer] = useState(false);

  const hasActiveGoogleConnector = connectorsQuery.data?.some(
    (connector) => connector.provider === "google_workspace" && connector.status !== "disconnected",
  );

  return (
    <AppShell title="Storage Connections">
      <div className="mx-auto flex max-w-2xl flex-col gap-4">
        <h1 className="text-xl font-semibold tracking-tight">Storage Connections</h1>

        {connectorsQuery.isLoading && <Skeleton className="h-28 rounded-2xl" />}
        {connectorsQuery.isError && (
          <EmptyState title="Couldn't load storage connections" description="Please try again." />
        )}
        {connectorsQuery.data?.length === 0 && (
          <EmptyState
            icon={Cloud}
            title="No storage connected yet"
            description="Add a storage account below to let AI Vault understand your files."
          />
        )}

        {localAgentsQuery.data?.map((agent) => (
          <LocalAgentCard key={agent.connector_id} agent={agent} canManage={canManage} />
        ))}

        {connectorsQuery.data
          ?.filter((connector) => connector.provider !== "local_agent")
          .map((connector) => (
          <Card key={connector.id} className="p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="font-medium">
                  {connector.display_name ?? connector.provider_name}
                </p>
                <p className="text-sm text-muted-foreground">
                  {connector.account_email ?? "—"}
                  {connector.workspace_domain ? ` (${connector.workspace_domain})` : ""}
                </p>
                <p className="text-xs text-muted-foreground">
                  Last synced:{" "}
                  {connector.last_synced_at ? formatRelativeTime(connector.last_synced_at) : "never"}
                  {connector.storage_used_bytes !== null && (
                    <>
                      {" · "}
                      {formatBytes(connector.storage_used_bytes)}
                      {connector.storage_total_bytes !== null
                        ? ` of ${formatBytes(connector.storage_total_bytes)} used`
                        : " used"}
                    </>
                  )}
                </p>
              </div>
              <Badge variant={connectorStatusBadgeVariant(connector.status)} className="capitalize">
                {connector.status === "reauth_required" ? "Needs reconnect" : connector.status}
              </Badge>
            </div>

            {connector.status === "reauth_required" ? (
              <div className="mt-3 flex items-start gap-3 rounded-lg bg-warning-muted p-3 text-sm">
                <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" />
                <div>
                  <p className="font-medium text-warning">Google Drive connection needs attention.</p>
                  <p className="mt-1 text-muted-foreground">
                    Reason: Google authorization has expired or was revoked.
                  </p>
                </div>
              </div>
            ) : (
              connector.last_error && (
                <p className="mt-2 text-sm text-destructive">{connector.last_error}</p>
              )
            )}

            <div className="mt-3 flex gap-2">
              {connector.status === "reauth_required" ? (
                canManage && (
                  <Button
                    size="sm"
                    onClick={() => connectMutation.mutate()}
                    disabled={connectMutation.isPending}
                  >
                    <Link2 className="size-4" />
                    {connectMutation.isPending ? "Redirecting…" : "Reconnect Google Drive"}
                  </Button>
                )
              ) : (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={verifyMutation.isPending}
                  onClick={() => verifyMutation.mutate(connector.id)}
                >
                  {verifyMutation.isPending ? "Verifying…" : "Verify"}
                </Button>
              )}

              {canManage &&
                connector.status !== "disconnected" &&
                (confirmingDisconnectId === connector.id ? (
                  <>
                    <Button
                      variant="destructive"
                      size="sm"
                      disabled={disconnectMutation.isPending}
                      onClick={() => disconnectMutation.mutate(connector.id)}
                    >
                      {disconnectMutation.isPending ? "Disconnecting…" : "Confirm disconnect"}
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => setConfirmingDisconnectId(null)}>
                      Cancel
                    </Button>
                  </>
                ) : (
                  <Button variant="outline" size="sm" onClick={() => setConfirmingDisconnectId(connector.id)}>
                    Disconnect
                  </Button>
                ))}
            </div>
          </Card>
          ))}

        {canManage && (
          <section className="mt-2 flex flex-col gap-3">
            <h2 className="flex items-center gap-2 text-base font-semibold">
              <Plus className="size-4" /> Add storage
            </h2>
            <div className="grid gap-3 sm:grid-cols-2">
              <Card className="flex items-center justify-between gap-3 p-4">
                <div>
                  <p className="font-medium">Google Drive</p>
                  <p className="text-xs text-muted-foreground">My Drive and shared drives</p>
                </div>
                {hasActiveGoogleConnector ? (
                  <Badge variant="success">Connected</Badge>
                ) : (
                  <Button
                    size="sm"
                    onClick={() => connectMutation.mutate()}
                    disabled={connectMutation.isPending}
                  >
                    <Link2 className="size-4" />
                    {connectMutation.isPending ? "Redirecting…" : "Connect"}
                  </Button>
                )}
              </Card>
              <Card className="flex items-center justify-between gap-3 p-4">
                <div>
                  <p className="font-medium">This computer</p>
                  <p className="text-xs text-muted-foreground">Local folders and external drives</p>
                </div>
                {(localAgentsQuery.data?.length ?? 0) > 0 ? (
                  <Badge variant="success">Connected</Badge>
                ) : (
                  <Button size="sm" onClick={() => setConnectingComputer(true)}>
                    <Link2 className="size-4" />
                    Connect
                  </Button>
                )}
              </Card>
              {UPCOMING_PROVIDERS.map((provider) => (
                <Card key={provider} className="flex items-center justify-between gap-3 p-4 opacity-70">
                  <p className="font-medium">{provider}</p>
                  <Badge variant="outline">Coming soon</Badge>
                </Card>
              ))}
            </div>
          </section>
        )}
        {connectingComputer && (
          <ConnectComputerDialog onOpenChange={(open) => !open && setConnectingComputer(false)} />
        )}
        {connectMutation.isError && (
          <p className="text-sm text-destructive">Couldn&rsquo;t start the connection. Please try again.</p>
        )}
      </div>
    </AppShell>
  );
}
