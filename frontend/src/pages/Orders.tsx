import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearch } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Badge, Button, Card, ConfirmDialog, EmptyState, Modal, TextField } from "@/components/ui";
import { api, type FulfilmentState, type OrderDetail, type OrderRow } from "@/lib/api";
import { useStores } from "@/lib/session";
import { plural } from "@/lib/stock";

type Tone = "success" | "progress" | "warning" | "danger" | "neutral";
const dateTime = new Intl.DateTimeFormat(undefined, { hourCycle: "h23", dateStyle: "medium", timeStyle: "short" });

export const FULFILMENT: Record<FulfilmentState, { label: string; tone: Tone }> = {
  pending: { label: "Starting", tone: "progress" },
  awaiting_buyer: { label: "Waiting for the buyer", tone: "progress" },
  awaiting_approval: { label: "Waiting for your approval", tone: "warning" },
  reserved: { label: "Delivering", tone: "progress" },
  sent: { label: "Sent, confirming", tone: "progress" },
  done: { label: "Delivered", tone: "success" },
  manual: { label: "Manual", tone: "neutral" },
  awaiting_owner: { label: "Top-up: waiting for you", tone: "warning" },
  needs_attention: { label: "Needs attention", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "neutral" },
  simulated: { label: "Simulated", tone: "neutral" },
};

const MODE_LABEL: Record<string, string> = { manual: "Manual", assisted: "Assisted", auto: "Automatic", text: "Message only", topup: "Top-up" };
const FILTERS = [
  { key: "attention", label: "Needs you" },
  { key: "waiting", label: "In progress" },
  { key: "all", label: "All" },
] as const;

function money(amount: string, currency: string) {
  return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(Number(amount));
}

function OrderDialog({ storeId, orderId, onClose }: { storeId: number; orderId: number; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const detail = useQuery({
    queryKey: ["orders", storeId, "detail", orderId],
    queryFn: () => api<OrderDetail>(`/stores/${storeId}/orders/${orderId}`),
    refetchInterval: 5000,
  });
  const act = async (action: "approve" | "retry" | "resolve", done: string) => {
    try {
      await api(`/stores/${storeId}/orders/${orderId}/${action}`, { method: "POST", body: action === "resolve" ? { reason } : undefined });
      await queryClient.invalidateQueries({ queryKey: ["orders", storeId] });
      toast.success(done);
    } catch (error) {
      toast.error("Could not do that", { description: (error as Error).message });
    }
  };
  const o = detail.data;
  if (!o) return null;
  const state = o.fulfilment_state;
  const closed = state === "done" || state === "cancelled" || state === "simulated" || state === null;
  return (
    <Modal
      open
      onOpenChange={(v) => !v && onClose()}
      wide
      title={o.offer_title ?? "Order"}
      description={`Order ${o.remote_id} · ${o.buyer_name ?? "buyer"} · ${plural(o.quantity, "unit")} · ${money(o.total, o.currency)}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        {state && <Badge tone={FULFILMENT[state].tone}>{FULFILMENT[state].label}</Badge>}
        {o.mode && <span className="text-[13px] text-muted">{MODE_LABEL[o.mode] ?? o.mode} delivery</span>}
        {o.shadow && <Badge tone="warning">Shadow mode</Badge>}
        {o.pool_name && <span className="text-[13px] text-muted">· from {o.pool_name}</span>}
      </div>
      {o.last_error && state === "needs_attention" && (
        <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-[13px] text-danger">
          {o.last_error}
        </p>
      )}
      {o.reserved.length > 0 && (
        <div className="text-[13px]">
          <div className="mb-1 text-muted">Accounts for this order</div>
          <div className="flex flex-wrap gap-2">
            {o.reserved.map((r) => (
              <span key={r.login} className="rounded-sm bg-hover px-2 py-0.5 font-mono text-xs">
                {r.login} · {r.state}
              </span>
            ))}
          </div>
        </div>
      )}
      <ol className="flex flex-col gap-3">
        {o.events.map((e, i) => (
          <li key={i} className="flex flex-col gap-0.5 border-l-2 border-line-strong pl-3 text-[13px]">
            <span>{e.message}</span>
            <span className="text-muted">
              {dateTime.format(new Date(e.at))} · {e.actor}
            </span>
          </li>
        ))}
      </ol>
      {!closed && (
        <div className="flex flex-wrap items-end gap-2 border-t border-line pt-4">
          {state === "awaiting_approval" && (
            <ConfirmDialog
              trigger={<Button variant="primary">Approve delivery</Button>}
              title={`Deliver order ${o.remote_id}?`}
              description={`${plural(o.units ?? 1, "account")} will be sent to the buyer in the chat and the order marked delivered. This cannot be undone.`}
              confirmLabel="Deliver order"
              onConfirm={() => act("approve", "Delivery approved")}
            />
          )}
          {state === "needs_attention" && (
            <Button variant="primary" onClick={() => act("retry", "Retrying the delivery")}>
              Retry
            </Button>
          )}
          <TextField label="Note (optional)" value={reason} onChange={(e) => setReason(e.target.value)} className="min-w-56 flex-1" />
          <ConfirmDialog
            trigger={<Button>Mark handled by hand</Button>}
            title="Mark this order as handled by hand?"
            description="Automation stops for this order. Accounts set aside and not sent go back to stock."
            confirmLabel="Mark handled"
            onConfirm={() => act("resolve", "Order marked as handled by hand")}
          />
        </div>
      )}
    </Modal>
  );
}

export function OrdersPage() {
  const { store } = useStores();
  const storeId = store?.id;
  const [show, setShow] = useState<(typeof FILTERS)[number]["key"]>("attention");
  const search = useSearch({ from: "/app/orders" });
  const [open, setOpen] = useState<number | null>(search.order ?? null);
  useEffect(() => {
    if (search.order) setOpen(search.order);
  }, [search.order]);
  const listingId = search.listing;
  const orders = useQuery({
    queryKey: ["orders", storeId, show, listingId ?? null],
    queryFn: () =>
      api<OrderRow[]>(`/stores/${storeId}/orders?show=${show}${listingId ? `&listing_id=${listingId}` : ""}`),
    enabled: storeId !== undefined,
    refetchInterval: 15_000,
  });
  const poll = useMutation({
    mutationFn: () => api(`/stores/${storeId}/orders/poll`, { method: "POST" }),
    onSuccess: () => toast.success("Checking for new orders", { description: "New paid orders appear here within a minute." }),
    onError: (error) => toast.error("Could not check", { description: error.message }),
  });
  if (!store || storeId === undefined) return null;
  const rows = orders.data ?? [];
  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-[22px] font-medium">Orders</h1>
          <p className="mt-1 text-muted">
            {store.name} orders and their delivery.{store.shadow_mode && " Shadow mode: nothing is sent to buyers."}
          </p>
          {listingId && (
            <p className="mt-1 text-[13px]">
              Only the orders of one offer.{" "}
              <Link to="/orders" className="text-accent underline">
                Show every offer
              </Link>
            </p>
          )}
        </div>
        <Button className="ml-auto" busy={poll.isPending} onClick={() => poll.mutate()}>
          Check for new orders
        </Button>
      </div>
      <Card
        actions={
          <div role="group" aria-label="Show" className="flex gap-1">
            {FILTERS.map((f) => (
              <Button key={f.key} size="sm" variant={show === f.key ? "selected" : "ghost"} aria-pressed={show === f.key} onClick={() => setShow(f.key)}>
                {f.label}
              </Button>
            ))}
          </div>
        }
        title="Orders"
      >
        {orders.data && rows.length === 0 ? (
          <EmptyState title={show === "attention" ? "Nothing needs you right now" : "No orders here"}>
            {show === "attention" && "Orders that wait for your approval or stopped with a problem appear here."}
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] border-collapse text-[13px]">
              <thead>
                <tr className="text-left text-muted">
                  <th className="px-4 py-2.5 font-medium">When</th>
                  <th className="px-4 py-2.5 font-medium">Order</th>
                  <th className="px-4 py-2.5 font-medium">Buyer</th>
                  <th className="px-4 py-2.5 text-right font-medium">Total</th>
                  <th className="px-4 py-2.5 font-medium">Delivery</th>
                  <th className="px-4 py-2.5" />
                </tr>
              </thead>
              <tbody>
                {rows.map((o) => (
                  <tr key={o.id} className="border-t border-line">
                    <td className="tabular px-4 py-2.5 whitespace-nowrap text-muted">{dateTime.format(new Date(o.created_at))}</td>
                    <td className="px-4 py-2.5">
                      <div className="font-medium">{o.offer_title ?? "Order"}</div>
                      <div className="text-muted">
                        {plural(o.quantity, "unit")}
                        {o.remote_state ? ` · ${o.remote_state}` : ""}
                      </div>
                    </td>
                    <td className="px-4 py-2.5">{o.buyer_name ?? "—"}</td>
                    <td className="tabular px-4 py-2.5 text-right">{money(o.total, o.currency)}</td>
                    <td className="px-4 py-2.5">
                      {o.fulfilment_state ? (
                        <Badge tone={FULFILMENT[o.fulfilment_state].tone}>{FULFILMENT[o.fulfilment_state].label}</Badge>
                      ) : (
                        <span className="text-muted">Recorded</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      {o.fulfilment_id && (
                        <Button size="sm" onClick={() => setOpen(o.id)}>
                          Open
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {open !== null && <OrderDialog storeId={storeId} orderId={open} onClose={() => setOpen(null)} />}
    </>
  );
}
