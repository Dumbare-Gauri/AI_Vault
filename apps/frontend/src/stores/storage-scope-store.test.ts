import type { Connector } from "@vault/types";
import { beforeEach, describe, expect, it } from "vitest";

import { settleWorkspace, useStorageScopeStore, withWorkspace } from "./storage-scope-store";

function connector(id: string, status = "connected"): Connector {
  return { id, status, provider: "google_workspace" } as Connector;
}

describe("storage workspace", () => {
  beforeEach(() => useStorageScopeStore.setState({ connectorId: "drive-1" }));

  it("attaches the workspace to every workspace-scoped API", () => {
    expect(withWorkspace("/v1/search")).toBe("/v1/search?connector_id=drive-1");
    expect(withWorkspace("/v1/storage/large-files?limit=50")).toBe(
      "/v1/storage/large-files?limit=50&connector_id=drive-1",
    );
    expect(withWorkspace("/v1/conversations/c1/messages")).toBe(
      "/v1/conversations/c1/messages?connector_id=drive-1",
    );
  });

  it("leaves other APIs, and an explicit workspace, alone", () => {
    expect(withWorkspace("/v1/connectors")).toBe("/v1/connectors");
    expect(withWorkspace("/v1/files/f1")).toBe("/v1/files/f1");
    expect(withWorkspace("/v1/storage/overview?connector_id=local-1")).toBe(
      "/v1/storage/overview?connector_id=local-1",
    );
  });

  it("never falls back to all storage — a stale workspace becomes a connected one", () => {
    useStorageScopeStore.setState({ connectorId: "gone" });

    settleWorkspace([connector("drive-1", "disconnected"), connector("local-1")]);

    expect(useStorageScopeStore.getState().connectorId).toBe("local-1");
  });

  it("keeps the chosen workspace while it is still connected", () => {
    useStorageScopeStore.setState({ connectorId: "local-1" });

    settleWorkspace([connector("drive-1"), connector("local-1")]);

    expect(useStorageScopeStore.getState().connectorId).toBe("local-1");
  });
});
