import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ApiError } from "@/lib/api";
import { makeRouter } from "@/router";
import "@/styles/index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      refetchOnWindowFocus: true,
    },
  },
});

// A request that comes back 401 means the session ended (expired or revoked elsewhere).
queryClient.getQueryCache().subscribe((event) => {
  const error = event.query.state.error;
  if (error instanceof ApiError && error.status === 401 && event.query.queryKey[0] !== "me") {
    queryClient.setQueryData(["me"], null);
    void router.navigate({ to: "/login" });
  }
});

const router = makeRouter(queryClient);

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root element");
createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
