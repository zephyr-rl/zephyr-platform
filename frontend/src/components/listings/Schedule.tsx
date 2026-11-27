/** Offer schedule: pause an offer every day at one time and turn it back on at another. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { Button, Modal, TextField } from "@/components/ui";
import { api, type Listing } from "@/lib/api";

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;

/** Typed as digits, shown as 24-hour HH:MM ("130" → "1:30" → "01:30" on leaving the field). */
function typed(raw: string): string {
  const digits = raw.replace(/\D/g, "").slice(0, 4);
  return digits.length <= 2
    ? digits
    : `${digits.slice(0, -2)}:${digits.slice(-2)}`;
}

function padded(value: string): string {
  const m = /^(\d{1,2}):?(\d{2})$/.exec(value) ?? /^(\d{1,2})$/.exec(value);
  if (!m) return value;
  return `${(m[1] ?? "").padStart(2, "0")}:${m[2] ?? "00"}`;
}

export function scheduleLabel(listing: Listing): string | null {
  if (!listing.schedule_off || !listing.schedule_on) return null;
  return `Paused ${listing.schedule_off}–${listing.schedule_on}`;
}

/** One line under the offer when it has a schedule. */
export function ScheduleLine({ listing }: { listing: Listing }) {
  const label = scheduleLabel(listing);
  if (!label) return null;
  return (
    <p
      className="text-[12px] text-muted"
      title={listing.schedule_note ?? "No scheduled change yet"}
    >
      Schedule: {label} every day
    </p>
  );
}

export function ScheduleButton({
  storeId,
  listing,
}: {
  storeId: number;
  listing: Listing;
}) {
  const [open, setOpen] = useState(false);
  if (listing.removed_remotely) return null;
  return (
    <>
      <Button
        size="sm"
        variant={scheduleLabel(listing) ? "selected" : "secondary"}
        onClick={() => setOpen(true)}
        title={
          scheduleLabel(listing) ?? "Pause and turn on this offer at set times"
        }
      >
        Schedule
      </Button>
      {open && (
        <ScheduleDialog
          storeId={storeId}
          listing={listing}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

function ScheduleDialog({
  storeId,
  listing,
  onClose,
}: {
  storeId: number;
  listing: Listing;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [off, setOff] = useState(listing.schedule_off ?? "01:00");
  const [on, setOn] = useState(listing.schedule_on ?? "09:00");
  const save = useMutation({
    mutationFn: (body: { off: string | null; on: string | null }) =>
      api<Listing>(`/stores/${storeId}/listings/${listing.id}/schedule`, {
        method: "PUT",
        body,
      }),
    onSuccess: async (l) => {
      await queryClient.invalidateQueries({ queryKey: ["listings", storeId] });
      toast.success(
        scheduleLabel(l)
          ? `${l.title.slice(0, 50)}: ${scheduleLabel(l)} every day`
          : `${l.title.slice(0, 50)}: schedule removed`,
      );
      onClose();
    },
    onError: (e) =>
      toast.error("Could not save the schedule", { description: e.message }),
  });
  const offOk = HHMM.test(padded(off));
  const onOk = HHMM.test(padded(on));
  const valid = offOk && onOk && padded(off) !== padded(on);
  return (
    <Modal
      open
      onOpenChange={(o) => !o && onClose()}
      title="Schedule"
      description={
        <>
          Every day the offer is paused at the first time and turned back on at
          the second, in the store&apos;s time zone. The window can cross
          midnight. If you pause or turn it on by hand in between, that stays
          until the next scheduled time.
        </>
      }
    >
      <p className="line-clamp-2 text-[13px] font-medium" title={listing.title}>
        {listing.title}
      </p>
      <div className="grid grid-cols-2 gap-3">
        <TextField
          label="Pause at (24 h)"
          placeholder="HH:MM"
          inputMode="numeric"
          maxLength={5}
          value={off}
          onChange={(e) => setOff(typed(e.target.value))}
          onBlur={() => setOff(padded(off))}
        />
        <TextField
          label="Turn on at (24 h)"
          placeholder="HH:MM"
          inputMode="numeric"
          maxLength={5}
          value={on}
          onChange={(e) => setOn(typed(e.target.value))}
          onBlur={() => setOn(padded(on))}
        />
      </div>
      {(off !== "" && !offOk) || (on !== "" && !onOk) ? (
        <p className="text-[13px] text-danger">
          Use 24-hour time, from 00:00 to 23:59.
        </p>
      ) : (
        offOk &&
        padded(off) === padded(on) && (
          <p className="text-[13px] text-danger">The two times must differ.</p>
        )
      )}
      {listing.schedule_note && (
        <p className="text-[12px] text-muted">
          Last change: {listing.schedule_note}
        </p>
      )}
      <div className="flex justify-between gap-2">
        {scheduleLabel(listing) ? (
          <Button
            variant="ghost"
            onClick={() => save.mutate({ off: null, on: null })}
            disabled={save.isPending}
          >
            Remove schedule
          </Button>
        ) : (
          <span />
        )}
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            busy={save.isPending}
            disabled={!valid}
            onClick={() => save.mutate({ off: padded(off), on: padded(on) })}
          >
            Save
          </Button>
        </div>
      </div>
    </Modal>
  );
}
