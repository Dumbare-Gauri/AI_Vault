import type { ConversationMessage } from "@vault/types";
import { Sparkles } from "lucide-react";

import { MarkdownMessage } from "@/components/markdown-message";
import { Badge } from "@/components/ui/badge";
import { assistantToolLabel } from "@/lib/assistant-tool";
import { pickRenderVariant } from "@/lib/ai-response-renderer";

import { AIFileDetailCard } from "./ai-file-detail-card";
import { AIFileResultList } from "./ai-file-result-list";
import { AnswerBlocks } from "./answer-blocks";

/** A full assistant answer: the text, then its structured parts — file
 * lists, duplicate groups, storage summaries, proposals to confirm and
 * their live results — and, for answers drawn from file content, the
 * sources it cites. Structured data never comes from parsing `content`. */
export function AIResponseRenderer({
  message,
  conversationId,
}: {
  message: ConversationMessage;
  conversationId: string;
}) {
  const variant = pickRenderVariant(message);
  return (
    <div className="flex flex-col items-start gap-2">
      <div className="flex w-full items-start gap-2.5">
        <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-ai-muted text-ai">
          <Sparkles className="size-3.5" />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="text-sm">
            <MarkdownMessage content={message.content} />
            {message.tool_name && (
              <p className="mt-1.5 flex items-center gap-1.5 text-xs opacity-60">
                <Badge variant="ai">{assistantToolLabel(message.tool_name)}</Badge>
              </p>
            )}
          </div>
          <AnswerBlocks message={message} conversationId={conversationId} />
          {variant === "file-detail" && message.citations[0] && (
            <AIFileDetailCard citation={message.citations[0]} />
          )}
          {variant === "file-list" && (
            <AIFileResultList citations={message.citations} toolName={message.tool_name} />
          )}
        </div>
      </div>
    </div>
  );
}
