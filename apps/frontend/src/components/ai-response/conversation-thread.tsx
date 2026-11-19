import type { ConversationMessage } from "@vault/types";
import { Sparkles } from "lucide-react";

import { AIResponseRenderer } from "@/components/ai-response/ai-response-renderer";

/** Shown while an answer is being generated. */
export function ThinkingBubble() {
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

export function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex flex-col items-end gap-2">
      <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-tr-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground shadow-clay-sm">
        {text}
      </div>
    </div>
  );
}

/** One message of a conversation, the same in the chat page and the copilot. */
export function MessageBubble({
  message,
  conversationId,
}: {
  message: ConversationMessage;
  conversationId: string;
}) {
  if (message.role === "user") return <UserBubble text={message.content} />;
  return <AIResponseRenderer message={message} conversationId={conversationId} />;
}
