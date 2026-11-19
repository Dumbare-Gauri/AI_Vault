import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { AskPageContext, AskResponse, ConversationDetail } from "@vault/types";
import {
  ChevronDown,
  Cloud,
  Laptop,
  Maximize2,
  MessageSquarePlus,
  Minimize2,
  Minus,
  PanelRightOpen,
  Send,
  Sparkles,
} from "lucide-react";
import { type PointerEvent as ReactPointerEvent, useEffect, useState } from "react";

import { MessageBubble, ThinkingBubble, UserBubble } from "@/components/ai-response/conversation-thread";
import { storageLabel, useStorageScope } from "@/components/storage-intelligence/storage-scope-switcher";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ApiError, apiClient } from "@/lib/api-client";
import { cn } from "@/lib/utils";
import { type CopilotPage, MAX_WIDTH, pageKey, useCopilotStore } from "@/stores/copilot-store";

export function asContext(page: CopilotPage | null, wholeStorage: boolean): AskPageContext | undefined {
  if (!page) return undefined;
  return {
    page: page.page,
    label: page.label,
    file_ids: page.fileIds ?? [],
    folder_id: wholeStorage ? null : (page.folderId ?? null),
    source_id: wholeStorage ? null : (page.sourceId ?? null),
    selected_file_ids: page.selectedFileIds ?? [],
  };
}

async function ask(conversationId: string | null, question: string, context: AskPageContext | undefined) {
  const body = { question, context };
  if (!conversationId) return apiClient.post<AskResponse>("/v1/conversations", body);
  try {
    return await apiClient.post<AskResponse>(`/v1/conversations/${conversationId}/messages`, body);
  } catch (error) {
    // The conversation belongs to another workspace (or no longer exists):
    // start a new one here rather than continue it.
    if (error instanceof ApiError && (error.status === 409 || error.status === 404)) {
      return apiClient.post<AskResponse>("/v1/conversations", body);
    }
    throw error;
  }
}

/** What answers will draw on, in words — shown above the input. */
export function scopeSummary(page: CopilotPage | null, wholeStorage: boolean, storage: string): string {
  const selected = page?.selectedFileIds?.length ?? 0;
  if (selected > 0) return `${selected.toLocaleString()} selected file${selected === 1 ? "" : "s"}`;
  const inFolder = !wholeStorage && (page?.folderId || page?.sourceId);
  if (inFolder) return `everything in ${page?.label ?? "this folder"}`;
  return `all of ${storage}`;
}

function suggestions(page: CopilotPage | null): string[] {
  if ((page?.selectedFileIds?.length ?? 0) > 0) {
    return ["Summarize the selected files", "Archive the selected files", "Which of these are duplicates?"];
  }
  if (page?.folderId || page?.sourceId) {
    return ["Summarize all files in this folder", "Find duplicates here", "Organize this folder", "How many files are here?"];
  }
  return ["Show my largest files", "Find duplicate files", "What can I clean up?"];
}

/** The one assistant, available on every page and kept open across
 * navigation. It works in the current storage workspace only; what it
 * answers about follows the screen — the folder, the selection — and is
 * always shown, so an earlier screen never silently answers for this one. */
export function CopilotPanel() {
  const open = useCopilotStore((state) => state.open);
  const setOpen = useCopilotStore((state) => state.setOpen);
  const expanded = useCopilotStore((state) => state.expanded);
  const setExpanded = useCopilotStore((state) => state.setExpanded);
  const width = useCopilotStore((state) => state.width);
  const setWidth = useCopilotStore((state) => state.setWidth);
  const page = useCopilotStore((state) => state.page);
  const wholeStorage = useCopilotStore((state) => state.wholeStorage);
  const setWholeStorage = useCopilotStore((state) => state.setWholeStorage);
  const conversations = useCopilotStore((state) => state.conversations);
  const setConversation = useCopilotStore((state) => state.setConversation);
  const { connectorId, connector } = useStorageScope();
  const queryClient = useQueryClient();
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  // The screen the last question was asked from: a different one now is
  // pointed out, so old answers aren't mistaken for this place's.
  const [askedFrom, setAskedFrom] = useState<string | null>(null);

  useEffect(() => {
    function toggle(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "j") {
        event.preventDefault();
        setOpen(!useCopilotStore.getState().open);
      }
    }
    window.addEventListener("keydown", toggle);
    return () => window.removeEventListener("keydown", toggle);
  }, [setOpen]);

  const conversationId = connectorId ? (conversations[connectorId] ?? null) : null;
  const conversationQuery = useQuery({
    queryKey: ["conversations", conversationId],
    queryFn: () => apiClient.get<ConversationDetail>(`/v1/conversations/${conversationId}`),
    enabled: open && conversationId !== null,
  });
  // Only a conversation of this workspace is ever shown or continued here.
  const conversation = conversationQuery.data?.connector_id === connectorId ? conversationQuery.data : undefined;
  const storage = connector ? storageLabel(connector) : "your storage";
  const scope = scopeSummary(page, wholeStorage, storage);
  const here = page?.label ?? "this page";
  const currentKey = `${connectorId}|${pageKey(page)}|${wholeStorage}`;
  const movedSinceLastQuestion = conversation !== undefined && askedFrom !== null && askedFrom !== currentKey;

  const askMutation = useMutation({
    mutationFn: (text: string) => ask(conversation ? conversation.id : null, text, asContext(page, wholeStorage)),
    onSuccess: (turn) => {
      if (connectorId) setConversation(connectorId, turn.conversation.id);
      setPending(null);
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
    onError: () => {
      setQuestion(pending ?? "");
      setPending(null);
    },
  });

  function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || !connectorId || askMutation.isPending) return;
    setPending(trimmed);
    setQuestion("");
    setAskedFrom(currentKey);
    askMutation.mutate(trimmed);
  }

  function newChat() {
    if (connectorId) setConversation(connectorId, null);
    setAskedFrom(null);
  }

  function startResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const target = event.currentTarget;
    target.setPointerCapture(event.pointerId);
    const move = (moveEvent: PointerEvent) => setWidth(window.innerWidth - moveEvent.clientX);
    const stop = () => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", stop);
      if (expanded) setExpanded(false);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", stop);
  }

  const WorkspaceIcon = connector?.provider === "local_agent" ? Laptop : Cloud;

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="Open Ask Vault (Ctrl+J)"
        className="group fixed bottom-5 right-5 z-40 flex max-w-[min(360px,calc(100vw-2.5rem))] items-center gap-2.5 rounded-full border border-ai/30 bg-card py-2 pl-2 pr-4 text-left shadow-[0_8px_30px_-8px_var(--ai)] transition-all hover:-translate-y-0.5 hover:shadow-[0_12px_36px_-8px_var(--ai)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ai"
      >
        <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-ai to-primary text-ai-foreground">
          <Sparkles className="size-4" />
        </span>
        <span className="min-w-0">
          <span className="block text-sm font-semibold">Ask Vault{conversation || pending ? " · chat open" : ""}</span>
          <span className="block truncate text-xs text-muted-foreground">
            {connector ? `${storage} → ${here}` : "No storage connected"}
          </span>
        </span>
      </button>
    );
  }

  return (
    <aside
      aria-label="Ask Vault"
      style={{ ["--copilot-width" as string]: `${expanded ? MAX_WIDTH : width}px` }}
      className="fixed inset-y-0 right-0 z-40 flex w-full flex-col border-l border-ai/20 bg-background shadow-[-12px_0_40px_-20px_var(--ai)] transition-[width] duration-200 sm:w-[min(var(--copilot-width),100vw)]"
      onKeyDown={(event) => {
        if (event.key === "Escape") setOpen(false);
      }}
    >
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize Ask Vault"
        aria-valuemin={360}
        aria-valuemax={MAX_WIDTH}
        aria-valuenow={expanded ? MAX_WIDTH : width}
        tabIndex={0}
        onPointerDown={startResize}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") setWidth(width + 32);
          if (event.key === "ArrowRight") setWidth(width - 32);
        }}
        className="absolute inset-y-0 -left-1 hidden w-2 cursor-col-resize rounded-full transition-colors hover:bg-ai/30 focus-visible:bg-ai/40 focus-visible:outline-none sm:block"
      />

      <header className="relative flex flex-col gap-2 overflow-hidden border-b border-border px-4 pb-3 pt-3">
        <div aria-hidden className="pointer-events-none absolute -right-16 -top-20 size-48 rounded-full bg-ai/20 blur-3xl" />
        <div className="relative flex items-center gap-2">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-ai to-primary text-ai-foreground shadow-[0_0_18px_-4px_var(--ai)]">
            <Sparkles className="size-4" />
          </span>
          <p className="min-w-0 flex-1 text-sm font-semibold">Ask Vault</p>
          {conversation && (
            <Button variant="ghost" size="icon" className="size-8" asChild aria-label="Open in full view">
              <Link to="/chat/$conversationId" params={{ conversationId: conversation.id }}>
                <PanelRightOpen className="size-4" />
              </Link>
            </Button>
          )}
          <Button variant="ghost" size="icon" className="size-8" aria-label="New chat" title="New chat" onClick={newChat}>
            <MessageSquarePlus className="size-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="hidden size-8 sm:inline-flex"
            aria-label={expanded ? "Narrow the panel" : "Widen the panel"}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
          </Button>
          <Button variant="ghost" size="icon" className="size-8" aria-label="Minimize (Esc)" onClick={() => setOpen(false)}>
            <Minus className="size-4" />
          </Button>
        </div>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="relative flex w-full items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1.5 text-left text-xs shadow-clay-sm transition-colors hover:border-ai/40"
              aria-label="Inspect or change what Ask Vault works on"
            >
              <WorkspaceIcon className="size-3.5 shrink-0 text-ai" />
              <span className="min-w-0 flex-1 truncate">
                {connector ? (
                  <>
                    Working in <span className="font-medium">{storage}</span>
                    {page?.label && !wholeStorage && (
                      <>
                        {" → "}
                        <span className="font-medium">{page.label}</span>
                      </>
                    )}
                  </>
                ) : (
                  "No storage connected"
                )}
              </span>
              <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-72">
            <DropdownMenuLabel className="flex flex-col gap-1 font-normal">
              <span className="text-xs text-muted-foreground">Answers use</span>
              <span className="text-sm font-medium">{scope}</span>
              <span className="text-xs text-muted-foreground">
                Page: {page?.label ?? "—"}
                {(page?.fileIds?.length ?? 0) > 0 && ` · ${page?.fileIds?.length.toLocaleString()} visible`}
                {(page?.selectedFileIds?.length ?? 0) > 0 && ` · ${page?.selectedFileIds?.length.toLocaleString()} selected`}
              </span>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            {(page?.folderId || page?.sourceId) && (
              <DropdownMenuItem disabled={!wholeStorage} onSelect={() => setWholeStorage(false)}>
                Only {page?.label ?? "this folder"}
              </DropdownMenuItem>
            )}
            <DropdownMenuItem disabled={wholeStorage || !(page?.folderId || page?.sourceId)} onSelect={() => setWholeStorage(true)}>
              All of {storage}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={newChat}>Start a new chat here</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </header>

      <div className="flex flex-1 flex-col gap-4 overflow-y-auto px-4 py-4" aria-live="polite">
        {!conversation && !pending && (
          <div className="flex flex-col gap-3 text-sm text-muted-foreground">
            <p>
              Ask about {scope}, or say what to do. Nothing changes in {storage} until you confirm.
            </p>
            <div className="flex flex-wrap gap-2">
              {suggestions(page).map((text) => (
                <button
                  key={text}
                  type="button"
                  disabled={!connectorId}
                  onClick={() => send(text)}
                  className="rounded-full border border-ai/30 bg-ai-muted px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:border-ai hover:bg-ai/15 disabled:opacity-50"
                >
                  {text}
                </button>
              ))}
            </div>
          </div>
        )}
        {conversation?.messages.map((message) => (
          <MessageBubble key={message.id} message={message} conversationId={conversation.id} />
        ))}
        {pending && (
          <>
            <UserBubble text={pending} />
            <ThinkingBubble />
          </>
        )}
        {movedSinceLastQuestion && !pending && (
          <div role="note" className="flex items-center gap-2 rounded-lg border border-dashed border-ai/40 bg-ai-muted/60 px-3 py-2 text-xs">
            <span className="min-w-0 flex-1">
              You&rsquo;re now in <span className="font-medium">{here}</span>. New questions use {scope}; answers above were
              about where you were.
            </span>
            <button type="button" onClick={newChat} className="shrink-0 font-medium text-ai hover:underline">
              New chat
            </button>
          </div>
        )}
        {askMutation.isError && (
          <p role="alert" className="text-sm text-destructive">
            {askMutation.error instanceof ApiError ? askMutation.error.message : "Couldn't send that — try again."}
          </p>
        )}
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          send(question);
        }}
        className="flex flex-col gap-2 border-t border-border p-3"
      >
        <p className="truncate text-xs text-muted-foreground">
          Answers use <span className="font-medium text-foreground">{scope}</span>
        </p>
        <div className="flex items-end gap-2 rounded-2xl border border-input bg-card p-1.5 shadow-clay-sm transition-shadow focus-within:border-ai/50 focus-within:shadow-[0_0_0_3px_color-mix(in_srgb,var(--ai)_18%,transparent)]">
          <textarea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            placeholder={connector ? `Ask about ${scope}…` : "Connect a storage first"}
            disabled={!connectorId}
            rows={1}
            aria-label="Your question"
            className="max-h-40 min-h-9 flex-1 resize-none bg-transparent px-2.5 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none"
          />
          <Button
            type="submit"
            variant="ai"
            size="icon"
            className={cn("shrink-0 rounded-full", askMutation.isPending && "animate-pulse")}
            disabled={askMutation.isPending || !question.trim() || !connectorId}
            aria-label="Send"
          >
            <Send className="size-4" />
          </Button>
        </div>
      </form>
    </aside>
  );
}
