import type { Connector } from "@vault/types";

export interface StorageSummary {
  usedBytes: number;
  /** Null when no connected account reported a capacity (e.g. unlimited plans). */
  totalBytes: number | null;
  freeBytes: number | null;
  recoverableBytes: number;
  trashBytes: number;
  accountsWithQuota: number;
}

/** Adds up each connected account's own quota — accounts stay separate
 * providers; this is only a sum for the headline, never one virtual disk. */
export function summarizeStorage(
  connectors: Connector[],
  fallbackUsedBytes: number,
  recoverableBytes: number | null,
): StorageSummary {
  const connected = connectors.filter((connector) => connector.status === "connected");
  const withUsage = connected.filter((connector) => connector.storage_used_bytes !== null);
  const withCapacity = connected.filter((connector) => connector.storage_total_bytes !== null);

  const usedBytes =
    withUsage.length > 0
      ? withUsage.reduce((sum, connector) => sum + (connector.storage_used_bytes ?? 0), 0)
      : fallbackUsedBytes;
  const totalBytes =
    withCapacity.length > 0 && withCapacity.length === connected.length
      ? withCapacity.reduce((sum, connector) => sum + (connector.storage_total_bytes ?? 0), 0)
      : null;

  return {
    usedBytes,
    totalBytes,
    freeBytes: totalBytes === null ? null : Math.max(0, totalBytes - usedBytes),
    recoverableBytes: recoverableBytes ?? 0,
    trashBytes: connected.reduce((sum, connector) => sum + (connector.storage_trash_bytes ?? 0), 0),
    accountsWithQuota: withCapacity.length,
  };
}
