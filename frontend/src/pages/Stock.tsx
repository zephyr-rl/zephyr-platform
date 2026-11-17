/**
 * Stock: the old Account Manager in the panel. Pools on the left (by game), the selected
 * pool's accounts on the right with their credentials, and the store-wide tools on top.
 */
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Link,
  useNavigate,
  useParams,
  useSearch,
} from "@tanstack/react-router";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { ExtractionResult } from "@/components/stock/ExtractionResult";
import { ImportPanel } from "@/components/stock/ImportPanel";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  Modal,
  TextAreaField,
  TextField,
} from "@/components/ui";
import {
  api,
  ApiError,
  type AccountsPage,
  type Extraction,
  type ExtractionDetail,
  type ItemEvent,
  type ItemState,
  type StockAccount,
  type StockItem,
  type StockPool,
} from "@/lib/api";
import { icons } from "@/components/icons";
import { useStores } from "@/lib/session";
import {
  copyText,
  downloadText,
  EVENT_LABEL,
  plural,
  STATE_LABEL,
  STATE_TONE,
} from "@/lib/stock";

const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});
const STATES: (ItemState | "all")[] = [
  "available",
  "quarantined",
  "extracted",
  "delivered",
  "void",
  "all",
];
const PAGE_SIZES = [25, 50, 100, 250];
const selectCls =
  "min-h-9 rounded-md border border-line-strong bg-bg px-2 text-[13px]";

// ---- store-wide tools --------------------------------------------------------------------

function NewPoolDialog({
  storeId,
  games,
  game,
}: {
  storeId: number;
  games: string[];
  game: string;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [gameName, setGameName] = useState(game || "Rocket League");
  const [threshold, setThreshold] = useState("5");
  const create = useMutation({
    mutationFn: () =>
      api<StockPool>(`/stores/${storeId}/stock/pools`, {
        method: "POST",
        body: {
          name: name.trim(),
          game: gameName.trim() || "Rocket League",
          low_stock_threshold: Number(threshold),
        },
      }),
    onSuccess: async (pool) => {
      await queryClient.invalidateQueries({ queryKey: ["stock", storeId] });
      toast.success(`Pool ${pool.name} created`);
      setOpen(false);
      setName("");
      void navigate({
        to: "/stock/$poolId",
        params: { poolId: String(pool.id) },
      });
    },
    onError: (error) =>
      toast.error("Could not create the pool", { description: error.message }),
  });
  return (
    <>
      <Button
        size="sm"
        onClick={() => {
          setGameName(game || "Rocket League");
          setOpen(true);
        }}
      >
        New pool
      </Button>
      <Modal
        open={open}
        onOpenChange={setOpen}
        title="New stock pool"
        description="A pool groups the accounts of one product, e.g. “RLCS x6”."
      >
        <form
          className="flex flex-col gap-4"
          onSubmit={(e: FormEvent) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <TextField
            label="Name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
          <TextField
            label="Game"
            hint="Pick one or type a new game."
            list="stock-games"
            value={gameName}
            onChange={(e) => setGameName(e.target.value)}
          />
          <datalist id="stock-games">
            {games.map((g) => (
              <option key={g} value={g} />
            ))}
          </datalist>
          <TextField
            label="Low-stock alert at"
            hint="An alert is sent when the available count drops to this number."
            type="number"
            min={0}
            value={threshold}
            onChange={(e) => setThreshold(e.target.value)}
            className="max-w-48"
          />
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              busy={create.isPending}
              disabled={!name.trim()}
            >
              Create pool
            </Button>
          </div>
        </form>
      </Modal>
    </>
  );
}

function FindLogin({
  storeId,
  pools,
}: {
  storeId: number;
  pools: StockPool[];
}) {
  const [open, setOpen] = useState(false);
  const [login, setLogin] = useState("");
  const [result, setResult] = useState<StockItem | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!login.trim()) return;
    setBusy(true);
    try {
      setResult(
        await api<StockItem | null>(
          `/stores/${storeId}/stock/search?login=${encodeURIComponent(login.trim())}`,
        ),
      );
    } catch (error) {
      toast.error("Search failed", {
        description: (error as ApiError).message,
      });
    } finally {
      setBusy(false);
    }
  }
  const pool = result ? pools.find((p) => p.id === result.pool_id) : undefined;
  return (
    <>
      <Button size="sm" onClick={() => setOpen(true)}>
        Find a login
      </Button>
      <Modal
        open={open}
        onOpenChange={setOpen}
        title="Find an account in every pool"
        description="By its exact login, history included (sold, taken out, removed)."
      >
        <form onSubmit={submit} className="flex flex-col gap-3">
          <TextField
            label="Login"
            value={login}
            onChange={(e) => {
              setLogin(e.target.value);
              setResult(undefined);
            }}
            autoComplete="off"
            autoFocus
          />
          <div className="flex justify-end">
            <Button type="submit" variant="primary" busy={busy}>
              Search
            </Button>
          </div>
          {result === null && (
            <p className="text-muted">
              Not in this store's stock, history included.
            </p>
          )}
          {result && (
            <p className="flex flex-wrap items-center gap-2">
              <Badge tone={STATE_TONE[result.state]}>
                {STATE_LABEL[result.state]}
              </Badge>
              <span>
                in{" "}
                <Link
                  to="/stock/$poolId"
                  params={{ poolId: String(result.pool_id) }}
                  className="text-accent underline"
                  onClick={() => setOpen(false)}
                >
                  {pool?.name ?? `pool ${result.pool_id}`}
                </Link>
                , position {result.position}
              </span>
              {result.note && (
                <span className="text-muted">· {result.note}</span>
              )}
            </p>
          )}
        </form>
      </Modal>
    </>
  );
}

function UndoLastExtraction({ storeId }: { storeId: number }) {
  const queryClient = useQueryClient();
  const last = useQuery({
    queryKey: ["stock", storeId, "extractions", "last"],
    queryFn: () =>
      api<Extraction[]>(`/stores/${storeId}/stock/extractions?limit=1`),
  });
  const e = last.data?.[0];
  if (!e || e.undone_at) return null;
  return (
    <ConfirmDialog
      trigger={
        <Button size="sm">
          Undo last extraction · {e.pool_name} ({e.count})
        </Button>
      }
      title={`Put back ${plural(e.count, "account")} in ${e.pool_name}?`}
      description={`Extracted ${dateTime.format(new Date(e.created_at))}${e.reason ? ` (${e.reason})` : ""}. Only undo it if those accounts were not used.`}
      confirmLabel="Undo extraction"
      onConfirm={async () => {
        try {
          await api(`/stores/${storeId}/stock/extractions/${e.id}/undo`, {
            method: "POST",
          });
          await queryClient.invalidateQueries({ queryKey: ["stock", storeId] });
          toast.success(`${plural(e.count, "account")} back in ${e.pool_name}`);
        } catch (error) {
          toast.error("Could not undo", {
            description: (error as Error).message,
          });
        }
      }}
    />
  );
}

function ExtractionHistory({
  storeId,
  pools,
}: {
  storeId: number;
  pools: StockPool[];
}) {
  const [open, setOpen] = useState(false);
  const [poolFilter, setPoolFilter] = useState("");
  const extractions = useQuery({
    queryKey: ["stock", storeId, "extractions", "all"],
    queryFn: () =>
      api<Extraction[]>(`/stores/${storeId}/stock/extractions?limit=200`),
    enabled: open,
  });
  const [detail, setDetail] = useState<ExtractionDetail | null>(null);
  const view = useMutation({
    mutationFn: (id: number) =>
      api<ExtractionDetail>(`/stores/${storeId}/stock/extractions/${id}`),
    onSuccess: setDetail,
    onError: (error) =>
      toast.error("Could not open the extraction", {
        description: error.message,
      }),
  });
  const rows = (extractions.data ?? []).filter(
    (e) => !poolFilter || e.pool_id === Number(poolFilter),
  );
  return (
    <>
      <Button size="sm" onClick={() => setOpen(true)}>
        Extraction history
      </Button>
      <Modal
        open={open}
        onOpenChange={setOpen}
        extraWide
        title="Extraction history"
        description="Every extraction of this store. Viewing the accounts is recorded."
      >
        <div className="flex items-center gap-2">
          <label htmlFor="history-pool" className="text-[13px] text-muted">
            Pool
          </label>
          <select
            id="history-pool"
            value={poolFilter}
            onChange={(e) => setPoolFilter(e.target.value)}
            className={selectCls}
          >
            <option value="">All pools</option>
            {pools.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
        {rows.length ? (
          <ul className="divide-y divide-line rounded-md border border-line">
            {rows.map((e) => (
              <li
                key={e.id}
                className="flex flex-wrap items-center gap-3 px-3 py-2.5 text-[13px]"
              >
                <span className="tabular text-muted">
                  {dateTime.format(new Date(e.created_at))}
                </span>
                <span className="font-medium">
                  {plural(e.count, "account")}
                </span>
                <span>from {e.pool_name}</span>
                <span className="text-muted">by {e.actor}</span>
                {e.reason && <span className="text-muted">· {e.reason}</span>}
                {e.undone_at && <Badge tone="neutral">Undone</Badge>}
                <Button
                  size="sm"
                  variant="ghost"
                  className="ml-auto"
                  busy={view.isPending && view.variables === e.id}
                  onClick={() => view.mutate(e.id)}
                >
                  View accounts
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState
            title={extractions.isLoading ? "Loading…" : "No extractions yet"}
          />
        )}
      </Modal>
      <ExtractionResult
        storeId={storeId}
        detail={detail}
        onClose={() => setDetail(null)}
      />
    </>
  );
}

function ConvertFormat({
  storeId,
  pool,
  onImport,
}: {
  storeId: number;
  pool?: StockPool;
  onImport: (text: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [withToken, setWithToken] = useState(true);
  const [out, setOut] = useState<{
    text: string;
    count: number;
    invalid_lines: number[];
  } | null>(null);
  const convert = useMutation({
    mutationFn: () =>
      api<{ text: string; count: number; invalid_lines: number[] }>(
        `/stores/${storeId}/stock/convert`,
        { method: "POST", body: { text: input, with_token: withToken } },
      ),
    onSuccess: setOut,
    onError: (error) =>
      toast.error("Could not convert", { description: error.message }),
  });
  return (
    <>
      <Button size="sm" onClick={() => setOpen(true)}>
        Convert format
      </Button>
      <Modal
        open={open}
        onOpenChange={setOpen}
        extraWide
        title="Convert format"
        description="Paste accounts in any format (name: / password: / token: blocks included) and get login:password lines."
      >
        <div className="grid gap-4 lg:grid-cols-2">
          <TextAreaField
            label="Paste"
            mono
            rows={12}
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
              setOut(null);
            }}
          />
          <TextAreaField
            label={out ? `Result · ${plural(out.count, "account")}` : "Result"}
            mono
            rows={12}
            readOnly
            value={out?.text ?? ""}
            hint={
              out?.invalid_lines.length
                ? `${plural(out.invalid_lines.length, "line")} not recognised and left out.`
                : undefined
            }
          />
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <div role="group" aria-label="Output format" className="flex gap-1">
            <Button
              size="sm"
              variant={!withToken ? "selected" : "ghost"}
              aria-pressed={!withToken}
              onClick={() => {
                setWithToken(false);
                setOut(null);
              }}
            >
              login:password
            </Button>
            <Button
              size="sm"
              variant={withToken ? "selected" : "ghost"}
              aria-pressed={withToken}
              onClick={() => {
                setWithToken(true);
                setOut(null);
              }}
            >
              login:password:token
            </Button>
          </div>
          <Button
            variant="primary"
            busy={convert.isPending}
            disabled={!input.trim()}
            onClick={() => convert.mutate()}
          >
            Convert
          </Button>
          {out && out.count > 0 && (
            <>
              <Button onClick={() => void copyText(out.text, "Result")}>
                Copy
              </Button>
              {pool && (
                <Button
                  onClick={() => {
                    onImport(out.text);
                    setOpen(false);
                  }}
                >
                  Import into {pool.name}
                </Button>
              )}
            </>
          )}
        </div>
      </Modal>
    </>
  );
}

// ---- pool list ---------------------------------------------------------------------------

function PoolList({
  storeId,
  pools,
  selectedId,
  game,
  setGame,
}: {
  storeId: number;
  pools: StockPool[];
  selectedId?: number;
  game: string;
  setGame: (g: string) => void;
}) {
  const [showArchived, setShowArchived] = useState(false);
  const games = useMemo(() => {
    const totals = new Map<string, number>();
    for (const p of pools)
      if (!p.archived)
        totals.set(p.game, (totals.get(p.game) ?? 0) + p.counts.available);
    return [...totals.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [pools]);
  const shown = pools.filter(
    (p) => (showArchived || !p.archived) && (!game || p.game === game),
  );
  const archived = pools.filter(
    (p) => p.archived && (!game || p.game === game),
  ).length;
  return (
    <section
      aria-label="Pools"
      className="flex flex-col gap-2 rounded-lg border border-line bg-surface p-3"
    >
      <div className="flex items-center gap-2">
        <label htmlFor="stock-game" className="sr-only">
          Game
        </label>
        <select
          id="stock-game"
          value={game}
          onChange={(e) => setGame(e.target.value)}
          className={`${selectCls} min-w-0 flex-1`}
        >
          <option value="">All games</option>
          {games.map(([g, n]) => (
            <option key={g} value={g}>
              {g} ({n.toLocaleString()})
            </option>
          ))}
        </select>
        <NewPoolDialog
          storeId={storeId}
          games={games.map(([g]) => g)}
          game={game}
        />
      </div>
      <nav aria-label="Stock pools" className="flex flex-col gap-1">
        {shown.map((p) => (
          <Link
            key={p.id}
            to="/stock/$poolId"
            params={{ poolId: String(p.id) }}
            aria-current={p.id === selectedId ? "page" : undefined}
            className={`flex items-center gap-2 rounded-md border px-2.5 py-2 text-[13px] ${
              p.id === selectedId
                ? "border-line-strong bg-hover shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]"
                : "border-transparent hover:bg-hover"
            } ${p.archived ? "opacity-60" : ""}`}
          >
            <span
              className="min-w-0 flex-1 truncate font-medium"
              title={p.name}
            >
              {p.name}
            </span>
            <span
              className={`tabular rounded-sm px-1.5 text-[12px] ${
                p.counts.available === 0
                  ? "bg-danger/14 text-danger"
                  : p.low
                    ? "bg-warning/14 text-warning"
                    : "text-muted"
              }`}
            >
              {p.counts.available.toLocaleString()}
            </span>
            {p.tokens > 0 && (
              <span
                className="tabular inline-flex items-center gap-1 text-[12px] text-muted"
                title="Available accounts with a token"
              >
                {icons.key}
                {p.tokens.toLocaleString()}
              </span>
            )}
          </Link>
        ))}
        {shown.length === 0 && (
          <p className="px-2 py-3 text-[13px] text-muted">
            No pools yet. Create one.
          </p>
        )}
      </nav>
      {archived > 0 && (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setShowArchived((v) => !v)}
        >
          {showArchived ? "Hide archived" : `Show archived (${archived})`}
        </Button>
      )}
    </section>
  );
}

// ---- pool panel --------------------------------------------------------------------------

function ExtractControl({
  storeId,
  pool,
  onResult,
}: {
  storeId: number;
  pool: StockPool;
  onResult: (d: ExtractionDetail) => void;
}) {
  const queryClient = useQueryClient();
  const [count, setCount] = useState("1");
  const [reason, setReason] = useState("");
  const n = Math.max(0, Math.floor(Number(count) || 0));
  const max = pool.counts.available;
  return (
    <div className="flex flex-wrap items-end gap-3">
      <TextField
        label="How many"
        type="number"
        min={1}
        max={max}
        value={count}
        onChange={(e) => setCount(e.target.value)}
        className="w-28"
      />
      <TextField
        label="Reason (optional)"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="Manual sale, moved to…"
        className="min-w-56 flex-1"
      />
      <ConfirmDialog
        trigger={
          <Button variant="primary" disabled={n < 1 || n > max}>
            Extract
          </Button>
        }
        title={`Extract ${plural(n, "account")} from ${pool.name}?`}
        description="The first accounts of the pool leave the stock and are shown to you. You can undo it afterwards if they were not used."
        confirmLabel={`Extract ${plural(n, "account")}`}
        onConfirm={async () => {
          try {
            const detail = await api<ExtractionDetail>(
              `/stores/${storeId}/stock/pools/${pool.id}/extract`,
              {
                method: "POST",
                body: { count: n, reason: reason.trim() || null },
              },
            );
            await queryClient.invalidateQueries({
              queryKey: ["stock", storeId],
            });
            setReason("");
            onResult(detail);
          } catch (error) {
            toast.error("Could not extract", {
              description: (error as Error).message,
            });
          }
        }}
      />
      {n > max && (
        <p className="basis-full text-[13px] text-danger">
          Only {plural(max, "account")} available.
        </p>
      )}
    </div>
  );
}

function HistoryDialog({
  storeId,
  item,
  onClose,
}: {
  storeId: number;
  item: { id: number; login_hint: string; note?: string | null } | null;
  onClose: () => void;
}) {
  const history = useQuery({
    queryKey: ["stock", storeId, "history", item?.id],
    queryFn: () =>
      api<ItemEvent[]>(`/stores/${storeId}/stock/items/${item?.id}/history`),
    enabled: item !== null,
  });
  if (!item) return null;
  return (
    <Modal
      open
      onOpenChange={(o) => !o && onClose()}
      title={`History of ${item.login_hint}`}
      description={item.note ?? undefined}
    >
      <ol className="flex flex-col gap-3">
        {history.data?.map((e, i) => (
          <li
            key={i}
            className="flex flex-col gap-0.5 border-l-2 border-line-strong pl-3 text-[13px]"
          >
            <span className="font-medium">
              {EVENT_LABEL[e.event] ?? e.event}
            </span>
            <span className="text-muted">
              {dateTime.format(new Date(e.at))} · {e.actor}
              {typeof e.details?.reason === "string"
                ? ` · ${e.details.reason}`
                : ""}
              {typeof e.details?.old_category === "string"
                ? ` · ${e.details.old_category}`
                : ""}
            </span>
          </li>
        ))}
      </ol>
    </Modal>
  );
}

function PoolSettings({
  storeId,
  pool,
  games,
  onClose,
}: {
  storeId: number;
  pool: StockPool;
  games: string[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState(pool.name);
  const [game, setGame] = useState(pool.game);
  const [threshold, setThreshold] = useState(String(pool.low_stock_threshold));
  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api<StockPool>(`/stores/${storeId}/stock/pools/${pool.id}`, {
        method: "PATCH",
        body,
      }),
    onSuccess: async (p) => {
      await queryClient.invalidateQueries({ queryKey: ["stock", storeId] });
      toast.success(`${p.name} saved`);
      onClose();
    },
    onError: (error) =>
      toast.error("Could not save", { description: error.message }),
  });
  const used = Object.values(pool.counts).reduce((a, b) => a + b, 0) > 0;
  return (
    <Modal
      open
      onOpenChange={(o) => !o && onClose()}
      title={`Pool settings · ${pool.name}`}
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate({
            name: name.trim(),
            game: game.trim(),
            low_stock_threshold: Number(threshold),
          });
        }}
      >
        <TextField
          label="Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <TextField
          label="Game"
          list="stock-games-settings"
          value={game}
          onChange={(e) => setGame(e.target.value)}
        />
        <datalist id="stock-games-settings">
          {games.map((g) => (
            <option key={g} value={g} />
          ))}
        </datalist>
        <TextField
          label="Low-stock alert at"
          type="number"
          min={0}
          value={threshold}
          onChange={(e) => setThreshold(e.target.value)}
          className="max-w-48"
        />
        <div className="flex flex-wrap items-center gap-2">
          {used ? (
            <Button
              type="button"
              variant="ghost"
              onClick={() => save.mutate({ archived: !pool.archived })}
            >
              {pool.archived ? "Unarchive pool" : "Archive pool"}
            </Button>
          ) : (
            <ConfirmDialog
              trigger={
                <Button type="button" variant="ghost" className="text-danger">
                  Delete pool
                </Button>
              }
              title={`Delete ${pool.name}?`}
              description="It never had accounts, so nothing is lost."
              confirmLabel="Delete pool"
              tone="danger"
              onConfirm={async () => {
                try {
                  await api(`/stores/${storeId}/stock/pools/${pool.id}`, {
                    method: "DELETE",
                  });
                  await queryClient.invalidateQueries({
                    queryKey: ["stock", storeId],
                  });
                  toast.success(`${pool.name} deleted`);
                  onClose();
                  void navigate({ to: "/stock" });
                } catch (error) {
                  toast.error("Could not delete", {
                    description: (error as Error).message,
                  });
                }
              }}
            />
          )}
          <Button type="button" className="ml-auto" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            busy={save.isPending}
            disabled={!name.trim()}
          >
            Save changes
          </Button>
        </div>
        {used && (
          <p className="text-[12px] text-muted">
            A pool with accounts or history is archived instead of deleted: its
            history keeps those accounts from being imported and sold again.
          </p>
        )}
      </form>
    </Modal>
  );
}

function CopyCell({ value, what }: { value: string | null; what: string }) {
  if (!value) return <span className="text-muted">—</span>;
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <span className="truncate font-mono text-[12px]" title={value}>
        {value}
      </span>
      <button
        type="button"
        aria-label={`Copy ${what.toLowerCase()} ${value}`}
        onClick={() => void copyText(value, what)}
        className="inline-flex size-6 shrink-0 items-center justify-center rounded-sm text-muted transition-colors hover:bg-hover hover:text-fg"
      >
        {icons.copy}
      </button>
    </span>
  );
}

function MoveToStore({
  storeId,
  ids,
  onDone,
}: {
  storeId: number;
  ids: number[];
  onDone: () => void;
}) {
  const { stores } = useStores();
  const queryClient = useQueryClient();
  const others = stores.filter((s) => s.id !== storeId);
  const [target, setTarget] = useState("");
  const [targetPool, setTargetPool] = useState("");
  const pools = useQuery({
    queryKey: ["stock", Number(target), "pools"],
    queryFn: () => api<StockPool[]>(`/stores/${target}/stock/pools`),
    enabled: target !== "",
  });
  if (others.length === 0) return null;
  const storeName = others.find((s) => s.id === Number(target))?.name ?? "";
  return (
    <>
      <label htmlFor="move-store" className="sr-only">
        Move to store
      </label>
      <select
        id="move-store"
        value={target}
        onChange={(e) => {
          setTarget(e.target.value);
          setTargetPool("");
        }}
        className={selectCls}
      >
        <option value="">Move to store…</option>
        {others.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name}
          </option>
        ))}
      </select>
      {target && (
        <>
          <label htmlFor="move-store-pool" className="sr-only">
            Pool in {storeName}
          </label>
          <select
            id="move-store-pool"
            value={targetPool}
            onChange={(e) => setTargetPool(e.target.value)}
            className={selectCls}
          >
            <option value="">Pool in {storeName}…</option>
            {pools.data
              ?.filter((p) => !p.archived)
              .map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
          </select>
          <ConfirmDialog
            trigger={
              <Button size="sm" disabled={!targetPool}>
                Move
              </Button>
            }
            title={`Move ${plural(ids.length, "account")} to ${storeName}?`}
            description="They leave this store's stock and go into the chosen pool there. Accounts taken out of that store earlier come back; accounts sold there are kept here."
            confirmLabel={`Move to ${storeName}`}
            onConfirm={async () => {
              try {
                const r = await api<{
                  moved: number;
                  skipped: number;
                  sold_there: number;
                }>(`/stores/${storeId}/stock/items/move`, {
                  method: "POST",
                  body: {
                    item_ids: ids,
                    target_pool_id: Number(targetPool),
                    target_store_id: Number(target),
                  },
                });
                await queryClient.invalidateQueries({ queryKey: ["stock"] });
                toast.success(
                  `${plural(r.moved, "account")} moved to ${storeName}`,
                  {
                    description: r.skipped
                      ? `${r.skipped} kept here${r.sold_there ? ` (${r.sold_there} were sold there before)` : " (already in stock there)"}.`
                      : undefined,
                  },
                );
                onDone();
              } catch (error) {
                toast.error("Could not move", {
                  description: (error as Error).message,
                });
              }
            }}
          />
        </>
      )}
    </>
  );
}

function PoolPanel({
  storeId,
  pool,
  pools,
  games,
  importText,
  setImportText,
}: {
  storeId: number;
  pool: StockPool;
  pools: StockPool[];
  games: string[];
  importText: string | null;
  setImportText: (t: string | null) => void;
}) {
  const queryClient = useQueryClient();
  const search = useSearch({ from: "/app/stock/$poolId" });
  const [panel, setPanel] = useState<"none" | "import" | "extract">("none");
  const [result, setResult] = useState<ExtractionDetail | null>(null);
  const [settings, setSettings] = useState(false);
  const [historyOf, setHistoryOf] = useState<{
    id: number;
    login_hint: string;
    note?: string | null;
  } | null>(
    search.item
      ? {
          id: search.item,
          login_hint: search.hint ?? `Account #${search.item}`,
        }
      : null,
  );
  const [state, setState] = useState<ItemState | "all">("available");
  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  const [size, setSize] = useState(50);
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [moveTarget, setMoveTarget] = useState("");

  useEffect(() => {
    const t = window.setTimeout(() => setQ(query.trim()), 300);
    return () => window.clearTimeout(t);
  }, [query]);
  useEffect(() => {
    setPage(0);
    setSelected(new Set());
  }, [pool.id, state, q, size]);
  useEffect(() => {
    if (importText !== null) setPanel("import");
  }, [importText]);

  const accounts = useQuery({
    queryKey: ["stock", storeId, "accounts", pool.id, state, q, size, page],
    queryFn: () => {
      const params = new URLSearchParams({
        limit: String(size),
        offset: String(page * size),
      });
      params.set("state", state);
      if (q) params.set("q", q);
      return api<AccountsPage>(
        `/stores/${storeId}/stock/pools/${pool.id}/accounts?${params}`,
      );
    },
    placeholderData: keepPreviousData,
    refetchOnWindowFocus: false,
  });
  const rows = accounts.data?.items ?? [];
  const total = accounts.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / size));

  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: ["stock", storeId] });

  const exportPool = async (format: string, download: boolean) => {
    try {
      const r = await api<{ text: string; count: number }>(
        `/stores/${storeId}/stock/pools/${pool.id}/export?format=${format}`,
      );
      if (!r.count) return toast.info("Nothing to copy");
      if (download) {
        downloadText(`${pool.name.replace(/[^\w-]+/g, "_")}.txt`, r.text);
        toast.success(`${plural(r.count, "account")} downloaded`);
      } else
        await copyText(
          r.text,
          format === "tokens"
            ? `${plural(r.count, "token")}`
            : `${plural(r.count, "account")}`,
        );
    } catch (error) {
      toast.error("Could not export", {
        description: (error as Error).message,
      });
    }
  };

  const post = async (
    path: string,
    body: object,
    done: (r: Record<string, number>) => string,
  ) => {
    try {
      const r = await api<Record<string, number>>(
        `/stores/${storeId}/stock/${path}`,
        {
          method: "POST",
          body,
        },
      );
      await refresh();
      setSelected(new Set());
      toast.success(done(r));
    } catch (error) {
      toast.error("Could not change the accounts", {
        description: (error as Error).message,
      });
    }
  };

  const ids = [...selected];
  const chosen = rows.filter((r) => selected.has(r.id));
  const inStock = chosen.some(
    (r) => r.state === "available" || r.state === "quarantined",
  );
  const removedByHand = (r: StockAccount) =>
    r.state === "extracted" && r.last_event === "removed";
  const allSelected = rows.length > 0 && rows.every((r) => selected.has(r.id));
  const toggle = (id: number) =>
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const exportWarning =
    "They stay in stock and can still be delivered by the platform. To take them out, use Extract.";

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <section className="flex flex-wrap items-start gap-3 rounded-lg border border-line bg-surface p-4">
        <div className="min-w-0 flex-1">
          <h2 className="text-[18px] font-medium">{pool.name}</h2>
          <p className="text-[13px] text-muted">
            {pool.game} · {plural(pool.counts.available, "available account")} ·{" "}
            {plural(pool.tokens, "token")}
            {pool.counts.quarantined > 0 &&
              ` · ${pool.counts.quarantined} quarantined`}
            {pool.archived && " · archived"}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant={panel === "import" ? "selected" : "secondary"}
            onClick={() => {
              setImportText(null);
              setPanel(panel === "import" ? "none" : "import");
            }}
          >
            Import
          </Button>
          <Button
            size="sm"
            variant={panel === "extract" ? "selected" : "secondary"}
            onClick={() => setPanel(panel === "extract" ? "none" : "extract")}
          >
            Extract
          </Button>
          <ConfirmDialog
            trigger={<Button size="sm">Copy all</Button>}
            title={`Copy the ${plural(pool.counts.available, "available account")} of ${pool.name}?`}
            description={exportWarning}
            confirmLabel="Copy all"
            onConfirm={() =>
              exportPool(
                pool.tokens ? "login_password_token" : "login_password",
                false,
              )
            }
          />
          <ConfirmDialog
            trigger={<Button size="sm">Download .txt</Button>}
            title={`Download the ${plural(pool.counts.available, "available account")} of ${pool.name}?`}
            description={exportWarning}
            confirmLabel="Download"
            onConfirm={() =>
              exportPool(
                pool.tokens ? "login_password_token" : "login_password",
                true,
              )
            }
          />
          {pool.tokens > 0 && (
            <Button size="sm" onClick={() => void exportPool("tokens", false)}>
              Copy tokens
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => setSettings(true)}>
            Settings
          </Button>
        </div>
      </section>

      {panel === "import" && (
        <ImportPanel
          key={importText ?? "blank"}
          storeId={storeId}
          pool={pool}
          initialText={importText ?? ""}
          onDone={() => {
            setPanel("none");
            setImportText(null);
          }}
        />
      )}
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

      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-line p-3">
          <input
            type="search"
            aria-label="Search accounts"
            placeholder="Search login, e-mail or token"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="min-h-9 min-w-56 flex-1 rounded-md border border-line-strong bg-bg px-3 text-[13px]"
          />
          <label htmlFor="stock-state" className="sr-only">
            State
          </label>
          <select
            id="stock-state"
            value={state}
            onChange={(e) => setState(e.target.value as ItemState | "all")}
            className={selectCls}
          >
            {STATES.map((s) => (
              <option key={s} value={s}>
                {s === "all" ? "Every state" : STATE_LABEL[s]}
              </option>
            ))}
          </select>
          <label htmlFor="stock-size" className="sr-only">
            Per page
          </label>
          <select
            id="stock-size"
            value={size}
            onChange={(e) => setSize(Number(e.target.value))}
            className={selectCls}
          >
            {PAGE_SIZES.map((n) => (
              <option key={n} value={n}>
                {n} per page
              </option>
            ))}
          </select>
        </div>

        {selected.size > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-b border-line bg-hover/40 px-3 py-2 text-[13px]">
            <span className="font-medium">
              {plural(selected.size, "account")} selected
            </span>
            {inStock && (
              <>
                <ConfirmDialog
                  trigger={<Button size="sm">Remove</Button>}
                  title={`Remove ${plural(selected.size, "account")} from sale?`}
                  description="They leave the stock but are not deleted: they stay in the history and you can put them back."
                  confirmLabel="Remove"
                  onConfirm={() =>
                    post(
                      "items/remove",
                      { item_ids: ids },
                      (r) => `${plural(r.removed ?? 0, "account")} removed`,
                    )
                  }
                />
                {chosen.some((r) => r.state === "available") && (
                  <Button
                    size="sm"
                    onClick={() =>
                      void post(
                        "items/state",
                        {
                          item_ids: ids,
                          state: "quarantined",
                          reason: "Quarantined by hand",
                        },
                        (r) =>
                          `${plural(r.changed ?? 0, "account")} quarantined`,
                      )
                    }
                  >
                    Quarantine
                  </Button>
                )}
                {chosen.some((r) => r.state === "quarantined") && (
                  <Button
                    size="sm"
                    onClick={() =>
                      void post(
                        "items/state",
                        {
                          item_ids: ids,
                          state: "available",
                          reason: "Released by hand",
                        },
                        (r) => `${plural(r.changed ?? 0, "account")} released`,
                      )
                    }
                  >
                    Release
                  </Button>
                )}
                <label htmlFor="move-pool" className="sr-only">
                  Move to pool
                </label>
                <select
                  id="move-pool"
                  value={moveTarget}
                  onChange={(e) => setMoveTarget(e.target.value)}
                  className={selectCls}
                >
                  <option value="">Move to pool…</option>
                  {pools
                    .filter((p) => p.id !== pool.id && !p.archived)
                    .map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                </select>
                <Button
                  size="sm"
                  disabled={!moveTarget}
                  onClick={() =>
                    void post(
                      "items/move",
                      { item_ids: ids, target_pool_id: Number(moveTarget) },
                      (r) => `${plural(r.moved ?? 0, "account")} moved`,
                    )
                  }
                >
                  Move
                </Button>
                <MoveToStore
                  storeId={storeId}
                  ids={ids}
                  onDone={() => setSelected(new Set())}
                />
              </>
            )}
            {chosen.some(removedByHand) && (
              <Button
                size="sm"
                onClick={() =>
                  void post(
                    "items/put-back",
                    { item_ids: chosen.filter(removedByHand).map((r) => r.id) },
                    (r) =>
                      `${plural(r.put_back ?? 0, "account")} back in stock`,
                  )
                }
              >
                Put back
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto"
              onClick={() => setSelected(new Set())}
            >
              Clear selection
            </Button>
          </div>
        )}

        {rows.length === 0 && !accounts.isLoading ? (
          <EmptyState
            title={
              q
                ? "No account matches"
                : state === "available"
                  ? "No available accounts"
                  : "Nothing here"
            }
          >
            {!q &&
              state === "available" &&
              "Use Import to add accounts to this pool."}
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px] table-fixed border-collapse text-[13px]">
              <thead>
                <tr className="text-left text-muted">
                  <th className="w-10 px-3 py-2.5">
                    <input
                      type="checkbox"
                      aria-label="Select all shown"
                      checked={allSelected}
                      onChange={() =>
                        setSelected(
                          allSelected
                            ? new Set()
                            : new Set(rows.map((r) => r.id)),
                        )
                      }
                      className="size-4 accent-accent"
                    />
                  </th>
                  <th className="w-14 px-2 py-2.5 font-medium">#</th>
                  <th className="px-2 py-2.5 font-medium">Login</th>
                  <th className="w-48 px-2 py-2.5 font-medium">Password</th>
                  <th className="w-44 px-2 py-2.5 font-medium">Token</th>
                  {state !== "available" && (
                    <th className="w-28 px-2 py-2.5 font-medium">State</th>
                  )}
                  <th className="w-40 px-2 py-2.5" />
                </tr>
              </thead>
              <tbody>
                {rows.map((a) => (
                  <tr key={a.id} className="border-t border-line">
                    <td className="px-3 py-1.5">
                      <input
                        type="checkbox"
                        aria-label={`Select ${a.login}`}
                        checked={selected.has(a.id)}
                        onChange={() => toggle(a.id)}
                        className="size-4 accent-accent"
                      />
                    </td>
                    <td className="tabular px-2 py-1.5 text-muted">
                      {a.state === "available" ? a.position : "—"}
                    </td>
                    <td className="px-2 py-1.5">
                      <CopyCell value={a.login} what="Login" />
                      {a.extra.email && (
                        <span
                          className="block truncate text-[12px] text-muted"
                          title={a.extra.email}
                        >
                          {a.extra.email}
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-1.5">
                      <CopyCell value={a.password} what="Password" />
                    </td>
                    <td className="px-2 py-1.5">
                      <CopyCell value={a.token} what="Token" />
                    </td>
                    {state !== "available" && (
                      <td className="px-2 py-1.5">
                        <Badge tone={STATE_TONE[a.state]}>
                          {a.last_event === "removed"
                            ? "Removed"
                            : STATE_LABEL[a.state]}
                        </Badge>
                      </td>
                    )}
                    <td className="px-2 py-1.5 text-right whitespace-nowrap">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() =>
                          setHistoryOf({
                            id: a.id,
                            login_hint: a.login,
                            note: a.note,
                          })
                        }
                      >
                        History
                      </Button>
                      {(a.state === "available" ||
                        a.state === "quarantined") && (
                        <ConfirmDialog
                          trigger={
                            <Button size="sm" variant="ghost">
                              Remove
                            </Button>
                          }
                          title={`Remove ${a.login} from sale?`}
                          description="Not deleted: it stays in the history and you can put it back."
                          confirmLabel="Remove"
                          onConfirm={() =>
                            post(
                              "items/remove",
                              { item_ids: [a.id] },
                              () => `${a.login} removed`,
                            )
                          }
                        />
                      )}
                      {removedByHand(a) && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() =>
                            void post(
                              "items/put-back",
                              { item_ids: [a.id] },
                              () => `${a.login} back in stock`,
                            )
                          }
                        >
                          Put back
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-3 border-t border-line px-3 py-2 text-[13px] text-muted">
          <span>
            {total
              ? `Showing ${(page * size + 1).toLocaleString()}–${Math.min(total, (page + 1) * size).toLocaleString()} of ${total.toLocaleString()}`
              : "No accounts"}
          </span>
          <span className="ml-auto flex items-center gap-2">
            <Button
              size="sm"
              variant="ghost"
              disabled={page === 0}
              onClick={() => setPage((p) => p - 1)}
            >
              ‹ Previous
            </Button>
            <span className="tabular">
              {page + 1} / {pages}
            </span>
            <Button
              size="sm"
              variant="ghost"
              disabled={page + 1 >= pages}
              onClick={() => setPage((p) => p + 1)}
            >
              Next ›
            </Button>
          </span>
        </div>
      </Card>

      {settings && (
        <PoolSettings
          storeId={storeId}
          pool={pool}
          games={games}
          onClose={() => setSettings(false)}
        />
      )}
      <ExtractionResult
        storeId={storeId}
        detail={result}
        onClose={() => setResult(null)}
      />
      <HistoryDialog
        storeId={storeId}
        item={historyOf}
        onClose={() => setHistoryOf(null)}
      />
    </div>
  );
}

// ---- page --------------------------------------------------------------------------------

function StockManager({ poolId }: { poolId?: number }) {
  const { store } = useStores();
  const storeId = store?.id;
  const navigate = useNavigate();
  const [game, setGame] = useState("");
  const [importText, setImportText] = useState<string | null>(null);
  const pools = useQuery({
    queryKey: ["stock", storeId, "pools"],
    queryFn: () => api<StockPool[]>(`/stores/${storeId}/stock/pools`),
    enabled: storeId !== undefined,
  });
  const list = pools.data ?? [];
  const pool = list.find((p) => p.id === poolId);
  const games = useMemo(
    () => [...new Set(list.map((p) => p.game))].sort(),
    [list],
  );

  useEffect(() => {
    // /stock opens the first pool, like the old Account Manager.
    const first = list.find((p) => !p.archived);
    if (poolId === undefined && first)
      void navigate({
        to: "/stock/$poolId",
        params: { poolId: String(first.id) },
        replace: true,
      });
  }, [poolId, list, navigate]);

  if (!store || storeId === undefined) {
    return (
      <Card>
        <EmptyState title="No store selected" />
      </Card>
    );
  }
  const available = list
    .filter((p) => !p.archived)
    .reduce((n, p) => n + p.counts.available, 0);

  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-[22px] font-medium">Stock</h1>
          <p className="mt-1 text-muted">
            {plural(available, "account")} available in{" "}
            {plural(list.filter((p) => !p.archived).length, "pool")} of{" "}
            {store.name}.
          </p>
        </div>
        <div className="ml-auto flex flex-wrap gap-2">
          <UndoLastExtraction storeId={storeId} />
          <ConvertFormat
            storeId={storeId}
            pool={pool}
            onImport={setImportText}
          />
          <ExtractionHistory storeId={storeId} pools={list} />
          <FindLogin storeId={storeId} pools={list} />
        </div>
      </div>
      <div className="grid items-start gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
        <PoolList
          storeId={storeId}
          pools={list}
          selectedId={poolId}
          game={game}
          setGame={setGame}
        />
        {pool ? (
          <PoolPanel
            key={pool.id}
            storeId={storeId}
            pool={pool}
            pools={list}
            games={games}
            importText={importText}
            setImportText={setImportText}
          />
        ) : (
          <Card>
            <EmptyState
              title={
                pools.isLoading
                  ? "Loading…"
                  : poolId !== undefined
                    ? "Pool not found in this store"
                    : "No pools yet"
              }
            >
              {!pools.isLoading &&
                poolId === undefined &&
                "Create a pool, then import accounts into it."}
            </EmptyState>
          </Card>
        )}
      </div>
    </>
  );
}

export function StockPage() {
  return <StockManager />;
}

export function StockPoolPage() {
  const { poolId } = useParams({ from: "/app/stock/$poolId" });
  return <StockManager poolId={Number(poolId)} />;
}
