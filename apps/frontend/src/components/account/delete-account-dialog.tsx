import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import type { AccountDeletionResult } from "@vault/types";
import { AlertTriangle, Loader2 } from "lucide-react";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { toast } from "@/components/ui/toaster";
import { ApiError, apiClient } from "@/lib/api-client";
import { useAuthStore } from "@/stores/auth-store";

/** Deletes the signed-in account and everything AI Vault holds about it.
 * Files in the person's own Google Drive and on their computer stay. Mounted
 * only while open, so the typed confirmation starts empty every time. */
export function DeleteAccountDialog({ email, onOpenChange }: { email: string; onOpenChange: (open: boolean) => void }) {
  const [typed, setTyped] = useState("");
  const submitted = useRef(false);
  const navigate = useNavigate();
  const matches = typed.trim().toLowerCase() === email.toLowerCase();

  const deleteMutation = useMutation({
    mutationFn: () => apiClient.post<AccountDeletionResult>("/v1/users/me/delete", { confirm_email: typed.trim() }),
    onSuccess: async (result) => {
      try {
        window.localStorage.removeItem("vault-copilot");
      } catch {
        // Storage may be unavailable; nothing of the account is left there.
      }
      await useAuthStore
        .getState()
        .logout()
        .catch(() => undefined);
      toast.success("Your AI Vault account is deleted", {
        description: `${result.files_unindexed.toLocaleString()} files were removed from AI Vault's index. Your files in your own storage are untouched.`,
      });
      void navigate({ to: "/login" });
    },
    onError: (error) => {
      submitted.current = false;
      toast.error(error instanceof ApiError ? error.message : "Couldn't delete your account — nothing was removed.");
    },
  });

  return (
    <Dialog open onOpenChange={(open) => !deleteMutation.isPending && onOpenChange(open)}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-destructive">
            <AlertTriangle className="size-5" /> Delete your AI Vault account
          </DialogTitle>
          <DialogDescription>This can&rsquo;t be undone.</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3 text-sm">
          <div>
            <p className="font-medium">Deleted from AI Vault</p>
            <ul className="mt-1 list-disc pl-5 text-muted-foreground">
              <li>Your account and workspace</li>
              <li>Google Drive and computer connections — AI Vault&rsquo;s access to your Drive is revoked</li>
              <li>The index of your files and folders, extracted text and search data</li>
              <li>Conversations, recommendations, automations and their schedules</li>
              <li>Archive records and AI Vault&rsquo;s cached copies of archives</li>
            </ul>
          </div>
          <div>
            <p className="font-medium">Not touched</p>
            <ul className="mt-1 list-disc pl-5 text-muted-foreground">
              <li>Every file in your Google Drive and on your computer, including archive zips saved there</li>
              <li>The AI Vault agent on your computer — uninstall it yourself; it can no longer connect</li>
            </ul>
          </div>
          <label htmlFor="delete-account-email" className="flex flex-col gap-1.5">
            <span>
              Type <strong className="text-foreground">{email}</strong> to confirm.
            </span>
            <Input
              id="delete-account-email"
              value={typed}
              autoComplete="off"
              onChange={(event) => setTyped(event.target.value)}
              disabled={deleteMutation.isPending}
            />
          </label>
        </div>

        <DialogFooter>
          <Button variant="outline" disabled={deleteMutation.isPending} onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={!matches || deleteMutation.isPending}
            onClick={() => {
              if (submitted.current) return;
              submitted.current = true;
              deleteMutation.mutate();
            }}
          >
            {deleteMutation.isPending ? (
              <>
                <Loader2 className="size-4 animate-spin" /> Deleting…
              </>
            ) : (
              "Delete my account"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
