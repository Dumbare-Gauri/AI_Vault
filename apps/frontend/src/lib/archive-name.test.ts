import { describe, expect, it } from "vitest";

import { archiveNameProblem, suggestArchiveName, withZip } from "./archive-name";

describe("archive names", () => {
  it("suggests a name from what is being archived", () => {
    expect(suggestArchiveName("Project Docs")).toBe("Project_Docs_Archive.zip");
  });

  it("drops a file's own extension from the suggestion", () => {
    expect(suggestArchiveName("report.pdf")).toBe("report_Archive.zip");
  });

  it("adds .zip once", () => {
    expect(withZip("Backup.ZIP")).toBe("Backup.zip");
    expect(withZip("Backup")).toBe("Backup.zip");
  });

  it.each(["", "  ", "../escape", "a/b", "what?", "con", "trailing.", "x".repeat(300)])(
    "refuses %j",
    (name) => {
      expect(archiveNameProblem(name)).not.toBeNull();
    },
  );

  it("accepts an ordinary name", () => {
    expect(archiveNameProblem("Project_Docs_Archive.zip")).toBeNull();
  });
});
