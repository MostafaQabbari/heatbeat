// What this file does: one typed API client for the whole portal. The types come from
// docs/openapi.json (npm run generate:api), so a changed API breaks the build here
// instead of breaking silently in the browser. It adds the login token to every request
// and turns problem+json errors into ApiError.
import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type PlantSummary = Schemas["PlantSummary"];
export type PlantDetail = Schemas["PlantDetail"];
export type Series = Schemas["Series"];
export type Alarm = Schemas["Alarm"];
export type AlarmEvent = Schemas["AlarmEvent"];
export type Command = Schemas["Command"];
export type AuditEntry = Schemas["AuditEntry"];
export type Parameter = Schemas["Parameter"];
export type Recommendation = Schemas["Recommendation"];
export type FaultStats = Schemas["FaultStats"];
export type Me = Schemas["Me"];

export class ApiError extends Error {
  constructor(
    public status: number,
    public title: string,
    public detail?: string | null,
  ) {
    super(detail ? `${title}: ${detail}` : title);
  }
}

let tokenProvider: () => string | null = () => null;
let onUnauthorized: () => void = () => {};

export function configureAuth(getToken: () => string | null, unauthorized: () => void): void {
  tokenProvider = getToken;
  onUnauthorized = unauthorized;
}

const authMiddleware: Middleware = {
  onRequest({ request }) {
    const token = tokenProvider();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    return request;
  },
  onResponse({ response }) {
    if (response.status === 401) onUnauthorized();
    return response;
  },
};

export const api = createClient<paths>({ baseUrl: window.location.origin });
api.use(authMiddleware);

/** Return the data, or throw ApiError with the problem+json title and detail. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.response.ok && result.data !== undefined) return result.data;
  const problem = (result.error ?? {}) as { title?: string; detail?: string | null };
  throw new ApiError(result.response.status, problem.title ?? result.response.statusText, problem.detail);
}

export async function requestToken(email: string, password: string): Promise<string> {
  const response = await fetch("/v1/auth/token", {
    method: "POST",
    body: new URLSearchParams({ username: email, password }),
  });
  const body = await response.json();
  if (!response.ok) throw new ApiError(response.status, body.title ?? "Login failed", body.detail);
  return body.access_token as string;
}
