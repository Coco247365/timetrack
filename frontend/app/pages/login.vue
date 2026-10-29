<script setup lang="ts">
definePageMeta({ layout: false });

const email = ref("");
const password = ref("");
const error = ref("");
const loading = ref(false);
const auth = useAuth();
const { login } = auth;

async function onSubmit() {
  error.value = "";
  loading.value = true;
  try {
    await login(email.value, password.value);
    // Ziel je nach Rolle: Admin/HR -> Mitarbeiterliste, Self-Service -> Meine Zeit.
    await navigateTo(auth.canViewEmployees.value ? "/mitarbeiter" : "/meine-zeit");
  } catch (e: any) {
    const msg = e?.data?.detail || e?.message || "Anmeldung fehlgeschlagen.";
    error.value = typeof msg === "string" ? msg : "Anmeldung fehlgeschlagen.";
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-col">
      <div class="wordmark">TimeTrack</div>
      <form class="card login-card" @submit.prevent="onSubmit">
        <h1>Anmelden</h1>
        <p class="muted" style="margin: 4px 0 18px">Zugang für Buchhaltung &amp; Geschäftsführung.</p>

        <label class="label" for="email">E-Mail</label>
        <input id="email" v-model="email" type="email" class="input" autocomplete="username" required />

        <label class="label" for="pw" style="margin-top: 12px; display: block">Passwort</label>
        <input id="pw" v-model="password" type="password" class="input" autocomplete="current-password" required />

        <p v-if="error" class="err">{{ error }}</p>

        <button class="btn btn-primary btn-block" type="submit" :disabled="loading" style="margin-top: 18px">
          {{ loading ? "Anmelden …" : "Anmelden" }}
        </button>
      </form>
    </div>
  </div>
</template>

<style scoped>
.login-wrap {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: radial-gradient(1200px 600px at 50% -10%, #fff, var(--bg));
  padding: 24px;
}
.login-col { width: 100%; max-width: 400px; }
.wordmark { text-align: center; font-weight: 700; font-size: 18px; margin-bottom: 16px; }
.login-card { padding: 26px; }
.login-card .label { display: block; margin-bottom: 6px; }
.err {
  margin: 14px 0 0; background: var(--red-bg); border: 1px solid #f0c8c6;
  color: var(--red-text); padding: 9px 11px; border-radius: var(--r-input); font-size: 13px;
}
</style>
