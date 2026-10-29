<script setup lang="ts">
// Monats-Stundenzettel als Overlay (wie das alte Altsystem-Blatt): pro Tag
// Kommt/Geht-Segmente, Pause, Ist/Soll/Differenz, Abwesenheit + Summenblock.
// Datenquelle: GET /admin/monthly-timesheet (Rechte: timesheet:view_all bzw.
// timesheet:view_team). Genutzt von auswertung.vue (Jahresansicht -> Monat)
// und dem Mitarbeiterprofil (Button am Arbeitszeit-Kalender).
const props = defineProps<{
  open: boolean;
  employeeId: string;
  year: number;
  month: number; // 1..12
}>();
const emit = defineEmits<{ close: [] }>();

const { apiFetch } = useApi();

interface SheetSegment {
  start: string;
  end: string;
  /** Rohstempel, wenn die Profil-Regel "fruehester Beginn" gekappt hat. */
  start_punched?: string | null;
  clipped_earliest_start?: boolean;
}
interface SheetDay {
  date: string;
  weekday: string;
  segments: SheetSegment[];
  pause_minutes: number;
  /** Tatsaechlich gestempelte Pause, falls die Mindestpause angehoben hat. */
  pause_punched_minutes?: number;
  pause_raised_by_law?: boolean;
  ist_minutes: number;
  soll_minutes: number;
  diff_minutes: number;
  absence?: string | null;
  holiday: boolean;
  /** Warnhinweis aus dem Tagesbogen (z. B. "Ausstempeln vergessen") — Ist ist dann 0. */
  flagged_reason?: string | null;
}
interface SheetSummary {
  soll_minutes: number;
  ist_minutes: number;
  saldo_minutes: number;
  urlaub_minutes: number;
  urlaub_tage: number;
  krank_minutes: number;
  krank_tage: number;
}
interface MonthSheet {
  employee_id: string;
  employee_name: string;
  employee_number?: string | null;
  year: number;
  month: number;
  days: SheetDay[];
  summary: SheetSummary;
}

const monthNames = [
  "Januar", "Februar", "März", "April", "Mai", "Juni",
  "Juli", "August", "September", "Oktober", "November", "Dezember",
];

const sheet = ref<MonthSheet | null>(null);
const loading = ref(false);
const err = ref("");

async function load() {
  if (!props.open || !props.employeeId) return;
  loading.value = true;
  err.value = "";
  try {
    sheet.value = await apiFetch<MonthSheet>(
      `/admin/monthly-timesheet?year=${props.year}&month=${props.month}&employee_id=${props.employeeId}`
    );
  } catch (e: any) {
    err.value = e?.data?.detail || "Stundenzettel konnte nicht geladen werden.";
    sheet.value = null;
  }
  loading.value = false;
}
watch(() => [props.open, props.employeeId, props.year, props.month], load, { immediate: true });

// Minuten -> "10:14" bzw. "-0:55" (H:MM, Vorzeichen bei negativ).
function hmm(min: number): string {
  const sign = min < 0 ? "-" : "";
  const a = Math.abs(Math.round(min));
  return `${sign}${Math.floor(a / 60)}:${String(a % 60).padStart(2, "0")}`;
}
// Differenz mit explizitem Vorzeichen ("+1:59" / "-0:55" / "0:00").
function hmmDiff(min: number): string {
  return (min > 0 ? "+" : "") + hmm(min);
}
// "2026-06-01" + "Mo" -> "01.Mo" (Tag-Spalte wie auf dem Altsystem-Blatt).
function dayLabel(d: SheetDay): string {
  return `${d.date.slice(8, 10)}.${d.weekday}`;
}
function absenceLabel(d: SheetDay): string {
  if (d.flagged_reason) return `⚠ ${d.flagged_reason}`;
  if (d.absence) return d.absence;
  if (d.holiday) return "Feiertag";
  return "";
}
function badge(d: SheetDay): string {
  if (d.flagged_reason) return "red";
  if (d.absence === "Urlaub") return "green";
  if (d.absence === "Krank") return "red";
  return "neutral";
}
// Legende nur zeigen, wenn im Monat tatsächlich gekappt bzw. angehoben wurde.
const hatKappung = computed(() =>
  (sheet.value?.days || []).some((d) => d.segments.some((s) => s.clipped_earliest_start))
);
const hatPausenAnhebung = computed(() =>
  (sheet.value?.days || []).some((d) => d.pause_raised_by_law)
);

// Tage ohne Soll (Sa/So/Feiertag) gedämpft darstellen.
function isOffDay(d: SheetDay): boolean {
  return d.weekday === "Sa" || d.weekday === "So" || d.holiday;
}
// Tage-Zahl ohne unnötige Nachkommastellen ("1", "0,5").
function days(n: number): string {
  return (Math.round(n * 100) / 100).toLocaleString("de-DE");
}
function fmtD(isoDate: string): string {
  return `${isoDate.slice(8, 10)}.${isoDate.slice(5, 7)}.${isoDate.slice(0, 4)}`;
}

// --- Excel (CSV): eine Zeile pro Tag, Segmente in einer Zelle -------------
function csvCell(v: string): string {
  return /[";\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
}
function downloadCsv(lines: string[], filename: string) {
  // BOM, damit Excel Umlaute korrekt erkennt.
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
function exportCsv() {
  const v = sheet.value;
  if (!v) return;
  const lines: string[] = [];
  lines.push(csvCell(
    `Stundenzettel ${monthNames[v.month - 1]} ${v.year} — ${v.employee_name}` +
    (v.employee_number ? ` (Personalnr. ${v.employee_number})` : "")
  ));
  lines.push("");
  lines.push(
    ["Tag", "Wochentag", "Kommt/Geht", "Pause", "Zeit", "Soll", "+/-", "Abwesenheit"]
      .map(csvCell).join(";")
  );
  for (const d of v.days) {
    const segs = d.segments
      .map((s) =>
        s.clipped_earliest_start
          ? `${s.start} (gestempelt ${s.start_punched})-${s.end}`
          : `${s.start}-${s.end}`
      )
      .join(" / ");
    lines.push(
      [
        fmtD(d.date),
        d.weekday,
        segs,
        d.pause_raised_by_law
          ? `${hmm(d.pause_minutes)} (gestempelt ${hmm(d.pause_punched_minutes || 0)})`
          : d.pause_minutes
            ? hmm(d.pause_minutes)
            : "",
        d.ist_minutes ? hmm(d.ist_minutes) : "",
        d.soll_minutes ? hmm(d.soll_minutes) : "",
        d.soll_minutes || d.ist_minutes ? hmmDiff(d.diff_minutes) : "",
        absenceLabel(d),
      ].map((c) => csvCell(String(c))).join(";")
    );
  }
  const s = v.summary;
  lines.push("");
  lines.push(["Soll", hmm(s.soll_minutes)].map(csvCell).join(";"));
  lines.push(["Ist", hmm(s.ist_minutes)].map(csvCell).join(";"));
  lines.push(["Saldo", hmmDiff(s.saldo_minutes)].map(csvCell).join(";"));
  lines.push(
    ["Urlaub", `${hmm(s.urlaub_minutes)} (${days(s.urlaub_tage)} T)`].map(csvCell).join(";")
  );
  lines.push(
    ["Krank", `${hmm(s.krank_minutes)} (${days(s.krank_tage)} T)`].map(csvCell).join(";")
  );
  const safeName = v.employee_name.replace(/[^\wäöüÄÖÜß-]+/g, "_");
  const m2 = String(v.month).padStart(2, "0");
  downloadCsv(lines, `Stundenzettel_${v.year}-${m2}_${safeName}.csv`);
}

// --- Drucken: nur das Blatt, nicht die Seite dahinter ----------------------
// body bekommt fürs Drucken eine Marker-Klasse; die (unscoped) Print-Regeln
// unten blenden alles außer dem Overlay aus.
function printSheet() {
  document.body.classList.add("sz-printing");
  const cleanup = () => {
    document.body.classList.remove("sz-printing");
    window.removeEventListener("afterprint", cleanup);
  };
  window.addEventListener("afterprint", cleanup);
  window.print();
}
</script>

<template>
  <div v-if="open" class="sz-overlay" @click.self="emit('close')">
    <div class="sz-panel">
      <div class="row sz-toolbar" style="gap: 8px; align-items: center">
        <button class="btn" style="padding: 7px 12px" @click="emit('close')">‹ Schließen</button>
        <div style="flex: 1"></div>
        <button class="btn" :disabled="!sheet" @click="exportCsv">Excel (CSV)</button>
        <button class="btn btn-primary" :disabled="!sheet" @click="printSheet">Drucken / PDF</button>
      </div>

      <div v-if="err" class="wip-banner" style="margin-bottom: 14px">{{ err }}</div>
      <div v-else-if="loading" class="card"><div class="empty">Lädt …</div></div>

      <div v-else-if="sheet" class="card" style="padding: 0">
        <div class="spread" style="padding: 14px 18px 6px">
          <h2 style="margin: 0">
            {{ sheet.employee_name }}
            <span v-if="sheet.employee_number" class="muted" style="font-weight: 400">
              · Personalnr. {{ sheet.employee_number }}
            </span>
          </h2>
          <span class="muted">Stundenzettel {{ monthNames[sheet.month - 1] }} {{ sheet.year }}</span>
        </div>
        <table class="data">
          <thead>
            <tr>
              <th>Tag</th>
              <th style="text-align: right">Kommt</th>
              <th style="text-align: right">Geht</th>
              <th style="text-align: right">Pause</th>
              <th style="text-align: right">Zeit</th>
              <th style="text-align: right">Soll</th>
              <th style="text-align: right">+/−</th>
              <th>Abw.</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="d in sheet.days" :key="d.date">
              <tr :class="{ 'day-off': isOffDay(d) }">
                <td class="num" style="font-weight: 600">{{ dayLabel(d) }}</td>
                <td class="num" style="text-align: right">
                  {{ d.segments[0]?.start || "" }}
                  <span
                    v-if="d.segments[0]?.clipped_earliest_start"
                    class="roh"
                    :title="`Gestempelt ${d.segments[0]?.start_punched} — gewertet ab ${d.segments[0]?.start} (frühester Beginn laut Profil)`"
                    >({{ d.segments[0]?.start_punched }})</span
                  >
                </td>
                <td class="num" style="text-align: right">{{ d.segments[0]?.end || "" }}</td>
                <td class="num" style="text-align: right">
                  {{ d.pause_minutes ? hmm(d.pause_minutes) : "" }}
                  <span
                    v-if="d.pause_raised_by_law"
                    class="roh"
                    :title="`Gestempelt ${hmm(d.pause_punched_minutes || 0)} — auf die gesetzliche Mindestpause angehoben (ArbZG §4)`"
                    >({{ hmm(d.pause_punched_minutes || 0) }})<sup>§</sup></span
                  >
                </td>
                <td class="num" style="text-align: right">
                  {{ d.ist_minutes ? hmm(d.ist_minutes) : "" }}
                </td>
                <td class="num" style="text-align: right">
                  {{ d.soll_minutes ? hmm(d.soll_minutes) : "" }}
                </td>
                <td class="num" :class="{ neg: d.diff_minutes < 0 }" style="text-align: right">
                  {{ d.soll_minutes || d.ist_minutes ? hmmDiff(d.diff_minutes) : "" }}
                </td>
                <td>
                  <span v-if="absenceLabel(d)" class="badge" :class="badge(d)">
                    <span class="dot"></span>{{ absenceLabel(d) }}
                  </span>
                </td>
              </tr>
              <!-- Weitere Segmente des Tags (mehrfach Kommt/Geht) als Zusatzzeilen -->
              <tr v-for="(s, i) in d.segments.slice(1)" :key="d.date + '-' + i" class="seg-row">
                <td></td>
                <td class="num" style="text-align: right">
                  {{ s.start }}
                  <span
                    v-if="s.clipped_earliest_start"
                    class="roh"
                    :title="`Gestempelt ${s.start_punched} — gewertet ab ${s.start} (frühester Beginn laut Profil)`"
                    >({{ s.start_punched }})</span
                  >
                </td>
                <td class="num" style="text-align: right">{{ s.end }}</td>
                <td colspan="5"></td>
              </tr>
            </template>
          </tbody>
        </table>

        <!-- Legende: erklaert die Klammerwerte in Kommt und Pause -->
        <div v-if="hatKappung || hatPausenAnhebung" class="legende">
          <span v-if="hatKappung"
            >(Klammer bei <b>Kommt</b>) = tatsächlich gestempelt, gewertet wird ab dem im Profil
            hinterlegten frühesten Beginn.</span
          >
          <span v-if="hatPausenAnhebung"
            >(Klammer bei <b>Pause</b>)<sup>§</sup> = so viel wurde gestempelt; angesetzt ist die
            gesetzliche Mindestpause nach ArbZG §4 (30 Min ab 6 h, 45 Min ab 9 h).</span
          >
        </div>

        <!-- Summenblock -->
        <div class="row" style="padding: 14px 18px; gap: 28px; flex-wrap: wrap; border-top: 2px solid var(--divider)">
          <div class="stack" style="gap: 2px">
            <span class="label">Soll</span>
            <span class="num" style="font-weight: 700">{{ hmm(sheet.summary.soll_minutes) }} h</span>
          </div>
          <div class="stack" style="gap: 2px">
            <span class="label">Ist</span>
            <span class="num" style="font-weight: 700">{{ hmm(sheet.summary.ist_minutes) }} h</span>
          </div>
          <div class="stack" style="gap: 2px">
            <span class="label">Saldo</span>
            <span class="num" :class="{ neg: sheet.summary.saldo_minutes < 0 }" style="font-weight: 700">
              {{ hmmDiff(sheet.summary.saldo_minutes) }} h
            </span>
          </div>
          <div class="stack" style="gap: 2px">
            <span class="label">Urlaub</span>
            <span class="num" style="font-weight: 700">
              {{ hmm(sheet.summary.urlaub_minutes) }} h ({{ days(sheet.summary.urlaub_tage) }} T)
            </span>
          </div>
          <div class="stack" style="gap: 2px">
            <span class="label">Krank</span>
            <span class="num" style="font-weight: 700">
              {{ hmm(sheet.summary.krank_minutes) }} h ({{ days(sheet.summary.krank_tage) }} T)
            </span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sz-overlay {
  position: fixed;
  inset: 0;
  z-index: 60;
  background: rgba(0, 0, 0, 0.35);
  overflow-y: auto;
  padding: 24px 16px;
}
.sz-panel {
  max-width: 860px;
  margin: 0 auto;
}
.sz-toolbar {
  margin-bottom: 14px;
}
/* Zusatzzeilen für weitere Kommt/Geht-Segmente eines Tags. */
.roh {
  color: var(--muted);
  font-size: 0.85em;
  margin-left: 4px;
  white-space: nowrap;
}
.legende {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 18px;
  color: var(--muted);
  font-size: 12px;
  border-top: 1px solid var(--divider);
}
.seg-row td {
  border-top: none;
  padding-top: 0;
}
/* Tage ohne Soll (Sa/So/Feiertag) gedämpft. */
.day-off td {
  color: var(--text-2);
}
</style>

<!-- Unscoped: beim Drucken nur das Blatt zeigen (Marker-Klasse auf body,
     gesetzt in printSheet()). Gilt nur, solange das Overlay offen ist. -->
<style>
@media print {
  /* Alles unsichtbar, nur das Overlay wieder einblenden — funktioniert
     unabhängig davon, wie tief das Overlay im Seitenbaum steckt. */
  body.sz-printing * {
    visibility: hidden;
  }
  body.sz-printing .sz-overlay,
  body.sz-printing .sz-overlay * {
    visibility: visible;
  }
  body.sz-printing .sz-toolbar {
    display: none !important;
  }
  body.sz-printing .sz-overlay {
    position: absolute;
    inset: 0 auto auto 0;
    width: 100%;
    background: none;
    padding: 0;
    overflow: visible;
  }
  body.sz-printing .sz-panel {
    max-width: none;
  }
  body.sz-printing .sz-panel .card {
    border: none;
    box-shadow: none;
  }
  body.sz-printing .data th,
  body.sz-printing .data td {
    font-size: 11px;
  }
}
</style>
