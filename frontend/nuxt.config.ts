// Nuxt-Config für das Zeiterfassungs-Frontend (HR/GF-Tool).
// SPA-Modus (ssr:false): reiner API-Client, redet ausschließlich über /api/v1.
export default defineNuxtConfig({
  compatibilityDate: "2025-06-01",
  ssr: false,
  devtools: { enabled: true },
  modules: ["@nuxt/eslint"],
  css: ["~/assets/css/main.css"],
  runtimeConfig: {
    public: {
      // Basis-URL des Backends. Überschreibbar via NUXT_PUBLIC_API_BASE.
      // WICHTIG (Silent-Refresh): der Refresh-Token liegt in einem httpOnly-
      // Cookie mit Path=/api/v1/auth und wird nur SAME-ORIGIN mitgeschickt.
      // Zeigt apiBase auf ein anderes Origin als der Nuxt-Dev-Server (Default:
      // http://127.0.0.1:8000 vs. Dev-Server auf :3000), sendet der Browser
      // den Cookie beim Refresh nicht mit. Für lokale Entwicklung mit
      // funktionierendem Silent-Refresh: NUXT_PUBLIC_API_BASE="" (bzw. "/")
      // setzen — dann laufen /api/*-Requests same-origin über den
      // nitro.devProxy unten zum Backend durch. Der Prod-Default (Backend
      // separat erreichbar) bleibt unverändert.
      apiBase: process.env.NUXT_PUBLIC_API_BASE || "http://127.0.0.1:8000",
    },
  },
  nitro: {
    devProxy: {
      "/api": {
        target: "http://127.0.0.1:8000/api",
        changeOrigin: true,
      },
    },
    // Security-Header (login-hardening, Spec Abschnitt 3). CSP pragmatisch:
    // Nuxt braucht Inline-Script/-Style fuer den App-Start — wichtigster
    // Gewinn bleibt: keine fremden Quellen, kein Framing.
    // In Produktion (nginx-Auslieferung) setzt frontend/docker/nginx.conf
    // dieselben Header — beide Stellen synchron halten.
    routeRules: {
      "/**": {
        headers: {
          "X-Frame-Options": "DENY",
          "X-Content-Type-Options": "nosniff",
          "Referrer-Policy": "strict-origin-when-cross-origin",
          "Content-Security-Policy":
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
        },
      },
    },
  },
  app: {
    head: {
      title: "TimeTrack",
      htmlAttrs: { lang: "de" },
      meta: [
        { charset: "utf-8" },
        { name: "viewport", content: "width=device-width, initial-scale=1" },
      ],
    },
  },
});
