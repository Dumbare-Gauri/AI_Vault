import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { LocalAgentKey } from "@vault/types";

import { apiClient } from "@/lib/api-client";

/** The agent's pairing port on this computer — loopback only. */
const AGENT_HELPER = "http://127.0.0.1:47823";
const START_LINK = "aivault-agent://start";
const START_WAIT_MS = 15_000;

export class AgentNotInstalledError extends Error {
  constructor() {
    super("The AI Vault agent isn't installed on this computer yet.");
  }
}

interface HelperStatus {
  paired: boolean;
  device_name: string;
}

async function helperStatus(): Promise<HelperStatus | null> {
  try {
    const response = await fetch(`${AGENT_HELPER}/status`, { signal: AbortSignal.timeout(1500) });
    return response.ok ? ((await response.json()) as HelperStatus) : null;
  } catch {
    return null;
  }
}

async function waitForHelper(): Promise<HelperStatus | null> {
  const deadline = Date.now() + START_WAIT_MS;
  while (Date.now() < deadline) {
    const status = await helperStatus();
    if (status) return status;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  return null;
}

/** Starts the agent if it isn't running (through the link the one-time
 * install registered), then hands it a fresh key directly — nothing to copy
 * or type. */
export function useStartAgent() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ name }: { name?: string }) => {
      let status = await helperStatus();
      if (!status) {
        window.location.href = START_LINK;
        status = await waitForHelper();
      }
      if (!status) throw new AgentNotInstalledError();
      const key = await apiClient.post<LocalAgentKey>("/v1/local-agents", { name: name ?? status.device_name });
      const paired = await fetch(`${AGENT_HELPER}/pair`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: key.agent_key }),
      });
      if (!paired.ok) throw new Error("The agent refused the connection from this page.");
      return key;
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["local-agents"] });
      void queryClient.invalidateQueries({ queryKey: ["connectors"] });
    },
  });
}
