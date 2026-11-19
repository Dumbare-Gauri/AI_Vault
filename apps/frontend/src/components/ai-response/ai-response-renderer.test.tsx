import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ConversationMessage } from "@vault/types";
import { describe, expect, it, vi } from "vitest";

import { AIResponseRenderer } from "./ai-response-renderer";

// Real router context isn't needed to verify rendering structure — swap
// Link for a plain anchor so these tests exercise our renderer, not routing.
vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, ...props }: { children: React.ReactNode }) => <a {...props}>{children}</a>,
}));

// No user is signed in for these tests — the archive/select affordance is
// unrelated to the raw-text-vs-structured-card question these tests check.
vi.mock("@/stores/auth-store", () => ({
  useAuthStore: () => null,
}));

function renderWithQueryClient(ui: React.ReactElement) {
  const queryClient = new QueryClient();
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

function baseMessage(overrides: Partial<ConversationMessage>): ConversationMessage {
  return {
    id: "msg-1",
    role: "assistant",
    content: "50 files found (threshold: 5 GB). Showing 10:",
    retrieval_method: null,
    provider: "extractive_fallback",
    token_usage: null,
    tool_name: null,
    created_at: "2026-01-01T00:00:00Z",
    citations: [],
    blocks: [],
    ...overrides,
  };
}

describe("AIResponseRenderer", () => {
  it("renders a structured file list instead of relying on flattened text alone", () => {
    renderWithQueryClient(
      <AIResponseRenderer
        conversationId="conversation-1"
        message={baseMessage({
          tool_name: "vault:largest_files",
          citations: [
            {
              id: "cit-1",
              file_id: "file-1",
              snippet: null,
              confidence: 1,
              retrieval_method: "tool",
              page_number: null,
              passage_index: null,
              file_name: "SPEAKERS.stl",
              file_size_bytes: 51_302,
              file_mime_type: "model/stl",
            },
            {
              id: "cit-2",
              file_id: "file-2",
              snippet: null,
              confidence: 1,
              retrieval_method: "tool",
              page_number: null,
              passage_index: null,
              file_name: "GTA SA Sd Data.rar",
              file_size_bytes: 3_880_000,
              file_mime_type: "application/x-rar-compressed",
            },
          ],
        })}
      />,
    );

    // The prose sentence still renders (never suppressed)...
    expect(screen.getByText(/50 files found/)).toBeInTheDocument();
    // ...but each file is now a real row in a table, not a text line.
    const table = screen.getByRole("table");
    expect(table).toBeInTheDocument();
    expect(screen.getByText("SPEAKERS.stl")).toBeInTheDocument();
    expect(screen.getByText("GTA SA Sd Data.rar")).toBeInTheDocument();
    expect(screen.getByText("50.1 KB")).toBeInTheDocument();
    // The tool label heads the list *and* still appears in the small meta
    // badge below the prose (pre-existing, unrelated behavior) — both are
    // legitimate, hence two matches rather than one.
    expect(screen.getAllByText(/Largest files/)).toHaveLength(2);
    // Retrieval-method/confidence detail is collapsed by default.
    expect(screen.queryByText("Storage Assistant")).not.toBeInTheDocument();
  });

  it("renders prose-only with no table when a tool produced no citations", () => {
    renderWithQueryClient(
      <AIResponseRenderer
        conversationId="conversation-1"
        message={baseMessage({
          content: "You're using 842 GB across 12,400 files.",
          tool_name: "vault:storage_summary",
          citations: [],
    blocks: [],
        })}
      />,
    );

    expect(screen.getByText(/842 GB/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders an expanded single-file card for get_file", () => {
    renderWithQueryClient(
      <AIResponseRenderer
        conversationId="conversation-1"
        message={baseMessage({
          content: "Here's what I found.",
          tool_name: "get_file",
          citations: [
            {
              id: "cit-1",
              file_id: "file-1",
              snippet: "some snippet",
              confidence: 1,
              retrieval_method: "tool",
              page_number: null,
              passage_index: null,
              file_name: "Q3 Board Deck.pdf",
              file_size_bytes: 2_500_000,
              file_mime_type: "application/pdf",
            },
          ],
        })}
      />,
    );

    expect(screen.getByText("Q3 Board Deck.pdf")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders a file list from citations even when tool_name reports a no-citation tool", () => {
    // Regression guard for the bundled-tool-call gap: conversation_service.py
    // only stores the first fired tool's name.
    renderWithQueryClient(
      <AIResponseRenderer
        conversationId="conversation-1"
        message={baseMessage({
          tool_name: "vault:storage_summary",
          citations: [
            {
              id: "cit-1",
              file_id: "file-1",
              snippet: null,
              confidence: 1,
              retrieval_method: "tool",
              page_number: null,
              passage_index: null,
              file_name: "old-project.zip",
              file_size_bytes: 900_000,
              file_mime_type: "application/zip",
            },
          ],
        })}
      />,
    );

    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("old-project.zip")).toBeInTheDocument();
  });
});
