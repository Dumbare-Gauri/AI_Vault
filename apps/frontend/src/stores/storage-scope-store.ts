import type { Connector } from "@vault/types";
import { create } from "zustand";
import { persist } from "zustand/middleware";

interface StorageScopeState {
  /** The storage workspace the app works in: one connected storage. Google
   * Drive and this computer are never shown or acted on together. */
  connectorId: string | null;
  setConnectorId: (connectorId: string | null) => void;
}

export const useStorageScopeStore = create<StorageScopeState>()(
  persist(
    (set) => ({
      connectorId: null,
      setConnectorId: (connectorId) => set({ connectorId }),
    }),
    { name: "vault-storage-scope" },
  ),
);

/** Keeps the remembered workspace valid: if it is no longer connected (or
 * none was chosen yet), the first connected storage becomes the workspace.
 * Called with each fresh connector list, outside React rendering. */
export function settleWorkspace(connectors: Connector[]): void {
  const connected = connectors.filter((connector) => connector.status === "connected");
  const { connectorId, setConnectorId } = useStorageScopeStore.getState();
  if (connected.some((connector) => connector.id === connectorId)) return;
  setConnectorId(connected[0]?.id ?? null);
}

/** Adds the chosen storage to an API path. */
export function scopedPath(path: string, connectorId: string | null): string {
  if (!connectorId || /[?&]connector_id=/.test(path)) return path;
  return `${path}${path.includes("?") ? "&" : "?"}connector_id=${encodeURIComponent(connectorId)}`;
}

// Everything the backend serves per storage workspace. It validates the
// workspace itself; the client only says which one the user is in.
const WORKSPACE_SCOPED = [
  "/v1/storage/",
  "/v1/search",
  "/v1/conversations",
  "/v1/dashboard",
  "/v1/recommendations",
  "/v1/organization-recommendations",
  "/v1/archives",
  "/v1/activity",
  "/v1/execution-plans",
];

/** The path with the current workspace attached, for workspace-scoped APIs. */
export function withWorkspace(path: string): string {
  if (!WORKSPACE_SCOPED.some((prefix) => path.startsWith(prefix))) return path;
  return scopedPath(path, useStorageScopeStore.getState().connectorId);
}
