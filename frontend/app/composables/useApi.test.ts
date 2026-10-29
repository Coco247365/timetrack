// useApi ruft navigateTo/useRoute als Nuxt-Auto-Imports bare auf ->
// environment:'nuxt' (vitest.config.ts) + mockNuxtImport an der Navigations-
// Grenze, analog zu useAuth.test.ts (dort wird useApi selbst gemockt; hier
// testen wir die echte Implementierung). $fetch ist bei Nuxt kein
// unimport-Auto-Import, sondern ein echtes globalThis.$fetch (von ofetch
// gesetzt) -> per vi.stubGlobal gemockt, nicht per mockNuxtImport.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mockNuxtImport } from "@nuxt/test-utils/runtime";
import { navigateTo } from "#app";

type FetchOpts = { method?: string; credentials?: string; body?: unknown; headers?: Record<string, string> };
type Unauthorized = { response: { status: number } };

const fetchMock = vi.fn<(url: string, opts?: FetchOpts) => Promise<unknown>>();
const routeState = { path: "/mitarbeiter" };

vi.stubGlobal("$fetch", fetchMock);
mockNuxtImport("navigateTo", () => vi.fn());
mockNuxtImport("useRoute", () => () => routeState);

function unauthorized(): Unauthorized {
  return { response: { status: 401 } };
}

describe("useApi", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    routeState.path = "/mitarbeiter";
    const { token } = useApi();
    token.value = null;
    vi.mocked(navigateTo).mockClear();
  });

  it("401 auf normalem Call -> ein Refresh + ein Retry -> Erfolg", async () => {
    fetchMock.mockImplementation(async (url, opts) => {
      if (url.includes("/auth/refresh")) return { access_token: "new-token" };
      if (opts?.headers?.Authorization === "Bearer new-token") return { ok: true };
      throw unauthorized();
    });

    const { apiFetch, token } = useApi();
    const result = await apiFetch<{ ok: boolean }>("/foo");

    expect(result).toEqual({ ok: true });
    expect(token.value).toBe("new-token");
    expect(fetchMock).toHaveBeenCalledTimes(3); // /foo (401) -> /auth/refresh -> /foo (Retry)
    const refreshCall = fetchMock.mock.calls.find(([url]) => url.includes("/auth/refresh"));
    expect(refreshCall?.[1]).toMatchObject({ method: "POST", credentials: "include", body: {} });
    expect(navigateTo).not.toHaveBeenCalled();
  });

  it("401 -> Refresh schlaegt fehl -> Token weg, Redirect /login, kein Retry der Originalanfrage", async () => {
    fetchMock.mockImplementation(async (url) => {
      throw url.includes("/auth/refresh") ? unauthorized() : unauthorized();
    });

    const { apiFetch, token } = useApi();
    await expect(apiFetch("/foo")).rejects.toBeTruthy();

    expect(token.value).toBeNull();
    expect(navigateTo).toHaveBeenCalledWith("/login");
    expect(fetchMock).toHaveBeenCalledTimes(2); // /foo (401) -> /auth/refresh (401) -- kein dritter Call
  });

  it("Refresh erfolgreich, aber Retry erneut 401 -> kein zweiter Refresh-Versuch, Redirect /login", async () => {
    fetchMock.mockImplementation(async (url) => {
      if (url.includes("/auth/refresh")) return { access_token: "new-token" };
      throw unauthorized(); // /foo bleibt 401, auch mit dem neuen Token
    });

    const { apiFetch } = useApi();
    await expect(apiFetch("/foo")).rejects.toBeTruthy();

    expect(navigateTo).toHaveBeenCalledWith("/login");
    // /foo, /auth/refresh, /foo (Retry) -- danach Schluss, kein zweiter Refresh-Versuch.
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it.each(["/auth/login", "/auth/login/tenant-123", "/auth/refresh", "/auth/logout"])(
    "401 auf Credential-Endpoint %s selbst loest keinen Refresh-Versuch aus",
    async (path) => {
      fetchMock.mockImplementation(async () => {
        throw unauthorized();
      });

      const { apiFetch, token } = useApi();
      token.value = "stale";
      await expect(apiFetch(path)).rejects.toBeTruthy();

      expect(fetchMock).toHaveBeenCalledTimes(1); // kein zusaetzlicher /auth/refresh-Call
      expect(token.value).toBeNull();
      expect(navigateTo).toHaveBeenCalledWith("/login");
    }
  );

  it("401 auf /auth/me (kein Credential-Endpoint) -> ein Refresh + ein Retry -> Erfolg", async () => {
    // Regression: /auth/me wird von permissions.global.ts nach jedem
    // Boot/Reload geladen -- ein abgelaufener Access-Token darf hier NICHT
    // sofort ausloggen, sondern muss denselben Refresh-Retry-Flow wie jeder
    // andere geschuetzte Call durchlaufen.
    fetchMock.mockImplementation(async (url, opts) => {
      if (url.includes("/auth/refresh")) return { access_token: "new-token" };
      if (opts?.headers?.Authorization === "Bearer new-token") return { id: "1" };
      throw unauthorized();
    });

    const { apiFetch, token } = useApi();
    const result = await apiFetch<{ id: string }>("/auth/me");

    expect(result).toEqual({ id: "1" });
    expect(token.value).toBe("new-token");
    expect(fetchMock).toHaveBeenCalledTimes(3); // /auth/me (401) -> /auth/refresh -> /auth/me (Retry)
    expect(navigateTo).not.toHaveBeenCalled();
  });

  it("parallele 401s teilen sich genau EINEN Refresh-Call (Stampede-Schutz)", async () => {
    let refreshCalls = 0;
    fetchMock.mockImplementation(async (url, opts) => {
      if (url.includes("/auth/refresh")) {
        refreshCalls += 1;
        return { access_token: "new-token" };
      }
      if (opts?.headers?.Authorization === "Bearer new-token") return { ok: true };
      throw unauthorized();
    });

    const { apiFetch } = useApi();
    const [r1, r2] = await Promise.all([
      apiFetch<{ ok: boolean }>("/foo"),
      apiFetch<{ ok: boolean }>("/bar"),
    ]);

    expect(r1).toEqual({ ok: true });
    expect(r2).toEqual({ ok: true });
    expect(refreshCalls).toBe(1);
  });

  it("beginLogout() waehrend eines laufenden Refresh verwirft dessen (verspaeteten) Token -- Logout/Refresh-Race", async () => {
    let resolveRefresh!: (v: { access_token: string }) => void;
    fetchMock.mockImplementation(async (url) => {
      if (url.includes("/auth/refresh")) {
        return new Promise((resolve) => {
          resolveRefresh = resolve;
        });
      }
      throw unauthorized();
    });

    const { refreshAccessToken, token, beginLogout } = useApi();
    token.value = "old-token";

    // Refresh startet (haengt auf resolveRefresh) -- synchron bis zum ersten
    // await, d. h. resolveRefresh ist an dieser Stelle bereits gesetzt.
    const refreshResultPromise = refreshAccessToken();

    // logout() passiert JETZT, waehrend der Refresh noch in-flight ist.
    beginLogout();
    token.value = null;

    // Der (verspaetete) Refresh loest jetzt auf -- dieser Token darf NICHT
    // mehr uebernommen werden, sonst waere die abgemeldete Session wieder da.
    resolveRefresh({ access_token: "late-token" });
    const refreshed = await refreshResultPromise;

    expect(refreshed).toBe(false);
    expect(token.value).toBeNull();
  });

  it("typisiertes Fehler-detail {code,message} wird zu String flachgeklopft, code bleibt erhalten (#132)", async () => {
    // Backend (core/errors.py) liefert bei Domain-Fehlern detail als Objekt.
    // Die UI liest e.data.detail als String -> muss zentral flachgeklopft
    // werden, sonst zeigt sie "[object Object]".
    const typedErr = { data: { detail: { code: "ALREADY_CLOCKED_IN", message: "Bereits eingestempelt." } } };
    fetchMock.mockImplementation(async () => {
      throw typedErr;
    });

    const { apiFetch } = useApi();
    await expect(apiFetch("/clock/in")).rejects.toMatchObject({
      data: { detail: "Bereits eingestempelt.", code: "ALREADY_CLOCKED_IN" },
    });
  });

  it("statisches String-detail bleibt unveraendert", async () => {
    const strErr = { data: { detail: "Keine Berechtigung." } };
    fetchMock.mockImplementation(async () => {
      throw strErr;
    });

    const { apiFetch } = useApi();
    await expect(apiFetch("/x")).rejects.toMatchObject({ data: { detail: "Keine Berechtigung." } });
  });
});
