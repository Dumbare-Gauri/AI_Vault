/** Mirrors apps/backend's search endpoint
 * (app/presentation/api/v1/schemas.py's SearchRequest/SearchResponse/SearchResultResponse). */
export type RetrievalMethod = "metadata" | "semantic" | "both";

export type SearchSort = "relevance" | "largest" | "newest" | "oldest";

export interface SearchFilters {
  categories?: string[];
  extensions?: string[];
  size_min?: number | null;
  size_max?: number | null;
  modified_after?: string | null;
  modified_before?: string | null;
  folder?: string | null;
  connector_id?: string | null;
  ownership?: "mine" | "shared" | null;
  sort?: SearchSort;
}

export interface SearchRequest {
  query: string;
  filters?: SearchFilters | null;
  limit?: number;
  offset?: number;
}

export interface SearchResult {
  file_id: string;
  name: string;
  path: string;
  mime_type: string | null;
  size_bytes: number | null;
  provider_modified_at: string | null;
  web_view_link: string | null;
  provider: string | null;
  score: number;
  retrieval_method: RetrievalMethod;
}

export interface SearchResponse {
  query: string;
  total: number;
  /** Plain-language description of how the query was read, e.g. "PDFs", "over 10 MB". */
  understood: string[];
  interpreted_by_ai: boolean;
  results: SearchResult[];
}
