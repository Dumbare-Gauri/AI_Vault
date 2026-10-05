import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/organization-recommendations/")({
  beforeLoad: () => {
    throw redirect({ to: "/recommendations" });
  },
});
