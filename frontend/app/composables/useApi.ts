import type { components, paths } from "~/types/api";

// Zentrale API-Schicht: $fetch gegen das Backend unter <apiBase>/api/v1,
// haengt das JWT als Bearer an.
//
// Access-Token liegt NUR im Speicher (useState) -- kein Cookie, kein
// localStorage. Ein Hard-Reload verliert ihn; der Boot-Silent-Refresh
// (Plugin app/plugins/auth-init.client.ts) holt ihn ueber den httpOnly-
// Refresh-Cookie zurueck, bevor die Routen-Guards entscheiden.
//
// 401-Handling: EIN Refresh-Versuch, dann EIN Retry der urspruenglichen
// Anfrage. Parallele 401s teilen sich denselben In-Flight-Refresh (Stampede-
// Schutz via Modul-Variable). Schlaegt der Refresh fehl (oder scheitert der
// Retry erneut mit 401): Token weg, Redirect nach /login.
//
// Ausgenommen vom Auto-Retry sind NUR die drei Credential-Endpoints selbst
// (/auth/login inkl. /auth/login/{tenant_id}, /auth/refresh, /auth/logout) --
// ein 401 dort ist bereits die Endantwort (falsches PW / toter Refresh-
// Cookie), kein "Access-Token abgelaufen". Alle anderen /auth/*-Pfade (z. B.
// /auth/me, /auth/change-password) durchlaufen den normalen Refresh-Retry --
// sonst würde ein abgelaufener Access-Token beim naechsten /auth/me-Aufruf
// (permissions.global.ts) den User hart ausloggen statt still zu refreshen.
// Typed API contract, generiert aus dem committeten OpenAPI-Snapshot
// (docs/api/openapi-zeiterfassung.json → `pnpm gen:api`, siehe app/types/api.d.ts).
// Rein additiv: Schema<K> und ApiPaths sind Compile-Zeit-Helfer über
// components["schemas"]/paths -- das Laufzeitverhalten von apiFetch bleibt
// unveraendert. Shape-Drift zwischen Backend und Frontend wird dadurch beim
// Bauen (tsc/eslint) sichtbar statt erst im Browser.
export type ApiSchemas = components["schemas"];
export type Schema<K extends keyof ApiSchemas> = ApiSchemas[K];
export type ApiPaths = paths;

const CREDENTIAL_ENDPOINTS = ["/auth/login", "/auth/refresh", "/auth/logout"];
let inFlightRefresh: Promise<boolean> | null = null;
// Race-Schutz Logout vs. Refresh: ein Refresh, der VOR logout() gestartet
// wurde, darf NACH logout() nicht mehr den Token setzen -- sonst "loggt" ein
// spaet aufloesender Refresh die (bereits abgemeldete) Session heimlich
// wieder ein. logout() ruft beginLogout(), was die Generation hochzaehlt;
// refreshAccessToken() verwirft sein Ergebnis, wenn sich die Generation
// waehrend des In-Flight-Requests geaendert hat.
let authGeneration = 0;

// Das Backend liefert bei typisierten Domain-Fehlern detail={code,message}
// (siehe backend core/errors.py, #132), bei statischen Fehlern weiterhin
// detail="...". Die gesamte UI liest e.data.detail als String
// (`e?.data?.detail || "Fallback"`). Hier zentral flachklopfen: die message
// wird zum angezeigten detail-String, der code bleibt unter data.code fuer
// programmatische Nutzung erhalten. Ohne das wuerde die UI "[object Object]"
// anzeigen (Objekt ist truthy -> Fallback greift nicht). Idempotent.
function normalizeErrorDetail(e: unknown): void {
  const err = e as { data?: { detail?: unknown; code?: string } };
  const d = err?.data?.detail;
  if (err?.data && d !== null && typeof d === "object") {
    const obj = d as { code?: string; message?: string };
    if (obj.code !== undefined) err.data.code = obj.code;
    err.data.detail = obj.message ?? JSON.stringify(obj);
  }
}

export function useApi() {
  const config = useRuntimeConfig();
  const token = useState<string | null>("auth_token", () => null);
  const base = String(config.public.apiBase).replace(/\/$/, "") + "/api/v1";

  function authHeaders(opts: Parameters<typeof $fetch>[1]): Record<string, string> {
    const headers: Record<string, string> = { ...(opts?.headers as Record<string, string> || {}) };
    if (token.value) headers.Authorization = `Bearer ${token.value}`;
    return headers;
  }

  function isUnauthorized(e: unknown): boolean {
    // FetchError-Form nicht global importiert (SPA-Build) -> duck-typed lesen.
    const err = e as { response?: { status?: number }; statusCode?: number };
    return err?.response?.status === 401 || err?.statusCode === 401;
  }

  async function clearAndRedirectToLogin() {
    token.value = null;
    if (import.meta.client && useRoute().path !== "/login") {
      await navigateTo("/login");
    }
  }

  // Stampede-geschuetzter Silent-Refresh: liefert true bei Erfolg (neuer
  // Access-Token ist bereits in `token` gesetzt), false wenn die Session
  // ungueltig ist (kein/abgelaufener/revozierter Refresh-Cookie).
  async function refreshAccessToken(): Promise<boolean> {
    const startGeneration = authGeneration;
    if (!inFlightRefresh) {
      inFlightRefresh = (async () => {
        try {
          const res = await $fetch<{ access_token: string }>(base + "/auth/refresh", {
            method: "POST",
            credentials: "include",
            body: {},
          });
          if (authGeneration !== startGeneration) {
            // logout() ist waehrend dieses Refreshs passiert -- den (verspaeteten)
            // Token NICHT mehr uebernehmen.
            return false;
          }
          token.value = res.access_token;
          return true;
        } catch {
          return false;
        } finally {
          inFlightRefresh = null;
        }
      })();
    }
    return inFlightRefresh;
  }

  // Von logout() VOR dem Loeschen des Tokens aufzurufen -- markiert alle
  // bereits laufenden Refreshes als "veraltet" (siehe authGeneration oben).
  function beginLogout() {
    authGeneration += 1;
  }

  async function apiFetch<T>(
    path: string,
    opts: Parameters<typeof $fetch>[1] = {},
    _isRetry = false
  ): Promise<T> {
    try {
      return await $fetch<T>(base + path, { ...opts, headers: authHeaders(opts) });
    } catch (e) {
      normalizeErrorDetail(e);
      if (!isUnauthorized(e)) throw e;

      const isCredentialEndpoint = CREDENTIAL_ENDPOINTS.some((p) => path.startsWith(p));
      if (isCredentialEndpoint || _isRetry) {
        // Credential-Endpunkt selbst 401 (falsches PW / Session tot) oder
        // bereits der Retry ist erneut 401 -> hier ist Schluss, kein
        // weiterer Versuch.
        await clearAndRedirectToLogin();
        throw e;
      }

      const refreshed = await refreshAccessToken();
      if (!refreshed) {
        await clearAndRedirectToLogin();
        throw e;
      }
      return apiFetch<T>(path, opts, true);
    }
  }

  return { apiFetch, base, token, refreshAccessToken, beginLogout };
}
