import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Badge, Button, Card, EmptyState, Modal, Switch, TextAreaField, TextField } from "@/components/ui";
import { api, ApiError, type DeliveryMode, type Listing, type StockPool } from "@/lib/api";
import { useStores } from "@/lib/session";
import { plural } from "@/lib/stock";

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

const KIND_LABEL: Record<Listing["kind"], string> = { account: "Account", item: "Item", topup: "Top-up" };

const MODES: { value: DeliveryMode; label: string; help: string }[] = [
  { value: "manual", label: "Manual", help: "Nothing is sent automatically. You deliver it yourself." },
  { value: "assisted", label: "Assisted", help: "Stock is set aside when the order is paid; you approve the delivery." },
  { value: "auto", label: "Automatic", help: "Stock is delivered as soon as the order is paid." },
  { value: "text", label: "Message only", help: "Only the message below is sent; no stock is used." },
];

function money(amount: string, currency: string): string {
  return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(Number(amount));
}

function EditListing({ storeId, listing, pools, onClose }: { storeId: number; listing: Listing; pools: StockPool[]; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<DeliveryMode>(listing.delivery_mode);
  const [poolId, setPoolId] = useState(listing.pool_id ? String(listing.pool_id) : "");
  const [units, setUnits] = useState(String(listing.units_per_sale));
  const [confirmation, setConfirmation] = useState(listing.require_confirmation);
  const [template, setTemplate] = useState(listing.delivery_template ?? "");
  const [vaultInfo, setVaultInfo] = useState(listing.vault_additional_info ?? "");
  const [emailDetails, setEmailDetails] = useState(listing.include_email_details);
  const modes = listing.kind === "topup" ? MODES.filter((m) => m.value === "manual" || m.value === "text") : MODES;

  const save = useMutation({
    mutationFn: () =>
      api<Listing>(`/stores/${storeId}/listings/${listing.id}`, {
        method: "PATCH",
        body: {
          delivery_mode: mode,
          ...(poolId ? { pool_id: Number(poolId) } : { clear_pool: true }),
          units_per_sale: Number(units),
          require_confirmation: confirmation,
          delivery_template: template,
          vault_additional_info: vaultInfo,
          include_email_details: emailDetails,
        },
      }),
    onSuccess: async (l) => {
      await queryClient.invalidateQueries({ queryKey: ["listings", storeId] });
      toast.success(`${l.title} saved`, { description: `Delivery: ${MODES.find((m) => m.value === l.delivery_mode)?.label}` });
      onClose();
    },
  });

  const { reset } = save;
  useEffect(() => reset(), [mode, poolId, units, confirmation, template, vaultInfo, emailDetails, reset]);

  return (
    <Modal open onOpenChange={(o) => !o && onClose()} wide title={listing.title} description={`${KIND_LABEL[listing.kind]} offer · ${money(listing.price, listing.currency)}`}>
      <form
        className="flex flex-col gap-5"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-2 text-[13px] text-muted">How is it delivered?</legend>
          {modes.map((m) => (
            <label key={m.value} className={`flex cursor-pointer items-start gap-3 rounded-md border p-3 ${mode === m.value ? "border-accent bg-accent/8" : "border-line-strong"}`}>
              <input
                type="radio"
                name="mode"
                value={m.value}
                checked={mode === m.value}
                onChange={() => setMode(m.value)}
                aria-label={m.label}
                aria-describedby={`mode-help-${m.value}`}
                className="mt-1 size-4 accent-accent"
              />
              <span>
                <span className="font-medium">{m.label}</span>
                <span id={`mode-help-${m.value}`} className="block text-[13px] text-muted">
                  {m.help}
                </span>
              </span>
            </label>
          ))}
        </fieldset>

        {listing.kind !== "topup" && (
          <div className="flex flex-wrap gap-4">
            <div className="flex min-w-64 flex-1 flex-col gap-1.5">
              <label htmlFor="pool" className="text-[13px] text-muted">
                Stock pool
              </label>
              <select id="pool" value={poolId} onChange={(e) => setPoolId(e.target.value)} className="min-h-10 rounded-md border border-line-strong bg-bg px-3">
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
            <TextField label="Accounts per unit sold" hint="2 for a bundle of two accounts." type="number" min={1} max={100} value={units} onChange={(e) => setUnits(e.target.value)} className="w-48" />
          </div>
        )}

        {listing.kind === "item" && (
          <div className="flex items-start justify-between gap-4 rounded-md border border-line-strong p-3">
            <div>
              <div>Ask the buyer to confirm first</div>
              <div className="text-[13px] text-muted">For Twitch drops: the buyer confirms the account is linked before the delivery is sent.</div>
            </div>
            <Switch checked={confirmation} onCheckedChange={setConfirmation} label="Ask the buyer to confirm first" />
          </div>
        )}

        {(mode !== "manual" || template) && (
          <TextAreaField
            label={mode === "text" ? "Message to send" : "Delivery message (optional)"}
            hint={mode === "text" ? undefined : "Leave empty to use the store's default message. {items} is replaced by the accounts."}
            value={template}
            onChange={(e) => setTemplate(e.target.value)}
            rows={4}
          />
        )}

        {listing.kind === "account" && (
          <div className="flex flex-col gap-3 rounded-md border border-line-strong p-3">
            <div className="text-[13px] text-muted">Instant delivery (Eldorado vault)</div>
            <TextField label="Additional info shown to the buyer" value={vaultInfo} onChange={(e) => setVaultInfo(e.target.value)} />
            <label className="flex items-center gap-3">
              <input type="checkbox" checked={emailDetails} onChange={(e) => setEmailDetails(e.target.checked)} className="size-4 accent-accent" />
              Include the e-mail login and password (Ranked Ready accounts)
            </label>
          </div>
        )}

        {save.error && (
          <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-[13px] text-danger">
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

export function ListingsPage() {
  const { store } = useStores();
  const storeId = store?.id;
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<Listing | null>(null);
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
  const sync = useMutation({
    mutationFn: () => api<{ created: number; updated: number; removed: number; errors: string[] }>(`/stores/${storeId}/listings/sync`, { method: "POST" }),
    onSuccess: async (r) => {
      await queryClient.invalidateQueries({ queryKey: ["listings", storeId] });
      if (r.errors.length) toast.error("Some offers could not be read", { description: r.errors.join(" · ") });
      else
        toast.success("Offers updated from the marketplace", {
          description: `${plural(r.created, "new offer")}, ${plural(r.updated, "updated offer")}${r.removed ? `, ${r.removed} no longer on the marketplace` : ""}.`,
        });
    },
    onError: (error) => toast.error("Could not read the offers", { description: error.message }),
  });

  if (!store || storeId === undefined) return null;
  const rows = listings.data ?? [];
  const lastSync = rows.map((l) => l.last_synced_at).filter(Boolean).sort().pop();

  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-[22px] font-semibold">Listings</h1>
          <p className="mt-1 text-muted">
            {store.name}'s offers on the marketplace and how each one is delivered.
            {lastSync && ` Last read ${dateTime.format(new Date(lastSync))}.`}
          </p>
        </div>
        <Button variant="primary" className="ml-auto" busy={sync.isPending} onClick={() => sync.mutate()}>
          Read offers from marketplace
        </Button>
      </div>

      <Card>
        {listings.data && rows.length === 0 ? (
          <EmptyState title="No listings yet">
            Connect the marketplace in <Link to="/settings" className="text-accent underline">Settings</Link>, then read the offers. Reading
            changes nothing on the marketplace.
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] border-collapse text-[13px]">
              <thead>
                <tr className="text-left text-muted">
                  <th className="px-4 py-2.5 font-medium">Offer</th>
                  <th className="px-4 py-2.5 text-right font-medium">Price</th>
                  <th className="px-4 py-2.5 text-right font-medium">On marketplace</th>
                  <th className="px-4 py-2.5 font-medium">Stock pool</th>
                  <th className="px-4 py-2.5 font-medium">Delivery</th>
                  <th className="px-4 py-2.5" />
                </tr>
              </thead>
              <tbody>
                {rows.map((l) => (
                  <tr key={l.id} className={`border-t border-line ${l.removed_remotely ? "opacity-60" : ""}`}>
                    <td className="px-4 py-2.5">
                      <div className="font-medium">{l.title}</div>
                      <div className="mt-1 flex flex-wrap gap-1.5">
                        <Badge tone="neutral">{KIND_LABEL[l.kind]}</Badge>
                        {l.removed_remotely ? (
                          <Badge tone="danger">Gone from marketplace</Badge>
                        ) : l.remote_state === "active" ? (
                          <Badge tone="success">Active</Badge>
                        ) : (
                          <Badge tone="warning">{l.remote_state === "paused" ? "Paused" : "Closed"}</Badge>
                        )}
                      </div>
                    </td>
                    <td className="tabular px-4 py-2.5 text-right">{money(l.price, l.currency)}</td>
                    <td className="tabular px-4 py-2.5 text-right">{l.remote_quantity ?? "—"}</td>
                    <td className="px-4 py-2.5">
                      {l.pool_id ? (
                        <Link to="/stock/$poolId" params={{ poolId: String(l.pool_id) }} className="hover:underline">
                          {l.pool_name}
                        </Link>
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      {MODES.find((m) => m.value === l.delivery_mode)?.label}
                      {l.units_per_sale > 1 && <span className="text-muted"> · {l.units_per_sale} per unit</span>}
                      {l.require_confirmation && <span className="text-muted"> · confirmation</span>}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <Button size="sm" onClick={() => setEditing(l)}>
                        Edit
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {editing && <EditListing storeId={storeId} listing={editing} pools={pools.data ?? []} onClose={() => setEditing(null)} />}
    </>
  );
}
