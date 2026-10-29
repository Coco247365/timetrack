// Routen-Schutz nach Rechten: Konten dürfen nur Bereiche öffnen, die ihre Rolle
// erlaubt. Fallback-Ziel hängt von der Rolle ab (Admin/HR -> Mitarbeiterliste,
// Self-Service-Mitarbeiter -> Meine Zeit).
export default defineNuxtRouteMiddleware(async (to) => {
  if (import.meta.server) return;
  const pending = useState<Promise<void> | null>("auth_init_pending", () => null);
  if (pending.value) await pending.value;

  const { token } = useApi();
  if (!token.value) return; // auth.global kümmert sich um Login-Redirect

  const auth = useAuth();
  if (!auth.user.value) {
    try {
      await auth.fetchMe();
    } catch {
      // Fail closed: ein 401 hat apiFetch bereits selbst behandelt (Refresh-
      // Versuch + Redirect nach /login bei Fehlschlag). Jeder ANDERE Fehler
      // (Netzwerk, 500, ...) darf die Route NICHT einfach ungeprueft rendern
      // lassen -- ohne User/Rechte koennen die Regeln unten nicht greifen.
      return navigateTo("/login");
    }
  }

  // Startseite je nach Rolle.
  const home = () => (auth.canViewEmployees.value ? "/mitarbeiter" : "/meine-zeit");

  const rules: { prefix: string; allowed: () => boolean }[] = [
    { prefix: "/meine-zeit", allowed: () => auth.canSelfService.value },
    { prefix: "/mitarbeiter", allowed: () => auth.canViewEmployees.value },
    { prefix: "/team", allowed: () => auth.canViewTeam.value },
    { prefix: "/auswertung", allowed: () => auth.canViewReports.value },
    { prefix: "/export", allowed: () => auth.canExport.value },
    { prefix: "/einstellungen", allowed: () => auth.canManageSettings.value },
  ];
  for (const r of rules) {
    if (to.path === r.prefix || to.path.startsWith(r.prefix + "/")) {
      if (!r.allowed()) {
        const target = home();
        // Schleifen vermeiden: nie auf die gerade verbotene Route selbst umleiten.
        if (target !== to.path) return navigateTo(target);
        return navigateTo("/login");
      }
    }
  }
});
