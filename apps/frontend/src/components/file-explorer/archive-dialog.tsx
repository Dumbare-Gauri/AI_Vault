import { useQuery } from "@tanstack/react-query";
import type { FolderSummary } from "@vault/types";
import { Folder, FolderArchive, Search } from "lucide-react";
import { useEffect, useState } from "react";

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
import { apiClient } from "@/lib/api-client";
import { archiveNameProblem, withZip } from "@/lib/archive-name";
import { pluralFiles, runStorageAction } from "@/lib/storage-action";
import { cn } from "@/lib/utils";

interface ArchiveDialogProps {
  onOpenChange: (open: boolean) => void;
  connectorId: string;
  /** "Google Drive" or "this computer". */
  storage: string;
  fileIds?: string[];
  /** Whole folders, archived with their structure. */
  folderIds?: string[];
  /** What is being archived, in words: "Project Docs", "3 files". */
  subject: string;
  suggestedName: string;
  /** The folder the user is in — offered as the destination. */
  here?: { id: string; label: string } | null;
  onStarted?: () => void;
}

type Destination = "here" | "default" | "other";

/** Names an archive and picks where it goes, in the same storage. The
 * originals always stay: removing them is a separate step the user takes
 * after the archive is verified. Mounted only while open, so every open
 * starts from the suggestion. */
export function ArchiveDialog({
  onOpenChange,
  connectorId,
  storage,
  fileIds = [],
  folderIds = [],
  subject,
  suggestedName,
  here = null,
  onStarted,
}: ArchiveDialogProps) {
  const [name, setName] = useState(suggestedName);
  const [touched, setTouched] = useState(false);
  const [destination, setDestination] = useState<Destination>(here ? "here" : "default");
  const [other, setOther] = useState<FolderSummary | null>(null);
  const [folderQuery, setFolderQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [pending, setPending] = useState(false);

  useEffect(() => {
    const timeout = setTimeout(() => setDebouncedQuery(folderQuery), 250);
    return () => clearTimeout(timeout);
  }, [folderQuery]);

  const foldersQuery = useQuery({
    queryKey: ["folders", connectorId, debouncedQuery],
    queryFn: () =>
      apiClient.get<FolderSummary[]>(`/v1/connectors/${connectorId}/folders?query=${encodeURIComponent(debouncedQuery)}`),
    enabled: destination === "other",
  });

  const problem = archiveNameProblem(name);
  const finalName = withZip(name);
  const folderId = destination === "here" ? here?.id : destination === "other" ? other?.id : undefined;
  const whereLabel =
    destination === "here" ? here?.label : destination === "other" ? other?.path : `AI Vault Archive/${new Date().getFullYear()}`;
  const ready = !problem && !pending && (destination !== "other" || other !== null);

  async function archive() {
    setPending(true);
    onOpenChange(false);
    onStarted?.();
    await runStorageAction(
      {
        action_type: "create_archive",
        file_ids: fileIds.length > 0 ? fileIds : undefined,
        folder_ids: folderIds.length > 0 ? folderIds : undefined,
        archive_name: finalName,
        archive_folder_id: folderId,
      },
      `Archiving ${subject} into “${finalName}” in ${whereLabel}…`,
    );
  }

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <FolderArchive className="size-5" /> Archive {subject}
          </DialogTitle>
          <DialogDescription>
            AI Vault zips {fileIds.length > 0 && folderIds.length === 0 ? pluralFiles(fileIds.length) : "it, keeping its folder structure"},
            saves the zip in {storage}, and reads it back to check it before reporting success. Originals stay where
            they are.
          </DialogDescription>
        </DialogHeader>

        <form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (ready) void archive();
          }}
        >
          <label htmlFor="archive-name" className="flex flex-col gap-1.5 text-sm">
            <span className="font-medium">Archive name</span>
            <Input
              id="archive-name"
              value={name}
              autoFocus
              onChange={(event) => setName(event.target.value)}
              onBlur={() => setTouched(true)}
              aria-invalid={touched && problem !== null}
              aria-describedby="archive-name-hint"
            />
            <span
              id="archive-name-hint"
              className={cn("text-xs", touched && problem ? "text-destructive" : "text-muted-foreground")}
            >
              {touched && problem ? problem : `Saved as “${finalName}”. If that name is taken there, a number is added — nothing is overwritten.`}
            </span>
          </label>

          <fieldset className="flex flex-col gap-2 text-sm">
            <legend className="mb-1.5 font-medium">Save it in</legend>
            {here && (
              <DestinationOption
                checked={destination === "here"}
                onSelect={() => setDestination("here")}
                title={here.label}
                hint="The folder you're in"
              />
            )}
            <DestinationOption
              checked={destination === "default"}
              onSelect={() => setDestination("default")}
              title={`AI Vault Archive / ${new Date().getFullYear()}`}
              hint={`Created in ${storage} if it doesn't exist`}
            />
            <DestinationOption
              checked={destination === "other"}
              onSelect={() => setDestination("other")}
              title={other ? other.path : "Another folder…"}
              hint={`Any folder in ${storage}`}
            />
            {destination === "other" && (
              <div className="flex flex-col gap-2 rounded-lg border border-border p-2">
                <div className="relative">
                  <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={folderQuery}
                    onChange={(event) => setFolderQuery(event.target.value)}
                    placeholder="Search folders…"
                    className="pl-8"
                    aria-label="Search folders"
                  />
                </div>
                <ul className="max-h-40 overflow-y-auto">
                  {foldersQuery.data?.length === 0 && <li className="px-2 py-1.5 text-xs text-muted-foreground">No matching folders.</li>}
                  {foldersQuery.data?.map((folder) => (
                    <li key={folder.id}>
                      <button
                        type="button"
                        onClick={() => setOther(folder)}
                        className={cn(
                          "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-secondary",
                          other?.id === folder.id && "bg-secondary font-medium",
                        )}
                      >
                        <Folder className="size-4 shrink-0 text-muted-foreground" />
                        <span className="truncate">{folder.path}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </fieldset>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!ready}>
              Create archive
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function DestinationOption({
  checked,
  onSelect,
  title,
  hint,
}: {
  checked: boolean;
  onSelect: () => void;
  title: string;
  hint: string;
}) {
  return (
    <label
      className={cn(
        "flex cursor-pointer items-start gap-3 rounded-lg border p-2.5 transition-colors",
        checked ? "border-primary bg-primary/5" : "border-border hover:bg-secondary/60",
      )}
    >
      <input type="radio" name="archive-destination" checked={checked} onChange={onSelect} className="mt-1 accent-primary" />
      <span className="min-w-0">
        <span className="block truncate font-medium">{title}</span>
        <span className="block text-xs text-muted-foreground">{hint}</span>
      </span>
    </label>
  );
}
