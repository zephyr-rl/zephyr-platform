/** Minimal typed client for the /api/v1 endpoints. */

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(ApiError.describe(status, detail));
    this.status = status;
    this.detail = detail;
  }

  static describe(status: number, detail: unknown): string {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      return detail
        .map((d: { loc?: unknown[]; msg?: string }) => {
          const field = (d.loc ?? []).filter((p) => p !== "body" && p !== "settings").join(".");
          return field ? `${field}: ${d.msg ?? "invalid"}` : (d.msg ?? "Invalid value");
        })
        .join("; ");
    }
    return `Request failed (${status})`;
  }
}

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export async function api<T>(
  path: string,
  options: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;

  const response = await fetch(`/api/v1${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  });
  if (response.status === 204) return undefined as T;
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : null;
    throw new ApiError(response.status, detail);
  }
  return data as T;
}

// ---- response types (mirror backend/app/api/v1) -------------------------------------------

export interface User {
  id: number;
  email: string;
  display_name: string;
  role: string;
}

export interface Me {
  user: User;
  csrf_token: string;
  session_id: number;
}

export interface Store {
  id: number;
  name: string;
  slug: string;
  timezone: string;
  currency: string;
  is_active: boolean;
  shadow_mode: boolean;
}

export interface Connection {
  id: number;
  connector: string;
  connector_name: string;
  label: string;
  credentials: Record<string, string>;
  status: string;
  status_detail: string | null;
  last_checked_at: string | null;
}

export interface ConnectorInfo {
  key: string;
  name: string;
  capabilities: string[];
  credential_fields: string[];
}

export interface JsonSchemaProperty {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  items?: { type?: string };
}

export interface ModuleSchema {
  properties?: Record<string, JsonSchemaProperty>;
  required?: string[];
}

export interface StoreModule {
  key: string;
  name: string;
  description: string;
  enabled: boolean;
  available: boolean;
  missing_capabilities: string[];
  settings: Record<string, unknown>;
  settings_schema: ModuleSchema;
  version: number;
}

export interface SessionInfo {
  id: number;
  created_at: string;
  last_seen_at: string;
  ip: string | null;
  user_agent: string | null;
  current: boolean;
}

export interface AuditEntry {
  id: number;
  at: string;
  actor: string;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  store_id: number | null;
  before: unknown;
  after: unknown;
  reason: string | null;
}

export interface JobsSummary {
  counts: Record<string, number>;
  dead: { id: number; type: string; store_id: number | null; error: string | null }[];
}
