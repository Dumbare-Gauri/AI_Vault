import { useQuery } from "@tanstack/react-query";
import type { Connector } from "@vault/types";
import { Cloud, HardDrive, Layers, Laptop } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { apiClient } from "@/lib/api-client";
import { formatBytes } from "@/lib/format-bytes";
import { cn } from "@/lib/utils";
import { useStorageScopeStore } from "@/stores/storage-scope-store";

export function storageLabel(connector: Connector): string {
  if (connector.provider === "local_agent") return connector.display_name ?? "My computer";
  return connector.provider_name;
}

function storageIcon(connector: Connector): LucideIcon {
  return connector.provider === "local_agent" ? Laptop : Cloud;
}

export function useConnectedStorage() {
  const query = useQuery({
    queryKey: ["connectors"],
    queryFn: () => apiClient.get<Connector[]>("/v1/connectors"),
  });
  return (query.data ?? []).filter((connector) => connector.status === "connected");
}

/** The storage Storage Intelligence is scoped to — falls back to "all" if
 * the remembered one is no longer connected. */
export function useStorageScope(): { connectorId: string | null; connector: Connector | undefined } {
  const connected = useConnectedStorage();
  const chosen = useStorageScopeStore((state) => state.connectorId);
  const connector = connected.find((candidate) => candidate.id === chosen);
  return { connectorId: connector?.id ?? null, connector };
}

/** Switches Storage Intelligence between all storage and one connected storage. */
export function StorageScopeSwitcher() {
  const connected = useConnectedStorage();
  const setConnectorId = useStorageScopeStore((state) => state.setConnectorId);
  const { connectorId } = useStorageScope();
  if (connected.length < 2) return null;

  const options: { id: string | null; label: string; icon: LucideIcon }[] = [
    { id: null, label: "All storage", icon: Layers },
    ...connected.map((connector) => ({
      id: connector.id,
      label: storageLabel(connector),
      icon: storageIcon(connector),
    })),
  ];
  return (
    <div role="tablist" aria-label="Storage" className="flex w-fit flex-wrap gap-1 rounded-xl bg-secondary p-1">
      {options.map((option) => (
        <button
          key={option.id ?? "all"}
          type="button"
          role="tab"
          aria-selected={connectorId === option.id}
          onClick={() => setConnectorId(option.id)}
          className={cn(
            "flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors",
            connectorId === option.id
              ? "bg-card text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <option.icon className="size-4" aria-hidden="true" />
          {option.label}
        </button>
      ))}
    </div>
  );
}

/** Says what the numbers below cover, so they aren't confused with the
 * provider's own storage figures on the dashboard. */
export function StorageScopeNote({ connector }: { connector: Connector | undefined }) {
  if (!connector) {
    return (
      <p className="text-sm text-muted-foreground">
        Files AI Vault has indexed across all your connected storage.
      </p>
    );
  }
  const used = connector.storage_used_bytes;
  const total = connector.storage_total_bytes;
  const capacity =
    used != null && total != null ? `${formatBytes(used)} used of ${formatBytes(total)}` : null;
  return (
    <p className="flex items-center gap-2 text-sm text-muted-foreground">
      <HardDrive className="size-4 shrink-0" aria-hidden="true" />
      {connector.provider === "local_agent"
        ? `Files in the folders you added on ${storageLabel(connector)}.${capacity ? ` The drive itself has ${capacity}.` : ""}`
        : `Drive files you own in ${storageLabel(connector)}.${capacity ? ` Google counts ${capacity} for the whole account, including Gmail and Photos.` : ""}`}
    </p>
  );
}
