import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { ExtractionResult } from "@/components/stock/ExtractionResult";
import { Badge, Button, Card, EmptyState, Modal, TextField } from "@/components/ui";
import { api, ApiError, type Extraction, type ExtractionDetail, type StockItem, type StockPool } from "@/lib/api";
import { useStores } from "@/lib/session";
import { plural, STATE_LABEL, STATE_TONE } from "@/lib/stock";

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

function NewPoolDialog({ storeId }: { storeId: number }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [threshold, setThreshold] = useState("5");
  const create = useMutation({
    mutationFn: () =>
      api<StockPool>(`/stores/${storeId}/stock/pools`, {
        method: "POST",
        body: { name, low_stock_threshold: Number(threshold) },
      }),
    onSuccess: async (pool) => {
      await queryClient.invalidateQueries({ queryKey: ["stock", storeId] });
      toast.success(`Pool ${pool.name} created`);
      setOpen(false);
      setName("");
    },
    onError: (error) => toast.error("Could not create the pool", { description: error.message }),
  });
  return (
    <>
      <Button variant="primary" onClick={() => setOpen(true)}>
        New pool
      </Button>
      <Modal open={open} onOpenChange={setOpen} title="New stock pool" description="A pool groups the accounts of one product, e.g. “RLCS x6”.">
        <form
          className="flex flex-col gap-4"
          onSubmit={(e: FormEvent) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
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
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" busy={create.isPending} disabled={!name.trim()}>
              Create pool
            </Button>
          </div>
        </form>
      </Modal>
    </>
  );
}

function LoginSearch({ storeId, pools }: { storeId: number; pools: StockPool[] }) {
  const [login, setLogin] = useState("");
  const [result, setResult] = useState<StockItem | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!login.trim()) return;
    setBusy(true);
    try {
      setResult(await api<StockItem | null>(`/stores/${storeId}/stock/search?login=${encodeURIComponent(login.trim())}`));
    } catch (error) {
      toast.error("Search failed", { description: (error as ApiError).message });
    } finally {
      setBusy(false);
    }
  }
  const pool = result ? pools.find((p) => p.id === result.pool_id) : undefined;
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3 p-4">
      <TextField
        label="Find an account by its exact login"
        value={login}
        onChange={(e) => {
          setLogin(e.target.value);
          setResult(undefined);
        }}
        autoComplete="off"
        className="min-w-64 flex-1"
      />
      <Button type="submit" busy={busy}>
        Search
      </Button>
      {result === null && <p className="basis-full text-muted">Not in this store's stock, history included.</p>}
      {result && (
        <p className="flex basis-full flex-wrap items-center gap-2">
          <Badge tone={STATE_TONE[result.state]}>{STATE_LABEL[result.state]}</Badge>
          <span>
            in{" "}
            <Link to="/stock/$poolId" params={{ poolId: String(result.pool_id) }} className="text-accent underline">
              {pool?.name ?? `pool ${result.pool_id}`}
            </Link>
            , position {result.position}
          </span>
          {result.note && <span className="text-muted">· {result.note}</span>}
        </p>
      )}
    </form>
  );
}

function ExtractionsCard({ storeId }: { storeId: number }) {
  const extractions = useQuery({
    queryKey: ["stock", storeId, "extractions"],
    queryFn: () => api<Extraction[]>(`/stores/${storeId}/stock/extractions?limit=20`),
  });
  const [detail, setDetail] = useState<ExtractionDetail | null>(null);
  const view = useMutation({
    mutationFn: (id: number) => api<ExtractionDetail>(`/stores/${storeId}/stock/extractions/${id}`),
    onSuccess: setDetail,
    onError: (error) => toast.error("Could not open the extraction", { description: error.message }),
  });
  return (
    <Card title="Recent extractions">
      {extractions.data?.length ? (
        <ul className="divide-y divide-line">
          {extractions.data.map((e) => (
            <li key={e.id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-[13px]">
              <span className="tabular text-muted">{dateTime.format(new Date(e.created_at))}</span>
              <span className="font-medium">{plural(e.count, "account")}</span>
              <span>from {e.pool_name}</span>
              {e.reason && <span className="text-muted">· {e.reason}</span>}
              {e.undone_at && <Badge tone="neutral">Undone</Badge>}
              <Button size="sm" variant="ghost" className="ml-auto" busy={view.isPending && view.variables === e.id} onClick={() => view.mutate(e.id)}>
                View accounts
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState title="No extractions yet" />
      )}
      <ExtractionResult storeId={storeId} detail={detail} onClose={() => setDetail(null)} />
    </Card>
  );
}

export function StockPage() {
  const { store } = useStores();
  const storeId = store?.id;
  const [showArchived, setShowArchived] = useState(false);
  const pools = useQuery({
    queryKey: ["stock", storeId, "pools"],
    queryFn: () => api<StockPool[]>(`/stores/${storeId}/stock/pools`),
    enabled: storeId !== undefined,
  });

  if (!store || storeId === undefined) {
    return (
      <Card>
        <EmptyState title="No store selected" />
      </Card>
    );
  }
  const active = pools.data?.filter((p) => !p.archived) ?? [];
  const archived = pools.data?.filter((p) => p.archived) ?? [];
  const totalAvailable = active.reduce((sum, p) => sum + p.counts.available, 0);
  const shown = showArchived ? [...active, ...archived] : active;

  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-[22px] font-semibold">Stock</h1>
          <p className="mt-1 text-muted">
            {plural(totalAvailable, "account")} available in {plural(active.length, "pool")} of {store.name}.
          </p>
        </div>
        <div className="ml-auto">
          <NewPoolDialog storeId={storeId} />
        </div>
      </div>

      <Card>
        <LoginSearch storeId={storeId} pools={pools.data ?? []} />
      </Card>

      <Card
        title="Pools"
        actions={
          archived.length > 0 && (
            <Button size="sm" variant="ghost" onClick={() => setShowArchived((v) => !v)}>
              {showArchived ? "Hide archived" : `Show archived (${archived.length})`}
            </Button>
          )
        }
      >
        {pools.data && shown.length === 0 ? (
          <EmptyState title="No pools yet">Create a pool, then import accounts into it.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] border-collapse text-[13px]">
              <thead>
                <tr className="text-left text-muted">
                  <th className="px-4 py-2.5 font-medium">Pool</th>
                  <th className="px-4 py-2.5 text-right font-medium">Available</th>
                  <th className="px-4 py-2.5 text-right font-medium">Quarantined</th>
                  <th className="px-4 py-2.5 text-right font-medium">Out of stock (history)</th>
                  <th className="px-4 py-2.5 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((p) => (
                  <tr key={p.id} className="border-t border-line hover:bg-hover/50">
                    <td className="px-4 py-2.5">
                      <Link to="/stock/$poolId" params={{ poolId: String(p.id) }} className="font-medium hover:underline">
                        {p.name}
                      </Link>
                      <span className="ml-2 text-muted">{p.game}</span>
                    </td>
                    <td className="tabular px-4 py-2.5 text-right text-[15px] font-semibold">{p.counts.available.toLocaleString()}</td>
                    <td className="tabular px-4 py-2.5 text-right">{p.counts.quarantined || "—"}</td>
                    <td className="tabular px-4 py-2.5 text-right text-muted">
                      {(p.counts.extracted + p.counts.delivered + p.counts.void).toLocaleString()}
                    </td>
                    <td className="px-4 py-2.5">
                      {p.archived ? (
                        <Badge tone="neutral">Archived</Badge>
                      ) : p.counts.available === 0 ? (
                        <Badge tone="danger">Empty</Badge>
                      ) : p.low ? (
                        <Badge tone="warning">Low</Badge>
                      ) : (
                        <Badge tone="success">OK</Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <ExtractionsCard storeId={storeId} />
    </>
  );
}
