import { queryOptions, useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { api, ApiError, setCsrfToken, type Me, type Store } from "./api";

export const meQuery = queryOptions({
  queryKey: ["me"],
  queryFn: async () => {
    try {
      const me = await api<Me>("/auth/me");
      setCsrfToken(me.csrf_token);
      return me;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return null;
      throw error;
    }
  },
  staleTime: 60_000,
});

export const storesQuery = queryOptions({
  queryKey: ["stores"],
  queryFn: () => api<Store[]>("/stores"),
});

// ---- selected store ----------------------------------------------------------------------

const STORAGE_KEY = "zp.selectedStore";

function readStored(): number | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value ? Number(value) : null;
  } catch {
    return null;
  }
}

function writeStored(id: number): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, String(id));
  } catch {
    // Storage unavailable (private mode): the choice lasts for this visit only.
  }
}

interface StoreContextValue {
  stores: Store[];
  store: Store | null;
  select: (id: number) => void;
  loading: boolean;
}

const StoreContext = createContext<StoreContextValue | null>(null);

export function StoreProvider({ children }: { children: ReactNode }) {
  const { data: stores = [], isLoading } = useQuery(storesQuery);
  const [selectedId, setSelectedId] = useState<number | null>(readStored);

  const store = useMemo(
    () => stores.find((s) => s.id === selectedId) ?? stores[0] ?? null,
    [stores, selectedId],
  );

  useEffect(() => {
    if (store && store.id !== selectedId) setSelectedId(store.id);
  }, [store, selectedId]);

  const value = useMemo<StoreContextValue>(
    () => ({
      stores,
      store,
      loading: isLoading,
      select: (id) => {
        writeStored(id);
        setSelectedId(id);
      },
    }),
    [stores, store, isLoading],
  );
  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
}

export function useStores(): StoreContextValue {
  const value = useContext(StoreContext);
  if (!value) throw new Error("useStores must be used inside StoreProvider");
  return value;
}
