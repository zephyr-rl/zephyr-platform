import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { ScopeSwitch, useScope } from "@/components/Scope";
import { Badge, Button, Card, EmptyState, StatusDot } from "@/components/ui";
import { api, type Connection, type JobsSummary, type ReportSummary, type StockPool, type StoreModule } from "@/lib/api";

interface Activity {
  id: number;
  at: string;
  actor: string;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
}

const ACTION_LABEL: Record<string, string> = {
  "stock.imported": "Stock imported",
  "stock.extracted": "Accounts extracted",
  "stock.moved": "Accounts moved",
  "stock.quarantined": "Accounts quarantined",
  "order.approved": "Delivery approved",
  "order.resolved": "Order handled by hand",
  "listing.updated": "Listing changed",
  "listing.resumed": "Offer resumed after a restock",
  "listing.restock_requested": "Restock started",
  "module.updated": "Automation changed",
  "store.updated": "Store changed",
  "review_reminders.queued": "Review reminders queued",
  "chat.marked_handled": "Conversation marked handled",
};

function actionLabel(action: string): string {
  if (ACTION_LABEL[action]) return ACTION_LABEL[action];
  if (action.startsWith("shadow.")) return `Shadow mode: would ${action.slice(7).replaceAll("_", " ")}`;
  const text = action.replaceAll(".", " ").replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const timeOnly = new Intl.DateTimeFormat(undefined, { dateStyle: "short", timeStyle: "short" });
import { useStores } from "@/lib/session";

function Figure({ label, value, tone }: { label: string; value: string | number; tone?: "warning" }) {
  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <div className="text-xs text-muted">{label}</div>
      <div className={`tabular mt-1.5 text-[28px] font-semibold ${tone === "warning" ? "text-warning" : ""}`}>
        {value}
      </div>
    </div>
  );
}

interface CombinedOverview {
  currency: string;
  stores: {
    store_id: number;
    store: string;
    shadow_mode: boolean;
    revenue_today: string;
    orders_today: number;
    accounts_today: number;
    orders_need_you: number;
    buyers_waiting: number;
    urgent_messages: number;
    low_pools: number;
  }[];
  activity: { id: number; at: string; store: string | null; actor: string; action: string }[];
}

function AllStoresOverview() {
  const { select } = useStores();
  const navigate = useNavigate();
  const data = useQuery({
    queryKey: ["all", "overview"],
    queryFn: () => api<CombinedOverview>("/all/overview"),
    refetchInterval: 30_000,
  });
  const d = data.data;
  const money = (v: number | string) => new Intl.NumberFormat(undefined, { style: "currency", currency: d?.currency ?? "USD" }).format(Number(v));
  const sum = (pick: (s: CombinedOverview["stores"][number]) => number) => d?.stores.reduce((t, s) => t + pick(s), 0) ?? 0;
  const open = (storeId: number, to: "/" | "/orders" | "/messages" | "/stock") => {
    select(storeId);
    void navigate({ to });
  };
  return (
    <>
      <section aria-label="Today, all stores" className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Figure label="Revenue today" value={d ? money(sum((s) => Number(s.revenue_today))) : "—"} />
        <Figure label="Orders today" value={d ? sum((s) => s.orders_today) : "—"} />
        <Figure label="Accounts delivered today" value={d ? sum((s) => s.accounts_today) : "—"} />
        <Figure label="Orders that need you" value={d ? sum((s) => s.orders_need_you) : "—"} tone={sum((s) => s.orders_need_you) ? "warning" : undefined} />
      </section>
      <Card title="Stores">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] border-collapse text-[13px]">
            <thead>
              <tr className="text-left text-muted">
                <th className="px-4 py-2.5 font-medium">Store</th>
                <th className="px-4 py-2.5 text-right font-medium">Revenue today</th>
                <th className="px-4 py-2.5 text-right font-medium">Orders</th>
                <th className="px-4 py-2.5 text-right font-medium">Accounts</th>
                <th className="px-4 py-2.5 text-right font-medium">Need you</th>
                <th className="px-4 py-2.5 text-right font-medium">Buyers waiting</th>
                <th className="px-4 py-2.5 text-right font-medium">Low pools</th>
                <th className="px-4 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {d?.stores.map((s) => (
                <tr key={s.store_id} className="border-t border-line">
                  <td className="px-4 py-2.5">
                    <span className="font-medium">{s.store}</span>
                    {s.shadow_mode && <span className="ml-2 text-[12px] text-warning">shadow mode</span>}
                  </td>
                  <td className="tabular px-4 py-2.5 text-right">{money(s.revenue_today)}</td>
                  <td className="tabular px-4 py-2.5 text-right">{s.orders_today}</td>
                  <td className="tabular px-4 py-2.5 text-right">{s.accounts_today}</td>
                  <td className="tabular px-4 py-2.5 text-right">
                    {s.orders_need_you ? (
                      <button type="button" className="font-semibold text-warning hover:underline" onClick={() => open(s.store_id, "/orders")}>
                        {s.orders_need_you}
                      </button>
                    ) : (
                      0
                    )}
                  </td>
                  <td className="tabular px-4 py-2.5 text-right">
                    {s.buyers_waiting ? (
                      <button type="button" className="font-semibold text-warning hover:underline" onClick={() => open(s.store_id, "/messages")}>
                        {s.buyers_waiting}
                        {s.urgent_messages ? ` (${s.urgent_messages} urgent)` : ""}
                      </button>
                    ) : (
                      0
                    )}
                  </td>
                  <td className="tabular px-4 py-2.5 text-right">
                    {s.low_pools ? (
                      <button type="button" className="font-semibold text-warning hover:underline" onClick={() => open(s.store_id, "/stock")}>
                        {s.low_pools}
                      </button>
                    ) : (
                      0
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <Button size="sm" onClick={() => open(s.store_id, "/")}>
                      Open
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="Recent activity, all stores">
        {d && d.activity.length === 0 ? (
          <EmptyState title="Nothing recorded yet" />
        ) : (
          <ul className="divide-y divide-line">
            {d?.activity.map((a) => (
              <li key={a.id} className="flex flex-wrap items-center gap-x-3 gap-y-0.5 px-4 py-2.5 text-[13px]">
                <span className="font-medium">{actionLabel(a.action)}</span>
                {a.store && <span className="rounded-sm bg-hover px-1.5 text-[12px]">{a.store}</span>}
                <span className="text-muted">by {a.actor}</span>
                <span className="ml-auto text-muted">{timeOnly.format(new Date(a.at))}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

export function OverviewPage() {
  const { store, stores, loading } = useStores();
  const [scope, setScope] = useScope("overview");
  const storeId = store?.id;
  const connections = useQuery({
    queryKey: ["stores", storeId, "connections"],
    queryFn: () => api<Connection[]>(`/stores/${storeId}/connections`),
    enabled: storeId !== undefined,
  });
  const modules = useQuery({
    queryKey: ["stores", storeId, "modules"],
    queryFn: () => api<StoreModule[]>(`/stores/${storeId}/modules`),
    enabled: storeId !== undefined,
  });
  const pools = useQuery({
    queryKey: ["stock", storeId, "pools"],
    queryFn: () => api<StockPool[]>(`/stores/${storeId}/stock/pools`),
    enabled: storeId !== undefined,
  });
  const today = useQuery({
    queryKey: ["reports", storeId, "today"],
    queryFn: () => api<ReportSummary>(`/stores/${storeId}/reports/summary`),
    enabled: storeId !== undefined,
    refetchInterval: 60_000,
  });
  const orders = useQuery({
    queryKey: ["orders", storeId, "summary"],
    queryFn: () => api<{ attention: number }>(`/stores/${storeId}/orders/summary`),
    enabled: storeId !== undefined,
    refetchInterval: 15_000,
  });
  const chats = useQuery({
    queryKey: ["messages", storeId, "summary"],
    queryFn: () => api<{ needs_reply: number; urgent: number }>(`/stores/${storeId}/messages/summary`),
    enabled: storeId !== undefined,
    refetchInterval: 20_000,
  });
  const activity = useQuery({
    queryKey: ["activity", storeId],
    queryFn: () => api<Activity[]>(`/stores/${storeId}/activity?limit=12`),
    enabled: storeId !== undefined,
    refetchInterval: 30_000,
  });
  const jobs = useQuery({
    queryKey: ["jobs", "summary"],
    queryFn: () => api<JobsSummary>("/jobs/summary"),
    refetchInterval: 15_000,
  });

  if (!loading && stores.length === 0) {
    return (
      <Card>
        <EmptyState title="No stores yet">
          Create a store in <Link to="/settings" className="text-accent underline">Settings</Link> to start.
        </EmptyState>
      </Card>
    );
  }

  const activePools = pools.data?.filter((p) => !p.archived) ?? [];
  const inStock = activePools.reduce((sum, p) => sum + p.counts.available, 0);
  const lowPools = activePools.filter((p) => p.low);
  const connection = connections.data?.[0];
  const counts = jobs.data?.counts;

  const header = (
    <div className="flex flex-wrap items-center gap-3">
      <h1 className="text-[22px] font-semibold">Overview</h1>
      {stores.length > 1 && store && <ScopeSwitch scope={scope} onChange={setScope} storeName={store.name} />}
    </div>
  );
  if (scope === "all" && stores.length > 1) {
    return (
      <>
        {header}
        <AllStoresOverview />
      </>
    );
  }

  return (
    <>
      {header}

      {store?.shadow_mode && (
        <div role="status" className="flex flex-wrap items-center gap-3 rounded-lg border border-warning/40 bg-warning/10 px-4 py-3">
          <span aria-hidden="true" className="text-warning">▲</span>
          <span>
            <strong className="font-semibold">Shadow mode is on for {store.name}.</strong> Nothing is sent to
            the marketplace; every action is only recorded.
          </span>
          <Link to="/settings" className="ml-auto text-[13px] underline">
            Store settings
          </Link>
        </div>
      )}

      <section aria-label="Today" className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Figure
          label="Revenue today"
          value={today.data ? new Intl.NumberFormat(undefined, { style: "currency", currency: today.data.currency }).format(Number(today.data.revenue)) : "—"}
        />
        <Figure label="Orders today" value={today.data?.orders ?? "—"} />
        <Figure label="Accounts delivered today" value={today.data?.accounts ?? "—"} />
        <Figure label="Orders that need you" value={orders.data?.attention ?? "—"} tone={orders.data?.attention ? "warning" : undefined} />
      </section>

      {(Boolean(orders.data?.attention) || Boolean(chats.data?.needs_reply) || lowPools.length > 0 || Boolean(counts?.dead)) && (
        <Card title="To do">
          <ul className="divide-y divide-line">
            {Boolean(orders.data?.attention) && (
              <li className="flex items-center gap-3 px-4 py-2.5">
                <span>
                  {orders.data?.attention === 1 ? "1 order needs" : `${orders.data?.attention} orders need`} you (an approval, a problem, or a top-up to do)
                </span>
                <Link to="/orders" className="ml-auto text-[13px] text-accent hover:underline">
                  Open orders
                </Link>
              </li>
            )}
            {Boolean(chats.data?.needs_reply) && (
              <li className="flex items-center gap-3 px-4 py-2.5">
                <span>
                  {chats.data?.needs_reply} buyer{chats.data?.needs_reply === 1 ? " is" : "s are"} waiting for a reply
                  {chats.data?.urgent ? `, ${chats.data.urgent} urgent` : ""}
                </span>
                <Link to="/messages" className="ml-auto text-[13px] text-accent hover:underline">
                  Open messages
                </Link>
              </li>
            )}
            {lowPools.length > 0 && (
              <li className="flex items-center gap-3 px-4 py-2.5">
                <span>
                  {lowPools.length} pool{lowPools.length === 1 ? " is" : "s are"} low on stock
                </span>
                <Link to="/stock" className="ml-auto text-[13px] text-accent hover:underline">
                  Open stock
                </Link>
              </li>
            )}
            {Boolean(counts?.dead) && (
              <li className="flex items-center gap-3 px-4 py-2.5">
                <span>{counts?.dead} background job(s) failed: see below</span>
              </li>
            )}
          </ul>
        </Card>
      )}

      <section aria-label="Key figures" className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Figure label="Accounts in stock" value={pools.data ? inStock.toLocaleString() : "—"} />
        <Figure label="Pools low on stock" value={pools.data ? lowPools.length : "—"} tone={lowPools.length ? "warning" : undefined} />
        <Figure label="Jobs queued" value={counts?.queued ?? "—"} />
        <Figure label="Failed jobs" value={counts?.dead ?? "—"} tone={counts?.dead ? "warning" : undefined} />
      </section>

      <div className="grid gap-4 xl:grid-cols-[2fr_1fr]">
        <Card title="Marketplace connection" actions={<Link to="/settings" className="text-[13px] hover:underline">Manage</Link>}>
          <div className="flex flex-col gap-2 p-4">
            {connection ? (
              <>
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-medium">{connection.connector_name}</span>
                  <StatusDot tone={connection.status === "connected" ? "success" : "danger"}>
                    {connection.status === "connected" ? "Connected" : `Not connected (${connection.status})`}
                  </StatusDot>
                </div>
                {connection.status_detail && <p className="text-[13px] text-muted">{connection.status_detail}</p>}
              </>
            ) : (
              <p className="text-muted">No marketplace connected to this store yet.</p>
            )}
          </div>
        </Card>
        <Card title="Modules" actions={<Link to="/automation" className="text-[13px] hover:underline">Configure</Link>}>
          <ul className="flex flex-col gap-2 p-4">
            {modules.data?.map((m) => (
              <li key={m.key} className="flex items-center justify-between gap-3">
                <span>{m.name}</span>
                {m.enabled ? <Badge tone="success">On</Badge> : <Badge tone="neutral">Off</Badge>}
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <Card title="Recent activity">
        {activity.data && activity.data.length === 0 ? (
          <EmptyState title="Nothing recorded yet" />
        ) : (
          <ul className="divide-y divide-line">
            {activity.data?.map((a) => (
              <li key={a.id} className="flex flex-wrap items-center gap-x-3 gap-y-0.5 px-4 py-2.5 text-[13px]">
                <span className="font-medium">{actionLabel(a.action)}</span>
                <span className="text-muted">by {a.actor}</span>
                <span className="ml-auto text-muted">{timeOnly.format(new Date(a.at))}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {lowPools.length > 0 && (
        <Card title="Low on stock" actions={<Link to="/stock" className="text-[13px] hover:underline">Open stock</Link>}>
          <ul className="divide-y divide-line">
            {lowPools.map((p) => (
              <li key={p.id} className="flex items-center gap-3 px-4 py-2.5 text-[13px]">
                <Link to="/stock/$poolId" params={{ poolId: String(p.id) }} className="font-medium hover:underline">
                  {p.name}
                </Link>
                <span className="tabular ml-auto text-warning">{p.counts.available} left</span>
                <span className="text-muted">alert at {p.low_stock_threshold}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {jobs.data && jobs.data.dead.length > 0 && (
        <Card title="Failed jobs">
          <ul className="divide-y divide-line">
            {jobs.data.dead.map((j) => (
              <li key={j.id} className="flex flex-col gap-1 px-4 py-3 text-[13px]">
                <span className="font-medium">
                  #{j.id} · {j.type}
                </span>
                <span className="text-muted">{j.error}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </>
  );
}
