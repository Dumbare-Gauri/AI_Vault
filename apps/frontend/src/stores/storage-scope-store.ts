import { create } from "zustand";
import { persist } from "zustand/middleware";

interface StorageScopeState {
  /** The connected storage Storage Intelligence is showing; null = all of them. */
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

/** Adds the chosen storage to a Storage Intelligence API path. */
export function scopedPath(path: string, connectorId: string | null): string {
  if (!connectorId) return path;
  return `${path}${path.includes("?") ? "&" : "?"}connector_id=${encodeURIComponent(connectorId)}`;
}
