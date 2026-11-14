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

// ---- stock -------------------------------------------------------------------------------

export type ItemState = "available" | "reserved" | "delivered" | "extracted" | "quarantined" | "void";

export interface StockPool {
  id: number;
  name: string;
  game: string;
  item_type: string;
  low_stock_threshold: number;
  archived: boolean;
  counts: Record<ItemState, number>;
  low: boolean;
}

export interface StockItem {
  id: number;
  pool_id: number;
  login_hint: string;
  state: ItemState;
  position: number;
  has_token: boolean;
  note: string | null;
  created_at: string;
  state_changed_at: string;
}

export interface ItemsPage {
  items: StockItem[];
  total: number;
  next_after: number | null;
}

export interface ImportPreview {
  text_sha256: string;
  detected_format: string;
  counts: Record<string, number>;
  lines: {
    line: number;
    login_hint: string;
    status: "new" | "in_pool" | "in_other_pool" | "previously_removed" | "repeated";
    format: string;
    has_token: boolean;
    token_will_attach: boolean;
  }[];
  invalid_lines: number[];
  ignored_lines: number[];
}

export interface Extraction {
  id: number;
  pool_id: number;
  pool_name: string;
  actor: string;
  created_at: string;
  count: number;
  reason: string | null;
  undone_at: string | null;
}

export interface ExtractionDetail extends Extraction {
  items: { id: number; login: string; password: string; token: string | null; extra: Record<string, string> }[];
  text: string;
  tokens_text: string;
}

export interface ItemEvent {
  at: string;
  actor: string;
  event: string;
  from_state: string | null;
  to_state: string | null;
  details: Record<string, unknown> | null;
}

// ---- listings ----------------------------------------------------------------------------

export type DeliveryMode = "manual" | "assisted" | "auto" | "text";

export interface Listing {
  id: number;
  connection_id: number;
  connector: string;
  remote_offer_id: string;
  kind: "account" | "item" | "topup";
  title: string;
  remote_state: string;
  price: string;
  currency: string;
  remote_quantity: number | null;
  removed_remotely: boolean;
  last_synced_at: string | null;
  pool_id: number | null;
  pool_name: string | null;
  delivery_mode: DeliveryMode;
  units_per_sale: number;
  require_confirmation: boolean;
  delivery_template: string | null;
  vault_additional_info: string | null;
  include_email_details: boolean;
}
