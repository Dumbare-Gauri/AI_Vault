import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { EmptyTrashStatus, TrashSummary } from "@vault/types";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EmptyTrashDialog } from "./empty-trash-dialog";

const preview: TrashSummary = {
  file_count: 2,
  total_bytes: 2048,
  not_backed_up_count: 0,
  largest: [
    { name: "old.zip", size_bytes: 2000, backed_up: false },
    { name: "notes.txt", size_bytes: 48, backed_up: false },
  ],
  still_deleting_count: 0,
};

const post = vi.fn();
const get = vi.fn();
vi.mock("@/lib/api-client", () => ({
  ApiError: class extends Error {},
  apiClient: { post: (...args: unknown[]) => post(...args), get: (...args: unknown[]) => get(...args) },
}));

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <EmptyTrashDialog connectorId="c1" storage="this computer" onOpenChange={() => {}} />
    </QueryClientProvider>,
  );
}

const consent = "I understand these files will be permanently deleted and cannot be recovered.";

describe("EmptyTrashDialog", () => {
  beforeEach(() => {
    post.mockReset();
    get.mockReset();
  });

  it("keeps deletion disabled until the user ticks the acknowledgement", async () => {
    get.mockResolvedValue(preview);
    renderDialog();

    const checkbox = await screen.findByRole("checkbox", { name: consent });
    const button = screen.getByRole("button", { name: "Delete permanently" });
    expect(checkbox).not.toBeChecked();
    expect(button).toBeDisabled();

    fireEvent.click(checkbox);

    expect(button).toBeEnabled();
  });

  it("shows how many files, how much space, and which files", async () => {
    get.mockResolvedValue(preview);
    renderDialog();

    expect(await screen.findByText("old.zip")).toBeInTheDocument();
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
    expect(screen.getByText("2 files")).toBeInTheDocument();
  });

  it("sends one request with the reviewed count, however often it is clicked", async () => {
    const running: EmptyTrashStatus = { status: "running", summary: null, error: null };
    get.mockImplementation((url: string) => Promise.resolve(url.includes("/empty/") ? running : preview));
    post.mockResolvedValue({ operation_id: "op-1" });
    renderDialog();

    fireEvent.click(await screen.findByRole("checkbox", { name: consent }));
    const button = screen.getByRole("button", { name: "Delete permanently" });
    fireEvent.click(button);
    fireEvent.click(button);

    await screen.findByRole("status");
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith("/v1/connectors/c1/provider-trash/empty", {
      expected_count: 2,
      confirmed: true,
    });
  });

  it("names the files that could not be deleted", async () => {
    const done: EmptyTrashStatus = {
      status: "done",
      error: null,
      summary: {
        ...preview,
        still_deleting_count: 1,
        not_deleted: [{ name: "locked.db", size_bytes: 5, backed_up: false }],
      },
    };
    get.mockImplementation((url: string) => Promise.resolve(url.includes("/empty/") ? done : preview));
    post.mockResolvedValue({ operation_id: "op-1" });
    renderDialog();

    fireEvent.click(await screen.findByRole("checkbox", { name: consent }));
    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));

    await waitFor(() => expect(screen.getByText("locked.db")).toBeInTheDocument());
    expect(screen.getByText(/1 of 2 files permanently deleted/)).toBeInTheDocument();
  });
});
