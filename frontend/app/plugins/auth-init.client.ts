// Silent-Refresh beim App-Start.
//
// Der Access-Token lebt nur im Speicher (useState in useApi.ts) und ist nach
// jedem Hard-Reload weg. Ohne diesen Plugin wuerde eine eigentlich noch
// gueltige Session bei jedem Reload kurz auf /login aufblitzen, bevor sie
// (falls ueberhaupt) wiederhergestellt wird.
//
// Hier wird EINMAL beim Boot per httpOnly-Refresh-Cookie versucht, die
// Session wiederherzustellen -- ueber dieselbe stampede-geschuetzte
// refreshAccessToken()-Funktion, die auch der 401-Interceptor in useApi.ts
// benutzt. Die Routen-Guards (auth.global.ts, permissions.global.ts) warten
// auf das hier abgelegte Promise, bevor sie ueber Redirects entscheiden.
export default defineNuxtPlugin(async () => {
  const pending = useState<Promise<void> | null>("auth_init_pending", () => null);

  if (!pending.value) {
    pending.value = (async () => {
      const { refreshAccessToken, token } = useApi();
      try {
        const ok = await refreshAccessToken();
        if (ok && token.value) {
          const { fetchMe } = useAuth();
          await fetchMe();
        }
      } catch {
        // Keine gueltige Session -- Guards leiten zu /login, kein Fehler hier.
      }
    })();
  }

  await pending.value;
});
