import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DeleteAccountDialog } from "./delete-account-dialog";

const post = vi.fn();
vi.mock("@/lib/api-client", () => ({
  ApiError: class extends Error {},
  apiClient: { post: (...args: unknown[]) => post(...args) },
}));
vi.mock("@tanstack/react-router", () => ({ useNavigate: () => vi.fn() }));
vi.mock("@/stores/auth-store", () => ({
  useAuthStore: { getState: () => ({ logout: () => Promise.resolve() }) },
}));

function renderDialog() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <DeleteAccountDialog email="ada@example.com" onOpenChange={() => {}} />
    </QueryClientProvider>,
  );
}

describe("DeleteAccountDialog", () => {
  it("stays disabled until the account's email is typed", () => {
    renderDialog();
    const button = screen.getByRole("button", { name: "Delete my account" });

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "someone@example.com" } });
    expect(button).toBeDisabled();

    fireEvent.change(screen.getByRole("textbox"), { target: { value: " Ada@Example.com " } });
    expect(button).toBeEnabled();
  });

  it("says that files in the person's own storage are not touched", () => {
    renderDialog();

    expect(screen.getByText(/Every file in your Google Drive and on your computer/)).toBeInTheDocument();
  });

  it("sends one deletion request, however often it is clicked", async () => {
    post.mockReset();
    post.mockResolvedValue({ connections_removed: 1, tokens_revoked: 1, files_unindexed: 3, cached_archives_deleted: 0 });
    renderDialog();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "ada@example.com" } });
    const button = screen.getByRole("button", { name: "Delete my account" });

    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith("/v1/users/me/delete", { confirm_email: "ada@example.com" });
  });
});
