import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AnswerFile, ConversationMessage } from "@vault/types";
import { describe, expect, it, vi } from "vitest";

import { AnswerBlocks } from "./answer-blocks";

const post = vi.fn().mockResolvedValue({});
vi.mock("@/lib/api-client", () => ({
  ApiError: class extends Error {},
  apiClient: { post: (...args: unknown[]) => post(...args), get: vi.fn(), blob: vi.fn() },
}));
vi.mock("@/stores/auth-store", () => ({
  useAuthStore: (select: (state: { user: { role: string } }) => unknown) => select({ user: { role: "owner" } }),
}));
vi.mock("@tanstack/react-router", () => ({
  Link: ({ children }: { children: React.ReactNode }) => <a>{children}</a>,
}));

function file(n: number): AnswerFile {
  return {
    id: `file-${n}`,
    name: `Blarrow ${n}.pdf`,
    path: `/Clients/Blarrow ${n}.pdf`,
    mime_type: "application/pdf",
    size_bytes: 1024,
    modified_at: null,
    storage: "My computer",
    connector_id: "c1",
    trashed: false,
  };
}

function message(blocks: ConversationMessage["blocks"]): ConversationMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "",
    retrieval_method: "tool",
    provider: null,
    token_usage: null,
    tool_name: "vault:find_files",
    created_at: "2026-01-01T00:00:00Z",
    citations: [],
    blocks,
  };
}

function renderBlocks(msg: ConversationMessage) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <AnswerBlocks message={msg} conversationId="conv-1" />
    </QueryClientProvider>,
  );
}

describe("AnswerBlocks", () => {
  it("shows the real total and offers the rest, not just the first page", () => {
    const items = Array.from({ length: 25 }, (_, n) => file(n));
    renderBlocks(message([{ type: "file_list", title: "Matching “Blarrow”", total: 219, items, note: null }]));

    expect(screen.getByText("219 files")).toBeInTheDocument();
    expect(screen.getByText("Blarrow 0.pdf")).toBeInTheDocument();
    expect(screen.getByText(/Show 25 more of 194/)).toBeInTheDocument();
  });

  it("never shows internal ids", () => {
    renderBlocks(message([{ type: "file_list", title: "Files", total: 1, items: [file(1)], note: null }]));

    expect(screen.queryByText(/file-1/)).not.toBeInTheDocument();
  });

  it("confirms a proposal only when asked", async () => {
    renderBlocks(
      message([
        {
          type: "action_proposal",
          action_id: "a1",
          kind: "move",
          title: "Move the Blarrow files into “Finance”",
          description: "Moves 2 files.",
          risk: "medium",
          confirm_label: "Move 2 files",
          affected_count: 2,
          items: [file(1), file(2)],
          preview: null,
          status: "pending",
        },
      ]),
    );

    expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Move 2 files" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/v1/conversations/conv-1/actions/a1/confirm"));
  });
});
