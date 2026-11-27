import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { ScopeSwitch, useScope } from "@/components/Scope";
import { useState } from "react";
import { icons } from "@/components/icons";
import { Button, Card, EmptyState, StatusDot } from "@/components/ui";
import {
  api,
  type Connection,
  type JobsSummary,
  type ReportSummary,
  type StockPool,
  type StoreModule,
} from "@/lib/api";

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
  if (action.startsWith("shadow."))
    return `Shadow mode: would ${action.slice(7).replaceAll("_", " ")}`;
  const text = action.replaceAll(".", " ").replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const timeOnly = new Intl.DateTimeFormat(undefined, { hourCycle: "h23",
  dateStyle: "short",
  timeStyle: "short",
});
import { useStores } from "@/lib/session";

interface Need {
  key: string;
  text: string;
  tone: "warning" | "danger";
  action: string;
  onOpen: () => void;
}

/** The first thing to read: what is waiting for the owner. Calm when nothing is. */
function NeedsYou({ needs, loading }: { needs: Need[]; loading: boolean }) {
  return (
    <section
      aria-label="Needs you"
      className="flex flex-col rounded-lg border border-line bg-surface"
    >
      <header className="flex items-baseline gap-3 px-5 pt-4 pb-3">
        <h2 className="text-[15px] font-medium">Needs you</h2>
        <span
          className={`tabular ml-auto text-[32px] leading-none font-medium ${needs.length ? "text-warning" : "text-muted"}`}
        >
          {loading ? "—" : needs.length}
        </span>
      </header>
      {needs.length ? (
        <ul className="divide-y divide-line border-t border-line">
          {needs.map((n) => (
            <li key={n.key} className="flex items-center gap-3 px-5 py-3">
              <span
                aria-hidden="true"
                className={`size-2 shrink-0 rounded-full ${n.tone === "danger" ? "bg-danger" : "bg-warning"}`}
              />
              <span className="min-w-0 flex-1">{n.text}</span>
              <button
                type="button"
                onClick={n.onOpen}
                className="inline-flex shrink-0 items-center gap-1 text-[13px] text-accent transition-colors hover:text-fg"
              >
                {n.action}
                {icons.arrow}
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="flex items-center gap-2 border-t border-line px-5 py-4 text-muted">
          <span aria-hidden="true" className="size-2 rounded-full bg-success" />
          {loading ? "Checking…" : "Nothing needs you right now"}
        </p>
      )}
    </section>
  );
}

function TodayCard({
  revenue,
  orders,
  accounts,
  title = "Today",
}: {
  revenue: string;
  orders: number | string;
  accounts: number | string;
  title?: string;
}) {
  return (
    <section
      aria-label={title}
      className="rounded-lg border border-line bg-surface px-5 py-4"
    >
      <h2 className="text-[15px] font-medium">{title}</h2>
      <div className="tabular mt-3 text-[32px] leading-none font-medium">
        {revenue}
      </div>
      <div className="mt-1 text-[13px] text-muted">Revenue</div>
      <dl className="mt-4 grid grid-cols-2 gap-3 border-t border-line pt-3 text-[13px]">
        <div>
          <dt className="text-muted">Orders</dt>
          <dd className="tabular mt-0.5 text-[18px] font-medium">{orders}</dd>
        </div>
        <div>
          <dt className="text-muted">Accounts delivered</dt>
          <dd className="tabular mt-0.5 text-[18px] font-medium">{accounts}</dd>
        </div>
      </dl>
    </section>
  );
}

interface ActivityRow {
  id: number;
  at: string;
  actor: string;
  action: string;
  store?: string | null;
}

/** Collapses runs of the same event ("Notification rule created ×4"). */
function groupActivity(
  rows: ActivityRow[],
): (ActivityRow & { times: number })[] {
  const out: (ActivityRow & { times: number })[] = [];
  for (const r of rows) {
    const last = out[out.length - 1];
    if (
      last &&
      last.action === r.action &&
      last.actor === r.actor &&
      last.store === r.store
    )
      last.times += 1;
    else out.push({ ...r, times: 1 });
  }
  return out;
}

function ActivityCard({
  title,
  rows,
}: {
  title: string;
  rows: ActivityRow[] | undefined;
}) {
  const [all, setAll] = useState(false);
  const grouped = groupActivity(rows ?? []);
  const shown = all ? grouped : grouped.slice(0, 8);
  return (
    <Card title={title}>
      {rows && rows.length === 0 ? (
        <EmptyState title="Nothing recorded yet" />
      ) : (
        <>
          <ul className="divide-y divide-line">
            {shown.map((a) => (
              <li
                key={a.id}
                className="flex flex-wrap items-center gap-x-3 gap-y-0.5 px-4 py-2.5 text-[13px]"
              >
                <span>{actionLabel(a.action)}</span>
                {a.times > 1 && (
                  <span className="tabular font-mono text-[12px] text-muted">
                    ×{a.times}
                  </span>
                )}
                {a.store && (
                  <span className="text-[12px] text-muted">{a.store}</span>
                )}
                <span className="text-muted">by {a.actor}</span>
                <span className="tabular ml-auto text-muted">
                  {timeOnly.format(new Date(a.at))}
                </span>
              </li>
            ))}
          </ul>
          {grouped.length > 8 && (
            <div className="border-t border-line px-4 py-2">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setAll((v) => !v)}
              >
                {all ? "Show less" : `Show ${grouped.length - 8} more`}
              </Button>
            </div>
          )}
        </>
      )}
    </Card>
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
  activity: {
    id: number;
    at: string;
    store: string | null;
    actor: string;
    action: string;
  }[];
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
  const money = (v: number | string) =>
    new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: d?.currency ?? "USD",
    }).format(Number(v));
  const sum = (pick: (s: CombinedOverview["stores"][number]) => number) =>
    d?.stores.reduce((t, s) => t + pick(s), 0) ?? 0;
  const open = (
    storeId: number,
    to: "/" | "/orders" | "/messages" | "/stock",
  ) => {
    select(storeId);
    void navigate({ to });
  };
  return (
    <>
      <div className="grid items-stretch gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <NeedsYou
          loading={!d}
          needs={(d?.stores ?? []).flatMap((st) => [
            ...(st.orders_need_you
              ? [
                  {
                    key: `o${st.store_id}`,
                    text: `${st.store}: ${st.orders_need_you === 1 ? "1 order needs" : `${st.orders_need_you} orders need`} you`,
                    tone: "warning" as const,
                    action: "Open orders",
                    onOpen: () => open(st.store_id, "/orders"),
                  },
                ]
              : []),
            ...(st.buyers_waiting
              ? [
                  {
                    key: `m${st.store_id}`,
                    text: `${st.store}: ${st.buyers_waiting === 1 ? "1 buyer is" : `${st.buyers_waiting} buyers are`} waiting for a reply${st.urgent_messages ? `, ${st.urgent_messages} urgent` : ""}`,
                    tone: st.urgent_messages
                      ? ("danger" as const)
                      : ("warning" as const),
                    action: "Open messages",
                    onOpen: () => open(st.store_id, "/messages"),
                  },
                ]
              : []),
            ...(st.low_pools
              ? [
                  {
                    key: `p${st.store_id}`,
                    text: `${st.store}: ${st.low_pools === 1 ? "1 pool is" : `${st.low_pools} pools are`} low on stock`,
                    tone: "warning" as const,
                    action: "Open stock",
                    onOpen: () => open(st.store_id, "/stock"),
                  },
                ]
              : []),
          ])}
        />
        <TodayCard
          title="Today, all stores"
          revenue={d ? money(sum((st) => Number(st.revenue_today))) : "—"}
          orders={d ? sum((st) => st.orders_today) : "—"}
          accounts={d ? sum((st) => st.accounts_today) : "—"}
        />
      </div>
      <Card title="Stores">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] border-collapse text-[13px]">
            <thead>
              <tr className="text-left text-muted">
                <th className="px-4 py-2.5 font-medium">Store</th>
                <th className="px-4 py-2.5 text-right font-medium">
                  Revenue today
                </th>
                <th className="px-4 py-2.5 text-right font-medium">Orders</th>
                <th className="px-4 py-2.5 text-right font-medium">Accounts</th>
                <th className="px-4 py-2.5 text-right font-medium">Need you</th>
                <th className="px-4 py-2.5 text-right font-medium">
                  Buyers waiting
                </th>
                <th className="px-4 py-2.5 text-right font-medium">
                  Low pools
                </th>
                <th className="px-4 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {d?.stores.map((s) => (
                <tr key={s.store_id} className="border-t border-line">
                  <td className="px-4 py-2.5">
                    <span className="font-medium">{s.store}</span>
                    {s.shadow_mode && (
                      <span className="ml-2 text-[12px] text-warning">
                        shadow mode
                      </span>
                    )}
                  </td>
                  <td className="tabular px-4 py-2.5 text-right">
                    {money(s.revenue_today)}
                  </td>
                  <td className="tabular px-4 py-2.5 text-right">
                    {s.orders_today}
                  </td>
                  <td className="tabular px-4 py-2.5 text-right">
                    {s.accounts_today}
                  </td>
                  <td className="tabular px-4 py-2.5 text-right">
                    {s.orders_need_you ? (
                      <button
                        type="button"
                        className="font-medium text-warning hover:underline"
                        onClick={() => open(s.store_id, "/orders")}
                      >
                        {s.orders_need_you}
                      </button>
                    ) : (
                      0
                    )}
                  </td>
                  <td className="tabular px-4 py-2.5 text-right">
                    {s.buyers_waiting ? (
                      <button
                        type="button"
                        className="font-medium text-warning hover:underline"
                        onClick={() => open(s.store_id, "/messages")}
                      >
                        {s.buyers_waiting}
                        {s.urgent_messages
                          ? ` (${s.urgent_messages} urgent)`
                          : ""}
                      </button>
                    ) : (
                      0
                    )}
                  </td>
                  <td className="tabular px-4 py-2.5 text-right">
                    {s.low_pools ? (
                      <button
                        type="button"
                        className="font-medium text-warning hover:underline"
                        onClick={() => open(s.store_id, "/stock")}
                      >
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
      <ActivityCard title="Recent activity, all stores" rows={d?.activity} />
    </>
  );
}

export function OverviewPage() {
  const { store, stores, loading } = useStores();
  const navigate = useNavigate();
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
    queryFn: () =>
      api<{ attention: number }>(`/stores/${storeId}/orders/summary`),
    enabled: storeId !== undefined,
    refetchInterval: 15_000,
  });
  const chats = useQuery({
    queryKey: ["messages", storeId, "summary"],
    queryFn: () =>
      api<{ needs_reply: number; urgent: number }>(
        `/stores/${storeId}/messages/summary`,
      ),
    enabled: storeId !== undefined,
    refetchInterval: 20_000,
  });
  const activity = useQuery({
    queryKey: ["activity", storeId],
    queryFn: () => api<Activity[]>(`/stores/${storeId}/activity?limit=40`),
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
          Create a store in{" "}
          <Link to="/settings" className="text-accent underline">
            Settings
          </Link>{" "}
          to start.
        </EmptyState>
      </Card>
    );
  }

  const activePools = pools.data?.filter((p) => !p.archived) ?? [];
  const inStock = activePools.reduce((sum, p) => sum + p.counts.available, 0);
  const lowPools = activePools.filter((p) => p.low);
  const connection = connections.data?.[0];
  const counts = jobs.data?.counts;
  const modulesOff = modules.data?.filter((m) => !m.enabled) ?? [];
  const attention = orders.data?.attention ?? 0;
  const waiting = chats.data?.needs_reply ?? 0;
  const urgent = chats.data?.urgent ?? 0;
  const needs: Need[] = [
    ...(attention
      ? [
          {
            key: "orders",
            text: `${attention === 1 ? "1 order needs" : `${attention} orders need`} you: an approval, a problem or a top-up to do`,
            tone: "warning" as const,
            action: "Open orders",
            onOpen: () => void navigate({ to: "/orders" }),
          },
        ]
      : []),
    ...(waiting
      ? [
          {
            key: "chats",
            text: `${waiting === 1 ? "1 buyer is" : `${waiting} buyers are`} waiting for a reply${urgent ? `, ${urgent} urgent` : ""}`,
            tone: urgent ? ("danger" as const) : ("warning" as const),
            action: "Open messages",
            onOpen: () => void navigate({ to: "/messages" }),
          },
        ]
      : []),
    ...(lowPools.length
      ? [
          {
            key: "stock",
            text: `${lowPools.length === 1 ? "1 pool is" : `${lowPools.length} pools are`} low on stock`,
            tone: "warning" as const,
            action: "Open stock",
            onOpen: () => void navigate({ to: "/stock" }),
          },
        ]
      : []),
    ...(counts?.dead
      ? [
          {
            key: "jobs",
            text: `${counts.dead} background job${counts.dead === 1 ? "" : "s"} failed`,
            tone: "danger" as const,
            action: "See below",
            onOpen: () => document.getElementById("system")?.scrollIntoView(),
          },
        ]
      : []),
  ];

  const header = (
    <div className="flex flex-wrap items-center gap-3">
      <h1 className="text-[22px] font-medium">Overview</h1>
      {stores.length > 1 && store && (
        <ScopeSwitch scope={scope} onChange={setScope} storeName={store.name} />
      )}
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
        <div
          role="status"
          className="flex flex-wrap items-center gap-3 rounded-lg border border-warning/40 bg-warning/10 px-4 py-3"
        >
          <span aria-hidden="true" className="text-warning">
            {icons.alert}
          </span>
          <span>
            <strong className="font-medium">
              Shadow mode is on for {store.name}.
            </strong>{" "}
            Nothing is sent to the marketplace; every action is only recorded.
          </span>
          <Link to="/settings" className="ml-auto text-[13px] underline">
            Store settings
          </Link>
        </div>
      )}

      <div className="grid items-stretch gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <NeedsYou
          loading={!orders.data || !chats.data || !pools.data}
          needs={needs}
        />
        <TodayCard
          revenue={
            today.data
              ? new Intl.NumberFormat(undefined, {
                  style: "currency",
                  currency: today.data.currency,
                }).format(Number(today.data.revenue))
              : "—"
          }
          orders={today.data?.orders ?? "—"}
          accounts={today.data?.accounts ?? "—"}
        />
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <Card
          title="Stock"
          actions={
            <Link
              to="/stock"
              className="text-[13px] text-muted transition-colors hover:text-fg"
            >
              Open stock
            </Link>
          }
        >
          <div className="px-4 py-4">
            <div className="tabular text-[32px] leading-none font-medium">
              {pools.data ? inStock.toLocaleString() : "—"}
            </div>
            <div className="mt-1 text-[13px] text-muted">
              accounts available in {activePools.length} pool
              {activePools.length === 1 ? "" : "s"}
            </div>
          </div>
          {lowPools.length > 0 ? (
            <ul className="divide-y divide-line border-t border-line">
              {lowPools.map((p) => (
                <li
                  key={p.id}
                  className="flex items-center gap-3 px-4 py-2.5 text-[13px]"
                >
                  <span
                    aria-hidden="true"
                    className="size-2 shrink-0 rounded-full bg-warning"
                  />
                  <Link
                    to="/stock/$poolId"
                    params={{ poolId: String(p.id) }}
                    className="min-w-0 flex-1 truncate hover:underline"
                  >
                    {p.name}
                  </Link>
                  <span className="tabular text-warning">
                    {p.counts.available} left
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            pools.data && (
              <p className="border-t border-line px-4 py-2.5 text-[13px] text-muted">
                No pool is low on stock
              </p>
            )
          )}
        </Card>

        <div id="system">
          <Card title="System">
            <ul className="divide-y divide-line text-[13px]">
              <li className="flex items-center gap-3 px-4 py-2.5">
                {connection ? (
                  <StatusDot
                    tone={
                      connection.status === "connected" ? "success" : "danger"
                    }
                  >
                    <span className="text-fg">{connection.connector_name}</span>
                    {connection.status === "connected"
                      ? "connected"
                      : `not connected (${connection.status})`}
                  </StatusDot>
                ) : (
                  <StatusDot tone="warning">No marketplace connected</StatusDot>
                )}
                <Link
                  to="/settings"
                  className="ml-auto text-muted transition-colors hover:text-fg"
                >
                  Manage
                </Link>
              </li>
              <li className="flex items-center gap-3 px-4 py-2.5">
                <StatusDot tone={modulesOff.length ? "warning" : "success"}>
                  <span className="text-fg">
                    {modules.data
                      ? `${modules.data.length - modulesOff.length} of ${modules.data.length} modules on`
                      : "Modules"}
                  </span>
                  {modulesOff.length > 0 &&
                    `off: ${modulesOff.map((m) => m.name).join(", ")}`}
                </StatusDot>
                <Link
                  to="/automation"
                  className="ml-auto text-muted transition-colors hover:text-fg"
                >
                  Configure
                </Link>
              </li>
              <li className="flex items-center gap-3 px-4 py-2.5">
                <StatusDot tone={counts?.dead ? "danger" : "success"}>
                  <span className="tabular text-fg">
                    {counts
                      ? `${counts.queued} job${counts.queued === 1 ? "" : "s"} queued`
                      : "Jobs"}
                  </span>
                  {counts
                    ? counts.dead
                      ? `${counts.dead} failed`
                      : "none failed"
                    : ""}
                </StatusDot>
              </li>
              {connection?.status_detail && (
                <li className="px-4 py-2.5 text-muted">
                  {connection.status_detail}
                </li>
              )}
              {jobs.data?.dead.map((j) => (
                <li key={j.id} className="flex flex-col gap-1 px-4 py-2.5">
                  <span className="font-medium">
                    #{j.id} · {j.type}
                  </span>
                  <span className="text-muted">{j.error}</span>
                </li>
              ))}
            </ul>
          </Card>
        </div>
      </div>

      <ActivityCard title="Recent activity" rows={activity.data} />
    </>
  );
}
