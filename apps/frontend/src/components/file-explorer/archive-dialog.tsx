import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { pluralFiles, runStorageAction } from "@/lib/storage-action";

interface ArchiveDialogProps {
  onOpenChange: (open: boolean) => void;
  fileIds: string[];
  onStarted?: () => void;
}

/** Mounted only while open, so the checkbox starts unchecked on every open. */
export function ArchiveDialog({ onOpenChange, fileIds, onStarted }: ArchiveDialogProps) {
  const [removeOriginals, setRemoveOriginals] = useState(false);
  const [pending, setPending] = useState(false);

  async function archive() {
    setPending(true);
    onOpenChange(false);
    onStarted?.();
    await runStorageAction(
      { file_ids: fileIds, action_type: "create_archive", remove_originals: removeOriginals },
      `Archiving ${pluralFiles(fileIds.length)}…`,
    );
  }

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Archive {pluralFiles(fileIds.length)}</DialogTitle>
          <DialogDescription>
            AI Vault compresses the files into a zip, saves it to &ldquo;AI Vault Archive&rdquo; in
            your Google Drive, and checks the saved copy byte for byte.
          </DialogDescription>
        </DialogHeader>
        <label className="flex items-start gap-3 rounded-lg border border-border p-3 text-sm">
          <Checkbox
            checked={removeOriginals}
            onCheckedChange={(checked) => setRemoveOriginals(checked === true)}
            className="mt-0.5"
          />
          <span>
            <span className="font-medium">Remove originals after the archive is verified</span>
            <span className="block text-muted-foreground">
              Originals go to Google Drive&rsquo;s Trash only once the archive is confirmed. If
              anything fails, your originals stay exactly where they are.
            </span>
          </span>
        </label>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={pending} onClick={() => void archive()}>
            Archive
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
