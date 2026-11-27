/** Compact cards for the Custom items and Top-ups tabs of Listings. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { toast } from "sonner";
import { Badge, Button, Switch } from "@/components/ui";
import { api, type Listing, type StockPool, type StoreModule } from "@/lib/api";
import { ScheduleButton, ScheduleLine } from "@/components/listings/Schedule";

const MODE_LABEL: Record<Listing["delivery_mode"], string> = {
  manual: "Manual",
  assisted: "Assisted",
  auto: "Automatic",
  text: "Message only",
};

export function money(amount: string, currency: string): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
  }).format(Number(amount));
}

export function StateBadge({ listing }: { listing: Listing }) {
  if (listing.removed_remotely) return <Badge tone="danger">Gone</Badge>;
  if (listing.remote_state === "active")
    return <Badge tone="success">Active</Badge>;
  return (
    <Badge tone="warning">
      {listing.remote_state === "paused" ? "Paused" : "Closed"}
    </Badge>
  );
}

function offerUrl(listing: Listing): string | null {
  return listing.connector === "eldorado"
    ? `https://www.eldorado.gg/offer/${listing.remote_offer_id}`
    : null;
}

function firstLine(text: string | null | undefined): string {
  return (
    (text ?? "")
      .split("\n")
      .find((l) => l.trim())
      ?.trim() ?? ""
  );
}

function Shell({
  storeId,
  listing,
  children,
  onEdit,
}: {
  storeId: number;
  listing: Listing;
  children: React.ReactNode;
  onEdit: () => void;
}) {
  const url = offerUrl(listing);
  return (
    <section
      aria-label={listing.title}
      className={`flex flex-col gap-2 rounded-lg border border-line bg-surface p-3 text-[13px] ${listing.removed_remotely ? "opacity-60" : ""}`}
    >
      <div className="flex items-start gap-2">
        <h3
          className="line-clamp-2 min-w-0 flex-1 text-[14px] leading-snug font-medium"
          title={listing.title}
        >
          {listing.title}
        </h3>
        <StateBadge listing={listing} />
      </div>
      {children}
      <ScheduleLine listing={listing} />
      <div className="mt-auto flex justify-end gap-2 border-t border-line pt-2">
        {url && (
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex min-h-8 items-center rounded-md px-2.5 text-[12px] text-muted hover:bg-hover hover:text-fg"
          >
            Eldorado ↗
          </a>
        )}
        <ScheduleButton storeId={storeId} listing={listing} />
        <Button size="sm" onClick={onEdit}>
          Edit
        </Button>
      </div>
    </section>
  );
}

export function ItemCard({
  storeId,
  listing,
  pools,
  onEdit,
}: {
  storeId: number;
  listing: Listing;
  pools: StockPool[];
  onEdit: () => void;
}) {
  const queryClient = useQueryClient();
  const pool = pools.find((p) => p.id === listing.pool_id);
  const available = pool?.counts.available ?? 0;
  const on = listing.delivery_mode !== "manual";
  const change = useMutation({
    mutationFn: (mode: Listing["delivery_mode"]) =>
      api<Listing>(`/stores/${storeId}/listings/${listing.id}`, {
        method: "PATCH",
        body: { delivery_mode: mode },
      }),
    onSuccess: async (l) => {
      await queryClient.invalidateQueries({ queryKey: ["listings", storeId] });
      toast.success(`${l.title.slice(0, 50)}: ${MODE_LABEL[l.delivery_mode]}`);
    },
    onError: (e) =>
      toast.error("Could not change the delivery", { description: e.message }),
  });
  const toggle = (value: boolean) => {
    if (!value) return change.mutate("manual");
    if (listing.pool_id) return change.mutate("auto");
    if (listing.delivery_template?.trim()) return change.mutate("text");
    toast.info("Choose the stock pool or the message first");
    onEdit();
  };
  return (
    <Shell storeId={storeId} listing={listing} onEdit={onEdit}>
      <div className="text-muted">
        {money(listing.price, listing.currency)}
        {listing.remote_quantity !== null &&
          ` · ${listing.remote_quantity} on Eldorado`}
      </div>
      <div>
        {listing.delivery_mode === "text" ? (
          <span className="text-muted">Message only, no stock</span>
        ) : pool ? (
          <>
            <Link
              to="/stock/$poolId"
              params={{ poolId: String(pool.id) }}
              className="hover:underline"
            >
              {pool.name}
            </Link>{" "}
            <span
              className={
                available === 0 ? "font-medium text-danger" : "text-muted"
              }
            >
              · {available} left
            </span>
          </>
        ) : (
          <span className="text-muted">No stock pool</span>
        )}
      </div>
      <div className="flex items-center gap-2">
        <Badge tone={on ? "progress" : "neutral"}>
          {MODE_LABEL[listing.delivery_mode]}
        </Badge>
        <span className="min-w-0 flex-1 truncate text-[12px] text-muted">
          {[
            listing.units_per_sale > 1 && `${listing.units_per_sale} per unit`,
            listing.require_confirmation && "asks CONFIRM",
          ]
            .filter(Boolean)
            .join(" · ")}
        </span>
        <Switch
          checked={on}
          onCheckedChange={toggle}
          disabled={change.isPending || listing.removed_remotely}
          label={`Deliver ${listing.title} automatically`}
        />
      </div>
      {firstLine(listing.delivery_template) && (
        <p
          className="truncate text-[12px] text-muted"
          title={listing.delivery_template ?? ""}
        >
          {firstLine(listing.delivery_template)}
        </p>
      )}
    </Shell>
  );
}

export function TopupCard({
  storeId,
  listing,
  topups,
  onEdit,
}: {
  storeId: number;
  listing: Listing;
  topups?: StoreModule;
  onEdit: () => void;
}) {
  const queryClient = useQueryClient();
  const settings = (topups?.settings ?? {}) as {
    skip_offers?: string[];
    offer_messages?: Record<string, string>;
    instructions?: string;
  };
  const skipped = (settings.skip_offers ?? []).includes(
    listing.remote_offer_id,
  );
  const own =
    listing.delivery_template?.trim() ||
    settings.offer_messages?.[listing.remote_offer_id];
  const change = useMutation({
    mutationFn: async (send: boolean) => {
      if (!topups)
        throw new Error("The Top-Ups module is not available for this store.");
      const skip = new Set(settings.skip_offers ?? []);
      if (send) skip.delete(listing.remote_offer_id);
      else skip.add(listing.remote_offer_id);
      return api<StoreModule>(`/stores/${storeId}/modules/topups`, {
        method: "PUT",
        body: {
          settings: { ...topups.settings, skip_offers: [...skip] },
          expected_version: topups.version,
        },
      });
    },
    onSuccess: async (_, send) => {
      await queryClient.invalidateQueries({
        queryKey: ["stores", storeId, "modules"],
      });
      toast.success(
        `${listing.title.slice(0, 50)}: ${send ? "instructions sent when paid" : "by hand, nothing sent"}`,
      );
    },
    onError: (e) =>
      toast.error("Could not change the top-up", { description: e.message }),
  });
  return (
    <Shell storeId={storeId} listing={listing} onEdit={onEdit}>
      <div className="text-muted">
        {money(listing.price, listing.currency)}
        {listing.platform && ` · ${listing.platform}`}
        {listing.remote_quantity !== null &&
          ` · ${listing.remote_quantity} on Eldorado`}
      </div>
      <div className="flex items-center gap-2">
        <span className="flex-1">Send instructions</span>
        <Switch
          checked={!skipped}
          onCheckedChange={(v) => change.mutate(v)}
          disabled={change.isPending || listing.removed_remotely}
          label={`Send the instructions for ${listing.title}`}
        />
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone="neutral">
          {skipped
            ? "By hand, nothing sent"
            : own
              ? "Own message"
              : "General message"}
        </Badge>
        {listing.orders_need_you > 0 && (
          <Link
            to="/orders"
            search={{ listing: listing.id }}
            className="rounded-sm bg-warning/14 px-1.5 text-[12px] font-medium text-warning hover:underline"
          >
            {listing.orders_need_you} wait for you
          </Link>
        )}
      </div>
      {!skipped && (
        <p
          className="truncate text-[12px] text-muted"
          title={own || settings.instructions || ""}
        >
          {firstLine(own || settings.instructions)}
        </p>
      )}
    </Shell>
  );
}
