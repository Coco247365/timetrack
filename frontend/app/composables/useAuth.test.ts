// useAuth ruft useApi()/navigateTo bare (Nuxt-Auto-Import) auf -> environment:'nuxt'
// (vitest.config.ts) + mockNuxtImport statt Hand-Stubs auf globalThis.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mockNuxtImport } from "@nuxt/test-utils/runtime";
import { navigateTo } from "#app";
import { ref } from "vue";
import type { CurrentUser } from "./useAuth";

const apiFetchMock = vi.fn();
const tokenRef = ref<string | null>(null);
const beginLogoutMock = vi.fn();

mockNuxtImport("useApi", () => {
  return () => ({ apiFetch: apiFetchMock, token: tokenRef, base: "", beginLogout: beginLogoutMock });
});
mockNuxtImport("navigateTo", () => vi.fn());

function baseUser(overrides: Partial<CurrentUser> = {}): CurrentUser {
  return {
    id: "1",
    full_name: "Max Mustermann",
    first_name: "Max",
    last_name: "Mustermann",
    email: "max@example.com",
    permissions: [],
    ...overrides,
  };
}

describe("useAuth", () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
    beginLogoutMock.mockReset();
    tokenRef.value = null;
    const { user, features } = useAuth();
    user.value = null;
    features.value = {};
    vi.mocked(navigateTo).mockClear();
  });

  describe("login/logout", () => {
    it("login() setzt Token und laedt das Profil ueber fetchMe() -- refresh_token im Body wird ignoriert", async () => {
      // Backend kann uebergangsweise noch refresh_token im Body mitschicken;
      // die Frontend-Response-Type liest es bewusst nicht ein/speichert es nicht.
      apiFetchMock
        .mockResolvedValueOnce({ access_token: "abc", refresh_token: "r" })
        .mockResolvedValueOnce(baseUser());

      const { login, user, token } = useAuth();
      await login("max@example.com", "geheim");

      expect(token.value).toBe("abc");
      expect(user.value?.full_name).toBe("Max Mustermann");
      expect(apiFetchMock).toHaveBeenNthCalledWith(1, "/auth/login", {
        method: "POST",
        credentials: "include",
        body: { email: "max@example.com", password: "geheim" },
      });
      expect(apiFetchMock).toHaveBeenNthCalledWith(2, "/auth/me");
    });

    it("logout() ruft /auth/logout (Cookie-Credentials, leerer Body) und leert Token+User (auch wenn der Server-Call fehlschlaegt)", async () => {
      apiFetchMock.mockRejectedValueOnce(new Error("network"));
      const { logout, user, token } = useAuth();
      tokenRef.value = "xyz";
      user.value = baseUser();

      await logout();

      expect(apiFetchMock).toHaveBeenCalledWith("/auth/logout", {
        method: "POST",
        credentials: "include",
        body: {},
      });
      expect(token.value).toBeNull();
      expect(user.value).toBeNull();
      expect(navigateTo).toHaveBeenCalledWith("/login");
    });

    it("logout() ruft beginLogout() VOR dem /auth/logout-Call auf (Race-Schutz fuer in-flight Refreshes)", async () => {
      apiFetchMock.mockImplementation(async () => {
        // Zum Zeitpunkt des /auth/logout-Calls muss beginLogout() bereits
        // aufgerufen worden sein -- sonst kann ein zeitgleich laufender
        // Refresh den Token nach dem Logout wiederherstellen.
        expect(beginLogoutMock).toHaveBeenCalledTimes(1);
        return undefined;
      });
      const { logout } = useAuth();

      await logout();

      expect(beginLogoutMock).toHaveBeenCalledTimes(1);
    });
  });

  describe("Rechte/Rollen-Helfer", () => {
    it("hasPermission() prueft exakten Treffer in permissions", () => {
      const { user, hasPermission } = useAuth();
      user.value = baseUser({ permissions: ["timesheet:view_own"] });

      expect(hasPermission("timesheet:view_own")).toBe(true);
      expect(hasPermission("timesheet:view_all")).toBe(false);
    });

    it("hasNamespace() matcht sowohl den nackten Namespace als auch 'ns:*'", () => {
      const { user, hasNamespace } = useAuth();

      user.value = baseUser({ permissions: ["admin"] });
      expect(hasNamespace("admin")).toBe(true);

      user.value = baseUser({ permissions: ["admin:settings"] });
      expect(hasNamespace("admin")).toBe(true);

      user.value = baseUser({ permissions: ["adminfoo"] });
      expect(hasNamespace("admin")).toBe(false);
    });

    it("canAccessAdmin ist true bei admin:dashboard oder jedem admin:*-Recht", () => {
      const { user, canAccessAdmin } = useAuth();

      user.value = baseUser({ permissions: [] });
      expect(canAccessAdmin.value).toBe(false);

      user.value = baseUser({ permissions: ["admin:dashboard"] });
      expect(canAccessAdmin.value).toBe(true);

      user.value = baseUser({ permissions: ["admin:cards"] });
      expect(canAccessAdmin.value).toBe(true);
    });

    it("isSuperadmin spiegelt user.is_superadmin", () => {
      const { user, isSuperadmin } = useAuth();

      user.value = baseUser({ is_superadmin: false });
      expect(isSuperadmin.value).toBe(false);

      user.value = baseUser({ is_superadmin: true });
      expect(isSuperadmin.value).toBe(true);
    });
  });
});
