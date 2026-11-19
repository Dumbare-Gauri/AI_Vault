import type { Connector } from "@vault/types";
import { describe, expect, it } from "vitest";

import { summarizeStorage } from "./storage-summary";

const GB = 1024 ** 3;

function connector(overrides: Partial<Connector>): Connector {
  return {
    id: "c1",
    provider: "google_workspace",
    status: "connected",
    account_email: "a@example.com",
    workspace_domain: null,
    last_verified_at: null,
    last_failed_at: null,
    last_error: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    provider_name: "Google Drive",
    display_name: null,
    last_synced_at: null,
    storage_used_bytes: null,
    storage_total_bytes: null,
    storage_trash_bytes: null,
    quota_checked_at: null,
    ...overrides,
  };
}

describe("summarizeStorage", () => {
  it("sums each account's provider-reported quota", () => {
    const summary = summarizeStorage(
      [
        connector({ id: "a", storage_used_bytes: 5 * GB, storage_total_bytes: 15 * GB }),
        connector({ id: "b", storage_used_bytes: 1 * GB, storage_total_bytes: 100 * GB }),
      ],
      0,
      2 * GB,
    );

    expect(summary).toMatchObject({
      usedBytes: 6 * GB,
      totalBytes: 115 * GB,
      freeBytes: 109 * GB,
      recoverableBytes: 2 * GB,
    });
  });

  it("reports no capacity when any connected account has no known limit", () => {
    const summary = summarizeStorage(
      [
        connector({ id: "a", storage_used_bytes: 5 * GB, storage_total_bytes: 15 * GB }),
        connector({ id: "b", storage_used_bytes: 1 * GB }),
      ],
      0,
      null,
    );

    expect(summary.totalBytes).toBeNull();
  });

  it("falls back to scanned bytes before any quota has been fetched", () => {
    expect(summarizeStorage([connector({})], 42, null).usedBytes).toBe(42);
  });

  it("reports how much of the used space is sitting in Trash", () => {
    const summary = summarizeStorage(
      [
        connector({ id: "a", storage_used_bytes: 5 * GB, storage_trash_bytes: 1 * GB }),
        connector({ id: "b", storage_used_bytes: 2 * GB, storage_trash_bytes: 0 }),
      ],
      0,
      null,
    );

    expect(summary.trashBytes).toBe(1 * GB);
  });

  it("ignores disconnected accounts", () => {
    const summary = summarizeStorage(
      [connector({ status: "disconnected", storage_used_bytes: 9 * GB })],
      0,
      null,
    );

    expect(summary.usedBytes).toBe(0);
  });
});
