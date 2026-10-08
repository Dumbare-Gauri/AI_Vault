import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { AskResponse, ConversationDetail, ConversationMessage } from "@vault/types";
import { ChevronLeft, Send, Sparkles } from "lucide-react";
import { useState } from "react";

import { AppShell } from "@/components/app-shell/app-shell";
import { AIResponseRenderer } from "@/components/ai-response/ai-response-renderer";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { apiClient } from "@/lib/api-client";
import { useAuthStore } from "@/stores/auth-store";

export const Route = createFileRoute("/chat/$conversationId")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: ChatDetailPage,
});

function ThinkingBubble() {
  return (
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
  );
}

function MessageBubble({
  message,
  conversationId,
}: {
  message: ConversationMessage;
  conversationId: string;
}) {
  if (message.role === "user") {
    return (
      <div className="flex flex-col items-end gap-2">
        <div className="flex max-w-[85%] flex-row-reverse items-start gap-2.5">
          <div className="whitespace-pre-wrap rounded-2xl rounded-tr-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground shadow-clay-sm">
            {message.content}
          </div>
        </div>
      </div>
    );
  }

  return <AIResponseRenderer message={message} conversationId={conversationId} />;
}

function ChatDetailPage() {
  const { conversationId } = Route.useParams();
  const queryClient = useQueryClient();
  const [question, setQuestion] = useState("");
  // Echoed immediately on submit so the thread feels live while the real
  // answer is still generating server-side, rather than sitting blank for
  // the several seconds a real completion call takes.
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);

  const conversationQuery = useQuery({
    queryKey: ["conversations", conversationId],
    queryFn: () => apiClient.get<ConversationDetail>(`/v1/conversations/${conversationId}`),
  });

  const askMutation = useMutation({
    mutationFn: (askQuestion: string) =>
      apiClient.post<AskResponse>(`/v1/conversations/${conversationId}/messages`, {
        question: askQuestion,
      }),
    onSuccess: () => {
      setPendingQuestion(null);
      void queryClient.invalidateQueries({ queryKey: ["conversations", conversationId] });
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

  const conversation = conversationQuery.data;

  return (
    <AppShell title="Conversation">
      <div className="mx-auto flex max-w-3xl flex-col gap-4">
        <Link
          to="/chat"
          className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ChevronLeft className="size-4" /> Back to conversations
        </Link>

        {conversationQuery.isLoading && (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-16 w-2/3 rounded-2xl" />
            <Skeleton className="ml-auto h-10 w-1/2 rounded-2xl" />
          </div>
        )}
        {conversationQuery.isError && (
          <p className="text-sm text-destructive">Couldn&rsquo;t load this conversation.</p>
        )}

        {conversation && (
          <>
            <h1 className="text-lg font-semibold">
              {conversation.title ?? "Untitled conversation"}
            </h1>

            <div className="flex flex-col gap-5">
              {conversation.messages.map((message) => (
                <MessageBubble key={message.id} message={message} conversationId={conversationId} />
              ))}
              {pendingQuestion && (
                <>
                  <div className="flex flex-col items-end gap-2">
                    <div className="max-w-[85%] rounded-2xl rounded-tr-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground shadow-clay-sm">
                      {pendingQuestion}
                    </div>
                  </div>
                  <ThinkingBubble />
                </>
              )}
            </div>

            <form onSubmit={handleSubmit} className="sticky bottom-4 flex gap-2 pt-2">
              <textarea
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
                placeholder="Ask a follow-up, or say what to do — “move them into Finance”…"
                rows={1}
                className="flex-1 resize-none rounded-full border border-input bg-card px-4 py-2.5 text-sm shadow-clay placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
              <Button
                type="submit"
                variant="ai"
                size="icon"
                className="rounded-full"
                disabled={askMutation.isPending || question.trim().length === 0}
                aria-label="Send"
              >
                <Send className="size-4" />
              </Button>
            </form>
            {askMutation.isError && (
              <p className="text-sm text-destructive">
                Couldn&rsquo;t send that message. Please try again.
              </p>
            )}
          </>
        )}
      </div>
    </AppShell>
  );
}
