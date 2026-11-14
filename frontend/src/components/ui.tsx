/** Graphite base components. See DESIGN.md for the rules they follow. */
import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as SwitchPrimitive from "@radix-ui/react-switch";
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, TextareaHTMLAttributes } from "react";
import { forwardRef, useId, useState } from "react";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

// ---- Button ------------------------------------------------------------------------------

type Variant = "primary" | "secondary" | "ghost" | "danger";

const variants: Record<Variant, string> = {
  primary: "bg-accent text-on-accent font-semibold hover:brightness-110",
  secondary: "bg-raised text-fg border border-line-strong hover:bg-hover",
  ghost: "bg-transparent text-fg/80 hover:bg-hover hover:text-fg",
  danger: "bg-danger text-on-accent font-semibold hover:brightness-110",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  busy?: boolean;
  size?: "md" | "sm";
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", busy = false, size = "md", className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type="button"
      aria-busy={busy || undefined}
      disabled={disabled || busy}
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-md transition-[filter,background-color]",
        size === "md" ? "min-h-11 px-4 py-2.5" : "min-h-9 px-3 py-1.5 text-sm",
        "disabled:cursor-not-allowed disabled:bg-hover disabled:text-muted disabled:border-transparent disabled:filter-none",
        variants[variant],
        className,
      )}
      {...rest}
    >
      {busy && <Spinner />}
      {children}
    </button>
  );
});

export function Spinner({ className }: { className?: string }) {
  return (
    <svg
      className={cx("size-3.5 animate-spin", className)}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.4"
      aria-hidden="true"
    >
      <path d="M21 12a9 9 0 1 1-9-9" />
    </svg>
  );
}

// ---- Card --------------------------------------------------------------------------------

export function Card({
  title,
  actions,
  children,
  className,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cx("rounded-lg border border-line bg-surface", className)}>
      {(title || actions) && (
        <header className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
          {title && <h2 className="text-[15px] font-semibold">{title}</h2>}
          {actions && <div className="ml-auto flex items-center gap-2">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

// ---- Status ------------------------------------------------------------------------------

type Tone = "success" | "progress" | "warning" | "danger" | "neutral";

const toneStyles: Record<Tone, { cls: string; glyph: string }> = {
  success: { cls: "bg-success/12 text-success", glyph: "●" },
  progress: { cls: "bg-accent/14 text-accent", glyph: "◐" },
  warning: { cls: "bg-warning/14 text-warning", glyph: "▲" },
  danger: { cls: "bg-danger/14 text-danger", glyph: "✕" },
  neutral: { cls: "bg-hover text-muted", glyph: "○" },
};

/** Status is never color alone: every badge has a glyph and a label. */
export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  const t = toneStyles[tone];
  return (
    <span className={cx("inline-flex items-center gap-1.5 rounded-sm px-2.5 py-0.5 text-[13px]", t.cls)}>
      <span aria-hidden="true">{t.glyph}</span>
      {children}
    </span>
  );
}

export function StatusDot({ tone, children }: { tone: "success" | "warning" | "danger"; children: ReactNode }) {
  const color = { success: "bg-success", warning: "bg-warning", danger: "bg-danger" }[tone];
  return (
    <span className="inline-flex items-center gap-2 text-[13px] text-muted">
      <span className={cx("size-2 rounded-full", color)} aria-hidden="true" />
      {children}
    </span>
  );
}

// ---- Form fields -------------------------------------------------------------------------

const inputCls =
  "min-h-10 w-full rounded-md border bg-bg px-3 py-2 text-fg placeholder:text-muted/70 disabled:opacity-60";

interface FieldProps {
  label: string;
  hint?: string;
  error?: string | null;
}

export function TextField({
  label,
  hint,
  error,
  className,
  ...rest
}: FieldProps & InputHTMLAttributes<HTMLInputElement>) {
  const id = useId();
  return (
    <div className={cx("flex flex-col gap-1.5", className)}>
      <label htmlFor={id} className="text-[13px] text-muted">
        {label}
      </label>
      <input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={error || hint ? `${id}-msg` : undefined}
        className={cx(inputCls, error ? "border-danger" : "border-line-strong")}
        {...rest}
      />
      {(error || hint) && (
        <span id={`${id}-msg`} className={cx("text-xs", error ? "text-danger" : "text-muted")}>
          {error || hint}
        </span>
      )}
    </div>
  );
}

export function TextAreaField({
  label,
  hint,
  error,
  className,
  mono = false,
  ...rest
}: FieldProps & TextareaHTMLAttributes<HTMLTextAreaElement> & { mono?: boolean }) {
  const id = useId();
  return (
    <div className={cx("flex flex-col gap-1.5", className)}>
      <label htmlFor={id} className="text-[13px] text-muted">
        {label}
      </label>
      <textarea
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={error || hint ? `${id}-msg` : undefined}
        className={cx(
          inputCls,
          "min-h-24 resize-y leading-relaxed",
          mono && "font-mono text-[13px]",
          error ? "border-danger" : "border-line-strong",
        )}
        {...rest}
      />
      {(error || hint) && (
        <span id={`${id}-msg`} className={cx("text-xs", error ? "text-danger" : "text-muted")}>
          {error || hint}
        </span>
      )}
    </div>
  );
}

export function Switch({
  checked,
  onCheckedChange,
  disabled,
  label,
}: {
  checked: boolean;
  onCheckedChange: (value: boolean) => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <SwitchPrimitive.Root
      checked={checked}
      onCheckedChange={onCheckedChange}
      disabled={disabled}
      aria-label={label}
      className={cx(
        "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border border-line-strong transition-colors",
        "data-[state=checked]:bg-accent data-[state=unchecked]:bg-hover disabled:cursor-not-allowed disabled:opacity-50",
      )}
    >
      <SwitchPrimitive.Thumb className="block size-4.5 translate-x-0.5 rounded-full bg-fg shadow transition-transform data-[state=checked]:translate-x-[22px] data-[state=checked]:bg-on-accent" />
    </SwitchPrimitive.Root>
  );
}

// ---- Confirmation dialog -----------------------------------------------------------------

/**
 * Asks before an action that cannot be undone. The confirm button repeats the verb
 * ("Turn off shadow mode", "Log out other sessions"), never a bare "OK".
 */
export function ConfirmDialog({
  trigger,
  title,
  description,
  confirmLabel,
  tone = "primary",
  onConfirm,
}: {
  trigger: ReactNode;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  tone?: "primary" | "danger";
  onConfirm: () => Promise<unknown> | void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(v) => !busy && setOpen(v)}>
      <DialogPrimitive.Trigger asChild>{trigger}</DialogPrimitive.Trigger>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-black/60" />
        <DialogPrimitive.Content className="fixed top-1/2 left-1/2 z-50 flex w-[min(460px,calc(100vw-32px))] -translate-x-1/2 -translate-y-1/2 flex-col gap-4 rounded-lg border border-line-strong bg-raised p-6 shadow-raised">
          <DialogPrimitive.Title className="text-base font-semibold">{title}</DialogPrimitive.Title>
          <DialogPrimitive.Description className="leading-relaxed text-fg/80">
            {description}
          </DialogPrimitive.Description>
          <div className="flex justify-end gap-2">
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" disabled={busy}>
                Cancel
              </Button>
            </DialogPrimitive.Close>
            <Button
              variant={tone}
              busy={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await onConfirm();
                  setOpen(false);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {confirmLabel}
            </Button>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

// ---- Modal -------------------------------------------------------------------------------

export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  wide = false,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-black/60" />
        <DialogPrimitive.Content
          className={cx(
            "fixed top-1/2 left-1/2 z-50 flex max-h-[calc(100vh-32px)] -translate-x-1/2 -translate-y-1/2 flex-col gap-4 overflow-y-auto rounded-lg border border-line-strong bg-raised p-6 shadow-raised",
            wide ? "w-[min(760px,calc(100vw-32px))]" : "w-[min(480px,calc(100vw-32px))]",
          )}
        >
          <DialogPrimitive.Title className="text-base font-semibold">{title}</DialogPrimitive.Title>
          {description ? (
            <DialogPrimitive.Description className="text-fg/80">{description}</DialogPrimitive.Description>
          ) : (
            <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
          )}
          {children}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

// ---- Empty state -------------------------------------------------------------------------

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-start gap-2 px-4 py-10 sm:items-center sm:text-center">
      <p className="font-semibold">{title}</p>
      {children && <div className="max-w-md text-muted">{children}</div>}
    </div>
  );
}
