import { defineVitestConfig } from "@nuxt/test-utils/config";

// environment: 'nuxt' laedt echte Auto-Imports (useCookie, useState, navigateTo, ...) --
// die Composables (useApi/useAuth) rufen diese bare auf, ohne eigenen Import.
export default defineVitestConfig({
  test: {
    environment: "nuxt",
  },
});
