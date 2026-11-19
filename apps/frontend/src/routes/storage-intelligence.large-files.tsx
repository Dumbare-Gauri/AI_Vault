import { createFileRoute, redirect } from "@tanstack/react-router";
import { FileWarning } from "lucide-react";

import { StorageFileListPage } from "@/components/storage-intelligence/file-list-page";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/storage-intelligence/large-files")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: () => (
    <StorageFileListPage
      title="Large files"
      description="Files over 100 MB, largest first — whether or not they are still in use."
      apiPath="/v1/storage/large-files"
      emptyIcon={FileWarning}
      emptyTitle="No large files found"
      emptyDescription="Run a storage analysis to check for unusually large files."
      dateLabel="Modified"
      getDate={(file) => file.provider_modified_at}
    />
  ),
});
