import type { ApiErrorResponse } from "@vault/types";

import { getAccessToken, setAccessToken, tryRefreshAccessToken } from "./session";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function isAuthEndpoint(path: string): boolean {
  return path.startsWith("/v1/auth/");
}

async function parseErrorResponse(response: Response): Promise<ApiError> {
  let code = "unknown_error";
  let message = response.statusText;
  try {
    const body = (await response.json()) as ApiErrorResponse;
    code = body.error.code;
    message = body.error.message;
  } catch {
    // Response body wasn't our error shape (e.g. a proxy/network failure) —
    // fall back to the status text already assigned above.
  }
  return new ApiError(message, response.status, code);
}

function buildHeaders(init?: RequestInit): HeadersInit {
  const accessToken = getAccessToken();
  return {
    "Content-Type": "application/json",
    ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    ...init?.headers,
  };
}

/** Thin fetch wrapper — the only place `apps/frontend` talks to `apps/backend`
 * (Engineering Handbook §8.9: "the dashboard never queries a datastore or
 * module directly," only the backend's documented API surface).
 *
 * `credentials: "include"` on every call so the httpOnly refresh cookie
 * reaches /auth/refresh and /auth/logout — harmless on endpoints that don't
 * need it. On a 401 from anything *other* than the auth endpoints
 * themselves, attempts exactly one silent refresh-and-retry before giving up
 * (session recovery after an expired access token, not just on cold load). */
async function request<T>(path: string, init?: RequestInit, _isRetry = false): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    credentials: "include",
    headers: buildHeaders(init),
  });

  if (response.status === 401 && !_isRetry && !isAuthEndpoint(path)) {
    const refreshedToken = await tryRefreshAccessToken();
    if (refreshedToken) {
      setAccessToken(refreshedToken);
      return request<T>(path, init, true);
    }
    setAccessToken(null);
  }

  if (!response.ok) {
    throw await parseErrorResponse(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

/** A file's bytes, with the same auth and refresh handling as JSON calls. */
async function requestBlob(path: string, _isRetry = false): Promise<Blob> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "GET",
    credentials: "include",
    headers: buildHeaders(),
  });
  if (response.status === 401 && !_isRetry) {
    const refreshedToken = await tryRefreshAccessToken();
    if (refreshedToken) {
      setAccessToken(refreshedToken);
      return requestBlob(path, true);
    }
    setAccessToken(null);
  }
  if (!response.ok) {
    throw await parseErrorResponse(response);
  }
  return response.blob();
}

export const apiClient = {
  blob: (path: string) => requestBlob(path),
  get: <T>(path: string) => request<T>(path, { method: "GET" }),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "POST",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
