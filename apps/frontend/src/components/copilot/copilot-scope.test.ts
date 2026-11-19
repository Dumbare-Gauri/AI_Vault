import { describe, expect, it, vi } from "vitest";

import type { CopilotPage } from "@/stores/copilot-store";

import { asContext, scopeSummary } from "./copilot-panel";

vi.mock("@tanstack/react-router", () => ({ Link: () => null }));

const inDatabase: CopilotPage = {
  page: "files",
  label: "C:\\Database",
  folderId: "folder-1",
  sourceId: "source-1",
  fileIds: ["a", "b", "c"],
  selectedFileIds: [],
};

describe("what Ask Vault is told about the screen", () => {
  it("sends the folder, the visible files and the selection", () => {
    const context = asContext({ ...inDatabase, selectedFileIds: ["b"] }, false);

    expect(context).toEqual({
      page: "files",
      label: "C:\\Database",
      file_ids: ["a", "b", "c"],
      folder_id: "folder-1",
      source_id: "source-1",
      selected_file_ids: ["b"],
    });
  });

  it("drops the folder when the user widens the scope to the whole storage", () => {
    const context = asContext(inDatabase, true);

    expect(context?.folder_id).toBeNull();
    expect(context?.source_id).toBeNull();
  });

  it("says a selection is what answers use", () => {
    expect(scopeSummary({ ...inDatabase, selectedFileIds: ["a", "b"] }, false, "This computer")).toBe(
      "2 selected files",
    );
  });

  it("says the folder on screen is what answers use", () => {
    expect(scopeSummary(inDatabase, false, "This computer")).toBe("everything in C:\\Database");
  });

  it("says the whole storage once widened", () => {
    expect(scopeSummary(inDatabase, true, "This computer")).toBe("all of This computer");
  });
});
