import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { toast } from "sonner";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
} from "@/components/ui";
import { api, type ChatThread } from "@/lib/api";
import { useStores } from "@/lib/session";

const dateTime = new Intl.DateTimeFormat(undefined, { hourCycle: "h23",
  dateStyle: "medium",
  timeStyle: "short",
});

function ago(iso: string | null): string {
  if (!iso) return "";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return dateTime.format(new Date(iso));
}

function Thread({ storeId, thread }: { storeId: number; thread: ChatThread }) {
  const queryClient = useQueryClient();
  const handled = useMutation({
    mutationFn: () =>
      api<ChatThread>(`/stores/${storeId}/messages/${thread.id}/handled`, {
        method: "POST",
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["messages", storeId] });
      toast.success("Marked as handled", {
        description: `${thread.buyer_name ?? "The buyer"}'s conversation left the list.`,
      });
    },
    onError: (error) =>
      toast.error("Could not mark it", { description: error.message }),
  });
  return (
    <li className="flex flex-col gap-2 border-t border-line px-4 py-3 first:border-t-0 sm:flex-row sm:items-start sm:gap-4">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">
            {thread.buyer_name ?? "Unknown buyer"}
          </span>
          {thread.urgent && <Badge tone="danger">Urgent</Badge>}
          {thread.needs_reply ? (
            <Badge tone="warning">Waiting for a reply</Badge>
          ) : (
            <Badge tone="neutral">Answered or handled</Badge>
          )}
          <span className="text-[13px] text-muted">
            {ago(thread.last_buyer_at)}
          </span>
        </div>
        <blockquote className="mt-1.5 border-l-2 border-line-strong pl-3 break-words whitespace-pre-wrap">
          {thread.last_buyer_text}
        </blockquote>
        <div className="mt-1.5 text-[13px] text-muted">
          {[
            thread.offer_title,
            thread.order_remote_id &&
              `Order #${thread.order_remote_id.length > 12 ? `${thread.order_remote_id.slice(0, 8)}…` : thread.order_remote_id}`,
            thread.handled_by &&
              thread.handled_at &&
              `Handled by ${thread.handled_by} ${ago(thread.handled_at)}`,
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
      </div>
      <div className="flex shrink-0 gap-2">
        {thread.order_url && (
          <a
            href={thread.order_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex min-h-9 items-center rounded-md border border-line-strong bg-raised px-3 text-sm hover:bg-hover"
          >
            Open on Eldorado
          </a>
        )}
        {thread.needs_reply && (
          <Button
            size="sm"
            busy={handled.isPending}
            onClick={() => handled.mutate()}
          >
            Mark handled
          </Button>
        )}
      </div>
    </li>
  );
}

export function MessagesPage() {
  const { store } = useStores();
  const storeId = store?.id;
  const queryClient = useQueryClient();
  const [show, setShow] = useState<"attention" | "all">("attention");
  const threads = useQuery({
    queryKey: ["messages", storeId, show],
    queryFn: () =>
      api<ChatThread[]>(`/stores/${storeId}/messages?show=${show}`),
    enabled: storeId !== undefined,
    refetchInterval: 20_000,
  });
  const scan = useMutation({
    mutationFn: () =>
      api(`/stores/${storeId}/messages/scan`, { method: "POST" }),
    onSuccess: () => {
      toast.success("Reading the chats now", {
        description: "New messages appear here in a few seconds.",
      });
      setTimeout(
        () =>
          void queryClient.invalidateQueries({
            queryKey: ["messages", storeId],
          }),
        4000,
      );
    },
    onError: (error) =>
      toast.error("Could not start", { description: error.message }),
  });
  if (!store || storeId === undefined) return null;
  const rows = threads.data ?? [];
  const waiting =
    show === "attention"
      ? rows.length
      : rows.filter((r) => r.needs_reply).length;
  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-[22px] font-medium">Messages</h1>
          <p className="mt-1 text-muted">
            Buyer messages in {store.name}'s order chats that nobody has
            answered in the last 24 hours. Thanks and closing messages do not
            count. Alerts for them go to Discord as set in{" "}
            <Link
              to="/notifications"
              className="text-accent underline-offset-4 hover:underline"
            >
              Notifications
            </Link>
            .
          </p>
        </div>
        {waiting > 0 && (
          <ConfirmDialog
            trigger={<Button className="ml-auto">Mark all as read</Button>}
            title={`Mark ${waiting === 1 ? "1 conversation" : `${waiting} conversations`} as read?`}
            description="They stop counting as waiting. A new message from the buyer brings them back."
            confirmLabel="Mark all as read"
            onConfirm={async () => {
              try {
                const r = await api<{ marked: number }>(
                  `/stores/${storeId}/messages/handled-all`,
                  { method: "POST" },
                );
                await queryClient.invalidateQueries({
                  queryKey: ["messages", storeId],
                });
                toast.success(
                  `${r.marked === 1 ? "1 conversation" : `${r.marked} conversations`} marked as read`,
                );
              } catch (error) {
                toast.error("Could not mark them", {
                  description: (error as Error).message,
                });
              }
            }}
          />
        )}
        <Button
          variant="primary"
          className={waiting > 0 ? undefined : "ml-auto"}
          busy={scan.isPending}
          onClick={() => scan.mutate()}
        >
          Check chats now
        </Button>
      </div>
      <Card
        title="Conversations"
        actions={
          <div className="flex gap-1" role="group" aria-label="Show">
            {(["attention", "all"] as const).map((key) => (
              <Button
                key={key}
                size="sm"
                variant={show === key ? "selected" : "ghost"}
                aria-pressed={show === key}
                onClick={() => setShow(key)}
              >
                {key === "attention" ? "Waiting for a reply" : "All"}
              </Button>
            ))}
          </div>
        }
      >
        {threads.data && rows.length === 0 ? (
          <EmptyState
            title={
              show === "attention"
                ? "No buyer is waiting for you"
                : "No conversations yet"
            }
          >
            The chat monitor reads the chats of the most recent open orders
            every 30 seconds (Automation → Chat monitor).
          </EmptyState>
        ) : (
          <ul className="flex flex-col">
            {rows.map((t) => (
              <Thread key={t.id} storeId={storeId} thread={t} />
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}
