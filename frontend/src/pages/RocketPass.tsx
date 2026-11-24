/**
 * Tools → Rocket Pass check. Reads, claims and Xbox-syncs the Rocket Pass of Epic accounts.
 * Not tied to a store. Checks run in the background; this page follows them.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { toast } from "sonner";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  Modal,
  TextAreaField,
} from "@/components/ui";
import { api } from "@/lib/api";
import { copyText } from "@/lib/stock";

type Kind = "check" | "claim" | "xbox";
type State = "waiting" | "queued" | "running" | "done" | "failed";

interface PassResult {
  has_premium: boolean;
  tier: number;
  xp_boost_percent: number;
  pips: number;
  pips_per_level: number;
  season_ends_at: string | null;
  newly_activated?: boolean;
  claimed_products?: number;
  attempts?: number;
  xbox_gamertag?: string;
}

interface EpicAccount {
  id: number;
  display_name: string;
  account_id: string;
  signed_in: boolean;
  xbox_gamertag: string | null;
  xbox_linked: boolean;
  last_result: PassResult | null;
  last_checked_at: string | null;
}

interface PassCheck {
  id: number;
  epic_account_id: number | null;
  display_name: string;
  kind: Kind;
  state: State;
  progress: string | null;
  result: PassResult | null;
  error: string | null;
  actor: string;
  xbox_user_code: string | null;
  xbox_verification_uri: string | null;
  created_at: string;
  finished_at: string | null;
}

const BASE = "/tools/rocket-pass";
// The Epic login that returns the authorization code (Epic Games Launcher client).
const EPIC_LOGIN_URL =
  "https://www.epicgames.com/id/login?redirectUrl=https%3A%2F%2Fwww.epicgames.com%2Fid%2Fapi%2Fredirect%3FclientId%3D34a02cf8f4414e29b15921876da36f9a%26responseType%3Dcode";
const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});
const KIND_LABEL: Record<Kind, string> = {
  check: "Check",
  claim: "Claim pass",
  xbox: "Xbox sync",
};
const ACTIVE: State[] = ["waiting", "queued", "running"];

function seasonLeft(iso: string | null): string {
  if (!iso) return "Season end unknown";
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return "Season ended";
  const days = Math.floor(ms / 86_400_000);
  const hours = Math.floor((ms % 86_400_000) / 3_600_000);
  return days > 0
    ? `Season ends in ${days}d ${hours}h`
    : `Season ends in ${hours}h`;
}

function PassSummary({ result }: { result: PassResult }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px]">
      <Badge tone={result.has_premium ? "success" : "neutral"}>
        {result.has_premium ? "Premium pass" : "Free pass only"}
      </Badge>
      <span className="tabular">Tier {result.tier}</span>
      <span className="tabular text-muted">+{result.xp_boost_percent}% XP</span>
      <span className="text-muted">{seasonLeft(result.season_ends_at)}</span>
    </div>
  );
}

/** A button that copies a text and shows "Copied" for a moment. */
function CopyButton({
  text,
  label,
  size,
}: {
  text: string;
  label: string;
  size?: "sm";
}) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1800);
    return () => window.clearTimeout(t);
  }, [copied]);
  return (
    <Button
      type="button"
      size={size}
      variant={copied ? "selected" : "secondary"}
      aria-live="polite"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
        } catch {
          toast.error("Could not copy", {
            description: "The browser blocked the clipboard.",
          });
        }
      }}
    >
      {copied ? (
        <span className="inline-flex items-center gap-1.5 text-success">
          <svg
            aria-hidden="true"
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M5 12.5l4.5 4.5L19 7.5" />
          </svg>
          Copied
        </span>
      ) : (
        label
      )}
    </Button>
  );
}

const RULE = "-".repeat(62);

function seasonEnd(iso: string | null): string {
  if (!iso) return "Unknown";
  const target = new Date(iso);
  const date = `${target.toISOString().replace("T", " ").slice(0, 19)} UTC`;
  const diff = Math.floor((target.getTime() - Date.now()) / 1000);
  if (diff <= 0) return `${date} (Season Ended)`;
  const days = Math.floor(diff / 86400);
  const hours = Math.floor((diff % 86400) / 3600);
  const minutes = Math.floor((diff % 3600) / 60);
  const parts = [
    ...(days > 0 ? [`${days}d`] : []),
    ...(hours > 0 || days > 0 ? [`${hours}h`] : []),
    `${minutes}m`,
  ];
  return `${date} (in ${parts.join(" ")})`;
}

/** The old checker's text report, line by line. */
function reportLines(
  account: EpicAccount,
  r: PassResult,
  claimed: boolean,
): { text: string; claim: boolean }[] {
  const xp = r.xp_boost_percent;
  return [
    { text: `Account Display Name : ${account.display_name}`, claim: false },
    { text: `Epic Account ID      : ${account.account_id}`, claim: false },
    { text: RULE, claim: false },
    ...(claimed
      ? [
          {
            text: "🎉 Entitlement Claim  : ✅ ACTIVATED VIA FORTNITE CREW SYNC!",
            claim: true,
          },
        ]
      : []),
    {
      text: `🎫 Rocket Pass Status : ${r.has_premium ? "✅ ACTIVE (PREMIUM PASS OWNED)" : "❌ INACTIVE (FREE TIER ONLY)"}`,
      claim: false,
    },
    { text: `🏆 Current Tier Level : Tier ${r.tier}`, claim: false },
    {
      text: `⚡ XP Boost Multiplier: +${xp}% (${(1 + xp / 100).toFixed(2)}x XP)`,
      claim: false,
    },
    {
      text: `📊 Progress           : ${r.pips} Pips (${r.pips_per_level} Pips/Tier)`,
      claim: false,
    },
    {
      text: `⏳ Season End Date    : ${seasonEnd(r.season_ends_at)}`,
      claim: false,
    },
  ];
}

function PassReport({
  account,
  claimed,
  onClose,
}: {
  account: EpicAccount;
  claimed: boolean;
  onClose: () => void;
}) {
  const r = account.last_result;
  if (!r) return null;
  const lines = reportLines(account, r, claimed);
  const copy = lines
    .filter((l) => !l.claim)
    .map((l) => l.text)
    .join("\n");
  return (
    <Modal
      open
      onOpenChange={(o) => !o && onClose()}
      title={`Rocket Pass · ${account.display_name}`}
      wide
    >
      <pre className="overflow-x-auto rounded-md border border-line bg-bg p-4 font-mono text-[12.5px] leading-6 whitespace-pre">
        {lines.map((l) => l.text).join("\n")}
      </pre>
      <div className="flex items-center gap-3">
        <span className="text-[12px] text-muted">
          The copy leaves out the Entitlement Claim line
        </span>
        <span className="ml-auto flex gap-2">
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          <CopyButton text={copy} label="Copy text" />
        </span>
      </div>
    </Modal>
  );
}

function SignIn({ onStarted }: { onStarted: (c: PassCheck) => void }) {
  const queryClient = useQueryClient();
  const [code, setCode] = useState("");
  const add = useMutation({
    mutationFn: () =>
      api<PassCheck>(`${BASE}/accounts`, { method: "POST", body: { code } }),
    onSuccess: async (check) => {
      setCode("");
      await queryClient.invalidateQueries({ queryKey: ["rocket-pass"] });
      toast.success(`${check.display_name} added`, {
        description: "Checking the pass now.",
      });
      onStarted(check);
    },
    onError: (error) =>
      toast.error("Could not add the account", { description: error.message }),
  });
  return (
    <Card title="Add an Epic account">
      <form
        className="flex flex-col gap-4 p-4"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          add.mutate();
        }}
      >
        <div>
          <CopyButton text={EPIC_LOGIN_URL} label="Copy Epic login link" />
        </div>
        <TextAreaField
          label="Authorization code or JSON"
          hint="The 32-character code, or the whole JSON with “authorizationCode”."
          mono
          rows={4}
          value={code}
          onChange={(e) => setCode(e.target.value)}
          autoComplete="off"
          autoFocus
        />
        <div className="flex justify-end">
          <Button
            type="submit"
            variant="primary"
            busy={add.isPending}
            disabled={code.trim().length < 8}
          >
            Add and check
          </Button>
        </div>
      </form>
    </Card>
  );
}

function ActiveCheck({ check }: { check: PassCheck }) {
  if (check.state === "waiting" && check.xbox_user_code) {
    return (
      <div className="flex flex-col gap-3 rounded-md border border-warning/40 bg-warning/6 p-4">
        <p className="font-medium">
          Approve the Xbox sign-in for {check.display_name}
        </p>
        <p className="text-[13px] text-muted">
          Open the Microsoft page, sign in with the Xbox account that has
          Fortnite Crew and enter this code. The check continues on its own once
          it is approved.
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <span className="tabular rounded-md border border-line-strong bg-raised px-3 py-1.5 font-mono text-[20px] tracking-wider">
            {check.xbox_user_code}
          </span>
          <Button
            size="sm"
            onClick={() => void copyText(check.xbox_user_code ?? "", "Code")}
          >
            Copy code
          </Button>
          <Button
            size="sm"
            variant="primary"
            onClick={() =>
              window.open(
                check.xbox_verification_uri ?? "https://microsoft.com/link",
                "_blank",
                "noopener",
              )
            }
          >
            Open Microsoft sign-in
          </Button>
        </div>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-3 rounded-md border border-line bg-raised/50 px-4 py-3 text-[13px]">
      <span
        aria-hidden="true"
        className="size-2 shrink-0 animate-pulse rounded-full bg-accent"
      />
      <span className="font-medium">{check.display_name}</span>
      <span className="text-muted">{KIND_LABEL[check.kind]}</span>
      <span className="ml-auto text-muted">{check.progress ?? "Queued"}</span>
    </div>
  );
}

function AccountRow({
  account,
  busy,
  start,
  claimed,
}: {
  account: EpicAccount;
  busy: boolean;
  start: (kind: Kind) => void;
  claimed: boolean;
}) {
  const queryClient = useQueryClient();
  const [report, setReport] = useState(false);
  const forget = async (xboxOnly: boolean) => {
    try {
      await api(
        `${BASE}/accounts/${account.id}${xboxOnly ? "?xbox_only=true" : ""}`,
        { method: "DELETE" },
      );
      await queryClient.invalidateQueries({ queryKey: ["rocket-pass"] });
      toast.success(
        xboxOnly ? "Xbox unlinked" : `${account.display_name} forgotten`,
      );
    } catch (error) {
      toast.error("Could not change the account", {
        description: (error as Error).message,
      });
    }
  };
  return (
    <li className="flex flex-col gap-3 px-4 py-3.5 2xl:flex-row 2xl:items-center">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-3">
          <span className="font-medium">{account.display_name}</span>
          {account.xbox_gamertag && (
            <span className="text-[12px] text-muted">
              Xbox: {account.xbox_gamertag}
            </span>
          )}
          {!account.signed_in && <Badge tone="warning">Sign in again</Badge>}
        </div>
        <div className="mt-1.5">
          {account.last_result ? (
            <PassSummary result={account.last_result} />
          ) : (
            <span className="text-[13px] text-muted">Not checked yet</span>
          )}
        </div>
        {account.last_checked_at && (
          <div className="mt-1 text-[12px] text-muted">
            Checked {dateTime.format(new Date(account.last_checked_at))}
          </div>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {account.last_result?.has_premium && (
          <Button size="sm" variant="primary" onClick={() => setReport(true)}>
            Show report
          </Button>
        )}
        <Button
          size="sm"
          disabled={busy || !account.signed_in}
          onClick={() => start("check")}
        >
          Check
        </Button>
        <Button
          size="sm"
          disabled={busy || !account.signed_in}
          onClick={() => start("claim")}
        >
          Claim Fortnite Crew pass
        </Button>
        <Button
          size="sm"
          disabled={busy || !account.signed_in}
          onClick={() => start("xbox")}
        >
          {account.xbox_linked ? "Xbox sync" : "Link Xbox and sync"}
        </Button>
        {account.xbox_linked && (
          <ConfirmDialog
            trigger={
              <Button size="sm" variant="ghost">
                Unlink Xbox
              </Button>
            }
            title={`Unlink Xbox from ${account.display_name}?`}
            description="The saved Microsoft sign-in is deleted. Linking again asks for a new code."
            confirmLabel="Unlink Xbox"
            onConfirm={() => forget(true)}
          />
        )}
        <ConfirmDialog
          trigger={
            <Button size="sm" variant="ghost">
              Forget
            </Button>
          }
          title={`Forget ${account.display_name}?`}
          description="Its saved Epic and Xbox sign-ins are deleted. The check history stays."
          confirmLabel="Forget account"
          tone="danger"
          onConfirm={() => forget(false)}
        />
      </div>
      {report && (
        <PassReport
          account={account}
          claimed={claimed}
          onClose={() => setReport(false)}
        />
      )}
    </li>
  );
}

export function RocketPassPage() {
  const queryClient = useQueryClient();
  const accounts = useQuery({
    queryKey: ["rocket-pass", "accounts"],
    queryFn: () => api<EpicAccount[]>(`${BASE}/accounts`),
  });
  const checks = useQuery({
    queryKey: ["rocket-pass", "checks"],
    queryFn: () => api<PassCheck[]>(`${BASE}/checks?limit=50`),
    refetchInterval: (q) =>
      q.state.data?.some((c) => ACTIVE.includes(c.state)) ? 1500 : false,
  });
  const active = (checks.data ?? []).filter((c) => ACTIVE.includes(c.state));
  const busyAccounts = new Set(active.map((c) => c.epic_account_id));

  // Tell the owner when a check they started finishes, and refresh the accounts.
  const seen = useRef(new Map<number, State>());
  useEffect(() => {
    let finished = false;
    for (const c of checks.data ?? []) {
      const before = seen.current.get(c.id);
      if (before && ACTIVE.includes(before) && !ACTIVE.includes(c.state)) {
        finished = true;
        if (c.state === "failed")
          toast.error(`${KIND_LABEL[c.kind]} failed for ${c.display_name}`, {
            description: c.error ?? undefined,
          });
        else if (c.result?.newly_activated)
          toast.success(`Premium pass activated on ${c.display_name}`);
        else if (c.result)
          toast.success(
            `${c.display_name}: ${c.result.has_premium ? "Premium pass" : "free pass only"}`,
          );
      }
      seen.current.set(c.id, c.state);
    }
    if (finished)
      void queryClient.invalidateQueries({
        queryKey: ["rocket-pass", "accounts"],
      });
  }, [checks.data, queryClient]);

  const start = useMutation({
    mutationFn: ({ id, kind }: { id: number; kind: Kind }) =>
      api<PassCheck>(`${BASE}/accounts/${id}/checks`, {
        method: "POST",
        body: { kind },
      }),
    onSuccess: (check) => {
      seen.current.set(check.id, check.state);
      void queryClient.invalidateQueries({
        queryKey: ["rocket-pass", "checks"],
      });
    },
    onError: (error) =>
      toast.error("Could not start", { description: error.message }),
  });

  return (
    <>
      <div>
        <h1 className="text-[22px] font-medium">Rocket Pass check</h1>
        <p className="mt-1 text-muted">
          Read the Rocket Pass of an Epic account, claim a Fortnite Crew pass,
          or sync it from Xbox. Not tied to a store.
        </p>
      </div>

      {active.length > 0 && (
        <section aria-label="Running" className="flex flex-col gap-2">
          {active.map((c) => (
            <ActiveCheck key={c.id} check={c} />
          ))}
        </section>
      )}

      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card title="Accounts">
          {accounts.data?.length ? (
            <ul className="divide-y divide-line">
              {accounts.data.map((a) => (
                <AccountRow
                  key={a.id}
                  account={a}
                  busy={
                    busyAccounts.has(a.id) ||
                    (start.isPending && start.variables?.id === a.id)
                  }
                  start={(kind) => start.mutate({ id: a.id, kind })}
                  claimed={(checks.data ?? []).some(
                    (c) =>
                      c.epic_account_id === a.id &&
                      c.result?.newly_activated === true,
                  )}
                />
              ))}
            </ul>
          ) : (
            <EmptyState
              title={accounts.isLoading ? "Loading…" : "No accounts yet"}
            >
              {!accounts.isLoading && "Add an Epic account to check its pass."}
            </EmptyState>
          )}
        </Card>
        <SignIn onStarted={(c) => seen.current.set(c.id, c.state)} />
      </div>

      <Card title="History">
        {checks.data?.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] border-collapse text-[13px]">
              <thead>
                <tr className="text-left text-muted">
                  <th className="px-4 py-2.5 font-medium">When</th>
                  <th className="px-2 py-2.5 font-medium">Account</th>
                  <th className="px-2 py-2.5 font-medium">Action</th>
                  <th className="px-2 py-2.5 font-medium">Result</th>
                  <th className="px-4 py-2.5 font-medium">By</th>
                </tr>
              </thead>
              <tbody>
                {checks.data.map((c) => (
                  <tr key={c.id} className="border-t border-line align-top">
                    <td className="tabular px-4 py-2.5 whitespace-nowrap text-muted">
                      {dateTime.format(new Date(c.created_at))}
                    </td>
                    <td className="px-2 py-2.5">{c.display_name}</td>
                    <td className="px-2 py-2.5">{KIND_LABEL[c.kind]}</td>
                    <td className="px-2 py-2.5">
                      {c.state === "done" && c.result ? (
                        <span className="flex flex-col gap-0.5">
                          <PassSummary result={c.result} />
                          {c.result.newly_activated && (
                            <span className="text-success">
                              Pass activated by the claim
                            </span>
                          )}
                          {c.kind !== "check" && !c.result.has_premium && (
                            <span className="text-muted">
                              No pending pass arrived after{" "}
                              {c.result.attempts ?? 0} attempts
                            </span>
                          )}
                        </span>
                      ) : c.state === "failed" ? (
                        <span className="text-danger">{c.error}</span>
                      ) : (
                        <span className="text-muted">
                          {c.progress ?? "Queued"}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-muted">{c.actor}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title={checks.isLoading ? "Loading…" : "No checks yet"} />
        )}
      </Card>
    </>
  );
}
