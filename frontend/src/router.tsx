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
import { Card, EmptyState } from "@/components/ui";
import { meQuery } from "@/lib/session";
import { AutomationPage } from "@/pages/Automation";
import { LoginPage } from "@/pages/Login";
import { OverviewPage } from "@/pages/Overview";
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

function comingSoon(path: string, title: string, stage: string) {
  return createRoute({
    getParentRoute: () => appRoute,
    path,
    component: () => (
      <>
        <h1 className="text-[22px] font-semibold">{title}</h1>
        <Card>
          <EmptyState title={`${title} arrives in ${stage}`}>See PROGRESS.md for the roadmap.</EmptyState>
        </Card>
      </>
    ),
  });
}

const routeTree = rootRoute.addChildren([
  loginRoute,
  appRoute.addChildren([
    createRoute({ getParentRoute: () => appRoute, path: "/", component: OverviewPage }),
    createRoute({ getParentRoute: () => appRoute, path: "/automation", component: AutomationPage }),
    createRoute({ getParentRoute: () => appRoute, path: "/settings", component: SettingsPage }),
    comingSoon("/orders", "Orders", "Stage 5"),
    createRoute({ getParentRoute: () => appRoute, path: "/stock", component: StockPage }),
    createRoute({ getParentRoute: () => appRoute, path: "/stock/$poolId", component: StockPoolPage }),
    comingSoon("/listings", "Listings", "Stage 4"),
    comingSoon("/messages", "Messages", "Stage 7"),
    comingSoon("/reports", "Reports", "Stage 7"),
    comingSoon("/notifications", "Notifications", "Stage 7"),
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
