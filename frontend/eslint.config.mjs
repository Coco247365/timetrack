// @ts-check
import withNuxt from './.nuxt/eslint.config.mjs'

export default withNuxt(
  {
    // Bestandscode vor Einführung von ESLint (zeiterfassung-4k9): viele `any`
    // (lose typisierte Backend-Responses) und ein paar unbenutzte Bindings.
    // Als Warning statt Error, damit `pnpm lint` grün bleibt, ohne dass hier
    // unter Zeitdruck 2000+ Zeilen Fachlogik blind umgeschrieben werden —
    // Aufräumen ist eigener Task, siehe Follow-up-Issue.
    files: ["app/pages/**/*.vue", "app/components/**/*.vue", "app/layouts/**/*.vue"],
    rules: {
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-unused-vars": "warn",
      "@typescript-eslint/no-unused-expressions": "warn",
      "@typescript-eslint/no-dynamic-delete": "warn",
      "no-empty": "warn",
      "no-control-regex": "warn",
    },
  }
)
