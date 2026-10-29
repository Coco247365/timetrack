<script setup lang="ts">
// Meine Zeit — Self-Service für Mitarbeiter-Accounts (data_scope self).
// Quellen:
//   Stempeluhr    GET /clock/status  +  POST /clock/punch
//   Monat         GET /clock/timesheet/{employee_id}?date_from=&date_to=  +  GET /holidays?year=
//   Urlaub        GET /absences/balance?year=  +  GET/POST /absences/requests  +  PATCH .../cancel
//   Überstunden   GET /overtime/balance?employee_id=
import type { Schema } from "~/composables/useApi";
const { apiFetch } = useApi();
const { user } = useAuth();

const employeeId = computed(() => user.value?.employee_id || null);
const year = new Date().getFullYear();

// ── Typen ──
// ClockStatus/Balance kommen aus dem generierten OpenAPI-Contract (siehe
// useApi.ts Schema<K>) -- Shape-Drift zum Backend wird so zum Compile-Fehler.
type ClockStatus = Schema<"ClockStatusResponse">;
type Balance = Schema<"VacationBalance">;
interface TimesheetDay {
  work_date: string;
  net_work_minutes?: number | null;
  break_minutes?: number | null;
  flagged_reason?: string | null;
}
interface AbsenceRequest {
  id?: string;
  employee_id?: string;
  request_type?: string;
  status?: string;
  date_from?: string;
  date_to?: string;
  days_count?: number;
  notes?: string;
}
interface Overtime { balance_hours?: number; payout_mode?: string; cap_hours?: number }

const status = ref<ClockStatus | null>(null);
const balance = ref<Balance | null>(null);
const overtime = ref<Overtime | null>(null);
const requests = ref<AbsenceRequest[]>([]);
const timesheet = ref<TimesheetDay[]>([]);
const holidays = ref<Record<string, string>>({});

const loading = ref(true);

// ── Helfer (lokal, keine Imports aus anderen Pages) ──
function pad(n: number) { return String(n).padStart(2, "0"); }
// Urlaubstage: maximal eine Nachkommastelle — glättet Float-Artefakte wie
// 24.259999999999998 (30 − 5.74) aus der Balance-Berechnung.
function d1(v?: number | null): string {
  if (v == null) return "—";
  return String(Math.round(v * 10) / 10);
}
function iso(d: Date) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
function fmtMinutes(min?: number | null): string {
  if (min == null) return "—";
  const h = Math.floor(min / 60);
  const m = min % 60;
  return `${h}h ${pad(m)}m`;
}
// Saldo mit Vorzeichen — fmtMinutes kann keine negativen Minuten (JS-Modulo).
function fmtSaldo(min?: number | null): string {
  if (min == null) return "—";
  return `${min < 0 ? "−" : "+"}${fmtMinutes(Math.abs(min))}`;
}
// Weiche ArbZG-§3-Überschreitungs-Hinweise (8–10 h „…Regelarbeitszeit…") sind
// deaktiviert → aus gespeicherten Gründen filtern, damit kein ⚠ dafür erscheint.
function flaggedReasonShown(reason?: string | null): string {
  if (!reason) return "";
  return reason
    .split(";")
    .map((s) => s.trim())
    .filter((s) => s && !/Regelarbeitszeit/i.test(s))
    .join("; ");
}
// ISO YYYY-MM-DD → deutsch TT.MM.JJJJ (sonst "—")
function fmtDate(v?: string | null): string {
  if (!v) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(v);
  return m ? `${m[3]}.${m[2]}.${m[1]}` : v;
}
const todayIso = iso(new Date());
const todayLabel = new Date().toLocaleDateString("de-DE", {
  weekday: "long", day: "numeric", month: "long", year: "numeric",
});

// Vorzeichenbehaftete Stundenanzeige, z. B. +5 h / −10 h.
function fmtHours(h?: number): string {
  if (h == null) return "—";
  const sign = h < 0 ? "−" : "+";
  return `${sign}${Math.abs(h)}`;
}
const otModeText = computed(() => {
  const m = overtime.value?.payout_mode;
  if (m === "all") return "monatlich ausgezahlt";
  if (m === "above_cap") return `über ${overtime.value?.cap_hours ?? "—"} h ausgezahlt`;
  if (m === "none") return "nur ansammeln";
  return m || "";
});

// ── Stempeluhr ──
const punchBusy = ref(false);
const punchErr = ref("");

const clockSince = computed(() => {
  const v = status.value?.clocked_in_since;
  if (!v) return "";
  const d = new Date(v);
  const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  // Offene Schicht von einem früheren Tag: Datum mit anzeigen.
  if (iso(d) !== todayIso) return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}., ${hhmm}`;
  return hhmm;
});

async function loadStatus() {
  try { status.value = await apiFetch<ClockStatus>("/clock/status"); }
  catch { status.value = null; }
}

async function punch(eventType: "PUNCH_IN" | "PUNCH_OUT" | "BREAK_START" | "BREAK_END") {
  punchBusy.value = true;
  punchErr.value = "";
  try {
    await apiFetch("/clock/punch", {
      method: "POST",
      body: { event_type: eventType, source: "WEB_PORTAL" },
    });
    await loadStatus();
    // Heutige Zeiten auch im Kalender aktualisieren, wenn der Monat sichtbar ist.
    if (todayIso >= monthFrom.value && todayIso <= monthTo.value) await loadMonth();
  } catch (e: any) {
    const st = e?.response?.status || e?.statusCode;
    if (st === 429) punchErr.value = "Zu viele Stempelungen in kurzer Zeit — bitte kurz warten.";
    else punchErr.value = e?.data?.detail || "Stempelung fehlgeschlagen.";
  }
  punchBusy.value = false;
}

// ── Monat (readonly-Kalender) ──
const cursor = ref(new Date(year, new Date().getMonth(), 1));
const monthLabel = computed(() =>
  cursor.value.toLocaleDateString("de-DE", { month: "long", year: "numeric" })
);
const monthFrom = computed(() => iso(new Date(cursor.value.getFullYear(), cursor.value.getMonth(), 1)));
const monthTo = computed(() => iso(new Date(cursor.value.getFullYear(), cursor.value.getMonth() + 1, 0)));
function prevMonth() { cursor.value = new Date(cursor.value.getFullYear(), cursor.value.getMonth() - 1, 1); }
function nextMonth() { cursor.value = new Date(cursor.value.getFullYear(), cursor.value.getMonth() + 1, 1); }

// Monats-Summen fürs Kalender-Kopfzeilen-Trio: effektives Soll + Saldo (Ist − Soll)
// aus der Konto-Policy (Feiertage/Abwesenheiten reduzieren das Soll, laufender
// Monat = Soll bis heute). Fehlt der Abruf, bleiben die zwei Einträge weg.
interface MonthSummary { ist_minutes: number; soll_minutes: number; brutto_soll_minutes?: number; saldo_minutes: number }
const monthSummary = ref<MonthSummary | null>(null);

async function loadMonth() {
  if (!employeeId.value) return;
  try {
    timesheet.value = await apiFetch<TimesheetDay[]>(
      `/clock/timesheet/${employeeId.value}?date_from=${monthFrom.value}&date_to=${monthTo.value}`
    );
  } catch { timesheet.value = []; }
  try {
    holidays.value = (await apiFetch<Record<string, string>>(
      `/holidays?year=${cursor.value.getFullYear()}`
    )) || {};
  } catch { holidays.value = {}; }
  try {
    monthSummary.value = await apiFetch<MonthSummary>(
      `/overtime/month-summary?employee_id=${employeeId.value}&year=${cursor.value.getFullYear()}&month=${cursor.value.getMonth() + 1}`
    );
  } catch { monthSummary.value = null; }
}
watch(cursor, loadMonth);

const tsByDate = computed(() => {
  const m: Record<string, TimesheetDay> = {};
  for (const t of timesheet.value) m[t.work_date] = t;
  return m;
});
const holidaySet = computed(() => new Set(Object.keys(holidays.value)));

interface Cell {
  iso: string;
  day: number;
  inMonth: boolean;
  isWeekend: boolean;
  isHoliday: boolean;
  isFuture: boolean;
  ts?: TimesheetDay;
}

const weeks = computed<Cell[][]>(() => {
  const first = new Date(cursor.value.getFullYear(), cursor.value.getMonth(), 1);
  // Mo=0 … So=6
  const offset = (first.getDay() + 6) % 7;
  const start = new Date(first);
  start.setDate(first.getDate() - offset);
  const out: Cell[][] = [];
  const cur = new Date(start);
  for (let w = 0; w < 6; w++) {
    const row: Cell[] = [];
    for (let d = 0; d < 7; d++) {
      const key = iso(cur);
      const dow = cur.getDay();
      row.push({
        iso: key,
        day: cur.getDate(),
        inMonth: cur.getMonth() === cursor.value.getMonth(),
        isWeekend: dow === 0 || dow === 6,
        isHoliday: holidaySet.value.has(key),
        isFuture: key > todayIso,
        ts: tsByDate.value[key],
      });
      cur.setDate(cur.getDate() + 1);
    }
    out.push(row);
    if (cur.getMonth() !== cursor.value.getMonth() && out.length >= 4 && row[6]!.iso > monthTo.value) break;
  }
  return out;
});

// Bei laufender Schicht ersetzt der Live-Wert den gespeicherten Heute-Wert.
const totalWork = computed(() => {
  const liveToday = status.value?.is_clocked_in ? (status.value?.today_work_minutes || 0) : null;
  let sum = 0;
  for (const t of timesheet.value) {
    if (liveToday != null && t.work_date === todayIso) continue;
    sum += t.net_work_minutes || 0;
  }
  if (liveToday != null && todayIso >= monthFrom.value && todayIso <= monthTo.value) sum += liveToday;
  return sum;
});

// ── Urlaub / Abwesenheiten ──
const absTypeLabel: Record<string, string> = { VACATION: "Urlaub", SICK_LEAVE: "Krank", SPECIAL_LEAVE: "Sonderurlaub", OVERTIME_REDUCTION: "Überstundenabbau", UNPAID_LEAVE: "Unbezahlt" };
const absStatusLabel: Record<string, string> = {
  SUBMITTED: "Eingereicht",
  APPROVED: "Genehmigt",
  REJECTED: "Abgelehnt",
  CANCELLED: "Zurückgezogen",
};
function typeLabel(rt?: string): string {
  return absTypeLabel[String(rt || "").toUpperCase()] || rt || "—";
}
function statusLabel(st?: string): string {
  return absStatusLabel[String(st || "").toUpperCase()] || st || "—";
}
function isInactive(st?: string): boolean {
  const s = String(st || "").toUpperCase();
  return s === "CANCELLED" || s === "REJECTED";
}
function statusBadgeClass(st?: string): string {
  const s = String(st || "").toUpperCase();
  if (s === "APPROVED") return "badge green";
  if (s === "SUBMITTED") return "badge blue";
  if (s === "REJECTED") return "badge red";
  return "badge neutral";
}

async function loadBalance() {
  try { balance.value = await apiFetch<Balance>(`/absences/balance?year=${year}`); }
  catch { balance.value = null; }
}
async function loadRequests() {
  if (!employeeId.value) { requests.value = []; return; }
  try {
    // Server-seitig auf die eigene Person filtern — ohne employee_id liefert
    // der Endpoint (bei Admin-Scope tenant-weit!) nur die 50 zuletzt
    // erstellten Anträge; eigene fielen aus dem Fenster (#165).
    const all = await apiFetch<AbsenceRequest[]>(
      `/absences/requests?employee_id=${employeeId.value}&limit=500`
    );
    // Defensive: nur eigene Anträge anzeigen.
    requests.value = (all || [])
      .filter((r) => r.employee_id === employeeId.value)
      .slice()
      .sort((a, b) => (b.date_from || "").localeCompare(a.date_from || ""));
  } catch { requests.value = []; }
}
async function loadOvertime() {
  if (!employeeId.value) return;
  try { overtime.value = await apiFetch<Overtime>(`/overtime/balance?employee_id=${employeeId.value}`); }
  catch { overtime.value = null; }
}

// Antrag stellen (aufklappbares Formular)
const absFormOpen = ref(false);
const absFormType = ref<"VACATION" | "SICK_LEAVE">("VACATION");
const absFormFrom = ref("");
const absFormTo = ref("");
const absFormNotes = ref("");
const absFormErr = ref("");
const absFormBusy = ref(false);

function resetAbsForm() {
  absFormType.value = "VACATION";
  absFormFrom.value = "";
  absFormTo.value = "";
  absFormNotes.value = "";
  absFormErr.value = "";
}
function openAbsForm() { resetAbsForm(); absFormOpen.value = true; }
function cancelAbsForm() { resetAbsForm(); absFormOpen.value = false; }

async function submitAbsence() {
  absFormErr.value = "";
  if (!absFormFrom.value) { absFormErr.value = "Bitte ein Startdatum wählen."; return; }
  absFormBusy.value = true;
  try {
    await apiFetch("/absences/requests", {
      method: "POST",
      body: {
        request_type: absFormType.value,
        date_from: absFormFrom.value,
        date_to: absFormTo.value || absFormFrom.value,
        notes: absFormNotes.value || undefined,
      },
    });
    await loadRequests();
    await loadBalance();
    resetAbsForm();
    absFormOpen.value = false;
  } catch (e: any) {
    absFormErr.value = e?.data?.detail || "Antrag konnte nicht eingereicht werden.";
  }
  absFormBusy.value = false;
}

// Zurückziehen (zweistufige Bestätigung)
const cancelConfirmId = ref<string | null>(null);
const cancelBusy = ref(false);
const cancelErr = ref("");
function askCancel(rid?: string) { cancelErr.value = ""; cancelConfirmId.value = rid || null; }
function abortCancel() { cancelConfirmId.value = null; cancelErr.value = ""; }
async function doCancel(rid?: string) {
  if (!rid) return;
  cancelBusy.value = true;
  cancelErr.value = "";
  try {
    await apiFetch(`/absences/requests/${rid}/cancel`, { method: "PATCH" });
    await loadRequests();
    await loadBalance();
    cancelConfirmId.value = null;
  } catch (e: any) {
    cancelErr.value = e?.data?.detail || "Antrag konnte nicht zurückgezogen werden.";
  }
  cancelBusy.value = false;
}

let statusTimer: ReturnType<typeof setInterval> | null = null;

onMounted(async () => {
  if (!employeeId.value) { loading.value = false; return; }
  loading.value = true;
  await loadStatus();
  await loadMonth();
  await loadBalance();
  await loadRequests();
  await loadOvertime();
  loading.value = false;
  // Status + heutige Zeiten laufend aktualisieren (Server liefert Live-Minuten).
  statusTimer = setInterval(loadStatus, 60_000);
});
onBeforeUnmount(() => { if (statusTimer) clearInterval(statusTimer); });
</script>

<template>
  <div>
    <h1>Meine Zeit</h1>
    <p class="muted" style="margin: 4px 0 16px">{{ todayLabel }}</p>

    <!-- Konto ohne Mitarbeiter-Verknüpfung: keine Requests feuern -->
    <div v-if="!employeeId" class="card">
      <div class="empty">Dieses Konto ist mit keinem Mitarbeiter verknüpft.</div>
    </div>

    <div v-else-if="loading" class="empty">Lädt …</div>

    <template v-else>
      <!-- Stempeluhr -->
      <div class="card" style="margin-bottom: 16px">
        <div class="spread" style="flex-wrap: wrap; gap: 14px">
          <div class="stack" style="gap: 8px">
            <div class="row" style="gap: 10px">
              <span class="label">Status</span>
              <span v-if="status?.is_on_break" class="badge blue">
                <span class="dot" />In Pause
              </span>
              <span v-else-if="status?.is_clocked_in" class="badge green">
                <span class="dot" />Eingestempelt{{ clockSince ? " seit " + clockSince + " Uhr" : "" }}
              </span>
              <span v-else class="badge neutral"><span class="dot" />Nicht eingestempelt</span>
            </div>
            <div class="row" style="gap: 18px">
              <span class="muted">Heute gearbeitet:
                <strong class="num pos">{{ fmtMinutes(status?.today_work_minutes) }}</strong>
              </span>
              <span class="muted">Pause:
                <strong class="num">{{ fmtMinutes(status?.today_break_minutes) }}</strong>
              </span>
            </div>
          </div>

          <!-- Web-Stempeln vorerst deaktiviert: gestempelt wird am NFC-Terminal.
               (Buttons kommen zurueck, sobald Web-Stempeln freigeschaltet wird.) -->
          <span class="muted" style="font-size: 12.5px">Gestempelt wird am Terminal.</span>
        </div>
        <div v-if="punchErr" class="wip-banner" style="margin-top: 12px">{{ punchErr }}</div>
      </div>

      <!-- Kennzahlen -->
      <div class="row" style="gap: 14px; align-items: stretch; flex-wrap: wrap; margin-bottom: 16px">
        <div class="card" style="flex: 1; min-width: 200px">
          <div class="label">Heute gearbeitet</div>
          <div class="num" style="font-size: 22px; font-weight: 700; margin: 4px 0">
            {{ fmtMinutes(status?.today_work_minutes) }}
          </div>
          <span class="muted">Pause {{ fmtMinutes(status?.today_break_minutes) }}</span>
        </div>

        <div class="card" style="flex: 1; min-width: 200px">
          <div class="label">Überstunden-Saldo</div>
          <div class="num" style="font-size: 22px; font-weight: 700; margin: 4px 0">
            {{ fmtHours(overtime?.balance_hours) }} h
          </div>
          <span class="muted">{{ otModeText }}</span>
        </div>

        <div class="card" style="flex: 1; min-width: 200px">
          <div class="label">Resturlaub</div>
          <div class="num" style="font-size: 22px; font-weight: 700; margin: 4px 0">
            {{ d1(balance?.remaining_days) }}
            <span class="muted" style="font-size: 13px; font-weight: 500">
              von {{ d1(balance?.entitlement_days) }} Tagen
            </span>
          </div>
          <span v-if="balance?.pending_days" class="muted">({{ d1(balance.pending_days) }} beantragt)</span>
        </div>
      </div>

      <!-- Monatskalender (readonly) -->
      <div class="card" style="margin-bottom: 16px">
        <div class="spread" style="margin-bottom: 14px">
          <h2>Mein Monat</h2>
          <div class="row" style="gap: 8px">
            <button class="btn" style="padding: 7px 12px" @click="prevMonth">‹</button>
            <span class="num" style="min-width: 130px; text-align: center; font-weight: 600">{{ monthLabel }}</span>
            <button class="btn" style="padding: 7px 12px" @click="nextMonth">›</button>
          </div>
        </div>

        <div class="row" style="gap: 18px; margin-bottom: 12px">
          <span class="muted">Arbeit gesamt: <strong class="num pos">{{ fmtMinutes(totalWork) }}</strong></span>
          <template v-if="monthSummary">
            <span class="muted" title="Soll = effektives Soll nach Abzug der Fehlzeiten-Anteile. In Klammern die Bruttosollzeit (Arbeitstage × Tagessoll vor dem Abzug).">Soll: <strong class="num">{{ fmtMinutes(monthSummary.soll_minutes) }}</strong><template v-if="monthSummary.brutto_soll_minutes != null"><span style="font-size: 12px">&nbsp;(brutto {{ fmtMinutes(monthSummary.brutto_soll_minutes) }})</span></template></span>
            <span class="muted">Saldo: <strong class="num" :class="monthSummary.saldo_minutes < 0 ? 'neg' : 'pos'">{{ fmtSaldo(monthSummary.saldo_minutes) }}</strong></span>
          </template>
        </div>

        <div style="display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px">
          <div v-for="wd in ['Mo','Di','Mi','Do','Fr','Sa','So']" :key="wd"
            class="label" style="text-align: center">{{ wd }}</div>

          <template v-for="(week, wi) in weeks" :key="wi">
            <div v-for="c in week" :key="c.iso"
              class="cal-cell"
              :class="{
                'cal-out': !c.inMonth,
                'cal-weekend': c.isWeekend,
                'cal-holiday': c.isHoliday,
                'cal-future': c.isFuture,
              }"
              :title="c.isHoliday ? holidays[c.iso] : undefined"
              style="cursor: default">
              <div class="num" style="font-size: 12px; font-weight: 600">{{ c.day }}</div>
              <template v-if="c.inMonth && !c.isFuture">
                <div v-if="c.isHoliday" class="cal-tag">{{ holidays[c.iso] }}</div>
                <!-- Heute mit laufender Schicht: Live-Zeit statt gespeicherter 0 -->
                <template v-else-if="c.iso === todayIso && status?.is_clocked_in">
                  <div class="num" style="font-size: 11px">
                    {{ fmtMinutes(status?.today_work_minutes) }}
                    <span class="muted" style="font-size: 9px">läuft</span>
                  </div>
                </template>
                <template
                  v-else-if="c.ts && ((c.ts.net_work_minutes || 0) > 0 || c.ts.flagged_reason)">
                  <!-- leere Tage (0 min, kein Flag) bleiben leer -->
                  <div class="num" style="font-size: 11px">
                    {{ fmtMinutes(c.ts.net_work_minutes) }}
                    <span v-if="flaggedReasonShown(c.ts.flagged_reason)" :title="flaggedReasonShown(c.ts.flagged_reason)" style="color: #b5651d">⚠</span>
                  </div>
                </template>
              </template>
            </div>
          </template>
        </div>
      </div>

      <!-- Urlaub & Abwesenheiten -->
      <div class="card">
        <div class="spread" style="margin-bottom: 14px">
          <h2>Urlaub &amp; Abwesenheiten</h2>
          <button v-if="!absFormOpen" class="btn btn-primary" @click="openAbsForm">+ Urlaub beantragen</button>
        </div>

        <!-- Antrags-Formular -->
        <div v-if="absFormOpen" class="card" style="background: var(--bg); margin-bottom: 16px">
          <div class="row" style="gap: 12px; flex-wrap: wrap; align-items: flex-end">
            <div class="stack" style="gap: 4px">
              <label class="label">Typ</label>
              <select v-model="absFormType" class="input">
                <option value="VACATION">Urlaub</option>
                <option value="SICK_LEAVE">Krank</option>
              </select>
            </div>
            <div class="stack" style="gap: 4px">
              <label class="label">Von</label>
              <input v-model="absFormFrom" type="date" class="input">
            </div>
            <div class="stack" style="gap: 4px">
              <label class="label">Bis</label>
              <input v-model="absFormTo" type="date" class="input">
            </div>
            <div class="stack" style="gap: 4px; flex: 1; min-width: 160px">
              <label class="label">Notiz (optional)</label>
              <input v-model="absFormNotes" type="text" class="input" placeholder="z. B. Sommerurlaub">
            </div>
            <div class="row" style="gap: 8px">
              <button class="btn btn-primary" :disabled="absFormBusy" @click="submitAbsence">Absenden</button>
              <button class="btn" :disabled="absFormBusy" @click="cancelAbsForm">Abbrechen</button>
            </div>
          </div>
          <p class="muted" style="margin: 10px 0 0">Der Antrag geht zur Genehmigung an die zuständige Führungskraft.</p>
          <div v-if="absFormErr" class="wip-banner" style="margin-top: 10px">{{ absFormErr }}</div>
        </div>

        <div v-if="cancelErr" class="wip-banner" style="margin-bottom: 10px">{{ cancelErr }}</div>

        <div v-if="!requests.length" class="empty">Noch keine Anträge.</div>
        <table v-else class="data">
          <thead>
            <tr>
              <th>Typ</th>
              <th>Zeitraum</th>
              <th class="num">Tage</th>
              <th>Status</th>
              <th>Notiz</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in requests" :key="r.id" :class="{ muted: isInactive(r.status) }">
              <td>{{ typeLabel(r.request_type) }}</td>
              <td class="num">{{ fmtDate(r.date_from) }} – {{ fmtDate(r.date_to) }}</td>
              <td class="num">{{ r.days_count ?? "—" }}</td>
              <td><span :class="statusBadgeClass(r.status)">{{ statusLabel(r.status) }}</span></td>
              <td class="muted">{{ r.notes || "" }}</td>
              <td style="text-align: right">
                <template v-if="String(r.status || '').toUpperCase() === 'SUBMITTED'">
                  <template v-if="cancelConfirmId === r.id">
                    <span class="muted" style="margin-right: 6px">Wirklich?</span>
                    <button class="linkbtn" :disabled="cancelBusy" @click="doCancel(r.id)">Ja</button>
                    <button class="linkbtn" :disabled="cancelBusy" @click="abortCancel">Abbrechen</button>
                  </template>
                  <button v-else class="linkbtn" @click="askCancel(r.id)">Zurückziehen</button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* Kalender-Raster (readonly) — Styles analog zum Mitarbeiterprofil */
.cal-cell {
  min-height: 62px;
  border: 1px solid var(--divider);
  border-radius: 8px;
  padding: 5px 6px;
  background: #fff;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.cal-out { opacity: .4; }
.cal-weekend { background: var(--divider); }
.cal-future { color: var(--text-3); }
.cal-holiday { background: var(--feiertag-bg); color: var(--feiertag-text); }
.cal-tag {
  font-size: 10px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
