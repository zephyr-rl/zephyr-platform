/**
 * Store-wide switches shown in Listings: automatic deliveries (every tab), automatic restock
 * (Accounts) and top-up instructions (Top-ups). Each one is the store's module setting.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";
import {
  Button,
  ConfirmDialog,
  Modal,
  Switch,
  TextAreaField,
} from "@/components/ui";
import { api, type StoreModule } from "@/lib/api";

const INTERVALS = [1, 2, 3, 5, 10, 15, 30, 60];

function useModuleUpdate(storeId: number, module: StoreModule | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (change: {
      enabled?: boolean;
      settings?: Record<string, unknown>;
    }) => {
      if (!module)
        throw new Error("This setting is not available for the store.");
      return api<StoreModule>(`/stores/${storeId}/modules/${module.key}`, {
        method: "PUT",
        body: {
          ...(change.enabled !== undefined ? { enabled: change.enabled } : {}),
          // The whole settings object is saved: keep everything else as it is.
          ...(change.settings
            ? { settings: { ...module.settings, ...change.settings } }
            : {}),
          expected_version: module.version,
        },
      });
    },
    onSuccess: (updated) => {
      queryClient.setQueryData<StoreModule[]>(
        ["stores", storeId, "modules"],
        (old) => old?.map((m) => (m.key === updated.key ? updated : m)),
      );
    },
    onError: async (e) => {
      await queryClient.invalidateQueries({
        queryKey: ["stores", storeId, "modules"],
      });
      toast.error("Could not save the change", { description: e.message });
    },
  });
}

function Box({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-center gap-2 rounded-md border border-line-strong px-3 py-1.5 text-[13px]">
      {children}
    </div>
  );
}

/** Turning a switch on in a live store asks first; off is immediate. */
function GuardedSwitch({
  checked,
  label,
  live,
  busy,
  confirmTitle,
  confirmText,
  confirmLabel,
  onChange,
}: {
  checked: boolean;
  label: string;
  live: boolean;
  busy: boolean;
  confirmTitle: string;
  confirmText: string;
  confirmLabel: string;
  onChange: (value: boolean) => Promise<unknown>;
}) {
  if (checked || !live)
    return (
      <Switch
        checked={checked}
        disabled={busy}
        onCheckedChange={(v) => void onChange(v)}
        label={label}
      />
    );
  return (
    <ConfirmDialog
      trigger={
        <Button size="sm" variant="primary">
          Turn on
        </Button>
      }
      title={confirmTitle}
      description={confirmText}
      confirmLabel={confirmLabel}
      onConfirm={() => onChange(true)}
    />
  );
}

export function DeliveriesSwitch({
  storeId,
  module,
  shadow,
}: {
  storeId: number;
  module?: StoreModule;
  shadow: boolean;
}) {
  const update = useModuleUpdate(storeId, module);
  if (!module) return null;
  const on = module.settings.auto_deliver !== false;
  return (
    <Box>
      <span>
        Automatic deliveries{" "}
        <span className={on ? "text-success" : "text-warning"}>
          {on ? "on" : "paused"}
        </span>
      </span>
      <GuardedSwitch
        checked={on}
        label="Automatic deliveries"
        live={!shadow}
        busy={update.isPending}
        confirmTitle="Resume automatic deliveries?"
        confirmText="Paid orders of offers set to Automatic are delivered to the buyers again. Orders held while paused stay in Orders → Needs you."
        confirmLabel="Resume deliveries"
        onChange={async (v) => {
          await update.mutateAsync({ settings: { auto_deliver: v } });
          toast.success(
            v ? "Automatic deliveries on" : "Automatic deliveries paused",
            {
              description: v
                ? shadow
                  ? "Shadow mode: deliveries are only recorded."
                  : "Paid orders of automatic offers are delivered."
                : "Every paid order waits for your approval in Orders → Needs you.",
            },
          );
        }}
      />
    </Box>
  );
}

export function RestockSwitch({
  storeId,
  module,
  shadow,
}: {
  storeId: number;
  module?: StoreModule;
  shadow: boolean;
}) {
  const update = useModuleUpdate(storeId, module);
  if (!module) return null;
  const every = Number(module.settings.interval_minutes ?? 1);
  const options = INTERVALS.includes(every)
    ? INTERVALS
    : [...INTERVALS, every].sort((a, b) => a - b);
  return (
    <Box>
      <span>
        Automatic restock{" "}
        <span className={module.enabled ? "text-success" : "text-muted"}>
          {module.enabled ? "on" : "off"}
        </span>
      </span>
      <label className="flex items-center gap-1 text-muted">
        every
        <select
          aria-label="Restock check interval"
          value={every}
          disabled={update.isPending}
          onChange={async (e) => {
            const minutes = Number(e.target.value);
            await update.mutateAsync({
              settings: { interval_minutes: minutes },
            });
            toast.success(`Vaults checked every ${minutes} min`);
          }}
          className="rounded-md border border-line-strong bg-bg px-1.5 py-0.5 text-fg"
        >
          {options.map((m) => (
            <option key={m} value={m}>
              {m} min
            </option>
          ))}
        </select>
      </label>
      <GuardedSwitch
        checked={module.enabled}
        label="Automatic restock"
        live={!shadow}
        busy={update.isPending}
        confirmTitle="Turn on automatic restock?"
        confirmText="Every vault with restock on (cards and single offers) is topped up from its stock pool. Uploaded accounts are sent to Eldorado and leave the stock for good."
        confirmLabel="Turn on restock"
        onChange={async (v) => {
          await update.mutateAsync({ enabled: v });
          toast.success(v ? "Automatic restock on" : "Automatic restock off", {
            description: v
              ? shadow
                ? "Shadow mode: the uploads are only recorded."
                : "The vaults are topped up from the stock pools."
              : "Nothing is uploaded until you turn it on again. The vaults keep what they have.",
          });
        }}
      />
    </Box>
  );
}

export function TopupsSwitch({
  storeId,
  module,
  shadow,
}: {
  storeId: number;
  module?: StoreModule;
  shadow: boolean;
}) {
  const update = useModuleUpdate(storeId, module);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  if (!module) return null;
  const instructions = String(module.settings.instructions ?? "");
  return (
    <>
      <Box>
        <span>
          Top-up instructions{" "}
          <span className={module.enabled ? "text-success" : "text-muted"}>
            {module.enabled ? "on" : "off"}
          </span>
        </span>
        <GuardedSwitch
          checked={module.enabled}
          label="Send top-up instructions"
          live={!shadow}
          busy={update.isPending}
          confirmTitle="Send top-up instructions?"
          confirmText="When a top-up is paid, the buyer gets the instructions (the offer's own message or the general one). Offers set to 'by hand' get nothing."
          confirmLabel="Turn on instructions"
          onChange={async (v) => {
            await update.mutateAsync({ enabled: v });
            toast.success(
              v ? "Top-up instructions on" : "Top-up instructions off",
            );
          }}
        />
      </Box>
      <Button
        onClick={() => {
          setText(instructions);
          setEditing(true);
        }}
      >
        General message
      </Button>
      {editing && (
        <Modal
          open
          onOpenChange={(o) => !o && setEditing(false)}
          wide
          title="General top-up message"
          description="Sent to the buyer when a top-up is paid, for offers without their own message."
        >
          <form
            className="flex flex-col gap-4"
            onSubmit={async (e) => {
              e.preventDefault();
              await update.mutateAsync({ settings: { instructions: text } });
              toast.success("General top-up message saved");
              setEditing(false);
            }}
          >
            <TextAreaField
              label="Message"
              hint="{buyer}, {product}, {quantity} and {order} are replaced."
              rows={8}
              value={text}
              onChange={(e) => setText(e.target.value)}
              required
            />
            <div className="flex justify-end gap-2">
              <Button type="button" onClick={() => setEditing(false)}>
                Cancel
              </Button>
              <Button
                type="submit"
                variant="primary"
                busy={update.isPending}
                disabled={!text.trim()}
              >
                Save message
              </Button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}
