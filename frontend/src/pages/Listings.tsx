import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  Modal,
  Switch,
  TextAreaField,
  TextField,
} from "@/components/ui";
import {
  api,
  ApiError,
  type DeliveryMode,
  type Listing,
  type RestockResult,
  type StockPool,
  type StoreModule,
  type VaultUpload,
} from "@/lib/api";
import { CrossPlatformCards } from "@/components/listings/CrossPlatformCards";
import { useStores } from "@/lib/session";
import { plural } from "@/lib/stock";

const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

const KIND_LABEL: Record<Listing["kind"], string> = {
  account: "Account",
  item: "Item",
  topup: "Top-up",
};

const MODES: { value: DeliveryMode; label: string; help: string }[] = [
  {
    value: "manual",
    label: "Manual",
    help: "Nothing is sent automatically. You deliver it yourself.",
  },
  {
    value: "assisted",
    label: "Assisted",
    help: "Stock is set aside when the order is paid; you approve the delivery.",
  },
  {
    value: "auto",
    label: "Automatic",
    help: "Stock is delivered as soon as the order is paid.",
  },
  {
    value: "text",
    label: "Message only",
    help: "Only the message below is sent; no stock is used.",
  },
];

function money(amount: string, currency: string): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
  }).format(Number(amount));
}

interface RestockInfo {
  enabled: boolean;
  defaultTarget: number;
}

const UPLOAD_LABEL: Record<
  VaultUpload["state"],
  { label: string; tone: "success" | "warning" | "danger" }
> = {
  uploaded: { label: "In vault", tone: "success" },
  pending: { label: "Checking", tone: "warning" },
  failed: { label: "Not uploaded", tone: "danger" },
};

function RecentUploads({
  storeId,
  listingId,
}: {
  storeId: number;
  listingId: number;
}) {
  const uploads = useQuery({
    queryKey: ["listings", storeId, listingId, "uploads"],
    queryFn: () =>
      api<VaultUpload[]>(`/stores/${storeId}/listings/${listingId}/uploads`),
  });
  const rows = uploads.data ?? [];
  if (!rows.length)
    return (
      <p className="text-[13px] text-muted">
        No accounts uploaded from here yet.
      </p>
    );
  return (
    <ul
      className="flex max-h-44 flex-col gap-1 overflow-y-auto text-[13px]"
      aria-label="Recent uploads"
    >
      {rows.slice(0, 20).map((u) => (
        <li key={u.id} className="flex items-center gap-3">
          <span className="mono w-40 truncate">{u.login_hint}</span>
          <Badge tone={UPLOAD_LABEL[u.state].tone}>
            {UPLOAD_LABEL[u.state].label}
          </Badge>
          <span className="ml-auto text-muted">
            {dateTime.format(new Date(u.created_at))}
          </span>
        </li>
      ))}
    </ul>
  );
}

function EditListing({
  storeId,
  listing,
  pools,
  restock,
  topups,
  onClose,
}: {
  storeId: number;
  listing: Listing;
  pools: StockPool[];
  restock: RestockInfo;
  topups?: StoreModule;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const isTopup = listing.kind === "topup";
  const topupSettings = (topups?.settings ?? {}) as {
    skip_offers?: string[];
    offer_messages?: Record<string, string>;
  };
  const skipped = (topupSettings.skip_offers ?? []).includes(
    listing.remote_offer_id,
  );
  const importedMessage =
    topupSettings.offer_messages?.[listing.remote_offer_id];
  const [sendInstructions, setSendInstructions] = useState(!skipped);
  const [mode, setMode] = useState<DeliveryMode>(listing.delivery_mode);
  const [poolId, setPoolId] = useState(
    listing.pool_id ? String(listing.pool_id) : "",
  );
  const [units, setUnits] = useState(String(listing.units_per_sale));
  const [confirmation, setConfirmation] = useState(
    listing.require_confirmation,
  );
  const [template, setTemplate] = useState(
    listing.delivery_template ??
      (listing.kind === "topup" ? (importedMessage ?? "") : ""),
  );
  const [vaultInfo, setVaultInfo] = useState(
    listing.vault_additional_info ?? "",
  );
  const [emailDetails, setEmailDetails] = useState(
    listing.include_email_details,
  );
  const [restockOn, setRestockOn] = useState(listing.restock_enabled);
  const [target, setTarget] = useState(
    listing.restock_target ? String(listing.restock_target) : "",
  );
  const [autoResume, setAutoResume] = useState(listing.auto_resume);
  const modes =
    listing.kind === "topup"
      ? MODES.filter((m) => m.value === "manual" || m.value === "text")
      : MODES;

  const save = useMutation({
    mutationFn: async () => {
      const saved = await api<Listing>(
        `/stores/${storeId}/listings/${listing.id}`,
        {
          method: "PATCH",
          body: {
            delivery_mode: isTopup ? "manual" : mode,
            ...(poolId ? { pool_id: Number(poolId) } : { clear_pool: true }),
            units_per_sale: Number(units),
            require_confirmation: confirmation,
            delivery_template: template,
            vault_additional_info: vaultInfo,
            include_email_details: emailDetails,
            restock_enabled: restockOn,
            ...(target
              ? { restock_target: Number(target) }
              : { clear_restock_target: true }),
            auto_resume: autoResume,
          },
        },
      );
      // Top-ups: the instructions switch lives in the Top-Ups module settings, and an imported
      // per-offer message moves onto the listing.
      if (
        isTopup &&
        topups &&
        (sendInstructions === skipped || importedMessage !== undefined)
      ) {
        const offer = listing.remote_offer_id;
        const skip = new Set(topupSettings.skip_offers ?? []);
        if (sendInstructions) skip.delete(offer);
        else skip.add(offer);
        const messages = { ...(topupSettings.offer_messages ?? {}) };
        delete messages[offer];
        await api<StoreModule>(`/stores/${storeId}/modules/topups`, {
          method: "PUT",
          body: {
            settings: {
              ...topups.settings,
              skip_offers: [...skip],
              offer_messages: messages,
            },
            expected_version: topups.version,
          },
        });
        await queryClient.invalidateQueries({
          queryKey: ["stores", storeId, "modules"],
        });
      }
      return saved;
    },
    onSuccess: async (l) => {
      await queryClient.invalidateQueries({ queryKey: ["listings", storeId] });
      toast.success(`${l.title} saved`, {
        description: isTopup
          ? sendInstructions && topups?.enabled
            ? "The instructions are sent when a top-up is paid."
            : "No instructions are sent for this listing."
          : l.restock_enabled
            ? `Vault restock on, keeping ${l.restock_target ?? restock.defaultTarget} account(s) in the vault.`
            : `Delivery: ${MODES.find((m) => m.value === l.delivery_mode)?.label}`,
      });
      onClose();
    },
  });

  const { reset } = save;
  useEffect(
    () => reset(),
    [
      mode,
      poolId,
      units,
      confirmation,
      template,
      vaultInfo,
      emailDetails,
      restockOn,
      target,
      autoResume,
      reset,
    ],
  );

  return (
    <Modal
      open
      onOpenChange={(o) => !o && onClose()}
      wide
      title={listing.title}
      description={`${KIND_LABEL[listing.kind]} offer · ${money(listing.price, listing.currency)}`}
    >
      <form
        className="flex flex-col gap-5"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        {isTopup && (
          <div className="flex flex-col gap-3 rounded-md border border-line-strong p-3">
            <div className="flex items-start justify-between gap-4">
              <div>
                <div>Send the top-up instructions when an order is paid</div>
                <div className="text-[13px] text-muted">
                  The platform never marks a top-up delivered: the order waits
                  in Orders → Needs you until you do it.
                  {!topups?.enabled &&
                    " The Top-Ups module is off for this store: turn it on in Automation."}
                </div>
              </div>
              <Switch
                checked={sendInstructions}
                onCheckedChange={setSendInstructions}
                label="Send the top-up instructions"
              />
            </div>
            <TextAreaField
              label="Instructions for this listing (optional)"
              hint="Empty: the instructions set in Automation → Top-Ups. {buyer}, {product}, {quantity} and {order} are replaced."
              value={template}
              onChange={(e) => setTemplate(e.target.value)}
              rows={5}
            />
          </div>
        )}

        {!isTopup && (
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-[13px] text-muted">
              How is it delivered?
            </legend>
            {restockOn && (
              <p className="text-[13px] text-muted">
                The marketplace delivers from the vault, so delivery stays
                Manual while the vault restock is on.
              </p>
            )}
            {modes.map((m) => (
              <label
                key={m.value}
                className={`flex items-start gap-3 rounded-md border p-3 ${mode === m.value ? "border-accent bg-accent/8" : "border-line-strong"} ${restockOn && m.value !== "manual" ? "cursor-not-allowed opacity-50" : "cursor-pointer"}`}
              >
                <input
                  type="radio"
                  name="mode"
                  value={m.value}
                  checked={mode === m.value}
                  disabled={restockOn && m.value !== "manual"}
                  onChange={() => setMode(m.value)}
                  aria-label={m.label}
                  aria-describedby={`mode-help-${m.value}`}
                  className="mt-1 size-4 accent-accent"
                />
                <span>
                  <span className="font-medium">{m.label}</span>
                  <span
                    id={`mode-help-${m.value}`}
                    className="block text-[13px] text-muted"
                  >
                    {m.help}
                  </span>
                </span>
              </label>
            ))}
          </fieldset>
        )}

        {listing.kind !== "topup" && (
          <div className="flex flex-wrap gap-4">
            <div className="flex min-w-64 flex-1 flex-col gap-1.5">
              <label htmlFor="pool" className="text-[13px] text-muted">
                Stock pool
              </label>
              <select
                id="pool"
                value={poolId}
                onChange={(e) => setPoolId(e.target.value)}
                className="min-h-10 rounded-md border border-line-strong bg-bg px-3"
              >
                <option value="">No pool</option>
                {pools
                  .filter((p) => !p.archived)
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name} ({p.counts.available.toLocaleString()} available)
                    </option>
                  ))}
              </select>
            </div>
            <TextField
              label="Accounts per unit sold"
              hint="2 for a bundle of two accounts."
              type="number"
              min={1}
              max={100}
              value={units}
              onChange={(e) => setUnits(e.target.value)}
              className="w-48"
            />
          </div>
        )}

        {listing.kind === "item" && (
          <div className="flex items-start justify-between gap-4 rounded-md border border-line-strong p-3">
            <div>
              <div>Ask the buyer to confirm first</div>
              <div className="text-[13px] text-muted">
                For Twitch drops: the buyer confirms the account is linked
                before the delivery is sent.
              </div>
            </div>
            <Switch
              checked={confirmation}
              onCheckedChange={setConfirmation}
              label="Ask the buyer to confirm first"
            />
          </div>
        )}

        {!isTopup && (mode !== "manual" || template) && (
          <TextAreaField
            label={
              mode === "text"
                ? "Message to send"
                : "Delivery message (optional)"
            }
            hint={
              mode === "text"
                ? undefined
                : "Leave empty to use the store's default message. {items} is replaced by the accounts."
            }
            value={template}
            onChange={(e) => setTemplate(e.target.value)}
            rows={4}
          />
        )}

        {listing.kind === "account" && (
          <div className="flex flex-col gap-3 rounded-md border border-line-strong p-3">
            <div className="text-[13px] text-muted">
              Instant delivery (Eldorado vault)
            </div>
            <div className="flex items-start justify-between gap-4">
              <div>
                <div>Keep the vault stocked</div>
                <div className="text-[13px] text-muted">
                  Accounts from the stock pool are uploaded to the vault when it
                  runs low. Uploaded accounts leave the stock for good.
                  {!restock.enabled &&
                    " The Restock module is off for this store: turn it on in Automation."}
                </div>
              </div>
              <Switch
                checked={restockOn}
                onCheckedChange={(on) => {
                  setRestockOn(on);
                  if (on) setMode("manual");
                }}
                label="Keep the vault stocked"
              />
            </div>
            {restockOn && (
              <div className="flex flex-wrap items-end gap-4">
                <TextField
                  label="Accounts kept in the vault"
                  hint={`Empty: the store default (${restock.defaultTarget}).`}
                  type="number"
                  min={1}
                  max={100}
                  placeholder={String(restock.defaultTarget)}
                  value={target}
                  onChange={(e) => setTarget(e.target.value)}
                  className="w-56"
                />
                <label className="flex min-h-10 items-center gap-3">
                  <input
                    type="checkbox"
                    checked={autoResume}
                    onChange={(e) => setAutoResume(e.target.checked)}
                    className="size-4 accent-accent"
                  />
                  Resume the offer when it was paused with an empty vault
                </label>
              </div>
            )}
            <TextField
              label="Additional info shown to the buyer"
              value={vaultInfo}
              onChange={(e) => setVaultInfo(e.target.value)}
            />
            <label className="flex items-center gap-3">
              <input
                type="checkbox"
                checked={emailDetails}
                onChange={(e) => setEmailDetails(e.target.checked)}
                className="size-4 accent-accent"
              />
              Include the e-mail login and password (Ranked Ready accounts)
            </label>
            {listing.restock_enabled && (
              <div className="flex flex-col gap-2 border-t border-line pt-3">
                <div className="text-[13px] text-muted">Recent uploads</div>
                <RecentUploads storeId={storeId} listingId={listing.id} />
              </div>
            )}
          </div>
        )}

        {save.error && (
          <p
            role="alert"
            className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-[13px] text-danger"
          >
            {(save.error as ApiError).message}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={save.isPending}>
            Save changes
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function RestockNow({
  storeId,
  listing,
  shadow,
  target,
}: {
  storeId: number;
  listing: Listing;
  shadow: boolean;
  target: number;
}) {
  const queryClient = useQueryClient();
  return (
    <ConfirmDialog
      trigger={<Button size="sm">Restock now</Button>}
      title={`Restock ${listing.title}?`}
      description={
        shadow ? (
          <>
            The store is in shadow mode: the vault is read and the uploads are
            only recorded. Nothing is sent and no stock is used.
          </>
        ) : (
          <>
            The vault is read now and topped up to <strong>{target}</strong>{" "}
            account(s) from <strong>{listing.pool_name}</strong>. Uploaded
            accounts are sent to the marketplace and leave the stock for good.
          </>
        )
      }
      confirmLabel={shadow ? "Run in shadow mode" : "Upload to vault"}
      onConfirm={async () => {
        try {
          const r = await api<RestockResult>(
            `/stores/${storeId}/listings/${listing.id}/restock`,
            { method: "POST" },
          );
          await queryClient.invalidateQueries({
            queryKey: ["listings", storeId],
          });
          await queryClient.invalidateQueries({ queryKey: ["stock", storeId] });
          if (!r.ran)
            toast.warning("Restock did not run", { description: r.summary });
          else if (r.quarantined || /error|no available/i.test(r.summary))
            toast.warning(`${listing.title}: check the result`, {
              description: r.summary,
            });
          else
            toast.success(
              r.uploaded
                ? `${listing.title} restocked`
                : `${listing.title}: vault already full`,
              { description: r.summary },
            );
        } catch (error) {
          toast.error("Restock failed", {
            description: (error as Error).message,
          });
        }
      }}
    />
  );
}

function VaultCell({ listing, target }: { listing: Listing; target: number }) {
  const count = listing.vault_count;
  const low = count !== null && count < target;
  return (
    <div>
      <span className="font-medium">Vault restock</span>
      <span className={`tabular ${low ? "text-warning" : "text-muted"}`}>
        {" "}
        · {count ?? "?"}/{target}
      </span>
      {listing.last_restock_note && (
        <div
          className="max-w-72 truncate text-[12px] text-muted"
          title={listing.last_restock_note}
        >
          {listing.last_restock_at &&
            `${dateTime.format(new Date(listing.last_restock_at))}: `}
          {listing.last_restock_note}
        </div>
      )}
    </div>
  );
}

export function ListingsPage() {
  const { store } = useStores();
  const storeId = store?.id;
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<Listing | null>(null);
  const [editCard, setEditCard] = useState<number | null>(null);
  const listings = useQuery({
    queryKey: ["listings", storeId],
    queryFn: () => api<Listing[]>(`/stores/${storeId}/listings`),
    enabled: storeId !== undefined,
  });
  const pools = useQuery({
    queryKey: ["stock", storeId, "pools"],
    queryFn: () => api<StockPool[]>(`/stores/${storeId}/stock/pools`),
    enabled: storeId !== undefined,
  });
  const modules = useQuery({
    queryKey: ["stores", storeId, "modules"],
    queryFn: () => api<StoreModule[]>(`/stores/${storeId}/modules`),
    enabled: storeId !== undefined,
  });
  const restockModule = modules.data?.find((m) => m.key === "restock");
  const topupsModule = modules.data?.find((m) => m.key === "topups");
  const restock: RestockInfo = {
    enabled: restockModule?.enabled ?? false,
    defaultTarget: Number(restockModule?.settings.target_quantity ?? 1),
  };
  const sync = useMutation({
    mutationFn: () =>
      api<{
        created: number;
        updated: number;
        removed: number;
        errors: string[];
        linked: number;
      }>(`/stores/${storeId}/listings/sync`, { method: "POST" }),
    onSuccess: async (r) => {
      await queryClient.invalidateQueries({ queryKey: ["listings", storeId] });
      if (r.errors.length)
        toast.error("Some offers could not be read", {
          description: r.errors.join(" · "),
        });
      else
        toast.success("Offers updated from the marketplace", {
          description: `${plural(r.created, "new offer")}, ${plural(r.updated, "updated offer")}${r.removed ? `, ${r.removed} no longer on the marketplace` : ""}${r.linked ? `, ${plural(r.linked, "offer")} attached to cross-platform cards` : ""}.`,
        });
    },
    onError: (error) =>
      toast.error("Could not read the offers", { description: error.message }),
  });

  if (!store || storeId === undefined) return null;
  const rows = listings.data ?? [];
  const lastSync = rows
    .map((l) => l.last_synced_at)
    .filter(Boolean)
    .sort()
    .pop();

  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-[22px] font-semibold">Listings</h1>
          <p className="mt-1 text-muted">
            {store.name}'s offers on the marketplace and how each one is
            delivered.
            {lastSync && ` Last read ${dateTime.format(new Date(lastSync))}.`}
          </p>
        </div>
        <Button
          variant="primary"
          className="ml-auto"
          busy={sync.isPending}
          onClick={() => sync.mutate()}
        >
          Read offers from marketplace
        </Button>
      </div>

      <CrossPlatformCards
        storeId={storeId}
        shadow={store.shadow_mode}
        listings={rows}
        pools={pools.data ?? []}
        restockOn={restock.enabled}
        defaultTarget={restock.defaultTarget}
        editCardId={editCard}
        onEditDone={() => setEditCard(null)}
      />

      <Card title="All offers">
        {listings.data && rows.length === 0 ? (
          <EmptyState title="No listings yet">
            Connect the marketplace in{" "}
            <Link to="/settings" className="text-accent underline">
              Settings
            </Link>
            , then read the offers. Reading changes nothing on the marketplace.
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] border-collapse text-[13px]">
              <thead>
                <tr className="text-left text-muted">
                  <th className="px-4 py-2.5 font-medium">Offer</th>
                  <th className="px-4 py-2.5 text-right font-medium">Price</th>
                  <th className="px-4 py-2.5 text-right font-medium">
                    On marketplace
                  </th>
                  <th className="px-4 py-2.5 font-medium">Stock pool</th>
                  <th className="px-4 py-2.5 font-medium">Delivery</th>
                  <th className="px-4 py-2.5" />
                </tr>
              </thead>
              <tbody>
                {rows.map((l) => (
                  <tr
                    key={l.id}
                    className={`border-t border-line ${l.removed_remotely ? "opacity-60" : ""}`}
                  >
                    <td className="px-4 py-2.5">
                      <div className="font-medium">{l.title}</div>
                      <div className="mt-1 flex flex-wrap gap-1.5">
                        <Badge tone="neutral">{KIND_LABEL[l.kind]}</Badge>
                        {l.group_id !== null && (
                          <Badge tone="progress">
                            Card ·{" "}
                            {{ pc: "PC", playstation: "PlayStation", xbox: "Xbox" }[
                              l.device ?? "pc"
                            ]}
                          </Badge>
                        )}
                        {l.removed_remotely ? (
                          <Badge tone="danger">Gone from marketplace</Badge>
                        ) : l.remote_state === "active" ? (
                          <Badge tone="success">Active</Badge>
                        ) : (
                          <Badge tone="warning">
                            {l.remote_state === "paused" ? "Paused" : "Closed"}
                          </Badge>
                        )}
                      </div>
                    </td>
                    <td className="tabular px-4 py-2.5 text-right">
                      {money(l.price, l.currency)}
                    </td>
                    <td className="tabular px-4 py-2.5 text-right">
                      {l.remote_quantity ?? "—"}
                    </td>
                    <td className="px-4 py-2.5">
                      {l.pool_id ? (
                        <Link
                          to="/stock/$poolId"
                          params={{ poolId: String(l.pool_id) }}
                          className="hover:underline"
                        >
                          {l.pool_name}
                        </Link>
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      {l.kind === "topup" ? (
                        topupsModule?.enabled &&
                        !(
                          (topupsModule.settings.skip_offers as
                            string[] | undefined) ?? []
                        ).includes(l.remote_offer_id) ? (
                          "Top-up: instructions sent, then you"
                        ) : (
                          "Top-up: by hand, nothing sent"
                        )
                      ) : l.restock_enabled ? (
                        <VaultCell
                          listing={l}
                          target={l.restock_target ?? restock.defaultTarget}
                        />
                      ) : (
                        <>
                          {
                            MODES.find((m) => m.value === l.delivery_mode)
                              ?.label
                          }
                          {l.units_per_sale > 1 && (
                            <span className="text-muted">
                              {" "}
                              · {l.units_per_sale} per unit
                            </span>
                          )}
                          {l.require_confirmation && (
                            <span className="text-muted"> · confirmation</span>
                          )}
                        </>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <div className="flex justify-end gap-2">
                        {l.restock_enabled &&
                          restock.enabled &&
                          l.group_id === null &&
                          !l.removed_remotely && (
                            <RestockNow
                              storeId={storeId}
                              listing={l}
                              shadow={store.shadow_mode}
                              target={l.restock_target ?? restock.defaultTarget}
                            />
                          )}
                        {l.group_id !== null ? (
                          <Button size="sm" onClick={() => setEditCard(l.group_id)}>
                            Edit card
                          </Button>
                        ) : (
                          <Button size="sm" onClick={() => setEditing(l)}>
                            Edit
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {editing && (
        <EditListing
          storeId={storeId}
          listing={editing}
          pools={pools.data ?? []}
          restock={restock}
          topups={topupsModule}
          onClose={() => setEditing(null)}
        />
      )}
    </>
  );
}
