import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, createFileRoute, redirect, useNavigate } from "@tanstack/react-router";
import type { AskResponse, Conversation } from "@vault/types";
import { MessageCircle, Send, Sparkles } from "lucide-react";
import { useState } from "react";

import { AppShell } from "@/components/app-shell/app-shell";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { apiClient } from "@/lib/api-client";
import { formatRelativeTime } from "@/lib/format-relative-time";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/chat/")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: ChatPage,
});

const SUGGESTIONS = [
  "What is consuming the most storage?",
  "Show me every duplicate file",
  "How much storage can I recover?",
  "Find all PDFs from last year",
  "Which files can I safely archive?",
  "Organize my project files",
];

function ChatPage() {
  const navigate = useNavigate();
  const [question, setQuestion] = useState("");
  // Echoed immediately below the form so the request feels live rather than
  // a silent multi-second wait behind a disabled button.
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);

  const conversationsQuery = useQuery({
    queryKey: ["conversations"],
    queryFn: () => apiClient.get<Conversation[]>("/v1/conversations"),
  });

  const askMutation = useMutation({
    mutationFn: (askQuestion: string) =>
      apiClient.post<AskResponse>("/v1/conversations", { question: askQuestion }),
    onSuccess: (response) => {
      // An in-app SPA navigation — a prior version of this used a hard
      // `window.location.href` reload here as a workaround for a routing
      // bug (a missing `<Outlet />` on the parent layout route, since
      // fixed) that made the destination page never render. That's no
      // longer needed and was causing a jarring full-page flash plus lost
      // in-flight state on every new question.
      void navigate({
        to: "/chat/$conversationId",
        params: { conversationId: response.conversation.id },
      });
    },
    onError: () => {
      setQuestion(pendingQuestion ?? "");
      setPendingQuestion(null);
    },
  });

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = question.trim();
    if (trimmed.length === 0) return;
    setPendingQuestion(trimmed);
    setQuestion("");
    askMutation.mutate(trimmed);
  }

  const conversations = conversationsQuery.data ?? [];

  return (
    <AppShell title="Ask Vault">
      <div className="mx-auto flex max-w-2xl flex-col gap-6">
        <Card clay className="flex flex-col gap-4 p-6">
          <div className="flex items-center gap-2">
            <span className="flex size-9 items-center justify-center rounded-xl bg-ai-muted text-ai">
              <Sparkles className="size-[18px]" />
            </span>
            <div>
              <h1 className="text-base font-semibold">Ask Vault</h1>
              <p className="text-xs text-muted-foreground">
                Ask about your files or tell Vault what to change. Nothing changes until you confirm.
              </p>
            </div>
          </div>

          {pendingQuestion ? (
            <div className="flex flex-col gap-4">
              <div className="flex flex-col items-end gap-2">
                <div className="max-w-[85%] rounded-2xl rounded-tr-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground shadow-clay-sm">
                  {pendingQuestion}
                </div>
              </div>
              <div className="flex items-start gap-2.5">
                <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-ai-muted text-ai">
                  <Sparkles className="size-3.5" />
                </span>
                <div className="flex items-center gap-1 rounded-2xl rounded-tl-sm border border-border bg-card px-4 py-3 shadow-clay-sm">
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:-0.3s]" />
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:-0.15s]" />
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground" />
                </div>
              </div>
            </div>
          ) : (
            <form onSubmit={handleSubmit} className="flex flex-col gap-3">
              <textarea
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
                placeholder="Ask about your files, or tell Vault what to do — “find the Blarrow invoices”, “delete duplicate videos”…"
                rows={3}
                autoFocus
                className="w-full resize-none rounded-xl border border-input bg-card p-3 text-sm shadow-clay-inset placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
              <div className="flex flex-wrap gap-2">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    onClick={() => setQuestion(suggestion)}
                    className="rounded-full border border-border px-3 py-1 text-xs text-muted-foreground transition-colors hover:border-ai/40 hover:text-ai"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
              <Button type="submit" variant="ai" className="w-fit" disabled={question.trim().length === 0}>
                <Send className="size-4" />
                Ask
              </Button>
              {askMutation.isError && (
                <p className="text-sm text-destructive">
                  Couldn&rsquo;t send that question. Please try again.
                </p>
              )}
            </form>
          )}
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <MessageCircle className="size-4" /> Conversations
            </CardTitle>
          </CardHeader>
          <CardContent>
            {conversationsQuery.isLoading && (
              <div className="flex flex-col gap-2">
                {Array.from({ length: 3 }).map((_, i) => (
                  <Skeleton key={i} className="h-12 rounded-lg" />
                ))}
              </div>
            )}
            {conversationsQuery.isError && (
              <p className="text-sm text-destructive">Couldn&rsquo;t load conversation history.</p>
            )}
            {conversations.length === 0 && !conversationsQuery.isLoading ? (
              <EmptyState
                icon={MessageCircle}
                title="No conversations yet"
                description="Ask a question above to start your first conversation with Vault."
              />
            ) : (
              <ul className="flex flex-col divide-y divide-border">
                {conversations.map((conversation) => (
                  <li key={conversation.id}>
                    <Link
                      to="/chat/$conversationId"
                      params={{ conversationId: conversation.id }}
                      className="flex items-center justify-between gap-3 py-2.5 text-sm transition-colors hover:text-primary"
                    >
                      <span className="truncate font-medium">
                        {conversation.title ?? "Untitled conversation"}
                      </span>
                      <span className="shrink-0 text-xs text-muted-foreground">
                        {formatRelativeTime(conversation.updated_at)}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </AppShell>
  );
}
