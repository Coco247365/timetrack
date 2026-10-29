<script setup lang="ts">
// Mitarbeiter anlegen → POST /users.
// Selects: GET /departments , GET /roles , GET /admin/employees.
// Backend-Blocker (WipBadge):
//   - employment_type nur salaried|hourly.
const { apiFetch } = useApi();

interface Department { id: string; name: string; company_id?: string | null }
interface Role { id: string; name: string }
interface ManagerEmp { id: string; name: string; is_active?: boolean }
interface Company { id: string; name: string; is_active?: boolean }

const departments = ref<Department[]>([]);
const roles = ref<Role[]>([]);
const managers = ref<ManagerEmp[]>([]);
const companies = ref<Company[]>([]);

const form = reactive({
  first_name: "",
  last_name: "",
  email: "",
  employee_number: "",
  department_id: "",
  company_id: "",
  role_id: "",
  // Normalfall im Betrieb ist Stundenlohn; Beschäftigungsart steuert auch,
  // wer im DATEV-Export landet (Standard: nur Lohn-Empfänger).
  employment_type: "hourly",
  hire_date: "",
  counting_start_date: "",
  vacation_entitlement_days: null as number | null,
  initial_vacation_remaining_days: null as number | null,
  manager_id: "",
  time_capture: "auto",
  nfc_badge_id: "",
  contracted_hours_per_week: null as number | null,
  birthday: "", // → birth_date
  overtime_payout_mode: "above_cap",
  overtime_cap_hours: 20,
});

const loadErr = ref("");
const submitErr = ref("");
const emailErr = ref("");

// Bereiche gehören zu einer Firma — erst filtern, wenn eine Firma gewählt ist.
// Bereiche sind firmenunabhaengig (#183): immer alle Bereiche zeigen,
// die Firmenwahl beeinflusst die Auswahl nicht.
const filteredDepartments = computed(() => departments.value);

// --- Tages-Soll pro Wochentag (optional, gleicher Editor wie in den Stammdaten) ---
const WEEKDAY_LABELS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const wdEnabled = ref(false);
const wdHM = reactive<{ h: number; m: number }[]>(
  Array.from({ length: 7 }, () => ({ h: 0, m: 0 }))
);
// Beim Aktivieren mit Wochenstunden÷5 (Mo–Fr) vorbelegen — als Startpunkt.
function prefillWeekdays() {
  if (!wdEnabled.value) return;
  const perDay = Math.round(((Number(form.contracted_hours_per_week) || 40) / 5) * 60);
  wdHM.forEach((d, i) => {
    const v = i < 5 ? perDay : 0;
    d.h = Math.floor(v / 60); d.m = v % 60;
  });
}
const wdWeeklySum = computed(() => {
  const total = wdHM.reduce((s, d) => s + (Number(d.h) || 0) * 60 + (Number(d.m) || 0), 0);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
});
function weekdayMinutesFromForm(): number[] | null {
  if (!wdEnabled.value) return null;
  return wdHM.map((d) => (Number(d.h) || 0) * 60 + (Number(d.m) || 0));
}

// Gleiche Grenzen wie der Backend-Validator (EmailStr): nur ASCII —
// Umlaute (ö/ä/ü), Umlaut-Domains etc. sind keine gueltige Mail-Adresse.
function validEmail(v: string): boolean {
  return /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(v);
}
const saving = ref(false);

onMounted(async () => {
  try { departments.value = await apiFetch<Department[]>("/departments"); } catch { /* optional */ }
  try { roles.value = await apiFetch<Role[]>("/roles"); } catch { /* optional */ }
  try {
    const emps = await apiFetch<ManagerEmp[]>("/admin/employees");
    // Ausgeschiedene/inaktive Mitarbeiter kommen als Vorgesetzter nicht in Frage.
    managers.value = emps.filter((e) => e.is_active !== false);
  } catch { /* optional */ }
  try { companies.value = await apiFetch<Company[]>("/companies"); } catch { /* optional */ }
  if (!departments.value.length && !roles.value.length) {
    loadErr.value = "Auswahllisten konnten nicht geladen werden.";
  }
});


async function submit() {
  submitErr.value = "";
  emailErr.value = "";
  // Leer ist erlaubt; nur ein ausgefuelltes Feld muss ein gueltiges Format haben.
  if (form.email.trim() && !validEmail(form.email)) {
    emailErr.value = /[^\x00-\x7F]/.test(form.email)
      ? "Bitte korrektes E-Mail-Format eingeben — Umlaute (ä/ö/ü) sind in E-Mail-Adressen nicht erlaubt (z.B. roessler statt rössler)."
      : "Bitte korrektes E-Mail-Format eingeben (z.B. vorname.nachname@firma.de).";
    return;
  }
  saving.value = true;
  // Body: echte Backend-Felder. Kein Passwort — Mitarbeiter sind Personalakten,
  // Logins werden separat unter Einstellungen → Benutzerkonten angelegt.
  const body: Record<string, any> = {
    first_name: form.first_name,
    last_name: form.last_name,
    email: form.email.trim() || null,
    birth_date: form.birthday || null,
    employee_number: form.employee_number || null,
    department_id: form.department_id || null,
    company_id: form.company_id || null,
    role_id: form.role_id || null,
    employment_type: form.employment_type,
    hire_date: form.hire_date || null,
    counting_start_date: form.counting_start_date || null,
    manager_id: form.manager_id || null,
    time_capture: form.time_capture,
    nfc_badge_id: form.nfc_badge_id || null,
    overtime_payout_mode: form.overtime_payout_mode,
    overtime_cap_hours: form.overtime_cap_hours,
  };
  // Zahlenfelder nur senden, wenn gesetzt — sonst Backend-Defaults nutzen
  // (null würde an der Validierung scheitern).
  if (form.vacation_entitlement_days != null)
    body.vacation_entitlement_days = form.vacation_entitlement_days;
  if (form.initial_vacation_remaining_days != null)
    body.initial_vacation_remaining_days = form.initial_vacation_remaining_days;
  if (form.contracted_hours_per_week != null)
    body.contracted_hours_per_week = form.contracted_hours_per_week;
  // Wochentags-Soll nur senden, wenn aktiviert (null = Fallback Wochenstunden÷5).
  const wd = weekdayMinutesFromForm();
  if (wd) body.weekday_target_minutes = wd;
  try {
    await apiFetch("/users", { method: "POST", body });
    await navigateTo("/mitarbeiter");
  } catch (e: any) {
    submitErr.value = e?.data?.detail || "Mitarbeiter konnte nicht angelegt werden.";
  }
  saving.value = false;
}
</script>

<template>
  <div>
    <NuxtLink to="/mitarbeiter" class="muted">‹ Zurück zur Liste</NuxtLink>
    <h1 style="margin: 8px 0 16px">Mitarbeiter anlegen</h1>

    <div v-if="loadErr" class="wip-banner" style="margin-bottom: 14px">{{ loadErr }}</div>

    <form class="card" @submit.prevent="submit">
      <div v-if="submitErr" class="wip-banner" style="margin-bottom: 14px">{{ submitErr }}</div>

      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px">
        <div class="stack">
          <span class="label">Vorname</span>
          <input v-model="form.first_name" class="input" required />
        </div>
        <div class="stack">
          <span class="label">Nachname</span>
          <input v-model="form.last_name" class="input" required />
        </div>
        <div class="stack">
          <span class="label">E-Mail</span>
          <input v-model="form.email" type="text" inputmode="email" class="input"
            :style="emailErr ? 'border-color: var(--error, #b3261e)' : ''" @input="emailErr = ''" />
          <span v-if="emailErr" style="color: var(--error, #b3261e); font-size: 13px">{{ emailErr }}</span>
          <span v-else class="muted" style="font-size: 11px">Optional — leer lassen, wenn keine Adresse vorhanden ist</span>
        </div>
        <div class="stack">
          <span class="label">Personalnr.</span>
          <input v-model="form.employee_number" class="input" />
        </div>

        <div class="stack">
          <span class="label">Bereich</span>
          <select v-model="form.department_id" class="input" :disabled="!form.company_id">
            <option value="">—</option>
            <option v-for="d in filteredDepartments" :key="d.id" :value="d.id">{{ d.name }}</option>
          </select>
          <span v-if="!form.company_id" class="muted" style="font-size: 11px">Erst Firma wählen — Bereiche gehören zu einer Firma.</span>
        </div>
        <div class="stack">
          <span class="label">Firma</span>
          <select v-model="form.company_id" class="input">
            <option value="">—</option>
            <option v-for="c in companies" :key="c.id" :value="c.id">{{ c.name }}</option>
          </select>
        </div>
        <div class="stack">
          <span class="label">Rolle</span>
          <select v-model="form.role_id" class="input">
            <option value="">—</option>
            <option v-for="r in roles" :key="r.id" :value="r.id">{{ r.name }}</option>
          </select>
        </div>

        <div class="stack">
          <span class="label">Beschäftigungsart</span>
          <select v-model="form.employment_type" class="input">
            <option value="hourly">Stundenlohn</option>
            <option value="salaried">Gehalt</option>
            <option value="werkstudent">Werkstudent / Aushilfe</option>
          </select>
        </div>
        <div class="stack">
          <span class="label">Vorgesetzter</span>
          <select v-model="form.manager_id" class="input">
            <option value="">—</option>
            <option v-for="m in managers" :key="m.id" :value="m.id">{{ m.name }}</option>
          </select>
        </div>

        <div class="stack">
          <span class="label">Eintrittsdatum</span>
          <input v-model="form.hire_date" type="date" class="input" />
        </div>

        <div class="stack">
          <span class="label">Erfassungsbeginn</span>
          <input v-model="form.counting_start_date" type="date" class="input" />
          <span class="muted" style="font-size: 11px">Ab hier zählen Überstunden & Fehlzeiten</span>
        </div>
        <div class="stack">
          <span class="label">Urlaubsanspruch/Jahr</span>
          <input v-model.number="form.vacation_entitlement_days" type="number" step="any" min="0" class="input" />
        </div>
        <div class="stack">
          <span class="label">Resturlaub bei Erfassungsbeginn</span>
          <input v-model.number="form.initial_vacation_remaining_days" type="number" step="any" min="0" class="input" />
          <span class="muted" style="font-size: 11px">Übernommener Reststand im Jahr des Erfassungsbeginns; leer = voller Anspruch</span>
        </div>

        <div class="stack">
          <span class="label">Erfassungsmodus</span>
          <select v-model="form.time_capture" class="input">
            <option value="auto">Automatisch</option>
            <option value="web">Web</option>
            <option value="nfc">NFC</option>
          </select>
        </div>
        <div class="stack">
          <span class="label">NFC-Badge</span>
          <input v-model="form.nfc_badge_id" class="input" />
        </div>

        <div class="stack">
          <span class="label">Std/Woche</span>
          <input v-model.number="form.contracted_hours_per_week" type="number" step="0.5" class="input" />
        </div>

        <!-- Tages-Soll pro Wochentag (optional, wie in den Stammdaten) -->
        <div class="stack" style="grid-column: 1 / -1">
          <label class="row" style="gap: 8px; align-items: center; cursor: pointer">
            <input v-model="wdEnabled" type="checkbox" @change="prefillWeekdays" />
            <span class="label" style="margin: 0">Tages-Soll pro Wochentag festlegen</span>
          </label>
          <div v-if="wdEnabled" class="wd-grid">
            <div v-for="(d, i) in wdHM" :key="i" class="wd-day">
              <span class="label" style="width: 26px">{{ WEEKDAY_LABELS[i] }}</span>
              <input v-model.number="d.h" type="number" min="0" max="24" class="input wd-num" /><span class="muted">:</span>
              <input v-model.number="d.m" type="number" min="0" max="59" class="input wd-num" />
            </div>
            <div class="muted" style="grid-column: 1 / -1; font-size: 12px">
              Wochensumme: <strong>{{ wdWeeklySum }}</strong> — deaktiviert = Wochenstunden ÷ 5.
            </div>
          </div>
        </div>

        <div class="stack">
          <span class="label">Geburtstag</span>
          <input v-model="form.birthday" type="date" class="input" />
        </div>

        <div class="stack">
          <span class="label">Überstunden-Auszahlung</span>
          <select v-model="form.overtime_payout_mode" class="input">
            <option value="none">Nur ansammeln</option>
            <option value="all">Monatlich alles auszahlen</option>
            <option value="above_cap">Monatlich über Grenze</option>
          </select>
        </div>
        <div v-if="form.overtime_payout_mode === 'above_cap'" class="stack">
          <span class="label">Kappungsgrenze (h)</span>
          <input v-model.number="form.overtime_cap_hours" type="number" step="0.5" class="input" />
        </div>

      </div>

      <div class="row" style="justify-content: flex-end; gap: 10px; margin-top: 18px">
        <NuxtLink to="/mitarbeiter" class="btn">Abbrechen</NuxtLink>
        <button type="submit" class="btn btn-primary" :disabled="saving">
          {{ saving ? "Legt an …" : "Mitarbeiter anlegen" }}
        </button>
      </div>
    </form>
  </div>
</template>

<style scoped>
.wd-grid { display: grid; grid-template-columns: repeat(2, minmax(150px, 1fr)); gap: 8px 18px; margin-top: 8px; }
.wd-day { display: flex; align-items: center; gap: 6px; }
.wd-num { width: 56px; text-align: right; }
</style>
