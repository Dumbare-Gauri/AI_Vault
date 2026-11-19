import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BackLink } from "./back-link";

function setup(initialEntries: string[]) {
  const rootRoute = createRootRoute();
  const listRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/files",
    component: () => <p>Files list</p>,
  });
  const detailRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/files/$fileId",
    component: () => <BackLink to="/files">Back to files</BackLink>,
  });
  const history = createMemoryHistory({ initialEntries });
  const router = createRouter({
    routeTree: rootRoute.addChildren([listRoute, detailRoute]),
    history,
  });
  render(<RouterProvider router={router} />);
  return router;
}

describe("BackLink", () => {
  it("returns to the exact screen the user came from, filters and folder included", async () => {
    const router = setup(["/files?folder=f-1&page=2&q=invoice"]);
    await act(() => router.navigate({ to: "/files/$fileId", params: { fileId: "x" } }));

    fireEvent.click(await screen.findByText("Back to files"));

    await screen.findByText("Files list");
    expect(router.state.location.pathname).toBe("/files");
    expect(router.state.location.search).toEqual({ folder: "f-1", page: 2, q: "invoice" });
  });

  it("falls back to the parent page when opened directly", async () => {
    const router = setup(["/files/x"]);

    fireEvent.click(await screen.findByText("Back to files"));

    await screen.findByText("Files list");
    expect(router.state.location.pathname).toBe("/files");
  });
});
