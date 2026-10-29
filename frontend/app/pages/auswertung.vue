<script setup lang="ts">
// Auswertung / Stundenzettel — Monatsauswertung je Mitarbeiter.
// Quellen: GET /admin/monthly-report?year=&month= (Report), GET /admin/employees
// (Anreicherung Bereich/Firma + inaktive ausfiltern), GET /overtime/balances (Saldo).
// Excel-Export clientseitig als CSV (BOM, ";"). PDF via window.print().
import type { Reconciliation } from "~/types/monatsabgleich";
import { dez2, hmm, hmmH, payoutModeLabel, tage1 } from "~/utils/zeitformat";

const { apiFetch } = useApi();

interface ReportRow {
  employee_id: string;
  employee_name: string;
  employee_number?: string | null;
  days_worked?: number | null;
  total_work_minutes?: number | null;
  total_overtime_minutes?: number | null;
  total_break_minutes?: number | null;
  days_vacation?: number | null;
  days_sick?: number | null;
  night_supplement_minutes?: number | null;
  sunday_supplement_minutes?: number | null;
  holiday_supplement_minutes?: number | null;
}
interface Employee {
  id: string;
  name: string;
  employee_number?: string | null;
  department_name?: string | null;
  company_name?: string | null;
  is_active?: boolean;
}
interface Row {
  employee_id: string;
  name: string;
  employee_number: string;
  department_name: string;
  company_name: string;
  days_worked: number;
  ist_h: number;
  ueberstd_monat_h: number;
  days_vacation: number;
  days_sick: number;
  saldo_h: number | null;
}

// --- Zeitraum ---------------------------------------------------------------
const now = new Date();
const year = ref(now.getFullYear());
const month = ref(now.getMonth() + 1); // 1..12

const monthNames = [
  "Januar", "Februar", "März", "April", "Mai", "Juni",
  "Juli", "August", "September", "Oktober", "November", "Dezember",
];
const months = monthNames.map((label, i) => ({ value: i + 1, label }));
const years = computed(() => {
  const y = now.getFullYear();
  return [y - 2, y - 1, y];
});

// --- Clientseitige Filter ---------------------------------------------------
const department = ref("Alle Bereiche");
const company = ref("Alle Firmen");
const search = ref("");

// --- Daten ------------------------------------------------------------------
const rows = ref<Row[]>([]);
const loading = ref(true);
const loadError = ref("");

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const report = await apiFetch<ReportRow[]>(
      `/admin/monthly-report?year=${year.value}&month=${month.value}`
    );
    // Anreicherung (Bereich/Firma) ist optional — bei fehlendem Recht Report trotzdem zeigen.
    let employees: Employee[] = [];
    try {
      employees = await apiFetch<Employee[]>("/admin/employees");
    } catch { employees = []; }

    // Salden separat — fehlertolerant.
    let balances: Record<string, number> = {};
    try {
      balances = await apiFetch<Record<string, number>>("/overtime/balances");
    } catch {
      balances = {};
    }

    // Nur aktive Mitarbeiter, per id nachschlagbar.
    const empById = new Map<string, Employee>();
    for (const e of employees) {
      if (e.is_active !== false) empById.set(e.id, e);
    }

    const out: Row[] = [];
    for (const r of report) {
      const e = empById.get(r.employee_id);
      // Nur ausfiltern, wenn die Mitarbeiterliste verfügbar ist (sonst Report pur zeigen).
      if (employees.length && !e) continue; // inaktive/unbekannte MA ausfiltern
      const saldo = balances[r.employee_id];
      out.push({
        employee_id: r.employee_id,
        name: r.employee_name || e?.name || "—",
        employee_number: String(r.employee_number ?? e?.employee_number ?? ""),
        department_name: e?.department_name || "",
        company_name: e?.company_name || "",
        days_worked: Number(r.days_worked ?? 0),
        ist_h: Number(r.total_work_minutes ?? 0) / 60,
        ueberstd_monat_h: Number(r.total_overtime_minutes ?? 0) / 60,
        days_vacation: Number(r.days_vacation ?? 0),
        days_sick: Number(r.days_sick ?? 0),
        saldo_h: typeof saldo === "number" ? saldo : null,
      });
    }
    out.sort((a, b) => a.name.localeCompare(b.name, "de"));
    rows.value = out;
    // Standard: alle Mitarbeiter ausgewählt.
    selected.clear();
    for (const r of out) selected.add(r.employee_id);
  } catch (e: any) {
    rows.value = [];
    loadError.value = e?.data?.detail || "Auswertung konnte nicht geladen werden.";
  }
  loading.value = false;
}
onMounted(load);
// Zeitraum ändern → Report neu laden.
watch([year, month], load);

// --- Filter-Optionen (aus geladenen Zeilen) --------------------------------
// Bereichsliste passend zur gewählten Firma filtern (kein Firmenfilter aktiv = alle).
const departments = computed(() => {
  const set = new Set<string>();
  for (const r of rows.value) {
    if (company.value !== "Alle Firmen" && r.company_name !== company.value) continue;
    if (r.department_name) set.add(r.department_name);
  }
  return ["Alle Bereiche", ...[...set].sort((a, b) => a.localeCompare(b, "de"))];
});
const companies = computed(() => {
  const set = new Set<string>();
  for (const r of rows.value) if (r.company_name) set.add(r.company_name);
  return ["Alle Firmen", ...[...set].sort((a, b) => a.localeCompare(b, "de"))];
});

// Firmenwechsel: gewählten Bereich zurücksetzen, wenn er zur neuen Firma nicht mehr passt.
watch(company, () => {
  if (!departments.value.includes(department.value)) department.value = "Alle Bereiche";
});

// --- Gefilterte Zeilen ------------------------------------------------------
const filtered = computed(() =>
  rows.value.filter((r) => {
    const q = search.value.trim().toLowerCase();
    if (q) {
      const hay = `${r.name} ${r.employee_number}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    if (department.value !== "Alle Bereiche" && r.department_name !== department.value) return false;
    if (company.value !== "Alle Firmen" && r.company_name !== company.value) return false;
    return true;
  })
);

// --- Mitarbeiter-Auswahl (für Export/Druck) --------------------------------
const selected = reactive(new Set<string>());
function isSelected(id: string): boolean { return selected.has(id); }
function toggle(id: string) { selected.has(id) ? selected.delete(id) : selected.add(id); }
function selectAll() { for (const r of filtered.value) selected.add(r.employee_id); }
function selectNone() { for (const r of filtered.value) selected.delete(r.employee_id); }
const allSelected = computed(
  () => filtered.value.length > 0 && filtered.value.every((r) => selected.has(r.employee_id))
);
function toggleAll() { allSelected.value ? selectNone() : selectAll(); }
// Nur ausgewählte, sichtbare Zeilen — Basis für Export, Druck und Summe.
const selectedRows = computed(() => filtered.value.filter((r) => selected.has(r.employee_id)));

// --- Summenzeile (über die ausgewählten Zeilen) ----------------------------
const totals = computed(() => {
  const t = { days_worked: 0, ist_h: 0, days_vacation: 0, days_sick: 0 };
  for (const r of selectedRows.value) {
    t.days_worked += r.days_worked;
    t.ist_h += r.ist_h;
    t.days_vacation += r.days_vacation;
    t.days_sick += r.days_sick;
  }
  return t;
});

// --- Zahlenformatierung (deutsch, Komma) -----------------------------------
function num2(n: number): string {
  return n.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function hours(n: number): string {
  return `${num2(n)} h`;
}
function saldoStr(n: number | null): string {
  if (n == null) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${num2(n)} h`;
}
function days(n: number): string {
  return n.toLocaleString("de-DE", { maximumFractionDigits: 1 });
}

// --- Monatsabgleich je Mitarbeiter (Altsystem-Fussblock) ----------------------
// GET /admin/month-reconciliation — reine Leseansicht, wird je Zeile beim ersten
// Aufklappen geladen und beim Zeitraumwechsel verworfen. Block-Markup:
// components/MonatsabgleichBlock.vue (auch im Druck je Seite).
interface ReconState { loading: boolean; error: string; data: Reconciliation | null }

const expanded = reactive(new Set<string>());
const recon = reactive(new Map<string, ReconState>());

async function loadRecon(empId: string): Promise<ReconState> {
  const cached = recon.get(empId);
  if (cached?.data) return cached;
  recon.set(empId, { loading: true, error: "", data: null });
  // Ueber die Map lesen: nur der reaktive Proxy loest das Neurendern aus.
  const st = recon.get(empId)!;
  try {
    st.data = await apiFetch<Reconciliation>(
      `/admin/month-reconciliation?year=${year.value}&month=${month.value}&employee_id=${empId}`
    );
  } catch (e: any) {
    st.error = e?.data?.detail || "Monatsabgleich konnte nicht geladen werden.";
  }
  st.loading = false;
  return st;
}

async function toggleRecon(empId: string) {
  if (expanded.has(empId)) {
    expanded.delete(empId);
    return;
  }
  expanded.add(empId);
  await loadRecon(empId);
}
// Zeitraumwechsel: Blöcke gehören zum alten Monat -> alles zu, Cache leer.
watch([year, month], () => { expanded.clear(); recon.clear(); reconPrint.value = null; });

// --- Abgleich drucken: ein Mitarbeiter pro Seite (nur die ausgewählten) ------
interface ReconPage { row: Row; state: ReconState }
const reconPrint = ref<ReconPage[] | null>(null);
const reconPrintLoading = ref(false);
// Während des Abgleich-Drucks blendet die Root-Klasse alles andere aus.
const printMode = ref<"" | "recon">("");

async function printRecon() {
  if (!selectedRows.value.length || reconPrintLoading.value) return;
  reconPrintLoading.value = true;
  try {
    const states = await Promise.all(selectedRows.value.map((r) => loadRecon(r.employee_id)));
    reconPrint.value = selectedRows.value.map((row, i) => ({ row, state: states[i]! }));
    printMode.value = "recon";
    await nextTick();
    window.addEventListener("afterprint", () => { printMode.value = ""; }, { once: true });
    window.print();
  } finally {
    reconPrintLoading.value = false;
  }
}

const mm = computed(() => String(month.value).padStart(2, "0"));
const periodLabel = computed(() => `${monthNames[month.value - 1]} ${year.value}`);

// --- Excel (CSV) ------------------------------------------------------------
function csvCell(v: string): string {
  // Bei Sonderzeichen (; " Zeilenumbruch) in Anführungszeichen einschließen.
  if (/[";\n\r]/.test(v)) return `"${v.replace(/"/g, '""')}"`;
  return v;
}
function numCsv(n: number, digits = 2): string {
  return n.toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function exportCsv() {
  const header = [
    "Mitarbeiter", "Personalnr.", "Bereich", "Firma", "Tage",
    "Ist-Std", "Saldo (Monat)", "Urlaub (T)", "Krank (T)", "Überstd-Saldo",
  ];
  const lines: string[] = [header.map(csvCell).join(";")];
  for (const r of selectedRows.value) {
    lines.push(
      [
        r.name,
        r.employee_number || "—",
        r.department_name || "—",
        r.company_name || "—",
        numCsv(r.days_worked, 0),
        numCsv(r.ist_h),
        numCsv(r.ueberstd_monat_h),
        numCsv(r.days_vacation, 0),
        numCsv(r.days_sick, 0),
        r.saldo_h == null ? "—" : numCsv(r.saldo_h),
      ].map((c) => csvCell(String(c))).join(";")
    );
  }
  downloadCsv(lines, `Auswertung_${year.value}-${mm.value}.csv`);
}

function downloadCsv(lines: string[], filename: string) {
  // UTF-8-BOM voranstellen, damit Excel Umlaute korrekt anzeigt.
  const content = "﻿" + lines.join("\r\n");
  const blob = new Blob([content], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// --- Drucken / PDF ----------------------------------------------------------
function printPage() {
  window.print();
}

// --- Jahresansicht pro Mitarbeiter -------------------------------------------
// Klick auf einen Namen in der Monatstabelle -> 12-Monats-Aufschlüsselung
// (GET /admin/yearly-report) + Liste aller Abwesenheiten des Jahres
// (Urlaub/Krank inkl. AU-Status).
interface YearMonth {
  month: number;
  days_worked: number;
  work_minutes: number;
  overtime_minutes: number;
  days_vacation: number;
  days_sick: number;
  days_other_absence: number;
}
interface YearAbsence {
  id: string;
  request_type: string;
  date_from: string;
  date_to: string;
  day_part?: string | null;
  days_count: number;
  sick_status?: string | null;
}
interface YearReport {
  employee_id: string;
  employee_name: string;
  employee_number?: string | null;
  year: number;
  months: YearMonth[];
  absences: YearAbsence[];
}

const yearView = ref<YearReport | null>(null);
const yearViewEmpId = ref("");
const yearViewYear = ref(now.getFullYear());
const yearLoading = ref(false);
const yearErr = ref("");

async function openYearView(empId: string) {
  yearViewEmpId.value = empId;
  yearLoading.value = true;
  yearErr.value = "";
  try {
    yearView.value = await apiFetch<YearReport>(
      `/admin/yearly-report?year=${yearViewYear.value}&employee_id=${empId}`
    );
  } catch (e: any) {
    yearErr.value = e?.data?.detail || "Jahresauswertung konnte nicht geladen werden.";
    yearView.value = null;
  }
  yearLoading.value = false;
}
function closeYearView() {
  yearViewEmpId.value = "";
  yearView.value = null;
  yearErr.value = "";
  closeMonthSheet();
}
// Jahr wechseln -> neu laden.
watch(yearViewYear, () => {
  if (yearViewEmpId.value) openYearView(yearViewEmpId.value);
});

const typeLabels: Record<string, string> = {
  VACATION: "Urlaub",
  SICK_LEAVE: "Krank",
  UNPAID_LEAVE: "Unbezahlt",
  SPECIAL_LEAVE: "Sonderurlaub",
  UNEXCUSED_ABSENCE: "Unentschuldigt",
};
const sickStatusLabels: Record<string, string> = {
  EAU_TELEFON: "eAU (telefonisch)",
  AU_MIT_LFZ: "mit AU",
  OHNE_AU_MIT_LFZ: "ohne AU, mit LFZ",
  OHNE_AU_OHNE_LFZ: "ohne AU, ohne LFZ",
};
function typeBadge(t: string): string {
  if (t === "VACATION") return "green";
  if (t === "SICK_LEAVE") return "red";
  if (t === "UNEXCUSED_ABSENCE") return "amber";
  return "neutral";
}
function fmtD(iso: string): string {
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}.${m}.${y}`;
}

const yearTotals = computed(() => {
  const t = {
    days_worked: 0,
    work_minutes: 0,
    overtime_minutes: 0,
    days_vacation: 0,
    days_sick: 0,
    days_other_absence: 0,
  };
  for (const m of yearView.value?.months || []) {
    t.days_worked += m.days_worked;
    t.work_minutes += m.work_minutes;
    t.overtime_minutes += m.overtime_minutes;
    t.days_vacation += m.days_vacation;
    t.days_sick += m.days_sick;
    t.days_other_absence += m.days_other_absence;
  }
  return t;
});

// Excel (CSV) der Jahresansicht: Monatstabelle + Abwesenheitsliste in einer Datei.
function exportYearCsv() {
  const v = yearView.value;
  if (!v) return;
  const lines: string[] = [];
  lines.push(csvCell(`Jahresansicht ${v.year} — ${v.employee_name}` +
    (v.employee_number ? ` (Personalnr. ${v.employee_number})` : "")));
  lines.push("");
  lines.push(
    ["Monat", "Tage", "Ist-Std", "Saldo", "Urlaub (T)", "Krank (T)", "Sonst. abwesend (T)"]
      .map(csvCell).join(";")
  );
  for (const m of v.months) {
    lines.push(
      [
        monthNames[m.month - 1],
        numCsv(m.days_worked, 0),
        numCsv(m.work_minutes / 60),
        numCsv(m.overtime_minutes / 60),
        numCsv(m.days_vacation, 1),
        numCsv(m.days_sick, 1),
        numCsv(m.days_other_absence, 1),
      ].map((c) => csvCell(String(c))).join(";")
    );
  }
  const t = yearTotals.value;
  lines.push(
    [
      `Summe ${v.year}`,
      numCsv(t.days_worked, 0),
      numCsv(t.work_minutes / 60),
      numCsv(t.overtime_minutes / 60),
      numCsv(t.days_vacation, 1),
      numCsv(t.days_sick, 1),
      numCsv(t.days_other_absence, 1),
    ].map((c) => csvCell(String(c))).join(";")
  );
  lines.push("");
  lines.push(csvCell(`Abwesenheiten ${v.year}`));
  lines.push(["Von", "Bis", "Art", "AU-Status", "Tage"].map(csvCell).join(";"));
  for (const a of v.absences) {
    lines.push(
      [
        fmtD(a.date_from),
        fmtD(a.date_to),
        typeLabels[a.request_type] || a.request_type,
        a.sick_status ? (sickStatusLabels[a.sick_status] || a.sick_status) : "—",
        numCsv(a.days_count, 1),
      ].map((c) => csvCell(String(c))).join(";")
    );
  }
  const safeName = v.employee_name.replace(/[^\wäöüÄÖÜß-]+/g, "_");
  downloadCsv(lines, `Jahresansicht_${v.year}_${safeName}.csv`);
}

// --- Monats-Stundenzettel: ausgelagert nach StundenzettelSheet.vue --------
// Hier bleibt nur der Overlay-State (welcher Monat ist offen).
const sheetMonth = ref<number | null>(null);
function openMonthSheet(m: number) {
  sheetMonth.value = m;
}
function closeMonthSheet() {
  sheetMonth.value = null;
}
</script>

<template>
  <div :class="{ 'print-recon': printMode === 'recon' }">
    <div style="margin-bottom: 18px">
      <h1>Auswertung</h1>
      <div class="muted">
        {{ yearViewEmpId
          ? (sheetMonth != null ? "Monats-Stundenzettel je Mitarbeiter" : "Jahresansicht je Mitarbeiter")
          : "Stundenzettel je Mitarbeiter (Monat)" }}
      </div>
    </div>

    <template v-if="!yearViewEmpId">
    <!-- Filterkarte (nicht drucken) -->
    <div class="card no-print" style="padding: 14px; margin-bottom: 16px">
      <div class="row" style="flex-wrap: wrap; gap: 10px; align-items: flex-end">
        <div class="stack" style="gap: 4px">
          <span class="label">Jahr</span>
          <select v-model.number="year" class="input" style="width: auto">
            <option v-for="y in years" :key="y" :value="y">{{ y }}</option>
          </select>
        </div>
        <div class="stack" style="gap: 4px">
          <span class="label">Monat</span>
          <select v-model.number="month" class="input" style="width: auto">
            <option v-for="m in months" :key="m.value" :value="m.value">{{ m.label }}</option>
          </select>
        </div>
        <div class="stack" style="gap: 4px">
          <span class="label">Bereich</span>
          <select v-model="department" class="input" style="width: auto">
            <option v-for="d in departments" :key="d">{{ d }}</option>
          </select>
        </div>
        <div class="stack" style="gap: 4px">
          <span class="label">Firma</span>
          <select v-model="company" class="input" style="width: auto">
            <option v-for="c in companies" :key="c">{{ c }}</option>
          </select>
        </div>
        <div class="stack" style="gap: 4px; flex: 1; min-width: 200px">
          <span class="label">Suche</span>
          <input v-model="search" class="input" placeholder="Name oder Personalnr." />
        </div>
        <div class="row" style="gap: 8px; align-items: center">
          <button class="btn" style="padding: 7px 12px" @click="selectAll">Alle</button>
          <button class="btn" style="padding: 7px 12px" @click="selectNone">Keine</button>
          <span class="muted num" style="font-size: 12px">{{ selectedRows.length }}/{{ filtered.length }} gewählt</span>
          <button class="btn" :disabled="!selectedRows.length" @click="exportCsv">Excel (CSV)</button>
          <button class="btn btn-primary" :disabled="!selectedRows.length" @click="printPage">
            Drucken / PDF
          </button>
          <button
            class="btn"
            :disabled="!selectedRows.length || reconPrintLoading"
            title="Monatsabgleich der ausgewählten Mitarbeiter, ein Mitarbeiter pro Seite"
            @click="printRecon"
          >
            {{ reconPrintLoading ? "Lädt …" : "Abgleich drucken / PDF" }}
          </button>
        </div>
      </div>
    </div>

    <!-- Tabelle -->
    <div class="card" style="padding: 0">
      <div class="spread print-only" style="padding: 14px 18px 0">
        <h2 style="margin: 0">Auswertung {{ periodLabel }}</h2>
      </div>

      <div v-if="loadError" class="wip-banner" style="margin: 14px">{{ loadError }}</div>
      <div v-else-if="loading" class="empty">Lädt …</div>
      <div v-else-if="!filtered.length" class="empty">Keine Daten für diesen Zeitraum.</div>
      <table v-else class="data">
        <thead>
          <tr>
            <th class="no-print" style="width: 32px; text-align: center">
              <input type="checkbox" :checked="allSelected" title="Alle/Keine" @change="toggleAll" />
            </th>
            <th>Mitarbeiter</th>
            <th>Personalnr.</th>
            <th>Bereich</th>
            <th>Firma</th>
            <th style="text-align: right">Tage</th>
            <th style="text-align: right">Ist-Std</th>
            <th style="text-align: right" title="Ist − Soll des Monats, Plus und Minus verrechnet (wie Stundenzettel und Konto)">Saldo (Monat)</th>
            <th style="text-align: right">Urlaub (T)</th>
            <th style="text-align: right">Krank (T)</th>
            <th style="text-align: right">Überstd-Saldo</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="r in filtered" :key="r.employee_id">
          <tr :class="{ 'no-print': !isSelected(r.employee_id) }">
            <td class="no-print" style="text-align: center">
              <input type="checkbox" :checked="isSelected(r.employee_id)" @change="toggle(r.employee_id)" />
            </td>
            <td style="font-weight: 600">
              <button
                class="recon-toggle no-print"
                :class="{ open: expanded.has(r.employee_id) }"
                type="button"
                :title="expanded.has(r.employee_id) ? 'Monatsabgleich schließen' : 'Monatsabgleich anzeigen'"
                @click="toggleRecon(r.employee_id)"
              >›</button>
              <a
                class="linkbtn"
                style="cursor: pointer"
                title="Jahresansicht öffnen"
                @click="openYearView(r.employee_id)"
              >{{ r.name }}</a>
            </td>
            <td class="num">{{ r.employee_number || "—" }}</td>
            <td>{{ r.department_name || "—" }}</td>
            <td>{{ r.company_name || "—" }}</td>
            <td class="num" style="text-align: right">{{ days(r.days_worked) }}</td>
            <td class="num" style="text-align: right">{{ hours(r.ist_h) }}</td>
            <td class="num" style="text-align: right">{{ saldoStr(r.ueberstd_monat_h) }}</td>
            <td class="num" style="text-align: right">{{ days(r.days_vacation) }}</td>
            <td class="num" style="text-align: right">{{ days(r.days_sick) }}</td>
            <td class="num" :class="{ muted: r.saldo_h == null }" style="text-align: right">
              {{ saldoStr(r.saldo_h) }}
            </td>
          </tr>
          <!-- Monatsabgleich (aufgeklappt): Soll-Kette · Überstunden · Lohnarten -->
          <tr v-if="expanded.has(r.employee_id)" class="recon-row no-print">
            <td colspan="11">
              <div v-if="recon.get(r.employee_id)?.loading" class="muted">Monatsabgleich wird geladen …</div>
              <div v-else-if="recon.get(r.employee_id)?.error" class="wip-banner">
                {{ recon.get(r.employee_id)?.error }}
              </div>
              <MonatsabgleichBlock v-else-if="recon.get(r.employee_id)?.data" :data="recon.get(r.employee_id)!.data!" :period-label="periodLabel" />
            </td>
          </tr>
          </template>
        </tbody>
        <tfoot>
          <tr class="sum-row">
            <td class="no-print"></td>
            <td style="font-weight: 700">Summe</td>
            <td colspan="3" class="muted">{{ selectedRows.length }} Mitarbeiter</td>
            <td class="num" style="text-align: right; font-weight: 700">{{ days(totals.days_worked) }}</td>
            <td class="num" style="text-align: right; font-weight: 700">{{ hours(totals.ist_h) }}</td>
            <td class="num" style="text-align: right">—</td>
            <td class="num" style="text-align: right; font-weight: 700">{{ days(totals.days_vacation) }}</td>
            <td class="num" style="text-align: right; font-weight: 700">{{ days(totals.days_sick) }}</td>
            <td class="num" style="text-align: right">—</td>
          </tr>
        </tfoot>
      </table>
    </div>
    </template>

    <!-- ============ Jahresansicht pro Mitarbeiter ============ -->
    <template v-else>
      <div class="card no-print" style="padding: 14px; margin-bottom: 16px">
        <div class="row" style="flex-wrap: wrap; gap: 10px; align-items: flex-end">
          <button class="btn" style="padding: 7px 12px" @click="closeYearView">
            ‹ Monatsübersicht
          </button>
          <div class="stack" style="gap: 4px">
            <span class="label">Jahr</span>
            <select v-model.number="yearViewYear" class="input" style="width: auto">
              <option v-for="y in years" :key="y" :value="y">{{ y }}</option>
            </select>
          </div>
          <div style="flex: 1"></div>
          <button class="btn" :disabled="!yearView" @click="exportYearCsv">Excel (CSV)</button>
          <button class="btn btn-primary" :disabled="!yearView" @click="printPage">
            Drucken / PDF
          </button>
        </div>
      </div>

      <div v-if="yearErr" class="wip-banner" style="margin-bottom: 14px">{{ yearErr }}</div>
      <div v-else-if="yearLoading" class="card"><div class="empty">Lädt …</div></div>

      <template v-else-if="yearView">
        <div class="card" style="padding: 0; margin-bottom: 16px">
          <div class="spread" style="padding: 14px 18px 6px">
            <h2 style="margin: 0">
              {{ yearView.employee_name }}
              <span v-if="yearView.employee_number" class="muted" style="font-weight: 400">
                · Personalnr. {{ yearView.employee_number }}
              </span>
            </h2>
            <span class="muted">Jahresansicht {{ yearView.year }}</span>
          </div>
          <table class="data">
            <thead>
              <tr>
                <th>Monat</th>
                <th style="text-align: right">Tage</th>
                <th style="text-align: right">Ist-Std</th>
                <th style="text-align: right" title="Ist − Soll des Monats, Plus und Minus verrechnet">Saldo</th>
                <th style="text-align: right">Urlaub (T)</th>
                <th style="text-align: right">Krank (T)</th>
                <th style="text-align: right">Sonst. abwesend (T)</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="m in yearView.months"
                :key="m.month"
                class="month-row"
                title="Monats-Stundenzettel öffnen"
                @click="openMonthSheet(m.month)"
              >
                <td style="font-weight: 600">
                  <a class="linkbtn" style="cursor: pointer">{{ monthNames[m.month - 1] }}</a>
                </td>
                <td class="num" style="text-align: right">{{ days(m.days_worked) }}</td>
                <td class="num" style="text-align: right">{{ hours(m.work_minutes / 60) }}</td>
                <td class="num" style="text-align: right">{{ saldoStr(m.overtime_minutes / 60) }}</td>
                <td class="num" style="text-align: right" :class="{ muted: !m.days_vacation }">
                  {{ m.days_vacation ? days(m.days_vacation) : "—" }}
                </td>
                <td class="num" style="text-align: right" :class="{ muted: !m.days_sick }">
                  {{ m.days_sick ? days(m.days_sick) : "—" }}
                </td>
                <td class="num" style="text-align: right" :class="{ muted: !m.days_other_absence }">
                  {{ m.days_other_absence ? days(m.days_other_absence) : "—" }}
                </td>
              </tr>
            </tbody>
            <tfoot>
              <tr class="sum-row">
                <td style="font-weight: 700">Summe {{ yearView.year }}</td>
                <td class="num" style="text-align: right; font-weight: 700">{{ days(yearTotals.days_worked) }}</td>
                <td class="num" style="text-align: right; font-weight: 700">{{ hours(yearTotals.work_minutes / 60) }}</td>
                <td class="num" style="text-align: right; font-weight: 700">{{ saldoStr(yearTotals.overtime_minutes / 60) }}</td>
                <td class="num" style="text-align: right; font-weight: 700">{{ days(yearTotals.days_vacation) }}</td>
                <td class="num" style="text-align: right; font-weight: 700">{{ days(yearTotals.days_sick) }}</td>
                <td class="num" style="text-align: right; font-weight: 700">{{ days(yearTotals.days_other_absence) }}</td>
              </tr>
            </tfoot>
          </table>
        </div>

        <!-- Abwesenheiten des Jahres (wann war er/sie krank/im Urlaub) -->
        <div class="card" style="padding: 0">
          <div style="padding: 14px 18px 6px">
            <h2 style="margin: 0">Abwesenheiten {{ yearView.year }}</h2>
          </div>
          <div v-if="!yearView.absences.length" class="empty">
            Keine genehmigten Abwesenheiten in diesem Jahr.
          </div>
          <table v-else class="data">
            <thead>
              <tr>
                <th>Zeitraum</th>
                <th>Art</th>
                <th>AU-Status</th>
                <th style="text-align: right">Tage</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="a in yearView.absences" :key="a.id">
                <td class="num">
                  {{ fmtD(a.date_from) }}<template v-if="a.date_to !== a.date_from"> – {{ fmtD(a.date_to) }}</template>
                </td>
                <td>
                  <span class="badge" :class="typeBadge(a.request_type)">
                    <span class="dot"></span>{{ typeLabels[a.request_type] || a.request_type }}
                  </span>
                </td>
                <td :class="{ muted: !a.sick_status }">
                  {{ a.sick_status ? (sickStatusLabels[a.sick_status] || a.sick_status) : "—" }}
                </td>
                <td class="num" style="text-align: right">{{ days(a.days_count) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </template>

    <!-- Monats-Stundenzettel: gemeinsame Komponente (auch im MA-Profil) -->
    <StundenzettelSheet
      :open="sheetMonth != null"
      :employee-id="yearViewEmpId"
      :year="yearViewYear"
      :month="sheetMonth ?? 1"
      @close="closeMonthSheet"
    />

    <!-- Abgleich-Druck: nur im Druck sichtbar, ein Mitarbeiter pro Seite,
         Aufbau wie der Kopf-/Fussblock der Altsystem-Monatsblaetter -->
    <div v-if="reconPrint" class="recon-print">
      <section v-for="p in reconPrint" :key="p.row.employee_id" class="recon-page">
        <div class="at-head">
          <div class="at-cell at-name">{{ p.row.name }}</div>
          <div class="at-cell">
            <div class="at-kv"><span>Personalnr.</span><span>: {{ p.row.employee_number || "—" }}</span></div>
            <div class="at-kv"><span>Bereich</span><span>: {{ p.row.department_name || "—" }}</span></div>
            <div class="at-kv"><span>Firma</span><span>: {{ p.row.company_name || "—" }}</span></div>
          </div>
          <div class="at-cell at-month">{{ periodLabel }}</div>
        </div>

        <template v-if="p.state.data">
          <div class="at-foot">
            <div class="at-cell">
              <div class="at-kv"><span>Bruttosollzeit</span><span>: {{ hmm(p.state.data.stunden.brutto_soll_minutes) }}</span></div>
              <div class="at-kv"><span>effektive Sollzeit</span><span>: {{ hmm(p.state.data.stunden.effektiv_soll_minutes) }}</span></div>
              <div class="at-kv"><span>Istzeit</span><span>: {{ hmm(p.state.data.stunden.ist_minutes) }}</span></div>
              <div class="at-kv"><span>Saldo</span><span>: {{ hmm(p.state.data.stunden.saldo_minutes, true) }}</span></div>
              <div class="at-kv"><span>ausbezahlt Überstunden</span><span>: {{ hmmH(p.state.data.ueberstunden.ausgezahlt_hours) }}</span></div>
              <div class="at-kv"><span>davon verfallen</span><span>: {{ hmmH(p.state.data.ueberstunden.verfall_hours) }}</span></div>
              <div class="at-kv"><span>Korrekturen</span><span>: {{ hmmH(p.state.data.ueberstunden.korrektur_hours, true) }}</span></div>
              <div class="at-kv at-gap"><span>Gleitzeit-Vortrag</span><span>: {{ hmmH(p.state.data.ueberstunden.vortrag_hours, true) }}</span></div>
              <div class="at-kv"><span>Gleitzeit-Saldo</span><span>: {{ hmmH(p.state.data.ueberstunden.saldo_hours, true) }}</span></div>
              <div v-if="!p.state.data.ueberstunden.abgeschlossen" class="at-note">Monat noch nicht abgeschlossen — Gleitzeit-Zeilen sind eine Vorschau.</div>
            </div>
            <div class="at-cell">
              <div class="at-kv"><span>Nacht</span><span>: {{ hmm(p.state.data.zuschlaege.nacht_minutes) }}</span></div>
              <div class="at-kv"><span>Samstag</span><span>: {{ hmm(p.state.data.zuschlaege.samstag_minutes) }}</span></div>
              <div class="at-kv"><span>Sonntag</span><span>: {{ hmm(p.state.data.zuschlaege.sonntag_minutes) }}</span></div>
              <div class="at-kv"><span>Feiertag (gearbeitet)</span><span>: {{ hmm(p.state.data.zuschlaege.feiertag_minutes) }}</span></div>
              <div class="at-kv at-gap"><span>Modus</span><span>: {{ payoutModeLabel(p.state.data.ueberstunden.payout_mode) }}</span></div>
              <div v-if="p.state.data.ueberstunden.cap_hours != null" class="at-kv"><span>Kappe</span><span>: {{ dez2(p.state.data.ueberstunden.cap_hours) }} h</span></div>
            </div>
            <div class="at-cell">
              <div v-if="!p.state.data.lohnarten.length" class="at-note">keine Export-Zeilen</div>
              <div v-for="(la, i) in p.state.data.lohnarten" :key="i" class="at-kv at-la">
                <span>{{ String(la.lohnart).padStart(4, "0") }}-{{ la.bezeichnung || "Lohnart" }}</span>
                <span>: {{ hmmH(la.wert) }}<template v-if="la.tage != null">&nbsp;&nbsp;{{ tage1(la.tage) }} Tage</template></span>
              </div>
              <div class="at-kv at-gap"><span>Urlaubskonto</span><span>: {{ p.state.data.urlaub_rest_tage == null ? "—" : `${tage1(p.state.data.urlaub_rest_tage)} Tage` }}</span></div>
              <div class="at-kv"><span>Exportdatei</span><span>: {{ p.state.data.export_status === "DELIVERED" ? "übermittelt" : p.state.data.export_status === "GENERATED" ? "erzeugt" : "noch keine" }}</span></div>
            </div>
          </div>
        </template>
        <div v-else class="at-note">{{ p.state.error || "Kein Abgleich verfügbar." }}</div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.sum-row td {
  border-top: 2px solid var(--divider);
}
/* Monatsabgleich: Aufklapp-Pfeil vor dem Namen, Block als eingeschobene Zeile. */
.recon-toggle {
  border: none;
  background: transparent;
  cursor: pointer;
  color: var(--text-2);
  font-size: 16px;
  line-height: 1;
  width: 18px;
  margin-right: 4px;
  transition: transform 0.15s;
}
.recon-toggle.open {
  transform: rotate(90deg);
}
.recon-row td {
  background: var(--bg);
  padding: 14px 18px 16px;
}
/* Abgleich-Druck: am Bildschirm unsichtbar; im Druck ersetzt er die Seite.
   Aufbau wie das Altsystem-Monatsblatt: Kopfkasten (Name | Stammdaten | Monat),
   Fusskasten mit drei Spalten, Werte hinter Doppelpunkt. */
.recon-print {
  display: none;
  color: #000;
  font-size: 11px;
}
.at-head,
.at-foot {
  display: grid;
  border: 1.5px solid #000;
}
.at-head {
  grid-template-columns: 1.1fr 2fr 1fr;
  margin-bottom: 8px;
}
.at-foot {
  grid-template-columns: 1.2fr 1fr 1.4fr;
}
.at-cell {
  padding: 8px 10px;
  border-right: 1.5px solid #000;
}
.at-cell:last-child {
  border-right: none;
}
.at-name {
  font-weight: 700;
  font-size: 14px;
}
.at-month {
  font-weight: 700;
  font-size: 13px;
}
/* Wie auf dem Altsystem-Blatt: Bezeichnung in fester Spalte, Wert direkt
   hinter dem Doppelpunkt (nicht rechtsbuendig am Kastenrand). */
.at-kv {
  display: flex;
  gap: 6px;
  line-height: 1.55;
  font-variant-numeric: tabular-nums;
}
.at-kv > span:first-child {
  flex: 0 0 11.5em;
  white-space: nowrap;
}
.at-kv > span:last-child {
  white-space: nowrap;
}
.at-head .at-kv > span:first-child {
  flex-basis: 7em;
}
.at-la > span:first-child {
  flex-basis: 12.5em;
}
.at-gap {
  margin-top: 8px;
}
.at-note {
  margin-top: 6px;
  color: #444;
  font-style: italic;
}
/* Jahresansicht: Monatszeilen sind klickbar (öffnen den Stundenzettel). */
.month-row {
  cursor: pointer;
}
/* Nur beim Drucken sichtbar (Zeitraum-Überschrift). */
.print-only {
  display: none;
}

@media print {
  .no-print {
    display: none !important;
  }
  .print-recon > :not(.recon-print) {
    display: none !important;
  }
  .print-recon .recon-print {
    display: block;
  }
  .recon-page {
    page-break-after: always;
    break-after: page;
  }
  .recon-page:last-child {
    page-break-after: auto;
    break-after: auto;
  }
  .print-only {
    display: flex !important;
  }
  .card {
    border: none;
    box-shadow: none;
    padding: 0;
  }
  .data th,
  .data td {
    font-size: 11px;
  }
  h1 {
    font-size: 18px;
  }
}
</style>
