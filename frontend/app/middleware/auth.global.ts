// Globaler Guard: ohne Token nur /login erreichbar; mit Token kein /login.
// Wartet zuerst auf den Boot-Silent-Refresh (Plugin auth-init.client.ts),
// damit eine noch gueltige Session beim Reload nicht kurz auf /login
// aufblitzt (Redirect-Flash), bevor der Access-Token wiederhergestellt ist.
export default defineNuxtRouteMiddleware(async (to) => {
  if (import.meta.server) return; // SPA: nichts zu tun bei einem etwaigen Server-Pass
  const pending = useState<Promise<void> | null>("auth_init_pending", () => null);
  if (pending.value) await pending.value;

  const { token } = useApi();
  if (to.path !== "/login" && !token.value) {
    return navigateTo("/login");
  }
  if (to.path === "/login" && token.value) {
    return navigateTo("/mitarbeiter");
  }
});
