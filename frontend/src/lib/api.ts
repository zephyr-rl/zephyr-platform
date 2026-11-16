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
          const field = (d.loc ?? [])
            .filter((p) => p !== "body" && p !== "settings")
            .join(".");
          return field
            ? `${field}: ${d.msg ?? "invalid"}`
            : (d.msg ?? "Invalid value");
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
    const detail =
      data && typeof data === "object" && "detail" in data ? data.detail : null;
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
  enum?: string[];
  /** "listings": edited per listing in Listings, not in the module form. */
  ui?: string;
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
  dead: {
    id: number;
    type: string;
    store_id: number | null;
    error: string | null;
  }[];
}

// ---- stock -------------------------------------------------------------------------------

export type ItemState =
  "available" | "reserved" | "delivered" | "extracted" | "quarantined" | "void";

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
    status:
      "new" | "in_pool" | "in_other_pool" | "previously_removed" | "repeated";
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
  items: {
    id: number;
    login: string;
    password: string;
    token: string | null;
    extra: Record<string, string>;
  }[];
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
  restock_enabled: boolean;
  restock_target: number | null;
  auto_resume: boolean;
  vault_count: number | null;
  last_restock_at: string | null;
  last_restock_note: string | null;
  group_id: number | null;
  device: "pc" | "playstation" | "xbox" | null;
  platform: string | null;
  orders_need_you: number;
}

export interface CardDevice {
  device: "pc" | "playstation" | "xbox";
  label: string;
  remote_offer_id: string | null;
  listing_id: number | null;
  title: string | null;
  remote_state: string | null;
  price: string | null;
  currency: string | null;
  vault_count: number | null;
  remote_quantity: number | null;
  removed_remotely: boolean;
  last_restock_at: string | null;
  last_restock_note: string | null;
}

export interface ListingCard {
  id: number;
  title: string;
  pool_id: number | null;
  pool_name: string | null;
  pool_available: number | null;
  restock_enabled: boolean;
  restock_target: number | null;
  effective_target: number;
  auto_resume: boolean;
  vault_additional_info: string | null;
  include_email_details: boolean;
  imported: boolean;
  devices: CardDevice[];
}

export interface RestockResult {
  uploaded: number;
  quarantined: number;
  resolved: number;
  resumed: boolean;
  shadow: boolean;
  vault_count: number | null;
  target: number;
  summary: string;
  ran: boolean;
}

export interface VaultUpload {
  id: number;
  item_id: number;
  login_hint: string;
  state: "pending" | "uploaded" | "failed";
  error: string | null;
  created_at: string;
  resolved_at: string | null;
}

// ---- orders ------------------------------------------------------------------------------

export type FulfilmentState =
  | "pending"
  | "awaiting_buyer"
  | "awaiting_approval"
  | "reserved"
  | "sent"
  | "done"
  | "manual"
  | "awaiting_owner"
  | "needs_attention"
  | "cancelled"
  | "simulated";

export interface OrderRow {
  id: number;
  remote_id: string;
  offer_title: string | null;
  category: string | null;
  quantity: number;
  total: string;
  currency: string;
  buyer_name: string | null;
  remote_state: string | null;
  created_at: string;
  fulfilment_id: number | null;
  fulfilment_state: FulfilmentState | null;
  mode: string | null;
  shadow: boolean;
  units: number | null;
  last_error: string | null;
}

export interface OrderDetail extends OrderRow {
  events: { at: string; actor: string; event: string; message: string }[];
  reserved: { login: string; state: string }[];
  pool_name: string | null;
}

// ---- reports -----------------------------------------------------------------------------

export interface ReportSummary {
  start: string;
  end: string;
  timezone: string;
  currency: string;
  revenue: string;
  orders: number;
  accounts: number;
  cancelled_orders: number;
  cancelled_amount: string;
  series: { day: string; revenue: string; orders: number; accounts: number }[];
  by_type: {
    type: string;
    label: string;
    orders: number;
    units: number;
    revenue: string;
  }[];
  by_listing: {
    listing_id: number | null;
    title: string;
    type: string;
    orders: number;
    units: number;
    revenue: string;
  }[];
  stock: { pool_id: number; pool: string; available: number }[];
  today: string;
}

export interface DailyReportRow {
  id: number;
  day: string;
  created_at: string;
  revenue: string;
  orders: number;
  accounts: number;
}

// ---- messages ----------------------------------------------------------------------------

export interface ChatThread {
  id: number;
  order_id: number | null;
  order_remote_id: string | null;
  order_url: string | null;
  order_state: string | null;
  buyer_name: string | null;
  offer_title: string | null;
  last_buyer_at: string | null;
  last_buyer_text: string | null;
  last_seller_at: string | null;
  last_alert_at: string | null;
  urgent: boolean;
  needs_reply: boolean;
  handled_at: string | null;
  handled_by: string | null;
}

// ---- notifications -----------------------------------------------------------------------

export interface TemplateField {
  name: string;
  value: string;
  inline: boolean;
}

export interface NotificationTemplate {
  title: string;
  description: string;
  color: string;
  footer: string;
  fields: TemplateField[];
}

export interface EventInfo {
  type: string;
  label: string;
  description: string;
  group: string;
  variables: { name: string; description: string; sample: string }[];
}

export interface NotificationCatalog {
  events: EventInfo[];
  presets: {
    key: string;
    name: string;
    event_type: string;
    template: NotificationTemplate;
  }[];
}

export interface NotificationChannel {
  id: number;
  name: string;
  kind: string;
  target: string;
  sender_name: string | null;
  avatar_url: string | null;
  enabled: boolean;
  include_shadow: boolean;
  last_sent_at: string | null;
  last_error: string | null;
  rules: number;
}

export interface NotificationRule {
  id: number;
  name: string;
  event_type: string;
  channel_id: number;
  store_id: number | null;
  enabled: boolean;
  template: NotificationTemplate;
  filters: { listing_ids?: number[]; exclude_offer_ids?: string[] };
  updated_at: string;
}

export interface DiscordEmbed {
  title?: string;
  description?: string;
  color?: number;
  fields?: { name: string; value: string; inline: boolean }[];
  footer?: { text: string };
}

export interface NotificationDeliveryRow {
  id: number;
  created_at: string;
  sent_at: string | null;
  event_type: string;
  rule_id: number | null;
  channel_id: number;
  channel: string;
  store_id: number | null;
  state: "pending" | "sent" | "failed" | "skipped";
  summary: string;
  attempts: number;
  last_error: string | null;
}

// ---- review reminders --------------------------------------------------------------------

export interface ReminderCandidate {
  order_id: number;
  remote_id: string;
  buyer: string;
  title: string | null;
  placed_at: string;
  rating: number | null;
}

export interface ReminderRow {
  id: number;
  remote_order_id: string;
  buyer_name: string | null;
  state: "queued" | "sending" | "sent" | "skipped" | "failed";
  note: string | null;
  queued_by: string;
  created_at: string;
  sent_at: string | null;
}

export interface RemindersStatus {
  enabled: boolean;
  automatic: boolean;
  shadow: boolean;
  daily_cap: number;
  sent_last_day: number;
  queued: number;
  review_filter: string;
  history: ReminderRow[];
}
