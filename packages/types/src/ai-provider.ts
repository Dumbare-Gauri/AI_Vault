/** Mirrors apps/backend's per-organization AI provider endpoints
 * (app/presentation/api/v1/schemas.py's AIProviderConfig* schemas). */
export interface AIProviderOption {
  id: string;
  label: string;
  model_hint: string;
}

export interface AIProviderConfig {
  configured: boolean;
  model_name: string | null;
  /** One of `available_providers[].id`. */
  provider: string | null;
  available_providers: AIProviderOption[];
}

export interface AIProviderConfigUpdateRequest {
  api_key: string | null;
  model_name: string;
  provider: string;
}

export interface AIProviderConfigTestRequest {
  api_key: string | null;
  model_name: string;
  provider: string;
}

export interface AIProviderConfigTestResponse {
  success: boolean;
  error: string | null;
}
