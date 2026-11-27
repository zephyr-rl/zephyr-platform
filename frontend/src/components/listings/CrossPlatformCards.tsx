/** Cross-platform cards: one product sold as a PC, PlayStation and Xbox offer, one stock pool. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  Modal,
  Switch,
  TextAreaField,
  TextField,
} from "@/components/ui";
import { api, type Listing, type ListingCard, type StockPool } from "@/lib/api";

const PLATFORMS = [
  { key: "pc", label: "PC" },
  { key: "playstation", label: "PlayStation" },
  { key: "xbox", label: "Xbox" },
] as const;

type PlatformKey = (typeof PLATFORMS)[number]["key"];

const selectCls = "min-h-10 rounded-md border border-line-strong bg-bg px-3";

function stateBadge(d: ListingCard["devices"][number]) {
  if (!d.remote_offer_id) return <span className="text-muted">No offer</span>;
  if (!d.listing_id)
    return (
      <span
        className="text-warning"
        title="Read the offers from the marketplace to attach it"
      >
        Not read yet
      </span>
    );
  if (d.removed_remotely) return <Badge tone="danger">Gone</Badge>;
  if (d.remote_state === "active") return <Badge tone="success">Active</Badge>;
  return (
    <Badge tone="warning">
      {d.remote_state === "paused" ? "Paused" : "Closed"}
    </Badge>
  );
}

type TrimPlatform = {
  device: string;
  label: string;
  offer_state: string;
  in_vault: number;
  keep: number;
  remove: string[];
  known: number;
  removed: number;
  returned: number;
  quarantined: number;
  errors: string[];
};
type TrimResult = {
  pool: string;
  done: boolean;
  to_remove: number;
  platforms: TrimPlatform[];
};

/** Pull back extras: vaults down to a number per platform, the rest back to the pool. */
function PullBack({
  card,
  storeId,
  onClose,
  onDone,
}: {
  card: ListingCard;
  storeId: number;
  onClose: () => void;
  onDone: () => Promise<void>;
}) {
  const devices = card.devices.filter((d) => d.listing_id);
  const [keep, setKeep] = useState<Record<string, string>>(
    Object.fromEntries(
      devices.map((d) => [d.device, String(card.effective_target)]),
    ),
  );
  const [plan, setPlan] = useState<TrimResult | null>(null);
  const [busy, setBusy] = useState(false);
  const url = `/stores/${storeId}/listing-groups/${card.id}/trim`;
  const body = (apply: boolean) => ({
    keep: Object.fromEntries(
      Object.entries(keep).map(([k, v]) => [k, Number(v)]),
    ),
    apply,
  });
  const valid = Object.values(keep).every(
    (v) => /^\d+$/.test(v) && Number(v) >= 1,
  );
  const run = async (apply: boolean) => {
    setBusy(true);
    try {
      const r = await api<TrimResult>(url, {
        method: "POST",
        body: body(apply),
      });
      setPlan(r);
      if (apply) {
        await onDone();
        const back = r.platforms.reduce((n, p) => n + p.returned, 0);
        const held = r.platforms.reduce((n, p) => n + p.quarantined, 0);
        toast.success(`${back} accounts back in ${r.pool}`, {
          description: held
            ? `${held} quarantined: Eldorado did not confirm the removal, check they were not sold.`
            : undefined,
        });
      }
    } catch (error) {
      toast.error(apply ? "Pull back failed" : "Could not read the vaults", {
        description: (error as Error).message,
      });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open
      onOpenChange={(o) => !o && onClose()}
      title="Pull back extras"
      description={`Accounts above the number you keep are taken out of each vault and return to ${card.pool_name ?? "the pool"} as available. Keep at least 1: Eldorado does not allow an empty instant-delivery offer.`}
      wide
    >
      <div className="grid grid-cols-3 gap-3">
        {devices.map((d) => (
          <TextField
            key={d.device}
            label={`Keep on ${d.label}`}
            inputMode="numeric"
            value={keep[d.device] ?? ""}
            onChange={(e) => {
              setPlan(null);
              setKeep({ ...keep, [d.device]: e.target.value.trim() });
            }}
          />
        ))}
      </div>
      {plan && (
        <table className="w-full border-collapse text-[13px]">
          <thead>
            <tr className="text-left text-muted">
              <th className="py-1.5 font-normal">Platform</th>
              <th className="py-1.5 text-right font-normal">In vault</th>
              <th className="py-1.5 text-right font-normal">Keep</th>
              <th className="py-1.5 text-right font-normal">
                {plan.done ? "Back in pool" : "To pull back"}
              </th>
              <th className="py-1.5 pl-4 font-normal">Accounts</th>
            </tr>
          </thead>
          <tbody>
            {plan.platforms.map((p) => (
              <tr key={p.device} className="border-t border-line align-top">
                <td className="py-2 font-medium">
                  {p.label}
                  {p.offer_state !== "active" && (
                    <span className="ml-2 text-warning">{p.offer_state}</span>
                  )}
                </td>
                <td className="tabular py-2 text-right">{p.in_vault}</td>
                <td className="tabular py-2 text-right">{p.keep}</td>
                <td className="tabular py-2 text-right">
                  {plan.done ? p.returned : p.remove.length}
                </td>
                <td className="py-2 pl-4 text-muted">
                  {p.remove.join(", ") || "—"}
                  {p.errors.map((e) => (
                    <div key={e} className="text-danger">
                      {e}
                    </div>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="flex justify-end gap-2">
        <Button onClick={onClose}>{plan?.done ? "Close" : "Cancel"}</Button>
        {!plan?.done && (
          <Button onClick={() => run(false)} disabled={!valid || busy}>
            {busy && !plan ? "Reading vaults…" : "Preview"}
          </Button>
        )}
        {plan && !plan.done && plan.to_remove > 0 && (
          <ConfirmDialog
            trigger={
              <Button variant="primary" disabled={busy}>
                Pull back {plan.to_remove}
              </Button>
            }
            title={`Pull back ${plan.to_remove} accounts?`}
            description={`They are removed from the Eldorado vaults and return to ${plan.pool}. The vaults are read again first, so recent sales are taken into account.`}
            confirmLabel="Pull back"
            onConfirm={() => run(true)}
          />
        )}
      </div>
    </Modal>
  );
}

function CardView({
  card,
  storeId,
  shadow,
  restockOn,
  onEdit,
}: {
  card: ListingCard;
  storeId: number;
  shadow: boolean;
  restockOn: boolean;
  onEdit: () => void;
}) {
  const queryClient = useQueryClient();
  const refresh = async () => {
    await queryClient.invalidateQueries({
      queryKey: ["listing-groups", storeId],
    });
    await queryClient.invalidateQueries({ queryKey: ["listings", storeId] });
    await queryClient.invalidateQueries({ queryKey: ["stock", storeId] });
  };
  const toggle = useMutation({
    mutationFn: (enabled: boolean) =>
      api<ListingCard>(`/stores/${storeId}/listing-groups/${card.id}/enabled`, {
        method: "POST",
        body: { enabled },
      }),
    onSuccess: async (c) => {
      await refresh();
      toast.success(
        `${c.title.slice(0, 50)}: restock ${c.restock_enabled ? "on" : "off"}`,
      );
    },
    onError: (e) =>
      toast.error("Could not change the card", { description: e.message }),
  });
  const empty = card.pool_available === 0;
  const [pulling, setPulling] = useState(false);
  return (
    <section
      className="flex flex-col gap-3 rounded-lg border border-line bg-surface p-4"
      aria-label={card.title}
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="line-clamp-2 font-medium" title={card.title}>
            {card.title}
          </h3>
          <div className="mt-1 text-[13px] text-muted">
            {card.pool_id ? (
              <>
                <Link
                  to="/stock/$poolId"
                  params={{ poolId: String(card.pool_id) }}
                  className="hover:underline"
                >
                  {card.pool_name}
                </Link>{" "}
                ·{" "}
                <span className={empty ? "font-medium text-danger" : ""}>
                  {card.pool_available} available
                </span>
              </>
            ) : (
              <span className="text-warning">No stock pool chosen</span>
            )}
            {" · "}target {card.effective_target} per platform
          </div>
        </div>
        <Switch
          checked={card.restock_enabled}
          onCheckedChange={(v) => toggle.mutate(v)}
          disabled={toggle.isPending || !card.pool_id}
          label={`Restock ${card.title}`}
        />
      </div>
      <table className="w-full border-collapse text-[13px]">
        <tbody>
          {card.devices.map((d) => {
            const low =
              d.vault_count !== null && d.vault_count < card.effective_target;
            return (
              <tr key={d.device} className="border-t border-line">
                <td className="w-28 py-2 font-medium">{d.label}</td>
                <td className="py-2">{stateBadge(d)}</td>
                <td
                  className="tabular py-2 text-right"
                  title="Accounts in the vault / target"
                >
                  {d.listing_id ? (
                    <span className={low ? "text-warning" : ""}>
                      {d.vault_count ?? "?"}/{card.effective_target}
                    </span>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {card.devices.some((d) => d.last_restock_note) && (
        <p className="text-[12px] text-muted">
          {card.devices
            .filter((d) => d.last_restock_note)
            .map((d) => `${d.label}: ${d.last_restock_note}`)
            .join(" · ")}
        </p>
      )}
      <div className="mt-auto flex flex-wrap justify-end gap-2">
        {card.restock_enabled && restockOn && (
          <ConfirmDialog
            trigger={<Button size="sm">Restock now</Button>}
            title={`Restock ${card.title.slice(0, 60)}?`}
            description={
              shadow
                ? "The store is in shadow mode: each vault is read and the uploads are only recorded. Nothing is sent and no stock is used."
                : `Each platform's vault is topped up to ${card.effective_target} from ${card.pool_name}. Uploaded accounts are sent to the marketplace and leave the stock for good.`
            }
            confirmLabel={shadow ? "Run in shadow mode" : "Upload to vaults"}
            onConfirm={async () => {
              try {
                const r = await api<{
                  results: {
                    label: string;
                    summary: string;
                    uploaded: number;
                  }[];
                }>(`/stores/${storeId}/listing-groups/${card.id}/restock`, {
                  method: "POST",
                });
                await refresh();
                toast.success(`${card.title.slice(0, 50)}: checked`, {
                  description: r.results
                    .map((x) => `${x.label}: ${x.summary}`)
                    .join(" · "),
                });
              } catch (error) {
                toast.error("Restock failed", {
                  description: (error as Error).message,
                });
              }
            }}
          />
        )}
        {card.pool_id && card.devices.some((d) => d.listing_id) && (
          <Button
            size="sm"
            onClick={() => setPulling(true)}
            title="Take accounts above a number out of the vaults, back into the pool"
          >
            Pull back extras
          </Button>
        )}
        <Button size="sm" onClick={onEdit}>
          Edit
        </Button>
      </div>
      {pulling && (
        <PullBack
          card={card}
          storeId={storeId}
          onClose={() => setPulling(false)}
          onDone={refresh}
        />
      )}
    </section>
  );
}

function EditCard({
  storeId,
  card,
  listings,
  pools,
  defaultTarget,
  onClose,
}: {
  storeId: number;
  card: ListingCard | null;
  listings: Listing[];
  pools: StockPool[];
  defaultTarget: number;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState(card?.title ?? "");
  const [poolId, setPoolId] = useState(
    card?.pool_id ? String(card.pool_id) : "",
  );
  const [target, setTarget] = useState(
    card?.restock_target ? String(card.restock_target) : "",
  );
  const [enabled, setEnabled] = useState(card?.restock_enabled ?? false);
  const [info, setInfo] = useState(card?.vault_additional_info ?? "");
  const [email, setEmail] = useState(card?.include_email_details ?? false);
  const [resume, setResume] = useState(card?.auto_resume ?? true);
  const initial = Object.fromEntries(
    PLATFORMS.map((p) => [
      p.key,
      String(card?.devices.find((d) => d.device === p.key)?.listing_id ?? ""),
    ]),
  ) as Record<PlatformKey, string>;
  const [devices, setDevices] = useState<Record<PlatformKey, string>>(initial);
  const offers = listings.filter(
    (l) =>
      l.kind === "account" &&
      !l.removed_remotely &&
      (l.group_id === null || l.group_id === card?.id),
  );
  const waiting =
    card?.devices.filter((d) => d.remote_offer_id && !d.listing_id) ?? [];

  const refresh = async () => {
    await queryClient.invalidateQueries({
      queryKey: ["listing-groups", storeId],
    });
    await queryClient.invalidateQueries({ queryKey: ["listings", storeId] });
  };
  const save = useMutation({
    mutationFn: () => {
      const body = {
        title: title.trim(),
        pool_id: poolId ? Number(poolId) : null,
        restock_enabled: enabled,
        restock_target: target ? Number(target) : null,
        auto_resume: resume,
        vault_additional_info: info.trim() || null,
        include_email_details: email,
        // Platforms waiting for their offer to be read are left out, so they keep it.
        devices: Object.fromEntries(
          PLATFORMS.filter(
            (p) => devices[p.key] || !waiting.some((w) => w.device === p.key),
          ).map((p) => [p.key, devices[p.key] ? Number(devices[p.key]) : null]),
        ),
      };
      return card
        ? api<ListingCard>(`/stores/${storeId}/listing-groups/${card.id}`, {
            method: "PUT",
            body,
          })
        : api<ListingCard>(`/stores/${storeId}/listing-groups`, {
            method: "POST",
            body,
          });
    },
    onSuccess: async (c) => {
      await refresh();
      toast.success(card ? "Card saved" : "Card created", {
        description: c.title.slice(0, 80),
      });
      onClose();
    },
    onError: (e) =>
      toast.error("Could not save the card", { description: e.message }),
  });
  const remove = async () => {
    if (!card) return;
    await api(`/stores/${storeId}/listing-groups/${card.id}`, {
      method: "DELETE",
    });
    await refresh();
    toast.success("Card deleted", {
      description: "Its offers stay, with restock off.",
    });
    onClose();
  };

  function submit(e: FormEvent) {
    e.preventDefault();
    save.mutate();
  }

  return (
    <Modal
      open
      onOpenChange={(o) => !o && onClose()}
      wide
      title={card ? "Edit cross-platform card" : "New cross-platform card"}
      description="The PC, PlayStation and Xbox offers of one product, all filled from the same stock pool with the same vault settings."
    >
      <form className="flex flex-col gap-4" onSubmit={submit}>
        <TextField
          label="Name"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          required
          maxLength={300}
        />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {PLATFORMS.map((p) => {
            const wait = waiting.find((w) => w.device === p.key);
            return (
              <div key={p.key} className="flex flex-col gap-1.5">
                <label
                  htmlFor={`card-${p.key}`}
                  className="text-[13px] text-muted"
                >
                  {p.label} offer
                </label>
                <select
                  id={`card-${p.key}`}
                  value={devices[p.key]}
                  onChange={(e) =>
                    setDevices({ ...devices, [p.key]: e.target.value })
                  }
                  className={selectCls}
                >
                  <option value="">
                    {wait ? "Waiting: offer not read yet" : "None"}
                  </option>
                  {offers.map((l) => (
                    <option key={l.id} value={l.id}>
                      {l.title.slice(0, 70)} · {l.price} {l.currency}
                    </option>
                  ))}
                </select>
              </div>
            );
          })}
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="card-pool" className="text-[13px] text-muted">
              Stock pool
            </label>
            <select
              id="card-pool"
              value={poolId}
              onChange={(e) => setPoolId(e.target.value)}
              className={selectCls}
            >
              <option value="">No pool</option>
              {pools
                .filter((p) => !p.archived)
                .map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.counts.available ?? 0} available)
                  </option>
                ))}
            </select>
          </div>
          <TextField
            label="Accounts kept in each vault"
            type="number"
            min={1}
            max={100}
            placeholder={`Default: ${defaultTarget}`}
            value={target}
            onChange={(e) => setTarget(e.target.value)}
          />
        </div>
        <TextAreaField
          label="Extra text uploaded with each account (optional)"
          hint={`${info.length}/1000 · shown to the buyer with the login, on every platform.`}
          value={info}
          maxLength={1000}
          rows={5}
          onChange={(e) => setInfo(e.target.value)}
        />
        <div className="flex flex-col gap-3 rounded-md border border-line-strong p-3">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div>Restock the vaults automatically</div>
              <div className="text-[13px] text-muted">
                Each platform's vault is kept at the target from the pool. Needs
                the Restock module on.
              </div>
            </div>
            <Switch
              checked={enabled}
              onCheckedChange={setEnabled}
              label="Restock automatically"
            />
          </div>
          <div className="flex items-center justify-between gap-4">
            <div>
              <div>Include the e-mail details</div>
              <div className="text-[13px] text-muted">
                For accounts stored as login:password:email:email password.
              </div>
            </div>
            <Switch
              checked={email}
              onCheckedChange={setEmail}
              label="Include the e-mail details"
            />
          </div>
          <div className="flex items-center justify-between gap-4">
            <div>
              <div>Resume a paused offer after uploading</div>
            </div>
            <Switch
              checked={resume}
              onCheckedChange={setResume}
              label="Resume paused offers"
            />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {card && (
            <ConfirmDialog
              trigger={
                <Button type="button" variant="ghost">
                  Delete card
                </Button>
              }
              title="Delete this card?"
              description="The offers stay as they are on the marketplace, with restock off. Nothing is uploaded or removed."
              confirmLabel="Delete card"
              tone="danger"
              onConfirm={remove}
            />
          )}
          <Button type="button" className="ml-auto" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            busy={save.isPending}
            disabled={!title.trim()}
          >
            {card ? "Save card" : "Create card"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function CrossPlatformCards({
  storeId,
  shadow,
  listings,
  pools,
  restockOn,
  defaultTarget,
  editCardId,
  onEditDone,
  query = "",
}: {
  storeId: number;
  shadow: boolean;
  listings: Listing[];
  pools: StockPool[];
  restockOn: boolean;
  defaultTarget: number;
  /** Only the cards whose name contains these words. */
  query?: string;
  /** Open this card's editor (from a member row in the listings table). */
  editCardId?: number | null;
  onEditDone?: () => void;
}) {
  const cards = useQuery({
    queryKey: ["listing-groups", storeId],
    queryFn: () => api<ListingCard[]>(`/stores/${storeId}/listing-groups`),
  });
  const [editing, setEditing] = useState<ListingCard | "new" | null>(null);
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const all = cards.data ?? [];
  const list = all.filter((c) =>
    words.every((w) => c.title.toLowerCase().includes(w)),
  );
  const fromRow = editCardId ? all.find((c) => c.id === editCardId) : undefined;
  const open = fromRow ?? editing;
  const close = () => {
    setEditing(null);
    onEditDone?.();
  };

  return (
    <Card
      title="Cross-platform cards"
      actions={
        <Button size="sm" onClick={() => setEditing("new")}>
          New card
        </Button>
      }
    >
      {list.length === 0 && all.length > 0 ? (
        <p className="px-4 py-4 text-[13px] text-muted">
          No card matches the search.
        </p>
      ) : list.length === 0 ? (
        <p className="px-4 py-4 text-[13px] text-muted">
          One product sold as a PC, PlayStation and Xbox offer, all filled from
          one stock pool. None yet.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-3 p-4 lg:grid-cols-2 2xl:grid-cols-3">
          {list.map((c) => (
            <CardView
              key={c.id}
              card={c}
              storeId={storeId}
              shadow={shadow}
              restockOn={restockOn}
              onEdit={() => setEditing(c)}
            />
          ))}
        </div>
      )}
      {open && (
        <EditCard
          key={open === "new" ? "new" : open.id}
          storeId={storeId}
          card={open === "new" ? null : open}
          listings={listings}
          pools={pools}
          defaultTarget={defaultTarget}
          onClose={close}
        />
      )}
    </Card>
  );
}
