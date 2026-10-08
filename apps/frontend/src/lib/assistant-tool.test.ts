import { describe, expect, it } from "vitest";

import { assistantToolLabel } from "./assistant-tool";

describe("assistantToolLabel", () => {
  it("maps what Ask Vault did to a friendly label", () => {
    expect(assistantToolLabel("vault:storage_summary")).toBe("Storage overview");
    expect(assistantToolLabel("vault:duplicates")).toBe("Duplicates");
    expect(assistantToolLabel("vault:organize")).toBe("Organize");
  });

  it("falls back to the raw kind when unrecognized", () => {
    expect(assistantToolLabel("vault:some_future_kind")).toBe("some_future_kind");
  });
});
