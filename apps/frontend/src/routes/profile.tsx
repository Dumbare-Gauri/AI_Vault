import { useQuery } from "@tanstack/react-query";
import { createFileRoute, redirect } from "@tanstack/react-router";
import type { UserProfile } from "@vault/types";
import { useState } from "react";

import { DeleteAccountDialog } from "@/components/account/delete-account-dialog";
import { AppShell } from "@/components/app-shell/app-shell";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { apiClient } from "@/lib/api-client";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/profile")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: ProfilePage,
});

function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  const letters = parts.length > 1 ? [parts[0]?.[0], parts.at(-1)?.[0]] : [parts[0]?.[0]];
  return letters.filter(Boolean).join("").toUpperCase() || "?";
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right font-medium">{value}</span>
    </div>
  );
}

function ProfilePage() {
  const [deleting, setDeleting] = useState(false);
  const profileQuery = useQuery({
    queryKey: ["me"],
    queryFn: () => apiClient.get<UserProfile>("/v1/users/me"),
  });

  return (
    <AppShell title="Profile">
      <div className="mx-auto flex max-w-md flex-col gap-4">
        <h1 className="text-xl font-semibold tracking-tight">Profile</h1>

        {profileQuery.isLoading && <Skeleton className="h-48 rounded-2xl" />}
        {profileQuery.isError && (
          <EmptyState title="Couldn't load your profile" description="Please try again." />
        )}

        {profileQuery.data && (
          <Card clay className="p-6">
            <div className="flex items-center gap-3">
              <Avatar className="size-12">
                <AvatarFallback className="text-base">{initials(profileQuery.data.name)}</AvatarFallback>
              </Avatar>
              <div>
                <p className="font-semibold">{profileQuery.data.name}</p>
                <Badge variant="outline" className="mt-1 capitalize">
                  {profileQuery.data.role}
                </Badge>
              </div>
            </div>
            <CardContent className="mt-4 divide-y divide-border p-0">
              <Field label="Email" value={profileQuery.data.email} />
              <Field
                label="Last login"
                value={
                  profileQuery.data.last_login_at
                    ? new Date(profileQuery.data.last_login_at).toLocaleString()
                    : "—"
                }
              />
            </CardContent>
          </Card>
        )}

        {profileQuery.data && (
          <Card className="flex flex-col gap-3 border-destructive/30 p-6">
            <div>
              <p className="font-semibold">Delete account</p>
              <p className="text-sm text-muted-foreground">
                Removes your account and everything AI Vault holds about you. Files in your own storage stay.
              </p>
            </div>
            <Button variant="destructive" className="self-start" onClick={() => setDeleting(true)}>
              Delete my account…
            </Button>
          </Card>
        )}
        {deleting && profileQuery.data && (
          <DeleteAccountDialog email={profileQuery.data.email} onOpenChange={setDeleting} />
        )}
      </div>
    </AppShell>
  );
}
