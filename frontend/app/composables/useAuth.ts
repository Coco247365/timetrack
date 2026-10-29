// Auth: Login gegen /auth/login, Token im Cookie, Profil aus /auth/me.
export interface CurrentUser {
  id: string;
  full_name: string;
  first_name: string;
  last_name: string;
  email: string;
  department_name?: string | null;
  role_type?: string | null;
  data_scope?: string | null;
  permissions?: string[];
  employee_id?: string | null; // verknüpfter Mitarbeiter (Self-Service)
  is_superadmin?: boolean;
}

export function useAuth() {
  // Token-Referenz aus useApi mitbenutzen, damit ein frisch gesetzter Token
  // sofort für nachfolgende API-Aufrufe (z. B. /auth/me) sichtbar ist.
  const { apiFetch, token, beginLogout } = useApi();
  const user = useState<CurrentUser | null>("auth_user", () => null);
  // Funktions-Schalter des Mandanten (Einstellungen -> Funktionen).
  const features = useState<Record<string, boolean>>("tenant_features", () => ({}));

  async function login(email: string, password: string) {
    // Single-Tenant-Login. Bei mehreren Mandanten antwortet das Backend mit
    // HTTP 400 ("Use POST /auth/login/{tenant_id}") — der Mandanten-Umschalter
    // ist ein Backend-Blocker (siehe docs/api-anbindung-nuxt.md).
    // Die Antwort kann uebergangsweise noch `refresh_token` im Body enthalten
    // (Backend-Fallback) — wird bewusst NICHT gelesen/gespeichert: der
    // Refresh-Token lebt ausschliesslich im httpOnly-Cookie, den das Backend
    // per Set-Cookie setzt.
    const res = await apiFetch<{ access_token: string }>("/auth/login", {
      method: "POST",
      credentials: "include",
      body: { email, password },
    });
    token.value = res.access_token;
    await fetchMe();
  }

  async function fetchMe() {
    user.value = await apiFetch<CurrentUser>("/auth/me");
    return user.value;
  }

  async function logout() {
    // Race-Schutz ZUERST: ein Refresh, der gerade in-flight ist, darf nach
    // diesem Punkt keinen neuen Token mehr setzen (siehe useApi.beginLogout).
    beginLogout();
    try {
      // Revoziert die Refresh-Session serverseitig (Cookie reitet via
      // credentials:"include" mit); Body ist leer, das Backend liest den
      // Refresh-Token nur noch aus dem Cookie.
      await apiFetch("/auth/logout", { method: "POST", credentials: "include", body: {} });
    } catch {
      /* Best effort: Netzwerkfehler o.ae. duerfen den lokalen Logout nicht blockieren. */
    }
    token.value = null;
    user.value = null;
    await navigateTo("/login");
  }

  const initials = computed(() => {
    const u = user.value;
    if (!u) return "?";
    return ((u.first_name?.[0] || "") + (u.last_name?.[0] || "")).toUpperCase() || "?";
  });

  // ── Rechte/Rollen-Helfer (Hierarchie = role_type + permissions + data_scope) ──
  function hasPermission(p: string): boolean {
    return (user.value?.permissions ?? []).includes(p);
  }
  /** Namespace-Treffer: "admin" matcht "admin" und jedes "admin:*". */
  function hasNamespace(ns: string): boolean {
    return (user.value?.permissions ?? []).some((p) => p === ns || p.startsWith(ns + ":"));
  }
  const roleType = computed(() => user.value?.role_type ?? null);
  const dataScope = computed(() => user.value?.data_scope ?? "self");

  // Abgeleitete Fähigkeiten — steuern Navigation + Routen-Zugriff.
  const canAccessAdmin = computed(() => hasPermission("admin:dashboard") || hasNamespace("admin"));
  const isSuperadmin = computed(() => !!user.value?.is_superadmin);
  // Feinere Verwaltungs-Rechte (Backend erzwingt dieselben Regeln serverseitig).
  const canManageRoles = computed(() => isSuperadmin.value || hasPermission("admin:roles"));
  const canManageAccounts = computed(() => isSuperadmin.value || hasPermission("admin:accounts"));
  const canViewOrgChart = computed(
    () =>
      hasPermission("orgchart:view_all") ||
      hasPermission("orgchart:view_team") ||
      canAccessAdmin.value
  );
  const canManageCards = computed(
    () => isSuperadmin.value || hasPermission("admin:cards") || hasPermission("users:write")
  );
  const canManageSettings = computed(() => hasPermission("admin:settings"));
  const canManageFeatures = computed(() => isSuperadmin.value || hasPermission("admin:features"));
  const canManageUsers = computed(() => hasPermission("users:write"));
  const canViewEmployees = computed(
    () =>
      hasPermission("users:read_all") ||
      hasPermission("users:read_team") ||
      hasPermission("users:write") ||
      canAccessAdmin.value
  );
  const canViewTeam = computed(
    () =>
      hasPermission("calendar:view_all") ||
      hasPermission("calendar:view_team") ||
      hasPermission("timesheet:view_all") ||
      hasPermission("timesheet:view_team") ||
      canAccessAdmin.value
  );
  const canExport = computed(() => hasPermission("export:datev") || canAccessAdmin.value);
  const canViewReports = computed(
    () =>
      hasPermission("reports:view") ||
      hasPermission("timesheet:view_all") ||
      hasPermission("timesheet:view_team") ||
      canAccessAdmin.value
  );
  // Self-Service ("Meine Zeit"): Account ist mit einem Mitarbeiter verknüpft
  // und darf eigene Zeiten sehen.
  const canSelfService = computed(
    () => !!user.value?.employee_id && hasPermission("timesheet:view_own")
  );

  async function fetchFeatures() {
    // WICHTIG: apiFetch kommt aus dem Setup-Kontext oben — useApi() hier drin
    // wuerde nach einem await fehlschlagen (Nuxt-Instanz nicht verfuegbar)
    // und die Flags blieben leer -> alles wirkte "an".
    try {
      features.value = await apiFetch<Record<string, boolean>>("/admin/features");
    } catch { /* Schalter optional — Default: alles an */ }
  }
  function featureEnabled(key: string): boolean {
    return features.value[key] !== false; // unbekannt/nicht geladen = an
  }

  return {
    token, user, initials, login, logout, fetchMe,
    hasPermission, hasNamespace, roleType, dataScope,
    canAccessAdmin, canManageSettings, canManageUsers, canViewEmployees, canViewTeam, canExport,
    canViewReports, canSelfService,
    isSuperadmin, canManageRoles, canManageAccounts, canManageCards, canManageFeatures,
    canViewOrgChart,
    features, fetchFeatures, featureEnabled,
  };
}
