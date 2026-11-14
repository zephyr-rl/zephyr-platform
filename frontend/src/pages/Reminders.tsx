import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Badge, Button, Card, ConfirmDialog, EmptyState } from "@/components/ui";
import { api, type ReminderCandidate, type ReminderRow, type RemindersStatus } from "@/lib/api";
import { useStores } from "@/lib/session";

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

const STATE: Record<ReminderRow["state"], { label: string; tone: "success" | "progress" | "danger" | "neutral" }> = {
  queued: { label: "Queued", tone: "progress" },
  sending: { label: "Sending", tone: "progress" },
  sent: { label: "Sent", tone: "success" },
  skipped: { label: "Not sent", tone: "neutral" },
  failed: { label: "Failed", tone: "danger" },
};

const FILTER_LABEL: Record<string, string> = {
  skip_negative: "orders with a negative review are skipped",
  skip_reviewed: "orders with any review are skipped",
  remind_all: "reviewed orders are reminded too",
};

function rating(r: number | null) {
  if (r === null) return <span className="text-muted">No review</span>;
  if (r >= 4) return <Badge tone="success">Positive</Badge>;
  if (r <= 2) return <Badge tone="danger">Negative</Badge>;
  return <Badge tone="neutral">Neutral</Badge>;
}

export function RemindersPage() {
  const { store } = useStores();
  const storeId = store?.id;
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: ["reminders", storeId, "status"],
    queryFn: () => api<RemindersStatus>(`/stores/${storeId}/reminders`),
    enabled: storeId !== undefined,
    refetchInterval: 15_000,
  });
  const candidates = useQuery({
    queryKey: ["reminders", storeId, "candidates"],
    queryFn: () => api<ReminderCandidate[]>(`/stores/${storeId}/reminders/candidates`),
    enabled: storeId !== undefined && status.data?.enabled === true,
  });
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const rows = useMemo(() => candidates.data ?? [], [candidates.data]);
  useEffect(() => setSelected(new Set(rows.map((c) => c.order_id))), [rows]);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["reminders", storeId] });
  const cancel = useMutation({
    mutationFn: () => api<{ cancelled: number }>(`/stores/${storeId}/reminders/cancel`, { method: "POST" }),
    onSuccess: async (r) => {
      await refresh();
      toast.success(`${r.cancelled} reminder(s) taken out of the queue`);
    },
  });

  if (!store || storeId === undefined) return null;
  const s = status.data;
  const room = s ? Math.max(0, s.daily_cap - s.sent_last_day - s.queued) : 0;
  const all = rows.length > 0 && selected.size === rows.length;

  return (
    <>
      <div>
        <h1 className="text-[22px] font-semibold">Review reminders</h1>
        <p className="mt-1 text-muted">
          Ask {store.name}'s buyers for a review some time after their order. One reminder per order, ever, sent one at a time with a pause in between.
        </p>
      </div>

      {s && !s.enabled && (
        <Card>
          <EmptyState title="Review reminders are off for this store">
            Turn them on in{" "}
            <Link to="/automation" className="text-accent underline-offset-4 hover:underline">
              Automation
            </Link>
            , where the message, the waiting time, the review filter and the daily limit are set.
          </EmptyState>
        </Card>
      )}

      {s?.enabled && (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div className="rounded-lg border border-line bg-surface px-4 py-3">
              <div className="text-[13px] text-muted">Sent in the last 24 hours</div>
              <div className="mt-1 text-[28px] font-semibold">
                {s.sent_last_day} <span className="text-[15px] font-normal text-muted">of {s.daily_cap}</span>
              </div>
            </div>
            <div className="rounded-lg border border-line bg-surface px-4 py-3">
              <div className="text-[13px] text-muted">In the queue</div>
              <div className="mt-1 text-[28px] font-semibold">{s.queued}</div>
            </div>
            <div className="rounded-lg border border-line bg-surface px-4 py-3">
              <div className="text-[13px] text-muted">Mode</div>
              <div className="mt-1 text-[15px] font-semibold">{s.automatic ? "Automatic" : "You choose and start each batch"}</div>
              <div className="text-[13px] text-muted">Review filter: {FILTER_LABEL[s.review_filter] ?? s.review_filter}.</div>
            </div>
          </div>

          {s.shadow && (
            <p role="status" className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-[13px] text-warning">
              The store is in shadow mode: the buyers below are only shown. Nothing is sent until shadow mode is turned off.
            </p>
          )}

          <Card
            title="Buyers to remind"
            actions={
              <div className="flex flex-wrap items-center gap-2">
                {s.queued > 0 && (
                  <Button size="sm" variant="ghost" busy={cancel.isPending} onClick={() => cancel.mutate()}>
                    Empty the queue
                  </Button>
                )}
                <ConfirmDialog
                  trigger={
                    <Button size="sm" variant="primary" disabled={selected.size === 0 || s.shadow}>
                      Send to {selected.size} buyer{selected.size === 1 ? "" : "s"}
                    </Button>
                  }
                  title={`Send ${selected.size} review reminder${selected.size === 1 ? "" : "s"}?`}
                  description={
                    <>
                      Each buyer gets the message set in Automation, in the order chat, one every few seconds. Today {room} more fit in the daily
                      limit of {s.daily_cap}; the rest stay in this list for another day. A buyer who leaves a review meanwhile is skipped as the filter says.
                    </>
                  }
                  confirmLabel="Send reminders"
                  onConfirm={async () => {
                    try {
                      const r = await api<{ queued: number }>(`/stores/${storeId}/reminders/queue`, {
                        method: "POST",
                        body: { order_ids: [...selected] },
                      });
                      await refresh();
                      const left = selected.size - r.queued;
                      toast.success(`${r.queued} reminder(s) queued`, {
                        description:
                          left > 0
                            ? `${left} did not fit in today's limit and stay in the list for another day.`
                            : "They are sent one at a time; follow them in History.",
                      });
                    } catch (error) {
                      toast.error("Could not queue them", { description: (error as Error).message });
                    }
                  }}
                />
              </div>
            }
          >
            {candidates.error ? (
              <p role="alert" className="m-4 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-[13px] text-danger">
                {candidates.error.message}
              </p>
            ) : candidates.data && rows.length === 0 ? (
              <EmptyState title="Nobody to remind right now">Orders become eligible after the waiting time set in Automation.</EmptyState>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] border-collapse text-[13px]">
                  <thead>
                    <tr className="text-left text-muted">
                      <th className="w-10 px-4 py-2.5">
                        <input
                          type="checkbox"
                          aria-label="Select all"
                          checked={all}
                          onChange={() => setSelected(all ? new Set() : new Set(rows.map((c) => c.order_id)))}
                          className="size-4 accent-accent"
                        />
                      </th>
                      <th className="px-4 py-2.5 font-medium">Buyer</th>
                      <th className="px-4 py-2.5 font-medium">Order</th>
                      <th className="px-4 py-2.5 font-medium">Placed</th>
                      <th className="px-4 py-2.5 font-medium">Review</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((c) => (
                      <tr key={c.order_id} className="border-t border-line">
                        <td className="px-4 py-2.5">
                          <input
                            type="checkbox"
                            aria-label={`Remind ${c.buyer}`}
                            checked={selected.has(c.order_id)}
                            onChange={() => {
                              const next = new Set(selected);
                              if (next.has(c.order_id)) next.delete(c.order_id);
                              else next.add(c.order_id);
                              setSelected(next);
                            }}
                            className="size-4 accent-accent"
                          />
                        </td>
                        <td className="px-4 py-2.5 font-medium">{c.buyer || "Unknown buyer"}</td>
                        <td className="px-4 py-2.5">
                          {c.title} <span className="text-muted">#{c.remote_id.slice(0, 8)}</span>
                        </td>
                        <td className="px-4 py-2.5 text-muted">{dateTime.format(new Date(c.placed_at))}</td>
                        <td className="px-4 py-2.5">{rating(c.rating)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}

      <Card title="History">
        {s && s.history.length === 0 ? (
          <EmptyState title="No reminders yet" />
        ) : (
          <ul className="flex flex-col">
            {s?.history.map((r) => (
              <li key={r.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line px-4 py-2.5 text-[13px] first:border-t-0">
                <Badge tone={STATE[r.state].tone}>{STATE[r.state].label}</Badge>
                <span className="font-medium">{r.buyer_name ?? "Unknown buyer"}</span>
                <span className="text-muted">#{r.remote_order_id.slice(0, 8)}</span>
                {r.note && <span className="text-muted">{r.note}</span>}
                <span className="ml-auto text-muted">{dateTime.format(new Date(r.sent_at ?? r.created_at))}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}
