import type { Connector } from "@vault/types";

import { settleWorkspace } from "@/stores/storage-scope-store";

import { apiClient } from "./api-client";

/** The organization's connections — every `["connectors"]` query uses this,
 * so the remembered workspace is checked against each fresh list. */
export async function fetchConnectors(): Promise<Connector[]> {
  const connectors = await apiClient.get<Connector[]>("/v1/connectors");
  settleWorkspace(connectors);
  return connectors;
}
