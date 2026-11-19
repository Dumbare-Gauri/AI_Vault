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

  it("lists every file a removal affects and holds a large one until it's reviewed", () => {
    post.mockClear();
    const items = Array.from({ length: 25 }, (_, n) => file(n));
    renderBlocks(
      message([
        {
          type: "action_proposal",
          action_id: "a2",
          kind: "trash",
          title: "Move the files to Trash",
          description: "Moves 40 files to Trash.",
          risk: "high",
          confirm_label: "Move 40 files to Trash",
          affected_count: 40,
          items,
          preview: null,
          status: "pending",
        },
      ]),
    );

    expect(screen.getByText("Blarrow 0.pdf")).toBeInTheDocument();
    expect(screen.getByText(/Show 15 more of 15/)).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Move 40 files to Trash" });
    expect(confirm).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox"));

    expect(confirm).toBeEnabled();
  });

  it("names the protected files a proposal leaves alone, with the reason", () => {
    renderBlocks(
      message([
        {
          type: "action_proposal",
          action_id: "a3",
          kind: "trash",
          title: "Remove 1 duplicate copy",
          description: "",
          risk: "medium",
          confirm_label: "Move copies to Trash",
          affected_count: 1,
          items: [file(1)],
          protected_count: 1,
          protected_items: [
            { ...file(9), name: "__init__.py", reasons: ["Source code (.py)", "Part of the software project in /AI_Vault"] },
          ],
          preview: null,
          status: "pending",
        },
      ]),
    );

    fireEvent.click(screen.getByRole("button", { name: /1 protected file left untouched/ }));

    expect(screen.getByText("__init__.py")).toBeInTheDocument();
    expect(screen.getByText(/Source code \(\.py\) · Part of the software project/)).toBeInTheDocument();
  });
});
