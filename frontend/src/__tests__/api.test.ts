import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, setCsrfToken } from "@/lib/api";

describe("ApiError", () => {
  it("uses a plain detail message", () => {
    expect(new ApiError(401, "Not signed in.").message).toBe("Not signed in.");
  });

  it("joins validation errors by field", () => {
    const error = new ApiError(422, [
      { loc: ["body", "settings", "delay_hours"], msg: "must be at least 1" },
      { loc: ["body", "name"], msg: "required" },
    ]);
    expect(error.message).toBe("delay_hours: must be at least 1; name: required");
  });
});

describe("api()", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setCsrfToken(null);
  });

  it("sends the CSRF token on writes only", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("tok");
    await api("/stores");
    await api("/stores", { method: "POST", body: { name: "x" } });
    const [, getInit] = fetchMock.mock.calls[0]!;
    const [, postInit] = fetchMock.mock.calls[1]!;
    expect(getInit.headers["X-CSRF-Token"]).toBeUndefined();
    expect(postInit.headers["X-CSRF-Token"]).toBe("tok");
    expect(postInit.credentials).toBe("same-origin");
  });

  it("throws ApiError with the server detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Too many failed attempts." }), { status: 429 })),
    );
    await expect(api("/auth/login", { method: "POST", body: {} })).rejects.toMatchObject({
      status: 429,
      message: "Too many failed attempts.",
    });
  });
});
