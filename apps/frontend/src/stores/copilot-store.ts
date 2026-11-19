import { useEffect } from "react";
import { create } from "zustand";
import { persist } from "zustand/middleware";

/** What the user is looking at, sent with each question so "these files",
 * "this folder" or "here" mean what is on screen. Ids only — the server
 * checks each one belongs to the current storage workspace. */
export interface CopilotPage {
  page: string;
  /** Where, in words: "C:\Database", "Dashboard". */
  label?: string;
  /** Visible on the page. */
  fileIds?: string[];
  /** The folder being browsed, or a whole connected folder/drive. */
  folderId?: string | null;
  sourceId?: string | null;
  /** What the user ticked. */
  selectedFileIds?: string[];
}

export const MIN_WIDTH = 360;
export const MAX_WIDTH = 880;

interface CopilotState {
  open: boolean;
  setOpen: (open: boolean) => void;
  /** Wider panel for long answers and big result lists. */
  expanded: boolean;
  setExpanded: (expanded: boolean) => void;
  width: number;
  setWidth: (width: number) => void;
  /** One running conversation per storage workspace — a Drive conversation
   * never continues on this computer, or the reverse. */
  conversations: Record<string, string>;
  setConversation: (workspaceId: string, conversationId: string | null) => void;
  page: CopilotPage | null;
  setPage: (page: CopilotPage | null) => void;
  /** "Whole storage" lets one question look beyond the folder on screen. */
  wholeStorage: boolean;
  setWholeStorage: (wholeStorage: boolean) => void;
}

export const useCopilotStore = create<CopilotState>()(
  persist(
    (set) => ({
      open: false,
      setOpen: (open) => set({ open }),
      expanded: false,
      setExpanded: (expanded) => set({ expanded }),
      width: 440,
      setWidth: (width) => set({ width: Math.round(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, width))) }),
      conversations: {},
      setConversation: (workspaceId, conversationId) =>
        set((state) => {
          const conversations = { ...state.conversations };
          if (conversationId) conversations[workspaceId] = conversationId;
          else delete conversations[workspaceId];
          return { conversations };
        }),
      page: null,
      // A new screen starts from its own scope, never the last one's override.
      setPage: (page) => set({ page, wholeStorage: false }),
      wholeStorage: false,
      setWholeStorage: (wholeStorage) => set({ wholeStorage }),
    }),
    {
      name: "vault-copilot",
      partialize: (state) => ({
        conversations: state.conversations,
        open: state.open,
        expanded: state.expanded,
        width: state.width,
      }),
    },
  ),
);

export function pageKey(page: CopilotPage | null): string {
  if (!page) return "";
  return [
    page.page,
    page.label ?? "",
    page.folderId ?? "",
    page.sourceId ?? "",
    (page.fileIds ?? []).join(","),
    (page.selectedFileIds ?? []).join(","),
  ].join("|");
}

/** Tells the copilot what this page shows, for as long as it is mounted. */
export function useCopilotPage(page: CopilotPage): void {
  const setPage = useCopilotStore((state) => state.setPage);
  const key = pageKey(page);
  useEffect(() => {
    setPage(page);
    return () => setPage(null);
    // `key` captures everything in `page` that matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, setPage]);
}
