<!-- App-Shell: Sticky-Header mit Wortmarke, Mandanten-Umschalter (Backend-Blocker),
     Navigation, Nutzer + Abmelden. -->
<script setup lang="ts">
const { user, initials, logout, fetchMe, canViewEmployees, canViewTeam, canExport, canManageSettings, canViewReports, canSelfService, fetchFeatures, featureEnabled } = useAuth();
const { apiFetch } = useApi();

// Nutzerprofil laden, falls (z. B. nach Reload) noch nicht im State.
// Funktions-Schalter IMMER laden — nicht nur beim ersten Profil-Laden,
// sonst bleiben sie in einer laufenden Sitzung leer (= alles wirkt "an").
onMounted(async () => {
  if (!user.value) {
    try { await fetchMe(); } catch { /* Guard schickt ggf. zu /login */ }
  }
  fetchFeatures();
});

/* ---------- Profil-Menü (Dropdown oben rechts) ---------- */
const menuOpen = ref(false);
const profileRef = ref<HTMLElement | null>(null);

function toggleMenu() {
  menuOpen.value = !menuOpen.value;
}
// Klick außerhalb schließt das Dropdown.
function onDocClick(e: MouseEvent) {
  if (!menuOpen.value) return;
  if (profileRef.value && !profileRef.value.contains(e.target as Node)) {
    menuOpen.value = false;
  }
}
onMounted(() => document.addEventListener("click", onDocClick));
onBeforeUnmount(() => document.removeEventListener("click", onDocClick));

function onLogout() {
  menuOpen.value = false;
  logout();
}

/* ---------- Passwort ändern (Modal) ---------- */
const pwModalOpen = ref(false);
const pwCurrent = ref("");
const pwNew = ref("");
const pwRepeat = ref("");
const pwError = ref("");
const pwSaving = ref(false);
const pwSuccess = ref(false);

function resetPwFields() {
  pwCurrent.value = "";
  pwNew.value = "";
  pwRepeat.value = "";
  pwError.value = "";
  pwSaving.value = false;
}
function openPwModal() {
  menuOpen.value = false;
  resetPwFields();
  pwModalOpen.value = true;
}
function closePwModal() {
  pwModalOpen.value = false;
  resetPwFields();
}
async function savePassword() {
  pwError.value = "";
  if (pwNew.value.length < 8) {
    pwError.value = "Das neue Passwort muss mindestens 8 Zeichen lang sein.";
    return;
  }
  if (pwNew.value !== pwRepeat.value) {
    pwError.value = "Die beiden neuen Passwörter stimmen nicht überein.";
    return;
  }
  pwSaving.value = true;
  try {
    await apiFetch("/auth/change-password", {
      method: "POST",
      body: { current_password: pwCurrent.value, new_password: pwNew.value },
    });
    closePwModal();
    pwSuccess.value = true;
    setTimeout(() => { pwSuccess.value = false; }, 4000);
  } catch (e: any) {
    pwError.value = e?.data?.detail || "Passwort konnte nicht geändert werden.";
  }
  pwSaving.value = false;
}

// Navigation nach Rechten — Konten sehen nur, was ihre Rolle erlaubt.
const nav = computed(() =>
  [
    { to: "/meine-zeit", label: "Meine Zeit", show: canSelfService.value },
    { to: "/mitarbeiter", label: "Mitarbeiter", show: canViewEmployees.value },
    { to: "/team", label: "Team", show: canViewTeam.value },
    { to: "/auswertung", label: "Auswertung", show: canViewReports.value },
    { to: "/export", label: "Export", show: canExport.value },
    { to: "/einstellungen", label: "Einstellungen", show: canManageSettings.value },
  ].filter((n) => n.show)
);
const route = useRoute();
const isActive = (to: string) =>
  route.path === to || (to === "/mitarbeiter" && route.path.startsWith("/mitarbeiter"));
</script>

<template>
  <div>
    <header class="topbar">
      <div class="topbar-inner page">
        <div class="row" style="gap: 16px">
          <span class="brand">TimeTrack</span>
        </div>

        <nav class="nav">
          <NuxtLink
            v-for="n in nav"
            :key="n.to"
            :to="n.to"
            class="nav-item"
            :class="{ active: isActive(n.to) }"
          >{{ n.label }}</NuxtLink>
        </nav>

        <div ref="profileRef" class="profile">
          <button class="profile-btn row" type="button" style="gap: 8px" @click="toggleMenu">
            <span class="avatar">{{ initials }}</span>
            <span class="muted" style="font-size: 13px">{{ user?.department_name || user?.full_name || "—" }}</span>
            <span class="muted" style="font-size: 11px">▾</span>
          </button>

          <div v-if="menuOpen" class="profile-menu card">
            <button class="profile-item" type="button" @click="openPwModal">Passwort ändern</button>
            <button class="profile-item" type="button" @click="onLogout">Abmelden</button>
          </div>
        </div>
      </div>
    </header>

    <main class="page">
      <slot />
    </main>

    <!-- Kurze Erfolgsmeldung nach Passwortänderung -->
    <div v-if="pwSuccess" class="pw-toast">
      <span class="badge green"><span class="dot" />Passwort geändert</span>
    </div>

    <!-- Modal: Passwort ändern -->
    <div v-if="pwModalOpen" class="modal-overlay" @click.self="closePwModal">
      <div class="card modal-card">
        <h2 style="margin-bottom: 14px">Passwort ändern</h2>

        <div v-if="pwError" class="wip-banner" style="margin-bottom: 14px">{{ pwError }}</div>

        <div class="stack" style="gap: 12px">
          <div class="stack" style="gap: 4px">
            <span class="label">Aktuelles Passwort</span>
            <input v-model="pwCurrent" type="password" class="input" autocomplete="current-password" />
          </div>
          <div class="stack" style="gap: 4px">
            <span class="label">Neues Passwort</span>
            <input v-model="pwNew" type="password" class="input" autocomplete="new-password" />
          </div>
          <div class="stack" style="gap: 4px">
            <span class="label">Neues Passwort wiederholen</span>
            <input v-model="pwRepeat" type="password" class="input" autocomplete="new-password" />
          </div>
        </div>

        <div class="row" style="gap: 8px; justify-content: flex-end; margin-top: 18px">
          <button class="btn" type="button" :disabled="pwSaving" @click="closePwModal">Abbrechen</button>
          <button class="btn btn-primary" type="button" :disabled="pwSaving" @click="savePassword">Speichern</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.topbar {
  position: sticky; top: 0; z-index: 20;
  background: #fff; border-bottom: 1px solid var(--border);
}
.topbar-inner { display: flex; align-items: center; justify-content: space-between; gap: 18px; height: 60px; }
.brand { font-weight: 700; font-size: 16px; letter-spacing: .2px; }
.switcher { width: auto; padding: 6px 10px; font-size: 13px; }
.nav { display: flex; gap: 6px; }
.nav-item {
  padding: 7px 14px; border-radius: var(--r-input); font-weight: 600; font-size: 14px;
  color: var(--text-2);
}
.nav-item:hover { background: var(--bg); }
.nav-item.active { background: var(--accent-tint); color: var(--accent); }
.avatar {
  width: 30px; height: 30px; border-radius: 50%; display: inline-flex;
  align-items: center; justify-content: center; font-size: 12px; font-weight: 700;
  background: var(--accent-tint); color: var(--accent);
}

/* Profil-Menü */
.profile { position: relative; }
.profile-btn {
  background: none; border: 0; cursor: pointer; padding: 4px 6px;
  border-radius: var(--r-input);
}
.profile-btn:hover { background: var(--bg); }
.profile-menu {
  position: absolute; right: 0; top: calc(100% + 8px); z-index: 30;
  min-width: 180px; padding: 6px; display: flex; flex-direction: column;
  box-shadow: var(--shadow-modal);
}
.profile-item {
  text-align: left; background: none; border: 0; cursor: pointer;
  padding: 9px 12px; border-radius: var(--r-input); font-size: 14px; font-weight: 600;
  color: var(--text-2);
}
.profile-item:hover { background: var(--bg); color: var(--text-1); }

/* Modal */
.modal-overlay {
  position: fixed; inset: 0; z-index: 50;
  background: rgba(0, 0, 0, .35);
  display: flex; align-items: center; justify-content: center; padding: 20px;
}
.modal-card {
  width: 100%; max-width: 420px; padding: 22px;
  box-shadow: var(--shadow-modal);
}

/* Erfolgs-Toast */
.pw-toast {
  position: fixed; right: 20px; bottom: 20px; z-index: 60;
}
</style>
