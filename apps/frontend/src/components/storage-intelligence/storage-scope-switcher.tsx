import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { Connector } from "@vault/types";
import { Cloud, HardDrive, Laptop } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { fetchConnectors } from "@/lib/connectors";
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
  const query = useQuery({ queryKey: ["connectors"], queryFn: fetchConnectors });
  return (query.data ?? []).filter((connector) => connector.status === "connected");
}

/** The storage workspace the app is working in — always one connected
 * storage (or none, when nothing is connected). */
export function useStorageScope(): { connectorId: string | null; connector: Connector | undefined } {
  const connected = useConnectedStorage();
  const chosen = useStorageScopeStore((state) => state.connectorId);
  const connector = connected.find((candidate) => candidate.id === chosen);
  return { connectorId: connector?.id ?? null, connector };
}

/** Switches the whole app between Google Drive and this computer. Each is
 * its own workspace: switching clears everything loaded for the other one. */
export function WorkspaceSwitcher() {
  const connected = useConnectedStorage();
  const queryClient = useQueryClient();
  const setConnectorId = useStorageScopeStore((state) => state.setConnectorId);
  const { connectorId } = useStorageScope();
  if (connected.length === 0) return null;

  function choose(id: string) {
    if (id === connectorId) return;
    setConnectorId(id);
    queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== "connectors" });
  }

  return (
    <div role="tablist" aria-label="Storage workspace" className="flex w-fit gap-1 rounded-xl bg-secondary p-1">
      {connected.map((connector) => {
        const Icon = storageIcon(connector);
        return (
          <button
            key={connector.id}
            type="button"
            role="tab"
            aria-selected={connectorId === connector.id}
            onClick={() => choose(connector.id)}
            className={cn(
              "flex items-center gap-2 rounded-lg px-2.5 py-1 text-sm font-medium transition-colors",
              connectorId === connector.id
                ? "bg-card text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Icon className="size-4" aria-hidden="true" />
            <span className="hidden sm:inline">{storageLabel(connector)}</span>
          </button>
        );
      })}
    </div>
  );
}

/** Says what the numbers below cover, so they aren't confused with the
 * provider's own storage figures on the dashboard. */
export function StorageScopeNote({ connector }: { connector: Connector | undefined }) {
  if (!connector) {
    return (
      <p className="text-sm text-muted-foreground">
        Connect Google Drive or this computer on Storage Connections to see its files here.
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
