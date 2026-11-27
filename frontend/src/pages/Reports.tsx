import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ColumnChart } from "@/components/charts/ColumnChart";
import { Button, Card, EmptyState } from "@/components/ui";
import { api, type DailyReportRow, type ReportSummary } from "@/lib/api";
import { useStores } from "@/lib/session";
import { ScopeSwitch, useScope } from "@/components/Scope";

type Summary = Omit<
  ReportSummary,
  "timezone" | "today" | "by_listing" | "stock"
> & {
  timezone?: string;
  by_listing: (ReportSummary["by_listing"][number] & { store?: string })[];
  stock: (ReportSummary["stock"][number] & { store?: string })[];
  by_store?: {
    store_id: number;
    store: string;
    revenue: string;
    orders: number;
    accounts: number;
  }[];
};

type Preset = "today" | "yesterday" | "7d" | "30d" | "month" | "custom";

const PRESETS: { key: Preset; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "yesterday", label: "Yesterday" },
  { key: "7d", label: "Last 7 days" },
  { key: "30d", label: "Last 30 days" },
  { key: "month", label: "This month" },
  { key: "custom", label: "Custom" },
];

function shift(day: string, days: number): string {
  const d = new Date(`${day}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function range(
  preset: Preset,
  today: string,
  custom: { start: string; end: string },
): { start: string; end: string } {
  switch (preset) {
    case "today":
      return { start: today, end: today };
    case "yesterday":
      return { start: shift(today, -1), end: shift(today, -1) };
    case "7d":
      return { start: shift(today, -6), end: today };
    case "30d":
      return { start: shift(today, -29), end: today };
    case "month":
      return { start: `${today.slice(0, 8)}01`, end: today };
    default:
      return custom;
  }
}

const dayLabel = (day: string, opts: Intl.DateTimeFormatOptions) =>
  new Intl.DateTimeFormat(undefined, { hourCycle: "h23", ...opts, timeZone: "UTC" }).format(
    new Date(`${day}T12:00:00Z`),
  );

function Stat({
  label,
  value,
  note,
}: {
  label: string;
  value: string;
  note?: string;
}) {
  return (
    <div className="rounded-lg border border-line bg-surface px-4 py-3">
      <div className="text-[13px] text-muted">{label}</div>
      <div className="mt-1 text-[28px] font-medium">{value}</div>
      {note && <div className="text-[13px] text-muted">{note}</div>}
    </div>
  );
}

function ShareBar({ share }: { share: number }) {
  return (
    <div
      className="h-2 w-full min-w-24 rounded-full bg-hover"
      aria-hidden="true"
    >
      <div
        className="h-2 rounded-full bg-muted/60"
        style={{ width: `${Math.max(share > 0 ? 2 : 0, share * 100)}%` }}
      />
    </div>
  );
}

export function ReportsPage() {
  const { store, stores } = useStores();
  const storeId = store?.id;
  const [scope, setScope] = useScope("reports");
  const all = scope === "all" && stores.length > 1;
  const [preset, setPreset] = useState<Preset>("7d");
  const [custom, setCustom] = useState({ start: "", end: "" });
  const [asTable, setAsTable] = useState(false);

  const today = useQuery({
    queryKey: ["reports", storeId, "today"],
    queryFn: () => api<ReportSummary>(`/stores/${storeId}/reports/summary`),
    enabled: storeId !== undefined,
  });
  const todayDay = today.data?.today;
  const r = todayDay
    ? range(
        preset,
        todayDay,
        custom.start && custom.end
          ? custom
          : { start: todayDay, end: todayDay },
      )
    : null;
  const summary = useQuery({
    queryKey: ["reports", all ? "all" : storeId, r?.start, r?.end],
    queryFn: () =>
      api<Summary>(
        all
          ? `/all/summary?start=${r?.start}&end=${r?.end}`
          : `/stores/${storeId}/reports/summary?start=${r?.start}&end=${r?.end}`,
      ),
    enabled: storeId !== undefined && r !== null,
    placeholderData: (previous) => previous,
  });
  const daily = useQuery({
    queryKey: ["reports", storeId, "daily"],
    queryFn: () =>
      api<DailyReportRow[]>(`/stores/${storeId}/reports/daily?limit=14`),
    enabled: storeId !== undefined,
  });

  if (!store || storeId === undefined) return null;
  const s = summary.data;
  const fmt = (v: string | number) =>
    new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: s?.currency ?? store.currency,
    }).format(Number(v));
  const compact = (v: number) =>
    new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: s?.currency ?? store.currency,
      notation: v >= 10000 ? "compact" : "standard",
      maximumFractionDigits: 0,
    }).format(v);
  const revenue = Number(s?.revenue ?? 0);
  const multiDay = s ? s.series.length > 1 : false;
  const csvHref =
    r && !all
      ? `/api/v1/stores/${storeId}/reports/export.csv?start=${r.start}&end=${r.end}`
      : undefined;

  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-[22px] font-medium">Reports</h1>
          <p className="mt-1 text-muted">
            {all
              ? "All stores added up, each store's days in its own time zone."
              : `${store.name}'s sales, in the store's time zone (${s?.timezone ?? store.timezone}).`}{" "}
            Cancelled, refunded and unpaid orders are not counted as sales.
          </p>
        </div>
        {stores.length > 1 && (
          <ScopeSwitch
            scope={scope}
            onChange={setScope}
            storeName={store.name}
          />
        )}
      </div>

      <div
        className="flex flex-wrap items-end gap-2"
        role="group"
        aria-label="Date range"
      >
        {PRESETS.map((p) => (
          <Button
            key={p.key}
            size="sm"
            variant={preset === p.key ? "selected" : "ghost"}
            aria-pressed={preset === p.key}
            onClick={() => setPreset(p.key)}
          >
            {p.label}
          </Button>
        ))}
        {preset === "custom" && (
          <>
            <label className="flex flex-col gap-1 text-[13px] text-muted">
              From
              <input
                type="date"
                value={custom.start}
                max={todayDay}
                onChange={(e) =>
                  setCustom((c) => ({ ...c, start: e.target.value }))
                }
                className="min-h-9 rounded-md border border-line-strong bg-bg px-2 text-fg"
              />
            </label>
            <label className="flex flex-col gap-1 text-[13px] text-muted">
              To
              <input
                type="date"
                value={custom.end}
                max={todayDay}
                onChange={(e) =>
                  setCustom((c) => ({ ...c, end: e.target.value }))
                }
                className="min-h-9 rounded-md border border-line-strong bg-bg px-2 text-fg"
              />
            </label>
          </>
        )}
        {csvHref && (
          <a
            href={csvHref}
            download
            className="ml-auto inline-flex min-h-9 items-center rounded-md border border-line-strong bg-raised px-3 text-sm hover:bg-hover"
          >
            Export orders (CSV)
          </a>
        )}
      </div>
      {summary.error && (
        <p
          role="alert"
          className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-[13px] text-danger"
        >
          {summary.error.message}
        </p>
      )}

      <div
        className={`flex flex-col gap-6 transition-opacity ${summary.isFetching ? "opacity-60" : ""}`}
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Stat
            label="Revenue"
            value={fmt(revenue)}
            note={
              s && r
                ? r.start === r.end
                  ? dayLabel(r.start, { dateStyle: "medium" })
                  : `${dayLabel(r.start, { month: "short", day: "numeric" })} – ${dayLabel(r.end, { dateStyle: "medium" })}`
                : undefined
            }
          />
          <Stat label="Orders" value={(s?.orders ?? 0).toLocaleString()} />
          <Stat
            label="Accounts delivered"
            value={(s?.accounts ?? 0).toLocaleString()}
            note="Top-ups are not counted here"
          />
          <Stat
            label="Cancelled or refunded"
            value={(s?.cancelled_orders ?? 0).toLocaleString()}
            note={s && s.cancelled_orders ? fmt(s.cancelled_amount) : undefined}
          />
        </div>

        {s && multiDay && (
          <Card
            title="Revenue per day"
            actions={
              <Button
                size="sm"
                variant="ghost"
                aria-pressed={asTable}
                onClick={() => setAsTable((v) => !v)}
              >
                {asTable ? "Show chart" : "Show as table"}
              </Button>
            }
          >
            <div className="p-4">
              {asTable ? (
                <table className="w-full border-collapse text-[13px]">
                  <thead>
                    <tr className="text-left text-muted">
                      <th className="py-2 font-medium">Day</th>
                      <th className="py-2 text-right font-medium">Revenue</th>
                      <th className="py-2 text-right font-medium">Orders</th>
                      <th className="py-2 text-right font-medium">Accounts</th>
                    </tr>
                  </thead>
                  <tbody>
                    {s.series.map((p) => (
                      <tr key={p.day} className="border-t border-line">
                        <td className="py-2">
                          {dayLabel(p.day, {
                            weekday: "short",
                            month: "short",
                            day: "numeric",
                          })}
                        </td>
                        <td className="tabular py-2 text-right">
                          {fmt(p.revenue)}
                        </td>
                        <td className="tabular py-2 text-right">{p.orders}</td>
                        <td className="tabular py-2 text-right">
                          {p.accounts}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <ColumnChart
                  label={`Revenue per day from ${s.start} to ${s.end}`}
                  formatTick={compact}
                  columns={s.series.map((p) => ({
                    key: p.day,
                    label: dayLabel(p.day, { month: "short", day: "numeric" }),
                    value: Number(p.revenue),
                    tooltip: {
                      value: fmt(p.revenue),
                      lines: [
                        dayLabel(p.day, {
                          weekday: "long",
                          month: "short",
                          day: "numeric",
                        }),
                        `${p.orders} orders · ${p.accounts} accounts`,
                      ],
                    },
                  }))}
                />
              )}
            </div>
          </Card>
        )}

        {all && s?.by_store && (
          <Card title="By store">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="text-left text-muted">
                  <th className="px-4 py-2.5 font-medium">Store</th>
                  <th className="px-4 py-2.5 text-right font-medium">Orders</th>
                  <th className="px-4 py-2.5 text-right font-medium">
                    Accounts
                  </th>
                  <th className="px-4 py-2.5 text-right font-medium">
                    Revenue
                  </th>
                  <th className="w-40 px-4 py-2.5 font-medium">
                    <span className="sr-only">Share of revenue</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {s.by_store.map((b) => (
                  <tr key={b.store_id} className="border-t border-line">
                    <td className="px-4 py-2.5">{b.store}</td>
                    <td className="tabular px-4 py-2.5 text-right">
                      {b.orders}
                    </td>
                    <td className="tabular px-4 py-2.5 text-right">
                      {b.accounts}
                    </td>
                    <td className="tabular px-4 py-2.5 text-right">
                      {fmt(b.revenue)}
                    </td>
                    <td className="px-4 py-2.5">
                      <ShareBar
                        share={revenue ? Number(b.revenue) / revenue : 0}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}

        <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
          <Card title="By product type">
            {s && s.by_type.length === 0 ? (
              <EmptyState title="No sales in this period" />
            ) : (
              <table className="w-full border-collapse text-[13px]">
                <thead>
                  <tr className="text-left text-muted">
                    <th className="px-4 py-2.5 font-medium">Type</th>
                    <th className="px-4 py-2.5 text-right font-medium">
                      Orders
                    </th>
                    <th className="px-4 py-2.5 text-right font-medium">
                      Units
                    </th>
                    <th className="px-4 py-2.5 text-right font-medium">
                      Revenue
                    </th>
                    <th className="w-32 px-4 py-2.5 font-medium">
                      <span className="sr-only">Share of revenue</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {s?.by_type.map((t) => (
                    <tr key={t.type} className="border-t border-line">
                      <td className="px-4 py-2.5">{t.label}</td>
                      <td className="tabular px-4 py-2.5 text-right">
                        {t.orders}
                      </td>
                      <td className="tabular px-4 py-2.5 text-right">
                        {t.units}
                      </td>
                      <td className="tabular px-4 py-2.5 text-right">
                        {fmt(t.revenue)}
                      </td>
                      <td className="px-4 py-2.5">
                        <ShareBar
                          share={revenue ? Number(t.revenue) / revenue : 0}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          <Card title="Stock right now">
            {s && s.stock.length === 0 ? (
              <EmptyState title="No stock pools" />
            ) : (
              <ul className="flex flex-col">
                {s?.stock.map((p) => (
                  <li
                    key={`${p.store ?? ""}-${p.pool_id}`}
                    className="flex items-center justify-between border-t border-line px-4 py-2.5 text-[13px] first:border-t-0"
                  >
                    <span>
                      {p.store && (
                        <span className="text-muted">{p.store} · </span>
                      )}
                      {p.pool}
                    </span>
                    <span className="tabular">
                      {p.available.toLocaleString()} available
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        <Card title="By listing">
          {s && s.by_listing.length === 0 ? (
            <EmptyState title="No sales in this period" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] border-collapse text-[13px]">
                <thead>
                  <tr className="text-left text-muted">
                    <th className="px-4 py-2.5 font-medium">Listing</th>
                    <th className="px-4 py-2.5 text-right font-medium">
                      Orders
                    </th>
                    <th className="px-4 py-2.5 text-right font-medium">
                      Units
                    </th>
                    <th className="px-4 py-2.5 text-right font-medium">
                      Revenue
                    </th>
                    <th className="w-40 px-4 py-2.5 font-medium">
                      <span className="sr-only">Share of revenue</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {s?.by_listing.map((l) => (
                    <tr
                      key={`${l.store ?? ""}-${l.listing_id}-${l.title}`}
                      className="border-t border-line"
                    >
                      <td className="px-4 py-2.5">
                        {l.title}
                        {l.store && (
                          <span className="block text-[12px] text-muted">
                            {l.store}
                          </span>
                        )}
                      </td>
                      <td className="tabular px-4 py-2.5 text-right">
                        {l.orders}
                      </td>
                      <td className="tabular px-4 py-2.5 text-right">
                        {l.units}
                      </td>
                      <td className="tabular px-4 py-2.5 text-right">
                        {fmt(l.revenue)}
                      </td>
                      <td className="px-4 py-2.5">
                        <ShareBar
                          share={revenue ? Number(l.revenue) / revenue : 0}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card
          title={
            all ? `Daily reports sent (${store.name})` : "Daily reports sent"
          }
        >
          {daily.data && daily.data.length === 0 ? (
            <EmptyState title="No daily report yet">
              The report is sent once a day at the time set in Automation →
              Reports, to the Discord rules for “Daily report”.
            </EmptyState>
          ) : (
            <ul className="flex flex-col">
              {daily.data?.map((d) => (
                <li
                  key={d.id}
                  className="flex flex-wrap items-center gap-x-6 gap-y-1 border-t border-line px-4 py-2.5 text-[13px] first:border-t-0"
                >
                  <span className="w-36 font-medium">
                    {dayLabel(d.day, { dateStyle: "medium" })}
                  </span>
                  <span className="tabular">{fmt(d.revenue)}</span>
                  <span className="tabular text-muted">{d.orders} orders</span>
                  <span className="tabular text-muted">
                    {d.accounts} accounts
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
