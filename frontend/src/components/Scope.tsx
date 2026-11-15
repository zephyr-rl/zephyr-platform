/** "This store / All stores" switch for the pages that can show the stores together. */
import { useState } from "react";
import { Button } from "@/components/ui";

export type Scope = "store" | "all";

export function useScope(page: string): [Scope, (s: Scope) => void] {
  const key = `zp.scope.${page}`;
  const [scope, setScope] = useState<Scope>(() => {
    try {
      return window.localStorage.getItem(key) === "all" ? "all" : "store";
    } catch {
      return "store";
    }
  });
  const set = (s: Scope) => {
    setScope(s);
    try {
      window.localStorage.setItem(key, s);
    } catch {
      /* storage unavailable: the choice lasts until the page is closed */
    }
  };
  return [scope, set];
}

export function ScopeSwitch({
  scope,
  onChange,
  storeName,
}: {
  scope: Scope;
  onChange: (s: Scope) => void;
  storeName: string;
}) {
  return (
    <div className="flex gap-1" role="group" aria-label="Show">
      <Button
        size="sm"
        variant={scope === "store" ? "primary" : "ghost"}
        aria-pressed={scope === "store"}
        onClick={() => onChange("store")}
      >
        {storeName}
      </Button>
      <Button
        size="sm"
        variant={scope === "all" ? "primary" : "ghost"}
        aria-pressed={scope === "all"}
        onClick={() => onChange("all")}
      >
        All stores
      </Button>
    </div>
  );
}
