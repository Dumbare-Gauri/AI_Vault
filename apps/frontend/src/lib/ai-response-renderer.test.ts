import type { ConversationMessage } from "@vault/types";
import { describe, expect, it } from "vitest";

import { pickRenderVariant } from "./ai-response-renderer";

function message(overrides: Partial<ConversationMessage>): ConversationMessage {
  return {
    id: "msg-1",
    role: "assistant",
    content: "some content",
    retrieval_method: null,
    provider: null,
    token_usage: null,
    tool_name: null,
    created_at: "2026-01-01T00:00:00Z",
    citations: [],
    blocks: [],
    ...overrides,
  };
}

const citation = {
  id: "cit-1",
  file_id: "file-1",
  snippet: null,
  confidence: 1,
  retrieval_method: "tool" as const,
  page_number: null,
  passage_index: null,
  file_name: "report.pdf",
  file_size_bytes: 1024,
  file_mime_type: "application/pdf",
};

describe("pickRenderVariant", () => {
  it("returns prose-only when there are no citations", () => {
    expect(pickRenderVariant(message({ tool_name: "get_storage_overview", citations: [] }))).toBe(
      "prose-only",
    );
  });

  it("returns file-detail for get_file's single-citation answer", () => {
    expect(pickRenderVariant(message({ tool_name: "get_file", citations: [citation] }))).toBe(
      "file-detail",
    );
  });

  it("returns file-list for search_files and other multi-file tools", () => {
    expect(pickRenderVariant(message({ tool_name: "search_files", citations: [citation] }))).toBe(
      "file-list",
    );
    expect(
      pickRenderVariant(message({ tool_name: "get_large_files", citations: [citation] })),
    ).toBe("file-list");
  });

  it("returns file-list when tool_name is missing entirely", () => {
    expect(pickRenderVariant(message({ tool_name: null, citations: [citation] }))).toBe(
      "file-list",
    );
  });

  it("dispatches on citations, not tool_name, for the bundled-tool case", () => {
    // conversation_service.py only ever stores the *first* fired tool's
    // name (e.g. "get_storage_overview", which has cite_files=False) even
    // when a bundled second call (e.g. "get_cleanup_candidates") is the one
    // that actually produced citations. tool_name alone would wrongly hide
    // a real file list here.
    const bundled = message({ tool_name: "get_storage_overview", citations: [citation] });
    expect(pickRenderVariant(bundled)).toBe("file-list");
  });
});
