<script setup lang="ts">
// DATEV-LODAS-Export. Backend arbeitet mit Jahr + Monat (kein freies Von/Bis).
// Endpunkte: GET /admin/employees, POST /export/datev/preview, POST /export/datev/generate,
// GET /export/datev/{id}/download (Blob, braucht Bearer-Token).
const { apiFetch } = useApi();

interface Employee {
  id: string;
  name: string;
  employee_number?: string | null;
  is_active?: boolean;
  department_name?: string | null;
  company_id?: string | null;
  company_name?: string | null;
  employment_type?: string | null;
}
interface PreviewRow {
  personalnummer: string;
  lohnart: string | number;
  wert: string | number;
  datum_von?: string;
  datum_bis?: string;
  kostenstelle?: string;
}
interface PreviewResp {
  rows: PreviewRow[];
  year_month: string;
  row_count: number;
}
interface GenerateResp {
  id: string;
  [key: string]: any;
}

// --- Stammdaten / Auswahl ---------------------------------------------------
const employees = ref<Employee[]>([]);
const selectedIds = ref<Set<string>>(new Set());
const loading = ref(true);
const loadError = ref("");

// --- Firma-Filter (clientseitig, basierend auf geladener Mitarbeiterliste) ---
const companyFilter = ref(""); // "" = Alle Firmen

// --- Firmen inkl. DATEV-Mandantendaten ---
const companies = ref<
  { id: string; name: string; advisor_number?: string | null; client_number?: string | null }[]
>([]);
const selectedCompany = computed(
  () => companies.value.find((c) => c.name === companyFilter.value) || null,
);

const companyOptions = computed(() => {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const e of employees.value) {
    const c = e.company_name;
    if (c && !seen.has(c)) {
      seen.add(c);
      out.push(c);
    }
  }
  out.sort((a, b) => a.localeCompare(b, "de"));
  return out;
});

// Nach gewählter Firma gefilterte (anzeigbare/auswählbare) Mitarbeiter.
const filteredEmployees = computed(() => {
  if (!companyFilter.value) return employees.value;
  return employees.value.filter((e) => e.company_name === companyFilter.value);
});

// Export-Kreis: Firma × Mitarbeiterkreis (Lohn/Gehalt). Das ist die Menge,
// die tatsaechlich exportiert wird — standardmaessig komplett ausgewaehlt
// ("alle Lohn-Empfaenger der Firma kommen in den Export").
const scopeEmployees = computed(() => {
  if (!employmentType.value) return filteredEmployees.value;
  // "Lohn" umfasst Stundenlohn UND Werkstudenten (Ist-Stunden als Zeitlohn-Zeile).
  return filteredEmployees.value.filter((e) =>
    employmentType.value === "hourly"
      ? e.employment_type === "hourly" || e.employment_type === "werkstudent"
      : e.employment_type === employmentType.value
  );
});
// Fuer die aufgeklappte Anpass-Liste: sauber nach Lohn/Gehalt getrennt.
const lohnEmployees = computed(() =>
  filteredEmployees.value.filter((e) => e.employment_type === "hourly" || e.employment_type === "werkstudent")
);
const gehaltEmployees = computed(() =>
  filteredEmployees.value.filter((e) => e.employment_type === "salaried")
);

// --- Zeitraum (nur ganze Monate) -------------------------------------------
const now = new Date();
const month = ref(now.getMonth() + 1); // 1..12
const year = ref(now.getFullYear());

const monthNames = [
  "Januar", "Februar", "März", "April", "Mai", "Juni",
  "Juli", "August", "September", "Oktober", "November", "Dezember",
];
const months = monthNames.map((label, i) => ({ value: i + 1, label }));
const years = computed(() => {
  const y = now.getFullYear();
  return [y - 2, y - 1, y, y + 1];
});

function setCurrentMonth() {
  month.value = now.getMonth() + 1;
  year.value = now.getFullYear();
}
function setLastMonth() {
  const d = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  month.value = d.getMonth() + 1;
  year.value = d.getFullYear();
}

// --- Vorschau / Erzeugung ---------------------------------------------------
const preview = ref<PreviewResp | null>(null);
const previewLoading = ref(false);
const previewError = ref("");

const generating = ref(false);
const generateError = ref("");
const generatedId = ref<string | null>(null);
const generatedFileName = ref<string | null>(null);

const downloading = ref(false);
const downloadError = ref("");

const mm = computed(() => String(month.value).padStart(2, "0"));
const fileName = computed(() => `LODAS_${year.value}-${mm.value}.txt`);
const periodLabel = computed(() => `${monthNames[month.value - 1]} ${year.value}`);

// Personalnummer -> Name, um Preview-Zeilen zu beschriften.
const nameByPersonalnummer = computed(() => {
  const map: Record<string, string> = {};
  for (const e of employees.value) {
    if (e.employee_number) map[String(e.employee_number)] = e.name;
  }
  return map;
});

function nameFor(pnr: string | number): string {
  return nameByPersonalnummer.value[String(pnr)] || "—";
}

// Mitarbeiterkreis: Standard nur Lohn-Empfänger (Stundenlohn) — Gehaltsempfänger
// laufen fix über ihre Gehalts-Lohnart und werden nicht als Stunden exportiert.
const employmentType = ref<string>("hourly");

function requestBody() {
  return {
    year: year.value,
    month: month.value,
    company_id: selectedCompany.value?.id || null,
    employee_ids: [...selectedIds.value],
    employment_type: employmentType.value || null,
  };
}

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    // Gelöschte/inaktive Mitarbeiter gehören nicht in den Lohnexport.
    const all = await apiFetch<Employee[]>("/admin/employees");
    employees.value = all.filter((e) => e.is_active !== false);
    resetSelectionToScope();
    try {
      companies.value = await apiFetch("/companies");
    } catch {}
  } catch (e: any) {
    loadError.value = e?.data?.detail || "Mitarbeiter konnten nicht geladen werden.";
  }
  loading.value = false;
}
onMounted(load);

// Standard: der komplette Export-Kreis ist ausgewaehlt. Wer einzelne
// Mitarbeiter ausnehmen will, klappt "Auswahl anpassen" auf.
function resetSelectionToScope() {
  selectedIds.value = new Set(scopeEmployees.value.map((e) => e.id));
}
// Firma oder Mitarbeiterkreis gewechselt -> Auswahl zurueck auf den Standard.
watch([companyFilter, employmentType], resetSelectionToScope);

function toggle(id: string) {
  const next = new Set(selectedIds.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  selectedIds.value = next;
}
// „Alle/Keine" wirken auf den Export-Kreis.
function selectAll() {
  resetSelectionToScope();
}
function selectNone() {
  selectedIds.value = new Set();
}

// Einklappbare Einzelauswahl (Standard: zu — der Kreis ist komplett drin).
const adjustOpen = ref(false);

// Auswahl/Zeitraum ändern → bereits erzeugte Datei ist nicht mehr aktuell.
function invalidateResult() {
  preview.value = null;
  generatedId.value = null;
}
watch([month, year, selectedIds, companyFilter, employmentType], invalidateResult);

async function loadPreview() {
  previewLoading.value = true;
  previewError.value = "";
  generatedId.value = null;
  try {
    preview.value = await apiFetch<PreviewResp>("/export/datev/preview", {
      method: "POST",
      body: requestBody(),
    });
  } catch (e: any) {
    preview.value = null;
    previewError.value = e?.data?.detail || "Vorschau konnte nicht geladen werden.";
  }
  previewLoading.value = false;
}

async function generate() {
  generating.value = true;
  generateError.value = "";
  downloadError.value = "";
  try {
    const res = await apiFetch<GenerateResp>("/export/datev/generate", {
      method: "POST",
      body: requestBody(),
    });
    generatedId.value = res.id;
    generatedFileName.value = res.file_name || null;
  } catch (e: any) {
    generatedId.value = null;
    generateError.value = e?.data?.detail || "LODAS-Datei konnte nicht erzeugt werden.";
  }
  generating.value = false;
}

// Überstunden neu berechnen: vorhandenen Export des Monats stornieren, dann die
// Zeiten des Monats noch einmal auswerten (z. B. nach nachträglichen Korrekturen).
// Erst NACH Monatsende möglich: der laufende Monat rechnet ohnehin laufend mit,
// ein Lauf mitten im Monat würde Teilwerte einfrieren.
const resettleBusy = ref(false);
const resettleMsg = ref("");
const resettleErr = ref("");
// Zweistufig: der Knopf sitzt direkt unter "LODAS-Datei erzeugen" — ein Fehlgriff
// hat am 24.08.2026 den laufenden Monat abgeschlossen. Erst Nachfrage, dann Lauf.
const resettleConfirm = ref(false);
const istLaufenderMonat = computed(
  () => year.value === now.getFullYear() && month.value === now.getMonth() + 1
);
const istZukunft = computed(
  () => year.value > now.getFullYear()
    || (year.value === now.getFullYear() && month.value > now.getMonth() + 1)
);
const neuberechnenGesperrt = computed(() => istLaufenderMonat.value || istZukunft.value);
const neuberechnenHinweis = computed(() => {
  if (istZukunft.value) return "Dieser Monat liegt in der Zukunft.";
  if (istLaufenderMonat.value)
    return "Der laufende Monat rechnet automatisch mit — neu berechnen geht ab dem 1. des Folgemonats.";
  return "Wertet die Zeiten dieses Monats noch einmal aus — nötig, wenn Stempelzeiten oder Abwesenheiten nachträglich geändert wurden. Ein bereits erzeugter Export dieses Monats wird dabei storniert.";
});
async function resettleMonth() {
  resettleBusy.value = true; resettleMsg.value = ""; resettleErr.value = "";
  try {
    const hist = await apiFetch<{ id: string; year_month: string; status: string }[]>("/export/datev/history");
    const ym = `${year.value}-${String(month.value).padStart(2, "0")}`;
    for (const h of hist || []) {
      if (String(h.year_month || "").startsWith(ym)) {
        await apiFetch(`/export/datev/${h.id}`, { method: "DELETE" });
      }
    }
    const res = await apiFetch<{ resettled: number }>("/export/datev/resettle", {
      method: "POST", body: { year: year.value, month: month.value },
    });
    resettleMsg.value = `Überstunden neu berechnet (${res.resettled} Mitarbeiter). Jetzt die LODAS-Datei neu erzeugen.`;
    generatedId.value = null;
  } catch (e: any) {
    resettleErr.value = e?.data?.detail || "Neuberechnung fehlgeschlagen.";
  }
  resettleConfirm.value = false;
  resettleBusy.value = false;
}


// --- Monatsuebersicht (Excel): die sechs Spalten der bisherigen Handliste ---
// Ersetzt das Abtippen der Lohn-/Gehaltslisten. Bewusst getrennt vom
// DATEV-Export: andere Zielgruppe (Buchhaltung kopiert die Spalten in ihre
// Haupt-Excel), anderes Format, eigener Zeitraum (auch mehrere Monate).
const ovCompanyId = ref("");
const ovType = ref<"salaried" | "hourly">("salaried");
const ovYearFrom = ref(new Date().getFullYear());
const ovMonthFrom = ref(new Date().getMonth() + 1);
const ovMultiple = ref(false);
const ovYearTo = ref(new Date().getFullYear());
const ovMonthTo = ref(new Date().getMonth() + 1);
const ovBusy = ref(false);
const ovError = ref("");
const MONATE = ["Januar","Februar","M\u00e4rz","April","Mai","Juni","Juli","August","September","Oktober","November","Dezember"];

async function downloadOverview() {
  ovError.value = "";
  if (!ovCompanyId.value) { ovError.value = "Bitte eine Firma w\u00e4hlen."; return; }
  ovBusy.value = true;
  try {
    const q = new URLSearchParams({
      company_id: ovCompanyId.value,
      employment_type: ovType.value,
      year_from: String(ovYearFrom.value),
      month_from: String(ovMonthFrom.value),
    });
    if (ovMultiple.value) {
      q.set("year_to", String(ovYearTo.value));
      q.set("month_to", String(ovMonthTo.value));
    }
    const blob = await apiFetch<Blob>(`/export/monthly-overview.xlsx?${q}`, { responseType: "blob" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const art = ovType.value === "hourly" ? "Lohn" : "Gehalt";
    a.download = `Monatsuebersicht_${art}_${ovYearFrom.value}-${String(ovMonthFrom.value).padStart(2, "0")}.xlsx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e: any) {
    ovError.value = e?.data?.detail || "Export fehlgeschlagen.";
  }
  ovBusy.value = false;
}

async function download() {
  if (!generatedId.value) return;
  downloading.value = true;
  downloadError.value = "";
  try {
    // Download braucht den Bearer-Token im Header → als Blob über apiFetch holen
    // und clientseitig herunterladen (einfacher Link würde den Token nicht senden).
    const blob = await apiFetch<Blob>(
      `/export/datev/${generatedId.value}/download`,
      { responseType: "blob" }
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = generatedFileName.value || fileName.value;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e: any) {
    downloadError.value = e?.data?.detail || "Download fehlgeschlagen.";
  }
  downloading.value = false;
}

const selectedCount = computed(() => selectedIds.value.size);
const scopeCount = computed(() => scopeEmployees.value.length);
const kreisLabel = computed(() =>
  employmentType.value === "hourly"
    ? "Lohn-Empfänger"
    : employmentType.value === "salaried"
      ? "Gehaltsempfänger"
      : "Mitarbeiter"
);
const canRun = computed(() => selectedCount.value > 0);
// Erzeugen nur mit genau EINER Firma (DATEV-Mandant) inkl. gepflegter Berater-/Mandanten-Nr.
const canGenerate = computed(
  () =>
    canRun.value &&
    !!selectedCompany.value &&
    !!selectedCompany.value.advisor_number &&
    !!selectedCompany.value.client_number
);
</script>

<template>
  <div>
    <div class="spread" style="margin-bottom: 18px">
      <h1>Export</h1>
      <span class="muted num">DATEV LODAS</span>
    </div>

    <div v-if="loadError" class="wip-banner" style="margin-bottom: 16px">{{ loadError }}</div>

    <div class="export-grid">
      <!-- LINKS: Schritte -->
      <div class="stack" style="gap: 16px">
        <!-- Schritt 1: Abrechnungszeitraum -->
        <div class="card">
          <div class="spread" style="margin-bottom: 12px">
            <h2>1 · Abrechnungszeitraum</h2>
          </div>
          <div class="row" style="flex-wrap: wrap; gap: 10px">
            <div class="stack" style="gap: 4px">
              <span class="label">Monat</span>
              <select v-model.number="month" class="input" style="width: auto">
                <option v-for="m in months" :key="m.value" :value="m.value">{{ m.label }}</option>
              </select>
            </div>
            <div class="stack" style="gap: 4px">
              <span class="label">Jahr</span>
              <select v-model.number="year" class="input" style="width: auto">
                <option v-for="y in years" :key="y" :value="y">{{ y }}</option>
              </select>
            </div>
            <div class="row" style="gap: 6px; align-self: flex-end">
              <button class="btn" style="padding: 7px 12px" @click="setCurrentMonth">Aktueller Monat</button>
              <button class="btn" style="padding: 7px 12px" @click="setLastMonth">Vormonat</button>
            </div>
          </div>
        </div>

        <!-- Schritt 2: DATEV-Mandant -->
        <div class="card">
          <div class="spread" style="margin-bottom: 12px">
            <h2>2 · DATEV-Mandant</h2>
          </div>
          <p v-if="!companyFilter" class="muted" style="margin: 0; font-size: 12.5px">
            Bitte oben eine Firma auswählen, um den DATEV-Mandanten zu sehen.
          </p>
          <template v-else>
            <div class="row" style="flex-wrap: wrap; gap: 10px">
              <div class="stack" style="gap: 4px; flex: 1; min-width: 160px">
                <span class="label">Berater-Nr.</span>
                <input class="input" :value="selectedCompany?.advisor_number || '—'" disabled />
              </div>
              <div class="stack" style="gap: 4px; flex: 1; min-width: 160px">
                <span class="label">Mandanten-Nr.</span>
                <input class="input" :value="selectedCompany?.client_number || '—'" disabled />
              </div>
            </div>
            <p
              v-if="!selectedCompany?.advisor_number || !selectedCompany?.client_number"
              class="muted"
              style="margin: 10px 0 0; font-size: 12.5px"
            >
              Für diese Firma noch keine DATEV-Nummern hinterlegt — in Einstellungen → Firmen pflegen.
            </p>
          </template>
        </div>

        <!-- Schritt 3: Vorschau -->
        <div class="card" style="padding: 0">
          <div class="spread" style="padding: 16px 18px 12px">
            <h2>3 · Vorschau</h2>
            <button class="btn" :disabled="!canRun || previewLoading" @click="loadPreview">
              {{ previewLoading ? "Lädt …" : "Vorschau laden" }}
            </button>
          </div>

          <div v-if="previewError" class="wip-banner" style="margin: 0 16px 16px">{{ previewError }}</div>
          <div v-else-if="previewLoading" class="empty">Vorschau wird geladen …</div>
          <div v-else-if="!preview" class="empty">
            Zeitraum und Mitarbeiter wählen, dann „Vorschau laden“.
          </div>
          <div v-else-if="!preview.rows.length" class="empty">
            Keine Export-Zeilen für {{ periodLabel }}.
          </div>
          <template v-else>
            <table class="data">
              <thead>
                <tr>
                  <th>Mitarbeiter</th>
                  <th>Pers.-Nr.</th>
                  <th>Lohnart</th>
                  <th style="text-align: right">Wert</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(r, i) in preview.rows" :key="i">
                  <td style="font-weight: 600">{{ nameFor(r.personalnummer) }}</td>
                  <td class="num">{{ r.personalnummer }}</td>
                  <td class="num">{{ r.lohnart }}</td>
                  <td class="num" style="text-align: right">{{ r.wert }}</td>
                </tr>
              </tbody>
            </table>
            <p class="muted" style="padding: 12px 18px; margin: 0; font-size: 12.5px">
              {{ preview.row_count }} Zeilen · {{ preview.year_month }}
            </p>
          </template>
        </div>
        <!-- Monatsuebersicht: eigener Block unter der Vorschau, weil er nichts
             mit dem DATEV-Ablauf (1-3) zu tun hat — andere Zielgruppe, eigener
             Zeitraum. -->
        <div class="card ov-card">
          <h2>Monatsübersicht (Excel)</h2>
          <p class="muted ov-hint">
            Die sechs Spalten der bisherigen Handliste — Personalnummer, Name, U-Tage Vormonat,
            U-Tage Monat, genommene U-Tage, Krank. Zum Kopieren in die Haupt-Excel.
            Je Firma und Lohn/Gehalt eine eigene Datei; ein Blatt pro Monat.
          </p>
          <div class="ov-form">
            <label class="ov-field">
              <span class="label">Firma</span>
              <select v-model="ovCompanyId" class="input ov-input">
                <option value="">– wählen –</option>
                <option v-for="c in companies" :key="c.id" :value="c.id">{{ c.name }}</option>
              </select>
            </label>
            <label class="ov-field">
              <span class="label">Art</span>
              <select v-model="ovType" class="input ov-input">
                <option value="salaried">Gehalt</option>
                <option value="hourly">Lohn</option>
              </select>
            </label>
            <label class="ov-field">
              <span class="label">Monat</span>
              <div class="ov-row">
                <select v-model.number="ovMonthFrom" class="input ov-input">
                  <option v-for="(m, i) in MONATE" :key="m" :value="i + 1">{{ m }}</option>
                </select>
                <input v-model.number="ovYearFrom" type="number" class="input ov-input ov-year" />
              </div>
            </label>
            <label class="ov-field ov-check">
              <span class="label">&nbsp;</span>
              <span class="ov-input ov-checkbox">
                <input v-model="ovMultiple" type="checkbox" />
                <span class="muted">bis Monat</span>
              </span>
            </label>
            <label v-if="ovMultiple" class="ov-field">
              <span class="label">bis</span>
              <div class="ov-row">
                <select v-model.number="ovMonthTo" class="input ov-input">
                  <option v-for="(m, i) in MONATE" :key="m" :value="i + 1">{{ m }}</option>
                </select>
                <input v-model.number="ovYearTo" type="number" class="input ov-input ov-year" />
              </div>
            </label>
            <div class="ov-field">
              <span class="label">&nbsp;</span>
              <button class="btn btn-primary ov-input" :disabled="ovBusy" @click="downloadOverview">
                {{ ovBusy ? "Erstellt …" : "Excel herunterladen" }}
              </button>
            </div>
          </div>
          <div v-if="ovError" class="wip-banner" style="margin-top: 10px">{{ ovError }}</div>
        </div>
      </div>

      <!-- RECHTS: sticky Auswahl + Zusammenfassung -->
      <aside class="export-side">
        <div class="card">
          <h2 style="margin-bottom: 12px">Mitarbeiter</h2>

          <div v-if="!loading && companyOptions.length" class="stack" style="gap: 4px; margin-bottom: 10px">
            <span class="label">Firma</span>
            <select v-model="companyFilter" class="input">
              <option value="">Alle Firmen</option>
              <option v-for="c in companyOptions" :key="c" :value="c">{{ c }}</option>
            </select>
          </div>

          <div v-if="!loading" class="stack" style="gap: 4px; margin-bottom: 12px">
            <span class="label">Beschäftigungsart</span>
            <select
              v-model="employmentType"
              class="input"
              title="Gehaltsempfänger laufen fix über ihre Gehalts-Lohnart und werden üblicherweise nicht als Stunden exportiert"
            >
              <option value="hourly">Nur Lohn-Empfänger</option>
              <option value="salaried">Nur Gehaltsempfänger</option>
              <option value="">Alle Mitarbeiter</option>
            </select>
          </div>

          <div v-if="loading" class="empty">Lädt …</div>
          <div v-else-if="!employees.length" class="empty">Keine Mitarbeiter.</div>
          <div v-else-if="!scopeEmployees.length" class="empty">
            {{ companyFilter ? `Keine ${kreisLabel} in dieser Firma.` : `Keine ${kreisLabel} vorhanden.` }}
          </div>
          <template v-else>
            <p class="muted" style="margin: 0 0 10px; font-size: 13px">
              <template v-if="selectedCount === scopeCount">
                Alle {{ scopeCount }} {{ kreisLabel }}{{ companyFilter ? ` von ${companyFilter}` : "" }} kommen in den Export.
              </template>
              <template v-else>
                {{ selectedCount }} von {{ scopeCount }} {{ kreisLabel }}{{ companyFilter ? ` von ${companyFilter}` : "" }} im Export.
              </template>
            </p>
            <button class="linkbtn" style="font-size: 13px" @click="adjustOpen = !adjustOpen">
              {{ adjustOpen ? "▾" : "▸" }} Auswahl anpassen
            </button>

            <div v-if="adjustOpen" style="margin-top: 10px">
              <div class="row" style="gap: 6px; margin-bottom: 8px">
                <button class="btn" style="padding: 5px 10px" @click="selectAll">Alle</button>
                <button class="btn" style="padding: 5px 10px" @click="selectNone">Keine</button>
              </div>
              <div class="emp-list">
                <template v-if="employmentType !== 'salaried' && lohnEmployees.length">
                  <div class="label" style="padding: 6px 0 2px">Lohn-Empfänger</div>
                  <label v-for="e in lohnEmployees" :key="e.id" class="emp-row">
                    <input type="checkbox" :checked="selectedIds.has(e.id)" @change="toggle(e.id)" />
                    <span class="stack" style="gap: 1px">
                      <span style="font-weight: 600">
                        {{ e.name }}<template v-if="!companyFilter && e.company_name"> <span class="muted" style="font-weight: 400; font-size: 12px">· {{ e.company_name }}</span></template>
                      </span>
                      <span class="muted" style="font-size: 12px">
                        {{ e.employee_number || "—" }}<template v-if="e.department_name"> · {{ e.department_name }}</template>
                      </span>
                    </span>
                  </label>
                </template>
                <template v-if="employmentType !== 'hourly' && gehaltEmployees.length">
                  <div class="label" style="padding: 6px 0 2px">Gehaltsempfänger</div>
                  <label v-for="e in gehaltEmployees" :key="e.id" class="emp-row">
                    <input type="checkbox" :checked="selectedIds.has(e.id)" @change="toggle(e.id)" />
                    <span class="stack" style="gap: 1px">
                      <span style="font-weight: 600">
                        {{ e.name }}<template v-if="!companyFilter && e.company_name"> <span class="muted" style="font-weight: 400; font-size: 12px">· {{ e.company_name }}</span></template>
                      </span>
                      <span class="muted" style="font-size: 12px">
                        {{ e.employee_number || "—" }}<template v-if="e.department_name"> · {{ e.department_name }}</template>
                      </span>
                    </span>
                  </label>
                </template>
              </div>
            </div>
          </template>
        </div>

        <div class="card" style="margin-top: 16px">
          <h2 style="margin-bottom: 12px">Zusammenfassung</h2>
          <div class="stack" style="gap: 8px">
            <div class="spread">
              <span class="muted">Firma</span>
              <span>{{ companyFilter || "—" }}</span>
            </div>
            <div class="spread">
              <span class="muted">{{ kreisLabel }}</span>
              <span class="num">{{ selectedCount }} / {{ scopeCount }}</span>
            </div>
            <div class="spread">
              <span class="muted">Zeitraum</span>
              <span>{{ periodLabel }}</span>
            </div>
            <div class="spread">
              <span class="muted">Dateiname</span>
              <span class="num">{{ fileName }}</span>
            </div>
          </div>

          <button
            class="btn btn-primary btn-block"
            style="margin-top: 14px"
            :disabled="!canGenerate || generating"
            @click="generate"
          >
            {{ generating ? "Wird erzeugt …" : "LODAS-Datei erzeugen" }}
          </button>
          <div v-if="canRun && !canGenerate" class="muted" style="margin-top: 8px; font-size: 12.5px">
            <template v-if="!companyFilter">Bitte oben eine Firma wählen — der Export braucht die Berater-/Mandanten-Nr. der Firma.</template>
            <template v-else>Für diese Firma fehlen Berater-/Mandanten-Nr. (in Einstellungen → Firmen pflegen).</template>
          </div>

          <div v-if="generateError" class="wip-banner" style="margin-top: 10px">{{ generateError }}</div>

          <!-- Bewusst abgesetzt und leiser als der Haupt-Knopf: seltene Korrektur,
               kein Schritt im normalen Ablauf (Fehlklick 24.08.2026). -->
          <div style="margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--divider)">
            <div class="label" style="margin-bottom: 4px">Nachträgliche Korrektur</div>
            <div class="muted" style="font-size: 12px; margin-bottom: 8px">{{ neuberechnenHinweis }}</div>
            <template v-if="resettleConfirm">
              <div class="muted" style="font-size: 12.5px; margin-bottom: 6px">
                Überstunden für {{ monthNames[month - 1] }} {{ year }} wirklich neu berechnen?
              </div>
              <button class="linkbtn danger" :disabled="resettleBusy" @click="resettleMonth">
                {{ resettleBusy ? "Rechnet neu …" : "Ja, neu berechnen" }}
              </button>
              <button class="linkbtn" style="margin-left: 12px" :disabled="resettleBusy"
                @click="resettleConfirm = false">Abbrechen</button>
            </template>
            <button v-else class="linkbtn" :disabled="resettleBusy || neuberechnenGesperrt"
              :title="neuberechnenHinweis" @click="resettleConfirm = true">Überstunden neu berechnen</button>
          </div>
          <div v-if="resettleMsg" class="muted" style="margin-top: 8px; font-size: 12.5px">{{ resettleMsg }}</div>
          <div v-if="resettleErr" class="wip-banner" style="margin-top: 8px">{{ resettleErr }}</div>

          <template v-if="generatedId">
            <button
              class="btn btn-block"
              style="margin-top: 10px"
              :disabled="downloading"
              @click="download"
            >
              {{ downloading ? "Lädt herunter …" : "↓ Datei herunterladen" }}
            </button>
            <div v-if="downloadError" class="wip-banner" style="margin-top: 10px">{{ downloadError }}</div>
          </template>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.export-grid {
  display: grid;
  grid-template-columns: 1fr 320px;
  gap: 18px;
  align-items: start;
}
.export-side {
  position: sticky;
  top: 24px;
}
.emp-list {
  display: flex;
  flex-direction: column;
  max-height: 360px;
  overflow-y: auto;
}
.emp-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 4px;
  border-bottom: 1px solid var(--divider);
  cursor: pointer;
}
.emp-row:last-child { border-bottom: none; }
.emp-row input { width: 16px; height: 16px; }
@media (max-width: 900px) {
  .export-grid { grid-template-columns: 1fr; }
  .export-side { position: static; }
}

/* Monatsuebersicht: alle Bedienelemente exakt gleich hoch und an einer
   Grundlinie ausgerichtet — vorher hatten Selects, Zahlenfeld und Button
   je nach Browser unterschiedliche Hoehen. */
.ov-card { margin-bottom: 16px; }
.ov-hint { font-size: 12px; margin: 4px 0 14px; line-height: 1.45; }
.ov-form {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 12px;
}
.ov-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
}
/* Feste Label-Hoehe: sonst ist das Monat-Feld (Label + verschachtelte Zeile
   aus Monat+Jahr) hoeher als die einfachen Felder, und weil die Zeile unten
   ausgerichtet wird, rutscht sein Label nach oben. Mit gleicher Label- und
   Control-Hoehe stehen alle Felder exakt auf einer Linie. */
.ov-field .label {
  display: block;
  height: 15px;
  line-height: 15px;
  margin: 0;
}
.ov-row { display: flex; gap: 6px; height: 36px; align-items: center; }
.ov-input {
  height: 36px;
  box-sizing: border-box;
  padding: 0 10px;
  line-height: 34px;
}
select.ov-input { padding-right: 26px; }
.ov-year { width: 84px; }
.ov-checkbox {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 4px;
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}
.ov-checkbox input { margin: 0; cursor: pointer; }
.ov-form .btn { display: inline-flex; align-items: center; }
</style>
