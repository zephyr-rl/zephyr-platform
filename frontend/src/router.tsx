import type { QueryClient } from "@tanstack/react-query";
import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  Outlet,
  redirect,
} from "@tanstack/react-router";
import { Toaster } from "sonner";
import { AppShell } from "@/components/AppShell";
import { meQuery } from "@/lib/session";
import { AutomationPage } from "@/pages/Automation";
import { ListingsPage } from "@/pages/Listings";
import { LoginPage } from "@/pages/Login";
import { MessagesPage } from "@/pages/Messages";
import { NotificationsPage } from "@/pages/Notifications";
import { RocketPassPage } from "@/pages/RocketPass";
import { OrdersPage } from "@/pages/Orders";
import { OverviewPage } from "@/pages/Overview";
import { RemindersPage } from "@/pages/Reminders";
import { ReportsPage } from "@/pages/Reports";
import { SettingsPage } from "@/pages/Settings";
import { StockPage } from "@/pages/Stock";
import { StockPoolPage } from "@/pages/StockPool";

interface RouterContext {
  queryClient: QueryClient;
}

const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: () => (
    <>
      <Outlet />
      <Toaster
        position="bottom-right"
        theme="dark"
        closeButton
        toastOptions={{
          classNames: {
            toast: "!bg-raised !border !border-line-strong !text-fg !rounded-lg !shadow-raised",
            description: "!text-muted",
            actionButton: "!bg-accent !text-on-accent",
          },
        }}
      />
    </>
  ),
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  beforeLoad: async ({ context }) => {
    if (await context.queryClient.ensureQueryData(meQuery)) throw redirect({ to: "/" });
  },
  component: LoginPage,
});

const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "app",
  beforeLoad: async ({ context }) => {
    if (!(await context.queryClient.ensureQueryData(meQuery))) throw redirect({ to: "/login" });
  },
  component: AppShell,
});

const routeTree = rootRoute.addChildren([
  loginRoute,
  appRoute.addChildren([
    createRoute({ getParentRoute: () => appRoute, path: "/", component: OverviewPage }),
    createRoute({ getParentRoute: () => appRoute, path: "/automation", component: AutomationPage }),
    createRoute({ getParentRoute: () => appRoute, path: "/settings", component: SettingsPage }),
    createRoute({
      getParentRoute: () => appRoute,
      path: "/orders",
      component: OrdersPage,
      validateSearch: (s: Record<string, unknown>): { order?: number; listing?: number } => ({
        ...(Number(s.order) > 0 ? { order: Number(s.order) } : {}),
        ...(Number(s.listing) > 0 ? { listing: Number(s.listing) } : {}),
      }),
    }),
    createRoute({ getParentRoute: () => appRoute, path: "/stock", component: StockPage }),
    createRoute({
      getParentRoute: () => appRoute,
      path: "/stock/$poolId",
      component: StockPoolPage,
      validateSearch: (s: Record<string, unknown>): { item?: number; hint?: string } =>
        Number(s.item) > 0 ? { item: Number(s.item), hint: typeof s.hint === "string" ? s.hint : undefined } : {},
    }),
    createRoute({
      getParentRoute: () => appRoute,
      path: "/listings",
      component: ListingsPage,
      validateSearch: (s: Record<string, unknown>): { tab?: "accounts" | "items" | "topups" } =>
        s.tab === "items" || s.tab === "topups" ? { tab: s.tab } : {},
    }),
    createRoute({ getParentRoute: () => appRoute, path: "/messages", component: MessagesPage }),
    createRoute({ getParentRoute: () => appRoute, path: "/reports", component: ReportsPage }),
    createRoute({ getParentRoute: () => appRoute, path: "/reminders", component: RemindersPage }),
    createRoute({ getParentRoute: () => appRoute, path: "/notifications", component: NotificationsPage }),
    createRoute({ getParentRoute: () => appRoute, path: "/tools/rocket-pass", component: RocketPassPage }),
  ]),
]);

export function makeRouter(queryClient: QueryClient) {
  return createRouter({ routeTree, context: { queryClient }, defaultPreload: "intent" });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof makeRouter>;
  }
}
