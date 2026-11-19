import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, createFileRoute, redirect } from "@tanstack/react-router";
import type { AskResponse, ConversationDetail } from "@vault/types";
import { Send } from "lucide-react";
import { useState } from "react";

import { AppShell } from "@/components/app-shell/app-shell";
import { MessageBubble, ThinkingBubble, UserBubble } from "@/components/ai-response/conversation-thread";
import { storageLabel, useStorageScope } from "@/components/storage-intelligence/storage-scope-switcher";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { apiClient } from "@/lib/api-client";
import { useAuthStore } from "@/stores/auth-store";
import { BackLink } from "@/components/ui/back-link";

export const Route = createFileRoute("/chat/$conversationId")({
  beforeLoad: () => {
    if (useAuthStore.getState().status !== "authenticated") {
      throw redirect({ to: "/login" });
    }
  },
  component: ChatDetailPage,
});

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
  const { connectorId, connector } = useStorageScope();
  // A conversation works in one storage workspace; from another one it is
  // read-only, so nothing said here can act on the wrong storage.
  const otherWorkspace = conversation !== undefined && conversation.connector_id !== connectorId;

  return (
    <AppShell title="Conversation">
      <div className="mx-auto flex max-w-3xl flex-col gap-4">
        <BackLink to="/chat">Back to conversations</BackLink>

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
                  <UserBubble text={pendingQuestion} />
                  <ThinkingBubble />
                </>
              )}
            </div>

            {otherWorkspace ? (
              <p className="rounded-lg bg-secondary px-3 py-2 text-sm text-muted-foreground">
                {conversation.connector_id
                  ? "This conversation belongs to another storage workspace — switch to it to continue."
                  : "This conversation is from before storage workspaces and can only be read."}{" "}
                {connector ? (
                  <Link to="/chat" className="underline">
                    Start a new one in {storageLabel(connector)}
                  </Link>
                ) : null}
              </p>
            ) : (
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
            )}
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
