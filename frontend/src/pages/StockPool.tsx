import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useSearch } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { ExtractionResult } from "@/components/stock/ExtractionResult";
import { ImportPanel } from "@/components/stock/ImportPanel";
import { Badge, Button, Card, ConfirmDialog, EmptyState, Modal, TextField } from "@/components/ui";
import {
  api,
  type ExtractionDetail,
  type ItemEvent,
  type ItemsPage,
  type ItemState,
  type StockItem,
  type StockPool,
} from "@/lib/api";
import { useStores } from "@/lib/session";
import { EVENT_LABEL, plural, STATE_LABEL, STATE_TONE } from "@/lib/stock";

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const FILTERS: (ItemState | "all")[] = ["available", "quarantined", "extracted", "delivered", "void", "all"];

function ExtractControl({ storeId, pool, onResult }: { storeId: number; pool: StockPool; onResult: (d: ExtractionDetail) => void }) {
  const queryClient = useQueryClient();
  const [count, setCount] = useState("1");
  const [reason, setReason] = useState("");
  const n = Math.max(0, Math.floor(Number(count) || 0));
  const max = pool.counts.available;
  return (
    <div className="flex flex-wrap items-end gap-3">
      <TextField label="How many" type="number" min={1} max={max} value={count} onChange={(e) => setCount(e.target.value)} className="w-28" />
      <TextField label="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Manual sale, moved to…" className="min-w-56 flex-1" />
      <ConfirmDialog
        trigger={
          <Button variant="primary" disabled={n < 1 || n > max}>
            Extract
          </Button>
        }
        title={`Extract ${plural(n, "account")} from ${pool.name}?`}
        description="The oldest accounts in the pool leave the stock and are shown to you once. You can undo it afterwards if they were not used."
        confirmLabel={`Extract ${plural(n, "account")}`}
        onConfirm={async () => {
          try {
            const detail = await api<ExtractionDetail>(`/stores/${storeId}/stock/pools/${pool.id}/extract`, {
              method: "POST",
              body: { count: n, reason: reason.trim() || null },
            });
            await queryClient.invalidateQueries({ queryKey: ["stock", storeId] });
            setReason("");
            onResult(detail);
          } catch (error) {
            toast.error("Could not extract", { description: (error as Error).message });
          }
        }}
      />
      {n > max && <p className="basis-full text-[13px] text-danger">Only {plural(max, "account")} available.</p>}
    </div>
  );
}

type HistoryTarget = Pick<StockItem, "id" | "login_hint"> & { note?: string | null };

function HistoryDialog({ storeId, item, onClose }: { storeId: number; item: HistoryTarget | null; onClose: () => void }) {
  const history = useQuery({
    queryKey: ["stock", storeId, "history", item?.id],
    queryFn: () => api<ItemEvent[]>(`/stores/${storeId}/stock/items/${item?.id}/history`),
    enabled: item !== null,
  });
  if (!item) return null;
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title={`History of ${item.login_hint}`} description={item.note ?? undefined}>
      <ol className="flex flex-col gap-3">
        {history.data?.map((e, i) => (
          <li key={i} className="flex flex-col gap-0.5 border-l-2 border-line-strong pl-3 text-[13px]">
            <span className="font-medium">{EVENT_LABEL[e.event] ?? e.event}</span>
            <span className="text-muted">
              {dateTime.format(new Date(e.at))} · {e.actor}
              {typeof e.details?.reason === "string" ? ` · ${e.details.reason}` : ""}
              {typeof e.details?.old_category === "string" ? ` · ${e.details.old_category}` : ""}
            </span>
          </li>
        ))}
      </ol>
    </Modal>
  );
}

function PoolSettings({ storeId, pool }: { storeId: number; pool: StockPool }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(pool.name);
  const [threshold, setThreshold] = useState(String(pool.low_stock_threshold));
  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => api<StockPool>(`/stores/${storeId}/stock/pools/${pool.id}`, { method: "PATCH", body }),
    onSuccess: async (p) => {
      await queryClient.invalidateQueries({ queryKey: ["stock", storeId] });
      toast.success(`${p.name} saved`);
    },
    onError: (error) => toast.error("Could not save", { description: error.message }),
  });
  const dirty = name !== pool.name || Number(threshold) !== pool.low_stock_threshold;
  return (
    <Card title="Pool settings">
      <div className="flex flex-wrap items-end gap-3 p-4">
        <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} className="min-w-56 flex-1" />
        <TextField label="Low-stock alert at" type="number" min={0} value={threshold} onChange={(e) => setThreshold(e.target.value)} className="w-40" />
        <Button variant="primary" disabled={!dirty || !name.trim()} busy={save.isPending} onClick={() => save.mutate({ name, low_stock_threshold: Number(threshold) })}>
          Save changes
        </Button>
        <Button variant="ghost" onClick={() => save.mutate({ archived: !pool.archived })}>
          {pool.archived ? "Unarchive pool" : "Archive pool"}
        </Button>
      </div>
    </Card>
  );
}

export function StockPoolPage() {
  const { poolId } = useParams({ from: "/app/stock/$poolId" });
  const { store } = useStores();
  const storeId = store?.id;
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<ItemState | "all">("available");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [panel, setPanel] = useState<"none" | "import" | "extract">("none");
  const [result, setResult] = useState<ExtractionDetail | null>(null);
  const search = useSearch({ from: "/app/stock/$poolId" });
  const [historyOf, setHistoryOf] = useState<HistoryTarget | null>(
    search.item ? { id: search.item, login_hint: search.hint ?? `Account #${search.item}` } : null,
  );
  const [moveTarget, setMoveTarget] = useState("");

  const pools = useQuery({
    queryKey: ["stock", storeId, "pools"],
    queryFn: () => api<StockPool[]>(`/stores/${storeId}/stock/pools`),
    enabled: storeId !== undefined,
  });
  const pool = pools.data?.find((p) => p.id === Number(poolId));

  const items = useInfiniteQuery({
    queryKey: ["stock", storeId, "items", poolId, filter],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: "100" });
      if (filter !== "all") params.set("state", filter);
      if (pageParam !== null) params.set("after", String(pageParam));
      return api<ItemsPage>(`/stores/${storeId}/stock/pools/${poolId}/items?${params}`);
    },
    getNextPageParam: (last) => last.next_after,
    enabled: storeId !== undefined && pool !== undefined,
  });
  const rows = useMemo(() => items.data?.pages.flatMap((p) => p.items) ?? [], [items.data]);
  const total = items.data?.pages[0]?.total ?? 0;

  const changeState = async (state: ItemState, reason: string) => {
    const ids = [...selected];
    try {
      const r = await api<{ changed: number }>(`/stores/${storeId}/stock/items/state`, {
        method: "POST",
        body: { item_ids: ids, state, reason },
      });
      await queryClient.invalidateQueries({ queryKey: ["stock", storeId] });
      setSelected(new Set());
      toast.success(`${plural(r.changed, "account")} ${STATE_LABEL[state].toLowerCase()}`);
    } catch (error) {
      toast.error("Could not change the accounts", { description: (error as Error).message });
    }
  };

  const move = async () => {
    try {
      const r = await api<{ moved: number; skipped: number }>(`/stores/${storeId}/stock/items/move`, {
        method: "POST",
        body: { item_ids: [...selected], target_pool_id: Number(moveTarget) },
      });
      await queryClient.invalidateQueries({ queryKey: ["stock", storeId] });
      setSelected(new Set());
      toast.success(`${plural(r.moved, "account")} moved`, {
        description: r.skipped ? `${r.skipped} skipped (already there).` : undefined,
      });
    } catch (error) {
      toast.error("Could not move the accounts", { description: (error as Error).message });
    }
  };

  if (!store || storeId === undefined) return null;
  if (pools.data && !pool) {
    return (
      <Card>
        <EmptyState title="Pool not found in this store">
          <Link to="/stock" className="text-accent underline">
            Back to stock
          </Link>
        </EmptyState>
      </Card>
    );
  }
  if (!pool) return null;

  const allVisibleSelected = rows.length > 0 && rows.every((r) => selected.has(r.id));
  const selectedStates = new Set(rows.filter((r) => selected.has(r.id)).map((r) => r.state));
  const n = selected.size;

  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <Link to="/stock" className="text-[13px] text-muted hover:underline">
            Stock
          </Link>
          <h1 className="text-[22px] font-semibold">{pool.name}</h1>
        </div>
        <div className="ml-auto flex flex-wrap gap-2">
          <Button variant={panel === "import" ? "primary" : "secondary"} onClick={() => setPanel(panel === "import" ? "none" : "import")}>
            Import
          </Button>
          <Button variant={panel === "extract" ? "primary" : "secondary"} onClick={() => setPanel(panel === "extract" ? "none" : "extract")}>
            Extract
          </Button>
        </div>
      </div>

      <section aria-label="Counts" className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {(["available", "quarantined", "extracted", "delivered"] as const).map((s) => (
          <div key={s} className="rounded-lg border border-line bg-surface p-4">
            <div className="text-xs text-muted">{STATE_LABEL[s]}</div>
            <div className={`tabular mt-1 text-2xl font-semibold ${s === "available" && pool.low ? "text-warning" : ""}`}>
              {pool.counts[s].toLocaleString()}
            </div>
          </div>
        ))}
      </section>

      {panel === "import" && <ImportPanel storeId={storeId} pool={pool} onDone={() => setPanel("none")} />}
      {panel === "extract" && (
        <Card title="Extract accounts">
          <div className="p-4">
            <ExtractControl
              storeId={storeId}
              pool={pool}
              onResult={(d) => {
                setResult(d);
                setPanel("none");
              }}
            />
          </div>
        </Card>
      )}

      <Card
        title={
          <span className="flex flex-wrap items-center gap-2">
            Accounts
            <span className="text-[13px] font-normal text-muted">{plural(total, "result")}</span>
          </span>
        }
        actions={
          <div role="group" aria-label="Filter by state" className="flex flex-wrap gap-1">
            {FILTERS.map((f) => (
              <Button
                key={f}
                size="sm"
                variant={filter === f ? "primary" : "ghost"}
                aria-pressed={filter === f}
                onClick={() => {
                  setFilter(f);
                  setSelected(new Set());
                }}
              >
                {f === "all" ? "All" : STATE_LABEL[f]}
              </Button>
            ))}
          </div>
        }
      >
        {n > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-b border-line bg-hover/40 px-4 py-2.5 text-[13px]">
            <span className="font-medium">{plural(n, "account")} selected</span>
            {selectedStates.has("available") && (
              <ConfirmDialog
                trigger={<Button size="sm">Quarantine</Button>}
                title={`Quarantine ${plural(n, "account")}?`}
                description="They stay in this pool but are not sold until released."
                confirmLabel="Quarantine"
                onConfirm={() => changeState("quarantined", "Quarantined by hand")}
              />
            )}
            {selectedStates.has("quarantined") && (
              <Button size="sm" onClick={() => changeState("available", "Released by hand")}>
                Release
              </Button>
            )}
            {[...selectedStates].some((s) => s === "available" || s === "quarantined") && (
              <>
                <label htmlFor="move-target" className="sr-only">
                  Move to pool
                </label>
                <select id="move-target" value={moveTarget} onChange={(e) => setMoveTarget(e.target.value)} className="min-h-9 rounded-md border border-line-strong bg-bg px-2">
                  <option value="">Move to…</option>
                  {pools.data
                    ?.filter((p) => p.id !== pool.id && !p.archived)
                    .map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                </select>
                <Button size="sm" disabled={!moveTarget} onClick={move}>
                  Move
                </Button>
              </>
            )}
            <ConfirmDialog
              trigger={
                <Button size="sm" variant="ghost" className="text-danger">
                  Void
                </Button>
              }
              title={`Void ${plural(n, "account")}?`}
              description="Void accounts are removed for good (banned, invalid, sold elsewhere). This cannot be undone; they stay in the history."
              confirmLabel={`Void ${plural(n, "account")}`}
              tone="danger"
              onConfirm={() => changeState("void", "Voided by hand")}
            />
            <Button size="sm" variant="ghost" className="ml-auto" onClick={() => setSelected(new Set())}>
              Clear selection
            </Button>
          </div>
        )}
        {rows.length === 0 && !items.isLoading ? (
          <EmptyState title={filter === "available" ? "No available accounts" : "Nothing here"}>
            {filter === "available" && "Use Import to add accounts to this pool."}
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[620px] border-collapse text-[13px]">
              <thead>
                <tr className="text-left text-muted">
                  <th className="w-10 px-4 py-2.5">
                    <input
                      type="checkbox"
                      aria-label="Select all shown"
                      checked={allVisibleSelected}
                      onChange={() => setSelected(allVisibleSelected ? new Set() : new Set(rows.map((r) => r.id)))}
                      className="size-4 accent-accent"
                    />
                  </th>
                  <th className="px-4 py-2.5 font-medium">#</th>
                  <th className="px-4 py-2.5 font-medium">Login</th>
                  <th className="px-4 py-2.5 font-medium">Token</th>
                  <th className="px-4 py-2.5 font-medium">State</th>
                  <th className="px-4 py-2.5 font-medium">Since</th>
                  <th className="px-4 py-2.5" />
                </tr>
              </thead>
              <tbody>
                {rows.map((item) => (
                  <tr key={item.id} className="border-t border-line">
                    <td className="px-4 py-2">
                      <input
                        type="checkbox"
                        aria-label={`Select ${item.login_hint}`}
                        checked={selected.has(item.id)}
                        onChange={() =>
                          setSelected((s) => {
                            const next = new Set(s);
                            if (next.has(item.id)) next.delete(item.id);
                            else next.add(item.id);
                            return next;
                          })
                        }
                        className="size-4 accent-accent"
                      />
                    </td>
                    <td className="tabular px-4 py-2 text-muted">{item.position || "—"}</td>
                    <td className="px-4 py-2 font-mono text-xs">{item.login_hint}</td>
                    <td className="px-4 py-2">{item.has_token ? "Yes" : "—"}</td>
                    <td className="px-4 py-2">
                      <Badge tone={STATE_TONE[item.state]}>{STATE_LABEL[item.state]}</Badge>
                    </td>
                    <td className="tabular px-4 py-2 text-muted">{dateTime.format(new Date(item.state_changed_at))}</td>
                    <td className="px-4 py-2 text-right">
                      <Button size="sm" variant="ghost" onClick={() => setHistoryOf(item)}>
                        History
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {items.hasNextPage && (
          <div className="border-t border-line p-3 text-center">
            <Button size="sm" busy={items.isFetchingNextPage} onClick={() => items.fetchNextPage()}>
              Load more
            </Button>
          </div>
        )}
      </Card>

      <PoolSettings key={`${pool.id}-${pool.name}-${pool.low_stock_threshold}`} storeId={storeId} pool={pool} />
      <ExtractionResult storeId={storeId} detail={result} onClose={() => setResult(null)} />
      <HistoryDialog storeId={storeId} item={historyOf} onClose={() => setHistoryOf(null)} />
    </>
  );
}
