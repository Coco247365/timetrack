<script setup lang="ts">
import { folgetagKonflikt, istNachtschicht, localHHMM, nachtschichtStempel, nextDayIso } from "~/utils/nachtschicht";
// Mitarbeiterprofil. Quellen:
//   Stammdaten   GET /users/{id}
//   Resturlaub   GET /absences/balance?year=&employee_id=
//   Kalender     GET /clock/timesheet/{id}?date_from=&date_to=  +  GET /absences/calendar  +  GET /holidays?year=
//   Abwesenh.    GET /absences/requests  (clientseitig nach employee_id gefiltert)
//   Selects      GET /departments , GET /admin/employees
//   Tages-Editor GET/POST /clock/day-events|manual-event|manual-break|events/{id}/correct|void
// Backend-Blocker (WipBadge): nur noch Überstunden-Saldo.
import type { Schema } from "~/composables/useApi";
const route = useRoute();
const { apiFetch } = useApi();
// Frueher stand diese Destrukturierung bei ~Z.1840, wurde aber schon ab ~Z.630
// in computeds referenziert — funktionierte nur dank Lazy-Evaluation (TDZ-Falle).
const { hasPermission, canAccessAdmin, featureEnabled } = useAuth();
const id = String(route.params.id);
const year = new Date().getFullYear();

interface UserDetail {
  full_name?: string;
  first_name?: string;
  last_name?: string;
  email?: string;
  employee_number?: string | null;
  contracted_hours_per_week?: number | null;
  weekday_target_minutes?: number[] | null;
  department_id?: string | null;
  company_id?: string | null;
  manager_id?: string | null;
  time_capture?: string | null;
  wage_profile_id?: string | null;
  work_time_profile_id?: string | null;
  hire_date?: string | null;
  counting_start_date?: string | null;
  termination_date?: string | null;
  termination_reason?: string | null;
  employment_type?: string | null;
  vacation_entitlement_days?: number | null;
  initial_vacation_remaining_days?: number | null;
  birth_date?: string | null;
  overtime_payout_mode?: string;
  overtime_cap_hours?: number;
}
// Balance kommt aus dem generierten OpenAPI-Contract (siehe useApi.ts Schema<K>).
type Balance = Schema<"VacationBalance">;
interface Overtime { employee_id?: string; balance_hours?: number; payout_mode?: string; cap_hours?: number }
interface OvertimeEntry {
  id?: string | number;
  year_month?: string;
  entry_date?: string;
  entry_type?: string; // accrual | payout | adjustment | forfeit
  hours?: number;
  reason?: string;
}
interface OtDailyRow {
  date: string;
  type: "day" | "adjustment" | "accrual" | "payout" | "forfeit";
  worked_hours?: number;
  target_hours?: number;
  delta_hours: number;
  balance_hours: number;
  weekend?: boolean;
  holiday?: boolean;
  absence_fraction?: number;
  absent?: boolean; // unentschuldigtes Fehlen (NFC-Stempler)
  reduction?: number; // Anteil geplanter Überstundenabbau (0..1)
  reason?: string;
  id?: string;
}
interface Department { id: string; name: string; company_id?: string | null }
interface Company { id: string; name: string; is_active?: boolean }
interface ManagerEmp { id: string; name: string; is_active?: boolean }
interface TimesheetDay {
  work_date: string;
  start_time?: string | null;
  end_time?: string | null;
  break_minutes?: number | null;
  net_work_minutes?: number | null;
  overtime_minutes?: number | null;
  // false = manuell gesetzter Tag (ohne Stempel-Events) → Editor auch für Stempler.
  computed_from_events?: boolean;
  is_sunday_work?: boolean;
  is_holiday_work?: boolean;
  // Wochenend-/Feiertagsarbeit: null = Überstunden (Direktvergütung), "gleitzeit" = Konto.
  weekend_booking?: string | null;
  // Pausen-Aufschlag: null = Automatik (Pausenregel), sonst HR-Wert (Delta, min).
  break_auto_override_minutes?: number | null;
  flagged_reason?: string | null;
}
interface AbsenceCalDay { request_type?: string; date_from?: string; date_to?: string; status?: string; day_part?: string; days_count?: number; sick_status?: string | null }
interface Holiday { date?: string; name?: string }
interface AbsenceRequest {
  id?: string;
  employee_id?: string;
  request_type?: string;
  date_from?: string;
  date_to?: string;
  days_count?: number;
  status?: string;
  sick_status?: string | null;
  notes?: string;
}

const user = ref<UserDetail | null>(null);
const balance = ref<Balance | null>(null);
const overtime = ref<Overtime | null>(null);
const overtimeLog = ref<OvertimeEntry[]>([]);
// Verlauf = tagesgenauer Arbeitszeitkonto-Ledger ab Stichtag (Ist/Soll/Δ + laufender
// Saldo), inklusive der manuellen Korrekturen. Quelle: GET /overtime/daily.
const overtimeDaily = ref<OtDailyRow[]>([]);
// Einklappbare Bereiche — standardmäßig zu, damit die Seite kompakt bleibt.
const secOpen = reactive({ ot: false, abs: false, stamm: false, lohn: false });
// Neueste Buchung oben; die Saldo-Spalte zeigt den Kontostand bis zu diesem Tag.
// Tage ohne Saldo-Änderung (Δ 0, z. B. Auto-Erfassung Ist=Soll) werden ausgeblendet;
// Korrekturen und Tage mit +/- bleiben. Der laufende Saldo stimmt weiter (0-Tage ändern ihn nicht).
// AUSNAHME: Wochenend-/Feiertagsarbeit bleibt sichtbar (Δ 0, zählt nicht ins
// Konto — wird direkt ausbezahlt), damit der Vergütungsweg im Verlauf ersichtlich ist.
function isDirectPayout(r: OtDailyRow): boolean {
  return (
    r.type === "day" &&
    !!(r.weekend || r.holiday) &&
    (r.worked_hours || 0) > 0 &&
    Math.abs(r.delta_hours) < 0.005
  );
}
const overtimeRows = computed(() =>
  [...overtimeDaily.value]
    .reverse()
    .filter((r) => r.type !== "day" || Math.abs(r.delta_hours) >= 0.005 || isDirectPayout(r))
);
// Verlauf monatsweise (wie Lohnbuchungen): Monat/Jahr wählbar, angezeigt werden
// nur die Zeilen dieses Monats. Die Kopfzeile trägt den Monats-Gesamtsaldo und
// was mit den Überstunden passiert ist (ausbezahlt / in den Folgemonat übernommen).
// Lokale Zeit, nicht toISOString() (UTC): abends am Monatsletzten wäre sonst
// noch der Vormonat "aktuell".
const _otNow = new Date();
const otMonth = ref(`${_otNow.getFullYear()}-${String(_otNow.getMonth() + 1).padStart(2, "0")}`);
const otMonthNames = [
  "Januar", "Februar", "März", "April", "Mai", "Juni",
  "Juli", "August", "September", "Oktober", "November", "Dezember",
];
// Jahre mit Verlaufs-Daten (plus aktuelles Jahr), neueste zuerst.
const otYears = computed(() => {
  const ys = new Set<number>(overtimeDaily.value.map((r) => Number(r.date.slice(0, 4))));
  ys.add(_otNow.getFullYear());
  return [...ys].sort((a, b) => b - a);
});
// Bruttosollzeit des gewählten Verlaufs-Monats (aus /overtime/month-summary):
// die Verlaufszeilen selbst enthalten voll gedeckte Tage nicht (sie bewegen
// das Konto nicht), das Brutto muss deshalb serverseitig mitgezählt werden.
const otBruttoSoll = ref<number | null>(null);
async function loadOtBrutto() {
  otBruttoSoll.value = null;
  try {
    const ms = await apiFetch<MonthSummary>(
      `/overtime/month-summary?employee_id=${id}&year=${otYearSel.value}&month=${otMonthSel.value}`
    );
    otBruttoSoll.value = ms.brutto_soll_minutes ?? null;
  } catch { /* Anzeige bleibt ohne Klammerwert */ }
}
watch(otMonth, loadOtBrutto);
onMounted(loadOtBrutto);
const otMonthSel = computed<number>({
  get: () => Number(otMonth.value.slice(5, 7)),
  set: (mo) => { otMonth.value = `${otMonth.value.slice(0, 4)}-${String(mo).padStart(2, "0")}`; },
});
const otYearSel = computed<number>({
  get: () => Number(otMonth.value.slice(0, 4)),
  set: (y) => { otMonth.value = `${y}-${otMonth.value.slice(5, 7)}`; },
});
// Zeilen des gewählten Monats (neueste zuerst, 0-Tage bereits ausgefiltert).
const otMonthRows = computed(() => overtimeRows.value.filter((r) => r.date.startsWith(otMonth.value)));
// Monat abgeschlossen = es gibt eine Ansammlungs-Zeile (accrual) im Ledger für
// diesen Monat. Nur dann sind Übernahme/Auszahlung/Übertrag echte Buchungen.
const otMonthSettled = computed(() =>
  overtimeLog.value.some(
    (e) => e.entry_type === "accrual" && String(e.year_month || "").startsWith(otMonth.value),
  ),
);
// Monats-Summen für die Kopfzeile: Ist/Soll/Δ, Saldo am Monatsende, ausbezahlt,
// und was in den Folgemonat übernommen wird (= Kontostand am Monatsende).
const otMonthSummary = computed(() => {
  const rows = otMonthRows.value;
  let worked = 0, target = 0, delta = 0, payout = 0, forfeit = 0;
  for (const r of rows) {
    if (r.type === "day") {
      worked += r.worked_hours || 0;
      target += r.target_hours || 0;
    }
    if (r.type === "payout") payout += r.delta_hours;
    if (r.type === "forfeit") forfeit += r.delta_hours;
    delta += r.delta_hours;
  }
  const endBalance = rows.length ? rows[0].balance_hours : null; // neueste Zeile = Monatsstand
  return {
    worked,
    target,
    delta,
    payout, // negativ (verlässt das Konto)
    forfeit, // negativ; verlässt das Konto OHNE Vergütung
    endBalance, // Übertrag in den Folgemonat
    // Übernahme aus dem Vormonat = Kontostand vor der ersten Zeile dieses Monats.
    carryIn: endBalance === null ? null : Math.round((endBalance - delta) * 100) / 100,
  };
});
const departments = ref<Department[]>([]);
const companies = ref<Company[]>([]);
const managers = ref<ManagerEmp[]>([]);
// Als Vorgesetzten neu waehlbar: nur aktive Mitarbeiter. Ist der aktuell
// eingetragene Vorgesetzte inzwischen inaktiv, bleibt er sichtbar (sonst
// verschwaende die Auswahl beim Oeffnen der Seite stillschweigend), aber
// ausgegraut und nicht neu waehlbar fuer andere.
const managerOptions = computed(() =>
  managers.value.filter((m) => m.is_active !== false || m.id === form.manager_id)
);
const requests = ref<AbsenceRequest[]>([]);
const timesheet = ref<TimesheetDay[]>([]);
const absenceCal = ref<AbsenceCalDay[]>([]);
const holidays = ref<string[]>([]);
// ClockStatus kommt aus dem generierten OpenAPI-Contract (siehe useApi.ts Schema<K>).
const clockStatus = ref<Schema<"ClockStatusResponse"> | null>(null);
// Praesenz-Zeile dieses Mitarbeiters aus /clock/presence — dieselbe Quelle wie die
// Mitarbeiterliste, damit Liste und Profil nie verschiedene Status zeigen.
// (/clock/status allein reicht nicht: es kennt nur Stempel-Events und wuerde
// Auto-Erfasste als abwesend fuehren.)
interface PresenceEmp {
  employee_id?: string; id?: string; status?: string;
  clocked_in_since?: string | null; today_start?: string | null; today_end?: string | null;
}
const presence = ref<PresenceEmp | null>(null);

const loading = ref(true);
const err = ref("");

// --- Monat ---
// `?tag=YYYY-MM-DD` (aus dem Teamkalender verlinkt) öffnet direkt dessen
// Monat und wählt den Tag aus. Der Startmonat steht deshalb schon hier auf
// dem verlinkten Tag — sonst lüde `loadMonth()` erst den laufenden Monat und
// gleich darauf noch einmal den richtigen.
const tagAusUrl = /^\d{4}-\d{2}-\d{2}$/.test(String(route.query.tag ?? ""))
  ? String(route.query.tag)
  : null;
const cursor = ref(tagAusUrl
  ? new Date(Number(tagAusUrl.slice(0, 4)), Number(tagAusUrl.slice(5, 7)) - 1, 1)
  : new Date(year, new Date().getMonth(), 1));
const monthLabel = computed(() =>
  cursor.value.toLocaleDateString("de-DE", { month: "long", year: "numeric" })
);
function pad(n: number) { return String(n).padStart(2, "0"); }
function iso(d: Date) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
const monthFrom = computed(() => iso(new Date(cursor.value.getFullYear(), cursor.value.getMonth(), 1)));
const monthTo = computed(() => iso(new Date(cursor.value.getFullYear(), cursor.value.getMonth() + 1, 0)));

async function loadStatic() {
  try {
    user.value = await apiFetch<UserDetail>(`/users/${id}`);
  } catch (e: any) {
    err.value = e?.data?.detail || "Mitarbeiter nicht gefunden.";
  }
  await loadBalance();
  await reloadOvertime();
  try { departments.value = await apiFetch<Department[]>("/departments"); } catch { /* optional */ }
  try { companies.value = await apiFetch<Company[]>("/companies"); } catch { /* optional */ }
  try { managers.value = await apiFetch<ManagerEmp[]>("/admin/employees"); } catch { /* optional */ }
  await reloadRequests();
  try {
    clockStatus.value = await apiFetch<Schema<"ClockStatusResponse">>(`/clock/status?employee_id=${id}`);
  } catch { clockStatus.value = null; }
  // Optional: fehlt das Praesenz-Recht, greift der Fallback in heuteStatus.
  try {
    const p = await apiFetch<{ employees?: PresenceEmp[] }>("/clock/presence");
    presence.value = (p.employees || []).find((e) => (e.employee_id || e.id) === id) || null;
  } catch { presence.value = null; }
}

// Laufende Pause: Beginn aus dem Status (break_started_at, siehe ClockStatusResponse).
const breakSince = computed(() => {
  const v = clockStatus.value?.break_started_at;
  if (!v) return "";
  const d = new Date(v);
  if (isNaN(d.getTime())) return "";
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
});

// Monats-Summen fürs Kalender-Kopfzeilen-Trio: effektives Soll + Saldo (Ist − Soll)
// aus der Konto-Policy (Feiertage/Abwesenheiten reduzieren das Soll, laufender
// Monat = Soll bis heute). Fehlt der Abruf, bleiben die zwei Einträge weg.
interface MonthSummary { ist_minutes: number; soll_minutes: number; brutto_soll_minutes?: number; saldo_minutes: number }
const monthSummary = ref<MonthSummary | null>(null);

// Fehlgeschlagene Teil-Abrufe des Kalenders. WICHTIG: bei einem Fehler wird der
// bisherige Stand NICHT geleert. Vorher setzte jeder catch die Liste auf [] —
// ein einziger fehlgeschlagener Abruf (z. B. direkt nach "Nicht gearbeitet",
// das loadMonth() nachzieht) liess damit saemtliche Urlaubs- und Krank-Markierungen
// aus dem Kalender verschwinden, wortlos und ununterscheidbar von "gibt es nicht".
// Lieber ein sichtbar veralteter Kalender als ein still gefaelschter.
const monatLadeFehler = ref("");

async function loadMonth() {
  const from = monthFrom.value;
  const to = monthTo.value;
  const fehlt: string[] = [];
  try {
    timesheet.value = await apiFetch<TimesheetDay[]>(
      `/clock/timesheet/${id}?date_from=${from}&date_to=${to}`
    );
  } catch { fehlt.push("Arbeitszeiten"); }
  try {
    absenceCal.value = await apiFetch<AbsenceCalDay[]>(
      `/absences/calendar?date_from=${from}&date_to=${to}&employee_id=${id}`
    );
  } catch { fehlt.push("Abwesenheiten"); }
  try {
    const hs = await apiFetch<Record<string, string>>(`/holidays?year=${cursor.value.getFullYear()}`);
    holidays.value = Object.keys(hs || {});
  } catch { fehlt.push("Feiertage"); }
  try {
    monthSummary.value = await apiFetch<MonthSummary>(
      `/overtime/month-summary?employee_id=${id}&year=${cursor.value.getFullYear()}&month=${cursor.value.getMonth() + 1}`
    );
  } catch { monthSummary.value = null; }
  monatLadeFehler.value = fehlt.length
    ? `${fehlt.join(" und ")} konnten nicht geladen werden — der Kalender kann veraltet sein. Bitte neu laden.`
    : "";
}

// --- Krankheitstage pro Jahr (Werktage Mo–Fr) ---
interface SickCalEntry {
  employee_id?: string;
  request_type?: string;
  date_from?: string;
  date_to?: string;
  status?: string;
}
const selectedSickYear = ref(year);
const sickYears = computed(() => [year - 2, year - 1, year]);
const selectedVacYear = ref(year);
const vacYears = computed(() => [year - 2, year - 1, year, year + 1]);
async function loadBalance() {
  try {
    balance.value = await apiFetch<Balance>(
      `/absences/balance?year=${selectedVacYear.value}&employee_id=${id}`
    );
  } catch { /* optional */ }
}
const sickDays = ref(0);
// Unentschuldigte Fehltage im gewählten Jahr (aus dem Überstunden-Ledger: absent-Zeilen).
const unexcusedDays = computed(
  () =>
    overtimeDaily.value.filter(
      (r) => r.absent && r.date.slice(0, 4) === String(selectedSickYear.value)
    ).length
);

async function loadSickDays() {
  const y = selectedSickYear.value;
  const yStart = `${y}-01-01`;
  const yEnd = `${y}-12-31`;
  try {
    const cal = await apiFetch<SickCalEntry[]>(
      `/absences/calendar?date_from=${yStart}&date_to=${yEnd}&employee_id=${id}`
    );
    // Feiertage des Jahres (zum Ausschluss aus den Werktagen).
    let holidaySet = new Set<string>();
    try {
      const hs = await apiFetch<Record<string, string>>(`/holidays?year=${y}`);
      holidaySet = new Set(Object.keys(hs || {}));
    } catch { /* Feiertage optional */ }
    let count = 0;
    for (const e of cal || []) {
      if (String(e.request_type || "").toUpperCase() !== "SICK_LEAVE") continue;
      const from = e.date_from;
      const to = e.date_to || e.date_from;
      if (!from || !to) continue;
      const cur = new Date(from + "T00:00:00");
      const last = new Date(to + "T00:00:00");
      while (cur <= last) {
        const key = iso(cur);
        const dow = cur.getDay();
        // Nur echte Werktage: kein Wochenende, kein Feiertag.
        if (cur.getFullYear() === y && dow !== 0 && dow !== 6 && !holidaySet.has(key)) count++;
        cur.setDate(cur.getDate() + 1);
      }
    }
    sickDays.value = count;
  } catch {
    sickDays.value = 0;
  }
}

// --- Abwesenheit für diesen Mitarbeiter eintragen (auto-genehmigt) ---
const absFormType = ref<"VACATION" | "SICK_LEAVE" | "SPECIAL_LEAVE" | "OVERTIME_REDUCTION" | "UNPAID_LEAVE">("VACATION");
const absFormDayPart = ref<"FULL" | "HALF">("FULL"); // Ganzer Tag / Halber Tag (Urlaub + Krank)
const absFormHalfHours = ref(4);                     // Stunden bei Halbtag (Default 4h, editierbar)
const absFormSickStatus = ref("");                   // AU/LFZ-Status bei Krankmeldung
// Auswahl-Optionen für die Art der Krankmeldung (AU/LFZ = Lohnfortzahlung).
const sickStatusOptions = [
  { value: "EAU_TELEFON", label: "EAU-Telefon" },
  { value: "AU_MIT_LFZ", label: "mit AU mit LFZ" },
  { value: "OHNE_AU_MIT_LFZ", label: "ohne AU mit LFZ" },
  { value: "OHNE_AU_OHNE_LFZ", label: "ohne AU ohne LFZ" },
];
function sickStatusLabel(v?: string | null): string {
  return sickStatusOptions.find((o) => o.value === v)?.label || "";
}
// Austrittsgründe (kanonischer Wert → Label) für die Stammdaten.
const terminationReasons = [
  { value: "AUFHEBUNGSVERTRAG", label: "Aufhebungsvertrag" },
  { value: "AUSBILDUNG_ENDE", label: "Ausbildung Ende" },
  { value: "BEFRISTUNG_ENDE", label: "Befristung Ende" },
  { value: "KUENDIGUNG", label: "Kündigung" },
  { value: "KUENDIGUNG_MITARBEITER", label: "Kündigung Mitarbeiter" },
  { value: "KUENDIGUNG_UNTERNEHMEN", label: "Kündigung Unternehmen" },
  { value: "RENTE", label: "Rente" },
  { value: "TOD", label: "Tod" },
];
function terminationReasonLabel(v?: string | null): string {
  return terminationReasons.find((o) => o.value === v)?.label || "";
}
const absFormFrom = ref("");
const absFormTo = ref("");
const absFormNotes = ref("");
const absFormErr = ref("");
const absFormBusy = ref(false);
const absFormOpen = ref(false); // Eintragen-Formular erst nach Klick
// Halber Tag gilt nur für EINEN Tag → "Bis" leeren.
watch(absFormDayPart, (v) => { if (v !== "FULL") absFormTo.value = ""; });

function resetAbsForm() {
  absFormType.value = "VACATION";
  absFormDayPart.value = "FULL";
  absFormHalfHours.value = 4;
  absFormFrom.value = "";
  absFormTo.value = "";
  absFormNotes.value = "";
  absFormSickStatus.value = "";
  absFormErr.value = "";
}
function openAbsForm() { closeOtherEdits("absform"); resetAbsForm(); absFormOpen.value = true; }
function cancelAbsForm() { resetAbsForm(); absFormOpen.value = false; }

async function reloadRequests() {
  try {
    // Server-seitig auf den Mitarbeiter filtern — die tenant-weite Liste ist
    // auf die zuletzt erstellten Anträge begrenzt (limit), ältere eigene
    // Anträge fielen sonst aus dem Fenster (#161).
    const all = await apiFetch<AbsenceRequest[]>(`/absences/requests?employee_id=${id}&limit=500`);
    requests.value = (all || []).filter((r) => r.employee_id === id);
  } catch { /* optional */ }
}

async function submitAbsence() {
  absFormErr.value = "";
  if (!absFormFrom.value) { absFormErr.value = "Bitte ein Startdatum wählen."; return; }
  // Sonderurlaub verlangt eine Begründung (der Server erzwingt es ebenfalls).
  if (absFormType.value === "SPECIAL_LEAVE" && !absFormNotes.value.trim()) {
    absFormErr.value = "Sonderurlaub braucht eine Begründung (z. B. Hochzeit, Beerdigung).";
    return;
  }
  const isHalf = absFormDayPart.value === "HALF";
  absFormBusy.value = true;
  try {
    await apiFetch("/absences/admin/requests", {
      method: "POST",
      body: {
        employee_id: id,
        request_type: absFormType.value,
        // Halbtag (Urlaub + Krank): als day_part=AM + explizite Stunden gesendet.
        day_part: isHalf ? "AM" : "FULL",
        hours: isHalf ? Number(absFormHalfHours.value) : undefined,
        date_from: absFormFrom.value,
        // Halber Tag gilt nur für einen einzelnen Tag (kein Zeitraum).
        date_to: isHalf ? absFormFrom.value : (absFormTo.value || absFormFrom.value),
        // AU/LFZ-Status nur bei Krankmeldung mitschicken.
        sick_status:
          absFormType.value === "SICK_LEAVE" && absFormSickStatus.value
            ? absFormSickStatus.value
            : undefined,
        notes: absFormNotes.value || undefined,
      },
    });
    // sofort sichtbar machen
    await loadMonth();
    await reloadRequests();
    await loadBalance();
    await loadSickDays();
    await reloadOvertime(); // unentschuldigtes Fehlen wirkt sofort im Saldo/Verlauf
    // Formular zurücksetzen + schließen
    resetAbsForm();
    absFormOpen.value = false;
  } catch (e: any) {
    absFormErr.value = e?.data?.detail || "Abwesenheit konnte nicht eingetragen werden.";
  }
  absFormBusy.value = false;
}

// --- Lohnbuchungen (manuelle Pauschalen/Beträge für den Lohn-Export) ---
interface WageTypeOpt {
  internal_code: string;
  datev_lohnart: string;
  description: string;
  is_active: boolean;
  unit?: string;
}
interface PayrollBooking {
  id: string;
  employee_id: string;
  year_month: string;
  booking_date?: string | null;
  wage_type_code: string;
  value: number;
  note?: string | null;
  datev_lohnart?: string;
  description?: string;
  unit?: string;
}

const bookUnitShort: Record<string, string> = { hours: "Std", amount: "€", days: "Tage" };
const bookMonth = ref(`${new Date().getFullYear()}-${pad(new Date().getMonth() + 1)}`);
const bookings = ref<PayrollBooking[]>([]);
const bookWageTypes = ref<WageTypeOpt[]>([]);
const bookErr = ref("");
const bookBusy = ref(false);
const bookFormOpen = ref(false);
const bookRemoveConfirmId = ref<string | null>(null);

const bookForm = reactive<{ wage_type_code: string; value: string; note: string; date: string }>({
  wage_type_code: "",
  value: "",
  note: "",
  date: "",
});

function bookYearMonth(): { year: number; month: number } | null {
  const m = /^(\d{4})-(\d{2})$/.exec(bookMonth.value);
  if (!m) return null;
  return { year: Number(m[1]), month: Number(m[2]) };
}

// Monat/Jahr als Dropdowns (Quelle bleibt bookMonth = "YYYY-MM").
const bookMonthNames = [
  "Januar", "Februar", "März", "April", "Mai", "Juni",
  "Juli", "August", "September", "Oktober", "November", "Dezember",
];
const bookYears = computed(() => {
  const y = new Date().getFullYear();
  return [y - 1, y, y + 1];
});
const bookMonthSel = computed<number>({
  get: () => bookYearMonth()?.month ?? new Date().getMonth() + 1,
  set: (mo) => {
    const y = bookYearMonth()?.year ?? new Date().getFullYear();
    bookMonth.value = `${y}-${pad(mo)}`;
  },
});
const bookYearSel = computed<number>({
  get: () => bookYearMonth()?.year ?? new Date().getFullYear(),
  set: (y) => {
    const mo = bookYearMonth()?.month ?? new Date().getMonth() + 1;
    bookMonth.value = `${y}-${pad(mo)}`;
  },
});

function bookUnitLabel(u?: string): string {
  return bookUnitShort[u || "hours"] || "Std";
}
function bookWageLabel(w: WageTypeOpt): string {
  // Bezeichnung, sonst interner Code (statt "null") anzeigen.
  const name = w.description || w.internal_code || "Lohnart";
  return `${name} (DATEV ${w.datev_lohnart}) · ${bookUnitLabel(w.unit)}`;
}

async function loadBookWageTypes() {
  try {
    const all = await apiFetch<WageTypeOpt[]>("/admin/wage-types");
    bookWageTypes.value = (all || []).filter((w) => w.is_active);
  } catch { bookWageTypes.value = []; }
}

async function loadBookings() {
  bookErr.value = "";
  const ym = bookYearMonth();
  if (!ym) { bookings.value = []; return; }
  try {
    bookings.value = await apiFetch<PayrollBooking[]>(
      `/payroll/bookings?employee_id=${id}&year=${ym.year}&month=${ym.month}`
    );
  } catch (e: any) {
    bookings.value = [];
    bookErr.value = e?.data?.detail || "Lohnbuchungen konnten nicht geladen werden.";
  }
}

function resetBookForm() {
  bookForm.wage_type_code = "";
  bookForm.value = "";
  bookForm.note = "";
  bookForm.date = "";
  bookErr.value = "";
}
function openBookForm() { resetBookForm(); bookFormOpen.value = true; }
function cancelBookForm() { resetBookForm(); bookFormOpen.value = false; }

async function submitBooking() {
  bookErr.value = "";
  const ym = bookYearMonth();
  if (!ym) { bookErr.value = "Bitte einen gültigen Monat wählen."; return; }
  if (!bookForm.wage_type_code) { bookErr.value = "Bitte eine Lohnart wählen."; return; }
  if (bookForm.value === "" || isNaN(Number(bookForm.value))) { bookErr.value = "Bitte einen gültigen Wert eingeben."; return; }
  bookBusy.value = true;
  try {
    await apiFetch("/payroll/bookings", {
      method: "POST",
      body: {
        employee_id: id,
        wage_type_code: bookForm.wage_type_code,
        year: ym.year,
        month: ym.month,
        value: Number(bookForm.value),
        note: bookForm.note || undefined,
        booking_date: bookForm.date || undefined,
      },
    });
    await loadBookings();
    resetBookForm();
    bookFormOpen.value = false;
  } catch (e: any) {
    bookErr.value = e?.data?.detail || "Lohnbuchung konnte nicht angelegt werden.";
  }
  bookBusy.value = false;
}

function askRemoveBooking(bid?: string) { bookRemoveConfirmId.value = bid || null; }
function cancelRemoveBooking() { bookRemoveConfirmId.value = null; }
async function removeBooking(bid?: string) {
  if (!bid) return;
  bookBusy.value = true;
  bookErr.value = "";
  try {
    await apiFetch(`/payroll/bookings/${bid}`, { method: "DELETE" });
    await loadBookings();
    bookRemoveConfirmId.value = null;
  } catch (e: any) {
    bookErr.value = e?.data?.detail || "Lohnbuchung konnte nicht entfernt werden.";
  }
  bookBusy.value = false;
}

// Lohnbuchungen sind eine optionale Funktion (Einstellungen -> Funktionen).
const canSeeBookings = computed(
  () =>
    featureEnabled("payroll_bookings") &&
    (hasPermission("payroll:read") ||
      hasPermission("payroll:write") ||
      canAccessAdmin.value)
);
const canBook = computed(
  () => hasPermission("payroll:write") || canAccessAdmin.value
);

watch(bookMonth, loadBookings);

onMounted(async () => {
  loading.value = true;
  await loadStatic();
  await loadMonth();
  await loadSickDays();
  await loadBookWageTypes();
  await loadBookings();
  loading.value = false;
  // Aus dem Teamkalender verlinkter Tag: auswählen (lädt das Tagesdetail).
  // Erst NACH `loading`, sonst steht der Kalender noch gar nicht im DOM.
  // BEWUSST ohne scrollIntoView: die Seite sprang beim Öffnen bis zum
  // Tages-Editor herunter, Kopf und Stammdaten waren weg. Wer aus dem
  // Teamkalender kommt, soll oben anfangen — der Tag ist im Kalender
  // markiert, der Editor steht darunter bereit.
  // Der scrollTo(0) muss sein: war der Teamkalender gescrollt (bei vielen
  // Mitarbeitern die Regel), übernimmt der Router dessen Scroll-Position
  // und das Profil öffnet wieder mitten in der Seite.
  if (tagAusUrl) {
    selectedDay.value = tagAusUrl;
    await loadDay(tagAusUrl);
    window.scrollTo({ top: 0 });
  }
});

watch(selectedSickYear, loadSickDays);
watch(selectedVacYear, loadBalance);

watch(cursor, loadMonth);
function prevMonth() { cursor.value = new Date(cursor.value.getFullYear(), cursor.value.getMonth() - 1, 1); }
function nextMonth() { cursor.value = new Date(cursor.value.getFullYear(), cursor.value.getMonth() + 1, 1); }

// --- Helfer Anzeige ---
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
// Die weichen ArbZG-§3-Überschreitungs-Hinweise (8–10 h „…Regelarbeitszeit…")
// sind deaktiviert. Aus gespeicherten Tagesbögen (vor der Deaktivierung) diese
// Gründe herausfiltern, damit weder das ⚠ noch der Text dafür erscheint.
// Der harte >10 h-Fehler („Höchstmaß"), Pausen (§4) und Ruhezeit (§5) bleiben.
function flaggedReasonShown(reason?: string | null): string {
  if (!reason) return "";
  return reason
    .split(";")
    .map((s) => s.trim())
    .filter((s) => s && !/Regelarbeitszeit/i.test(s))
    .join("; ");
}
const todayIso = iso(new Date());

// ISO YYYY-MM-DD → deutsch TT.MM.JJJJ (sonst "—")
function fmtDate(v?: string | null): string {
  if (!v) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(v);
  return m ? `${m[3]}.${m[2]}.${m[1]}` : v;
}

// --- Kalender-Raster Mo–So ---
interface Cell {
  iso: string;
  day: number;
  inMonth: boolean;
  isWeekend: boolean;
  isHoliday: boolean;
  isFuture: boolean;
  isBirthday: boolean;
  birthdayAge: number; // Alter am Geburtstag (0 = unbekannt/kein Geburtstag)
  anniversaryYears: number; // Firmenjubiläum: Jahre seit Eintritt (0 = kein Jubiläum)
  isAbsent: boolean; // unentschuldigtes Fehlen (NFC, vergangener Werktag ohne Stempel)
  isHalfAbsence: boolean; // Halbtags-Abwesenheit → Kachel nur zur Hälfte grün
  absHours?: number; // Urlaubsstunden bei Halbtag (für "Urlaub (3h)")
  ts?: TimesheetDay;
  absence?: string; // "vacation" | "sick_leave" | ...
  sickStatus?: string; // Art der Krankmeldung (AU/LFZ) für "Krank (…)"
}

const tsByDate = computed(() => {
  const m: Record<string, TimesheetDay> = {};
  for (const t of timesheet.value) m[t.work_date] = t;
  return m;
});
// /absences/calendar liefert Bereiche (date_from..date_to). Auf Einzeltage
// expandieren und VACATION→"vacation", SICK_LEAVE→"sick_leave" klassifizieren.
function classifyAbsence(rt?: string): string | null {
  const t = String(rt || "").toUpperCase();
  if (t === "VACATION") return "vacation";
  if (t === "SICK_LEAVE" || t === "SICK") return "sick_leave";
  if (t === "SPECIAL" || t === "SPECIAL_LEAVE") return "special";
  if (t === "OVERTIME_REDUCTION") return "abbau";
  if (t === "UNPAID_LEAVE") return "unbezahlt";
  // Unentschuldigtes Fehlen ist KEIN eintragbarer Abwesenheitstyp mehr — es entsteht
  // automatisch (fehlende Zeit) und wird über den Überstunden-Ledger (absent) markiert.
  if (t === "UNEXCUSED_ABSENCE") return null;
  return t ? t.toLowerCase() : null;
}
const absByDate = computed(() => {
  const m: Record<string, string> = {};
  for (const a of absenceCal.value) {
    const cls = classifyAbsence(a.request_type);
    const from = a.date_from;
    const to = a.date_to || a.date_from;
    if (!cls || !from || !to) continue;
    const cur = new Date(from + "T00:00:00");
    const last = new Date(to + "T00:00:00");
    while (cur <= last) {
      m[iso(cur)] = cls;
      cur.setDate(cur.getDate() + 1);
    }
  }
  // Wochenend-Brücke: Fr und folgender Mo gleiche Abwesenheitsart -> Sa/So
  // mitmarkieren (rein visuell, wie bei einem Antrag, der Fr–Mo durchläuft;
  // gleiche Logik wie im Team-Kalender, team.vue bridgeWeekends).
  for (const key of Object.keys(m)) {
    const d = new Date(key + "T00:00:00");
    if (d.getDay() !== 5) continue; // nur Freitage als Brückenkopf
    const at = (n: number) => {
      const x = new Date(d);
      x.setDate(d.getDate() + n);
      return iso(x);
    };
    if (m[at(3)] === m[key]) {
      m[at(1)] ??= m[key];
      m[at(2)] ??= m[key];
    }
  }
  return m;
});
// Art der Krankmeldung (AU/LFZ) je Tag — für "Krank (mit AU mit LFZ)" in der Zelle.
const sickStatusByDate = computed(() => {
  const m: Record<string, string> = {};
  for (const a of absenceCal.value) {
    if (classifyAbsence(a.request_type) !== "sick_leave" || !a.sick_status) continue;
    const from = a.date_from;
    const to = a.date_to || a.date_from;
    if (!from || !to) continue;
    const cur = new Date(from + "T00:00:00");
    const last = new Date(to + "T00:00:00");
    while (cur <= last) {
      m[iso(cur)] = a.sick_status;
      cur.setDate(cur.getDate() + 1);
    }
  }
  return m;
});
const holidaySet = computed(() => new Set(holidays.value));
// Tage mit unentschuldigtem Fehlen (aus dem Überstunden-Ledger) — für Kalender-Hinweis.
const absentSet = computed(
  () => new Set(overtimeDaily.value.filter((r) => r.absent).map((r) => r.date))
);
// Halbtags-Abwesenheiten (day_part AM/PM): Kachel nur zur Hälfte einfärben,
// dazu die Urlaubsstunden je Tag (Tagesanteil × Tagessoll) für den Kachel-Hinweis.
const absHalfSet = computed(() => {
  const s = new Set<string>();
  for (const a of absenceCal.value) {
    const dp = String(a.day_part || "FULL").toUpperCase();
    if (dp !== "AM" && dp !== "PM") continue;
    const from = a.date_from;
    const to = a.date_to || a.date_from;
    if (!from || !to) continue;
    const cur = new Date(from + "T00:00:00");
    const last = new Date(to + "T00:00:00");
    while (cur <= last) { s.add(iso(cur)); cur.setDate(cur.getDate() + 1); }
  }
  return s;
});
const absHoursByDate = computed(() => {
  const m: Record<string, number> = {};
  const daily = (Number(user.value?.contracted_hours_per_week) || 40) / 5;
  for (const a of absenceCal.value) {
    const dp = String(a.day_part || "FULL").toUpperCase();
    if (dp !== "AM" && dp !== "PM") continue;
    const from = a.date_from;
    const to = a.date_to || a.date_from;
    if (!from) continue;
    const single = from === (to || from);
    const cur = new Date(from + "T00:00:00");
    const last = new Date((to || from) + "T00:00:00");
    while (cur <= last) {
      // Einzeltag: days_count = Tagesanteil (z. B. 0.375 = 3h/8h); Mehrtag: je 0.5.
      const frac = single ? Number(a.days_count) || 0.5 : 0.5;
      m[iso(cur)] = Math.round(frac * daily * 100) / 100;
      cur.setDate(cur.getDate() + 1);
    }
  }
  return m;
});
// Geburtstag als MM-DD (Jahr egal) — markiert die Kalender-Kachel mit einem Kuchen-Icon.
const birthdayMd = computed(() => user.value?.birth_date?.slice(5) || null);
const birthYear = computed(() =>
  user.value?.birth_date ? Number(user.value.birth_date.slice(0, 4)) : null
);
// Firmeneintritt (echtes Eintrittsdatum) → jährliches Firmenjubiläum im Kalender.
const hireMd = computed(() => user.value?.hire_date?.slice(5) || null);
const hireYear = computed(() =>
  user.value?.hire_date ? Number(user.value.hire_date.slice(0, 4)) : null
);

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
        isBirthday: !!birthdayMd.value && key.slice(5) === birthdayMd.value,
        birthdayAge:
          birthdayMd.value && key.slice(5) === birthdayMd.value && birthYear.value
            ? cur.getFullYear() - birthYear.value
            : 0,
        anniversaryYears:
          hireMd.value && key.slice(5) === hireMd.value && hireYear.value && cur.getFullYear() > hireYear.value
            ? cur.getFullYear() - hireYear.value
            : 0,
        isAbsent: absentSet.value.has(key),
        isHalfAbsence: absHalfSet.value.has(key),
        absHours: absHoursByDate.value[key],
        ts: tsByDate.value[key],
        absence: absByDate.value[key],
        sickStatus: sickStatusByDate.value[key],
      });
      cur.setDate(cur.getDate() + 1);
    }
    out.push(row);
    if (cur.getMonth() !== cursor.value.getMonth() && out.length >= 4 && row[6]!.iso > monthTo.value) break;
  }
  return out;
});

// Summen — bei laufender Schicht ersetzt der Live-Wert den gespeicherten Heute-Wert.
const totalWork = computed(() => {
  const liveToday = clockStatus.value?.is_clocked_in ? (clockStatus.value?.today_work_minutes || 0) : null;
  let sum = 0;
  for (const t of timesheet.value) {
    if (liveToday != null && t.work_date === todayIso) continue; // gespeicherten Heute-Wert überspringen
    sum += t.net_work_minutes || 0;
  }
  if (liveToday != null && todayIso >= monthFrom.value && todayIso <= monthTo.value) sum += liveToday;
  return sum;
});
// Tages-Detail + Editor (manuelle Korrekturen)
const selectedDay = ref<string | null>(null);
const selectedCell = computed(() =>
  weeks.value.flat().find((c) => c.iso === selectedDay.value) || null
);
// Krank-Tag: keine Arbeitszeit erfassbar (Backend blockt zusätzlich mit 422).
const dayIsSick = computed(() => selectedCell.value?.absence === "sick_leave");
const dayHasAbsence = computed(() => !!selectedCell.value?.absence);
// Abwesenheits-Tag, auf dem TROTZDEM Arbeitszeit steht (z. B. Auto-Erfassung
// lief, bevor der Urlaub rückwirkend eingetragen wurde) → "Nicht gearbeitet"
// muss anbietbar bleiben, sonst wertet die Policy den Tag als "gearbeitet
// trotz Urlaub" und gibt den Urlaubstag zurück.
const dayAbsenceWithWork = computed(
  () => dayHasAbsence.value && (selectedCell.value?.ts?.net_work_minutes || 0) > 0
);
// Tag ist bereits „nicht gearbeitet" (leerer Tagesbogen) → Button „Nicht gearbeitet" unnötig.
const dayIsUnworked = computed(() => {
  const ts = selectedCell.value?.ts;
  return !!ts && (ts.net_work_minutes || 0) === 0 && !ts.start_time;
});
function selectDay(c: Cell) {
  // Zukunftstage sind bewusst editierbar (Arbeitszeit im Voraus eintragen).
  if (!c.inMonth) return;
  const next = c.iso === selectedDay.value ? null : c.iso;
  if (next) closeOtherEdits("day");
  selectedDay.value = next;
  autoEdit.value = false;
  if (next) loadDay(next);
  else { dayDetail.value = null; syncEditTimes(); }
}
function closeDay() {
  selectedDay.value = null;
  dayDetail.value = null;
  syncEditTimes();
  dayEditing.value = false;
  autoEdit.value = false;
}

// Immer nur EINE Bearbeitung offen: beim Öffnen einer Aktion die anderen schließen.
function closeOtherEdits(keep: "stammdaten" | "day" | "absform" | "revoke") {
  if (keep !== "stammdaten" && editing.value) cancelEdit();
  if (keep !== "day" && selectedDay.value) closeDay();
  if (keep !== "absform" && absFormOpen.value) cancelAbsForm();
  if (keep !== "revoke" && absRevokeConfirmId.value) cancelRevokeAbs();
}

// Vom Einstempel-Status aus: heutigen Tag im Kalender öffnen + direkt bearbeiten.
async function editToday() {
  closeOtherEdits("day");
  const t = new Date();
  cursor.value = new Date(t.getFullYear(), t.getMonth(), 1); // aktuellen Monat anzeigen
  selectedDay.value = todayIso;
  await loadDay(todayIso);
  dayEditing.value = true; // direkt im Bearbeiten-Modus
  await nextTick();
  document.getElementById("tages-editor")?.scrollIntoView({ behavior: "smooth", block: "center" });
}

// --- Tages-Editor: Stempel-Log eines Tages laden/ändern ---
// Quelle: /clock/day-events/{id}. Korrekturen sind append-only Events; die
// Kiosk-Roh-Stempel bleiben erhalten, nur die Projektion (Timesheet) ändert sich.
interface DayEvent {
  id: number;
  event_type: string; // PUNCH_IN | PUNCH_OUT | BREAK_START | BREAK_END
  event_time: string;
  // Gewertete Zeit nach Kappung durchs Arbeitszeit-Profil (== event_time, wenn
  // keine Kappung greift). Fehlt bei älterem Backend → keine Zusatz-Anzeige.
  counted_time?: string | null;
  source: string;
  // Name des Terminals, an dem gestempelt wurde ("Halle", "Außenstelle", …).
  device_name?: string | null;
  device_id?: string | null;
  reason?: string | null;
  effective: boolean;
}
interface DayDetail {
  work_date: string;
  events: DayEvent[];
  timesheet?: TimesheetDay | null;
}
const dayDetail = ref<DayDetail | null>(null);
const dayErr = ref("");
const actionErr = ref("");
const busy = ref(false);
const canCorrect = ref(true);
// Auto-Erfassung: Zeiten werden automatisch gebucht → keine Stempelung/Bearbeitung.
const isAutoCapture = computed(() => (user.value?.time_capture || "") === "auto");

// Abrechnungsprofile (Einstellungen) fuer das Stammdaten-Dropdown.
const wageProfiles = ref<{ id: string; name: string }[]>([]);
onMounted(async () => {
  try {
    const d = await apiFetch<{ profiles: { id: string; name: string }[] }>("/admin/wage-profiles");
    wageProfiles.value = d.profiles || [];
  } catch { /* ohne admin:settings kein Dropdown-Inhalt */ }
});
function wageProfileName(id?: string | null): string {
  if (!id) return "Standard";
  return wageProfiles.value.find((p) => p.id === id)?.name || "Standard";
}

// Arbeitszeit-Profile (Einstellungen → Arbeitszeit-Profile) fuer das
// Stammdaten-Dropdown. Typgebunden: kind 'nfc' fuer Stempler (nfc/web),
// kind 'auto' fuer Auto-Erfassung — der Server erzwingt dasselbe (422).
interface WorkTimeProfileOpt {
  id: string;
  name: string;
  kind: string;
  is_active?: boolean;
  // Nachtrag 14.08.: JSON-String ("38.50"); nfc = gespeichert (null = Bestand
  // ohne Stundenbindung), auto = vom Server aus den Tagesfenstern berechnet.
  contracted_hours_per_week?: string | null;
  // Nur nfc: Tages-Soll je Wochentag in Minuten ("0".."6", fehlender Key = 0).
  weekday_target_minutes?: Record<string, number> | null;
}
const workTimeProfiles = ref<WorkTimeProfileOpt[]>([]);
onMounted(async () => {
  try {
    const d = await apiFetch<any>("/admin/work-time-profiles");
    // Tolerant gegenüber Liste bzw. { profiles: [...] } als Antwortform.
    workTimeProfiles.value = Array.isArray(d) ? d : (d?.profiles || []);
  } catch { /* ohne admin:settings kein Dropdown-Inhalt */ }
});
function workTimeProfileName(pid?: string | null): string {
  if (!pid) return "—";
  return workTimeProfiles.value.find((p) => p.id === pid)?.name || "—";
}
const dayEditing = ref(false); // Bearbeiten-Modus für den ausgewählten Tag

// --- Nachtschicht über Mitternacht (Spec 2026-09-15) -------------------------
// Liegt das eingegebene Geht nicht nach dem offenen Kommt, wird nicht der
// Fehler "Kommt muss vor Geht liegen" ausgelöst, sondern zurückgefragt und
// die Schicht an Mitternacht in zwei Sitzungen getrennt: Geht 23:59:59 am
// Anfangstag, Kommt 00:00:00 und Geht HH:MM am Folgetag. Die Rechenwege
// (Tagesbogen/Konto/Export) sehen ganz normale Stempel — Issue #96 (Zuordnung
// auf den Anfangstag) bleibt davon unberührt.
const nightConfirm = ref<{ kommt: string; geht: string; folgetag: string } | null>(null);
// Offenes Kommt des Tages (letztes wirksames PUNCH_IN ohne PUNCH_OUT danach).
const openKommtHHMM = computed<string | null>(() => {
  let offen: string | null = null;
  for (const e of effectiveEvents.value) {
    if (e.event_type === "PUNCH_IN") offen = localHHMM(e.event_time);
    else if (e.event_type === "PUNCH_OUT") offen = null;
  }
  return offen;
});
function folgetagLabel(iso: string): string {
  const d = new Date(iso + "T12:00:00");
  const wd = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"][d.getDay()];
  return `${wd}, ${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}.${d.getFullYear()}`;
}
function tryAusstempeln() {
  if (!selectedDay.value) return;
  const kommt = openKommtHHMM.value;
  if (kommt && istNachtschicht(kommt, newGeht.value)) {
    if (!requireCorrectionReason()) return;
    nightConfirm.value = { kommt, geht: newGeht.value, folgetag: nextDayIso(selectedDay.value) };
    return;
  }
  addEvent("PUNCH_OUT", newGeht.value);
}
async function confirmNightShift() {
  const nc = nightConfirm.value;
  if (!nc || !selectedDay.value) return;
  const reason = requireCorrectionReason();
  if (!reason) return;
  const startTag = selectedDay.value;
  busy.value = true; actionErr.value = "";
  try {
    // Konfliktprüfung VOR dem ersten Schreiben: ein Stempel am Folgetag vor
    // dem Geht würde die neue Sitzung 00:00–HH:MM zerschneiden.
    const folge = await apiFetch<DayDetail>(`/clock/day-events/${id}?work_date=${nc.folgetag}`);
    const konflikt = folgetagKonflikt(folge.events || [], nc.geht);
    if (konflikt) {
      actionErr.value = `Am ${folgetagLabel(nc.folgetag)} existiert bereits „${konflikt}“ — die Nachtschicht bis ${nc.geht} passt dort nicht hinein. Bitte den Folgetag zuerst bereinigen.`;
      return;
    }
    const st = nachtschichtStempel(startTag, nc.geht);
    const schritte: Array<[string, string, string]> = [
      ["PUNCH_OUT", st.gehtAnfangstag, `Geht 23:59 am ${folgetagLabel(startTag)}`],
      ["PUNCH_IN", st.kommtFolgetag, `Kommt 00:00 am ${folgetagLabel(nc.folgetag)}`],
      ["PUNCH_OUT", st.gehtFolgetag, `Geht ${nc.geht} am ${folgetagLabel(nc.folgetag)}`],
    ];
    for (const [typ, zeit, label] of schritte) {
      try {
        await apiFetch<DayDetail>("/clock/manual-event", {
          method: "POST",
          body: { employee_id: id, event_type: typ, event_time: zeit, reason },
        });
      } catch (e: any) {
        actionErr.value = `Nachtschicht unvollständig: „${label}“ konnte nicht angelegt werden (${e?.data?.detail || "Fehler"}). Bereits angelegte Stempel bleiben sichtbar und lassen sich hier korrigieren.`;
        return;
      }
    }
    nightConfirm.value = null;
    await loadDay(startTag);
    dayEditing.value = true;
    await loadMonth();
    await reloadOvertime();
  } finally {
    busy.value = false;
  }
}

const newKommt = ref("08:00");
const newGeht = ref("17:00");
const breakFrom = ref("12:00");
const breakTo = ref("12:30");

const srcLabel: Record<string, string> = { NFC_TERMINAL: "Kiosk", ADMIN_MANUAL: "Manuell" };
// Quelle-Chip: bei Terminal-Stempeln den Gerätenamen dahinter ("Kiosk · Halle"),
// damit man bei mehreren Terminals sieht, wo gestempelt wurde. Ist das Gerät
// nicht (mehr) auflösbar, bleibt eine gekürzte ID stehen statt der vollen UUID.
function srcTagLabel(source: string, deviceName?: string | null, deviceId?: string | null): string {
  const base = srcLabel[source] || source;
  if (deviceName) return `${base} · ${deviceName}`;
  if (source === "NFC_TERMINAL" && deviceId) {
    // Rohe UUID auf 8 Zeichen kürzen, sprechende Alt-Kennungen ("TERMINAL-01")
    // unverändert lassen. Die volle ID steht im title-Tooltip.
    const uuidish = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(deviceId);
    return `${base} · ${uuidish ? deviceId.slice(0, 8) : deviceId}`;
  }
  return base;
}

const effectiveEvents = computed(() => (dayDetail.value?.events || []).filter((e) => e.effective));
const dayTs = computed(() => dayDetail.value?.timesheet || null);
// Auto-Erfassung ohne Events → die Tages-Summe (Kommt/Geht/Pause) kommt aus dem
// echten Tagesbogen (Kalender-Row). Sobald Stempel existieren (auch bei
// Auto-MA möglich), zählt das Event-berechnete Timesheet.
const daySummaryTs = computed<any>(() =>
  isAutoCapture.value && !effectiveEvents.value.length ? (selectedCell.value?.ts || null) : dayTs.value
);

// --- Auto-Tag: Zeiten tageweise anpassen (ohne Stempeln) ---
const autoEdit = ref(false);
const autoStart = ref("07:00");
const autoEnd = ref("15:30");
const autoBreak = ref(30);
const autoBusy = ref(false);
const autoErr = ref("");
function openAutoEdit() {
  const ts = selectedCell.value?.ts;
  autoStart.value = isoToTime(ts?.start_time) || "07:00";
  autoEnd.value = isoToTime(ts?.end_time) || "15:30";
  autoBreak.value = ts?.break_minutes ?? 30;
  autoErr.value = "";
  autoEdit.value = true;
}
async function saveAutoDay() {
  if (!selectedDay.value) return;
  autoErr.value = "";
  autoBusy.value = true;
  try {
    await apiFetch("/clock/auto-day", {
      method: "PATCH",
      body: {
        employee_id: id,
        work_date: selectedDay.value,
        start: autoStart.value,
        end: autoEnd.value,
        break_minutes: Number(autoBreak.value) || 0,
      },
    });
    await loadMonth();       // Tagesbogen/Summe aktualisieren
    await reloadOvertime();  // Saldo/Verlauf ziehen mit (falls != Soll)
    autoEdit.value = false;
  } catch (e: any) {
    autoErr.value = e?.data?.detail || "Speichern fehlgeschlagen.";
  }
  autoBusy.value = false;
}
// Wochenend-/Feiertagsarbeit: Verbuchung des Tages wählen.
// Default (null/"ueberstunden") = zählt nicht ins Konto, wird direkt ausbezahlt;
// "gleitzeit" = erhöht das Überstundenkonto.
const dayIsWeekend = computed(() => {
  if (!selectedDay.value) return false;
  const wd = new Date(selectedDay.value + "T12:00:00").getDay();
  return wd === 0 || wd === 6;
});
const dayIsHoliday = computed(
  () => !!selectedDay.value && holidays.value.includes(selectedDay.value)
);
const weBookingBusy = ref(false);
async function setWeekendBooking(v: string) {
  if (!selectedDay.value) return;
  weBookingBusy.value = true;
  try {
    await apiFetch("/clock/weekend-booking", {
      method: "PATCH",
      body: { employee_id: id, work_date: selectedDay.value, booking: v },
    });
    await loadDay(selectedDay.value);
    await loadMonth();
    await reloadOvertime();
  } catch (e: any) {
    dayErr.value = e?.data?.detail || "Speichern fehlgeschlagen.";
  }
  weBookingBusy.value = false;
}

// Manuell gesetzter Tag (ohne Stempel-Events): der Editor gilt auch für
// NFC-Stempler — ein Tag ohne Stempel ist ein Tag ohne Stempel, egal wie der
// Mitarbeiter sonst erfasst. Mit echten Stempeln verschwindet der Block und
// es gilt die Stempel-Korrektur.
const dayIsManual = computed(
  () => !!daySummaryTs.value
    && daySummaryTs.value.computed_from_events === false
    && !effectiveEvents.value.length
);

// „Nicht gearbeitet": Tag leeren → wird unentschuldigt (überschreibbar durch Urlaub/Krank).
async function markUnworked() {
  if (!selectedDay.value) return;
  autoErr.value = "";
  autoBusy.value = true;
  try {
    await apiFetch("/clock/day-unworked", {
      method: "PATCH",
      body: { employee_id: id, work_date: selectedDay.value },
    });
    await loadMonth();
    await reloadOvertime();
    autoEdit.value = false;
  } catch (e: any) {
    autoErr.value = e?.data?.detail || "Speichern fehlgeschlagen.";
  }
  autoBusy.value = false;
}

// Effektive Events nach Art gruppieren (chronologisch) → logische Tagesübersicht.
function byTime(a: DayEvent, b: DayEvent) { return a.event_time.localeCompare(b.event_time); }
interface BreakPair { start: DayEvent; end: DayEvent | null }
const punchIns = computed(() => effectiveEvents.value.filter((e) => e.event_type === "PUNCH_IN").sort(byTime));
const punchOuts = computed(() => effectiveEvents.value.filter((e) => e.event_type === "PUNCH_OUT").sort(byTime));
// Frühester Beginn (Profil-Regel): wer vor der Schwelle stempelt, bekommt den
// Tag erst ab der Schwelle gewertet. Die echte Stempelzeit bleibt sichtbar —
// angezeigt wird dann „06:00 (05:55)" wie im Stundenzettel.
const gestempelterBeginn = computed<string | null>(() => {
  const first = punchIns.value[0];
  if (!first || !daySummaryTs.value?.start_time) return null;
  const roh = isoToTime(first.event_time);
  const gewertet = isoToTime(daySummaryTs.value.start_time);
  return roh && gewertet && roh !== gewertet ? roh : null;
});
const pausen = computed<BreakPair[]>(() => {
  const out: BreakPair[] = [];
  for (const e of effectiveEvents.value.slice().sort(byTime)) {
    if (e.event_type === "BREAK_START") out.push({ start: e, end: null });
    else if (e.event_type === "BREAK_END") {
      const open = [...out].reverse().find((p) => p.end === null);
      if (open) open.end = e; else out.push({ start: e, end: e });
    }
  }
  return out;
});
// Chronologische Leseansicht: eine Zeile pro Ereignis in Zeitreihenfolge,
// Pausen als Paar (Beginn–Ende). Ein Kommen ohne späteres Gehen ist eine
// noch offene Sitzung und wird entsprechend markiert.
interface TimelineRow {
  key: string; label: string; time: string; src?: string; device?: string | null; note?: string;
  // Kappung durchs Arbeitszeit-Profil: Original wird durchgestrichen gezeigt,
  // dahinter „→ gilt ab/bis HH:MM" mit der gewerteten Zeit.
  capped?: { label: string; time: string };
}
// Gewertete Zeit eines Events, wenn sie (minutengenau) von der Stempelzeit
// abweicht — sonst null (auch wenn counted_time fehlt, altes Backend).
function cappedTime(e: DayEvent): string | null {
  if (!e.counted_time) return null;
  const c = isoToTime(e.counted_time);
  return c !== isoToTime(e.event_time) ? c : null;
}
const timeline = computed<TimelineRow[]>(() => {
  const evs = effectiveEvents.value.slice().sort(byTime);
  const rows: TimelineRow[] = [];
  const usedBreakEnds = new Set<number>();
  for (let i = 0; i < evs.length; i++) {
    const e = evs[i]!;
    if (e.event_type === "PUNCH_IN") {
      const laterOut = evs.slice(i + 1).some((x) => x.event_type === "PUNCH_OUT");
      const c = cappedTime(e);
      rows.push({
        key: `e${e.id}`, label: "Kommt", time: isoToTime(e.event_time), src: e.source, device: e.device_name,
        note: laterOut ? undefined : "noch eingestempelt",
        capped: c ? { label: "gilt ab", time: c } : undefined,
      });
    } else if (e.event_type === "PUNCH_OUT") {
      const c = cappedTime(e);
      rows.push({
        key: `e${e.id}`, label: "Geht", time: isoToTime(e.event_time), src: e.source, device: e.device_name,
        capped: c ? { label: "gilt bis", time: c } : undefined,
      });
    } else if (e.event_type === "BREAK_START") {
      const end = evs.slice(i + 1).find((x) => x.event_type === "BREAK_END" && !usedBreakEnds.has(x.id));
      if (end) usedBreakEnds.add(end.id);
      const cs = cappedTime(e);
      const ce = end ? cappedTime(end) : null;
      rows.push({
        key: `e${e.id}`, label: "Pause",
        time: `${isoToTime(e.event_time)}–${end ? isoToTime(end.event_time) : "offen"}`,
        capped: cs || ce
          ? { label: "gewertet", time: `${cs ?? isoToTime(e.event_time)}–${end ? (ce ?? isoToTime(end.event_time)) : "offen"}` }
          : undefined,
      });
    } else if (e.event_type === "BREAK_END" && !usedBreakEnds.has(e.id)) {
      // Pausenende ohne Beginn (Korrektur-Restfall) trotzdem sichtbar machen.
      const c = cappedTime(e);
      rows.push({
        key: `e${e.id}`, label: "Pause", time: `bis ${isoToTime(e.event_time)}`,
        capped: c ? { label: "gilt bis", time: c } : undefined,
      });
    }
  }
  // Automatisch ergänzte Pause (Pausenregel: über 4 h Arbeit → mind. 45 min;
  // ArbZG-Staffel für Alt-Tage): Differenz Bogen-Pause − gestempelte Pausen.
  // Als eigene Zeile sichtbar, damit "Pause 45m" bei 20 gestempelten Minuten
  // nachvollziehbar ist. Nie negativ (Math.max in autoBreakMin).
  if (autoBreakMin.value > 0) {
    rows.push({
      key: "auto-pause", label: "Pause", time: `+${autoBreakMin.value}m`,
      note: `automatisch ergänzt — ${autoBreakHint.value}`,
    });
  }
  return rows;
});

// Gestempelte Pausenminuten des Tages (nur abgeschlossene Paare).
const stampedBreakMin = computed(() => {
  let sum = 0;
  for (const p of pausen.value) {
    if (!p.end) continue;
    const ms = new Date(p.end.event_time).getTime() - new Date(p.start.event_time).getTime();
    if (ms > 0) sum += ms / 60000;
  }
  return Math.round(sum);
});
// Stichtag der betrieblichen Pausenregel (muss zum Backend passen:
// policy.statutory.BETRIEB_BREAK_GUELTIG_AB). Steuert nur den Erklärtext —
// der Aufschlag selbst (auch die ältere gesetzliche Anhebung nach ArbZG §4)
// ist für ALLE Tage sichtbar und per Zahlenfeld korrigierbar.
const PAUSENREGEL_AB = "2026-08-28";
const dayUnterPausenregel = computed(() => !!selectedDay.value && selectedDay.value >= PAUSENREGEL_AB);
const autoBreakHint = computed(() =>
  dayUnterPausenregel.value
    ? "Pausenregel: über 4 h Arbeit mind. 45 min"
    : "gesetzliche Mindestpause, ArbZG §4"
);
// Automatisch ergänzte Pausenminuten: Bogen-Pause − gestempelt, nie negativ.
// Ohne Stempel-Events (reiner Auto-Tag) kommt die Pause aus dem Profil → 0.
const autoBreakMin = computed(() => {
  if (!effectiveEvents.value.length) return 0;
  return Math.max(0, (dayTs.value?.break_minutes ?? 0) - stampedBreakMin.value);
});
// Pausen-Aufschlag als direkt editierbares Zahlenfeld (HR kann hoch/runter
// korrigieren; 0 = kein Aufschlag, Backend erlaubt keine negativen Werte).
const breakOverrideInput = ref(0);
const breakOverrideBusy = ref(false);
watch([dayEditing, autoBreakMin], () => { if (dayEditing.value) breakOverrideInput.value = autoBreakMin.value; });
async function saveBreakOverride() {
  if (!selectedDay.value) return;
  breakOverrideBusy.value = true;
  actionErr.value = "";
  try {
    await apiFetch("/clock/break-override", {
      method: "PATCH",
      body: {
        employee_id: id,
        work_date: selectedDay.value,
        minutes: Math.max(0, Math.round(Number(breakOverrideInput.value) || 0)),
      },
    });
    await loadDay(selectedDay.value);
    await loadMonth();
    await reloadOvertime();
  } catch (e: any) {
    actionErr.value = e?.data?.detail || "Pausen-Aufschlag konnte nicht gespeichert werden.";
  }
  breakOverrideBusy.value = false;
}

// Zustandsabhängige Aktionen — verhindert z. B. ein zweites "Kommt" bei noch
// offener Sitzung; nach einem Gehen ist ein weiteres Kommen erlaubt
// (Mehrfach-Sitzungen pro Tag).
const canEinstempeln = computed(() => punchIns.value.length === punchOuts.value.length);
const canAusstempeln = computed(() => punchIns.value.length > punchOuts.value.length);
const canPause = computed(() => punchIns.value.length > 0);

// Pflicht-Freitext-Grund für Punch-Korrekturen (Hinzufügen/Ändern/Löschen von
// Stempelungen) -- wie bei submitOtAdj/otAdjReason: echte Begründung statt
// kanned string, sonst ist das Audit-Log für den häufigsten Korrekturtyp
// nutzlos (BetrVG §87 Nachvollziehbarkeit).
const correctionReason = ref("");
function requireCorrectionReason(): string | null {
  const r = correctionReason.value.trim();
  if (!r) {
    actionErr.value = "Bitte einen Grund für die Korrektur angeben.";
    return null;
  }
  return r;
}
function toggleDayEditing() {
  dayEditing.value = !dayEditing.value;
  if (dayEditing.value) { correctionReason.value = ""; actionErr.value = ""; }
}

// Bearbeitbare Zeit je Event (für Inline-Korrektur).
const editTimes = reactive<Record<number, string>>({});
// Zeilenweiser Abgleich statt globalem Watcher: ein Speichern/Löschen EINER
// Zeile ersetzt `dayDetail` komplett (neuer Tagesbogen vom Backend), darf
// dabei aber NICHT die unsaved Edits anderer, noch offener Zeilen wegwischen.
// affectedIds = die Event-IDs, die die gerade abgeschlossene Aktion selbst
// betroffen hat -- nur deren editTimes-Wert wird zwangsweise mit dem
// Server-Stand ueberschrieben. Ohne affectedIds (z. B. beim Laden eines neuen
// Tages) werden ALLE Zeilen frisch befuellt.
function syncEditTimes(affectedIds?: number[]) {
  const d = dayDetail.value;
  const currentIds = new Set((d?.events || []).filter((e) => e.effective).map((e) => e.id));
  // Nicht mehr vorhandene/entwertete (voided) Events aus dem Edit-Zustand entfernen.
  for (const k of Object.keys(editTimes)) {
    if (!currentIds.has(Number(k))) delete editTimes[Number(k)];
  }
  if (!d) return;
  for (const ev of d.events) {
    if (!ev.effective) continue;
    const isAffected = !affectedIds || affectedIds.includes(ev.id);
    // Neue Zeilen immer befuellen; bekannte Zeilen nur ueberschreiben, wenn
    // sie Teil der gerade abgeschlossenen Aktion waren -- unsaved Edits
    // anderer Zeilen bleiben beim Refresh erhalten.
    if (editTimes[ev.id] === undefined || isAffected) {
      editTimes[ev.id] = isoToTime(ev.event_time);
    }
  }
}
function timeChanged(ev: DayEvent | null): boolean {
  return !!ev && editTimes[ev.id] !== undefined && editTimes[ev.id] !== isoToTime(ev.event_time);
}

function isoToTime(isoStr?: string | null): string {
  if (!isoStr) return "—";
  const d = new Date(isoStr);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
// --- Kopfleiste: Status + "Heute eingestempelt" (wie in der Mitarbeiterliste) ---
// Zeitpunkt als "HH:MM"; liegt er nicht heute (offene Schicht von gestern),
// kommt das Datum davor — sonst liest es sich wie heute Morgen.
function zeitPunkt(v?: string | null): string {
  if (!v) return "";
  const d = new Date(v);
  if (isNaN(d.getTime())) return "";
  const t = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return iso(d) !== todayIso ? `${pad(d.getDate())}.${pad(d.getMonth() + 1)}., ${t}` : t;
}
// Reihenfolge wie im Backend (clock_service.get_presence_overview): laufende
// Pause und Anwesenheit schlagen die Abwesenheit — wer trotz Halbtags-Urlaub
// gestempelt hat, ist anwesend.
const heuteStatus = computed(() => {
  const s = presence.value?.status || "";
  if (s === "on_break") return "pause";
  if (s === "clocked_in") return "anwesend";
  if (s === "vacation") return "urlaub";
  if (s === "sick_leave") return "krank";
  if (s === "clocked_out") return "abwesend";
  // Fallback ohne Praesenz-Recht: Stempel-Status + heutige Abwesenheit.
  if (clockStatus.value?.is_on_break) return "pause";
  if (clockStatus.value?.is_clocked_in) return "anwesend";
  const abs = absByDate.value[todayIso];
  if (abs === "vacation") return "urlaub";
  if (abs === "sick_leave") return "krank";
  return "abwesend";
});
// "seit … Uhr" (noch drin) / "von … bis … Uhr" (war da) / "–" (noch gar nicht).
const heuteEingestempelt = computed(() => {
  const drin = heuteStatus.value === "anwesend" || heuteStatus.value === "pause";
  const seit = presence.value?.clocked_in_since || clockStatus.value?.clocked_in_since || null;
  if (drin && seit) return `seit ${zeitPunkt(seit)} Uhr`;
  const ts = tsByDate.value[todayIso];
  const start = presence.value?.today_start || ts?.start_time || null;
  const ende = presence.value?.today_end || ts?.end_time || null;
  if (start && ende) return `von ${zeitPunkt(start)} bis ${zeitPunkt(ende)} Uhr`;
  if (start && drin) return `seit ${zeitPunkt(start)} Uhr`;
  return "–";
});

// dayIso = yyyy-mm-dd, hhmm = "HH:MM" (lokal) → ISO-UTC-String fürs Backend.
function toIsoUtc(dayIso: string, hhmm: string): string {
  const [Y, Mo, D] = dayIso.split("-").map(Number);
  const [h, m] = hhmm.split(":").map(Number);
  return new Date(Y!, Mo! - 1, D!, h || 0, m || 0, 0).toISOString();
}

async function loadDay(workDate: string) {
  dayErr.value = ""; actionErr.value = ""; dayEditing.value = false;
  try {
    dayDetail.value = await apiFetch<DayDetail>(`/clock/day-events/${id}?work_date=${workDate}`);
    canCorrect.value = true;
  } catch (e: any) {
    dayDetail.value = null;
    if (e?.response?.status === 403 || e?.statusCode === 403) canCorrect.value = false;
    else dayErr.value = e?.data?.detail || "Tag konnte nicht geladen werden.";
  }
  // Neuer Tag -> Edit-Zustand komplett neu befuellen (kein Merge mit dem vorherigen Tag).
  syncEditTimes();
}

// affectedIds: die Event-IDs, die diese Aktion selbst veraendert hat (siehe
// syncEditTimes) -- nur deren Zeile wird beim Refresh zwangsweise
// ueberschrieben, unsaved Edits anderer Zeilen bleiben erhalten.
async function runAction(p: Promise<DayDetail>, affectedIds: number[] = []) {
  busy.value = true; actionErr.value = "";
  try {
    dayDetail.value = await p;
    syncEditTimes(affectedIds);
    await loadMonth();
    // Stempel geändert → Tagesbogen neu gerechnet → Überstunden-Saldo/Verlauf mitziehen.
    await reloadOvertime();
  } catch (e: any) {
    actionErr.value = e?.data?.detail || "Aktion fehlgeschlagen.";
  }
  busy.value = false;
}

function addEvent(type: "PUNCH_IN" | "PUNCH_OUT", hhmm: string) {
  if (!selectedDay.value) return;
  const reason = requireCorrectionReason();
  if (!reason) return;
  runAction(apiFetch<DayDetail>("/clock/manual-event", {
    method: "POST",
    body: { employee_id: id, event_type: type, event_time: toIsoUtc(selectedDay.value, hhmm), reason },
  }));
}
function addBreak() {
  if (!selectedDay.value) return;
  const reason = requireCorrectionReason();
  if (!reason) return;
  runAction(apiFetch<DayDetail>("/clock/manual-break", {
    method: "POST",
    body: { employee_id: id, start_time: toIsoUtc(selectedDay.value, breakFrom.value), end_time: toIsoUtc(selectedDay.value, breakTo.value), reason },
  }));
}
function correctEvent(ev: DayEvent) {
  if (!selectedDay.value) return;
  const reason = requireCorrectionReason();
  if (!reason) return;
  runAction(apiFetch<DayDetail>(`/clock/events/${ev.id}/correct`, {
    method: "POST",
    body: { event_time: toIsoUtc(selectedDay.value, editTimes[ev.id]!), reason },
  }), [ev.id]);
}
function voidEvent(ev: DayEvent) {
  const reason = requireCorrectionReason();
  if (!reason) return;
  runAction(apiFetch<DayDetail>(`/clock/events/${ev.id}/void`, {
    method: "POST", body: { reason },
  }), [ev.id]);
}

// Pause = Paar aus BREAK_START/BREAK_END → beide Seiten zusammen speichern/löschen.
// Beide Calls laufen sequenziell; schlaegt der zweite fehl, wird das Ergebnis
// des ERSTEN trotzdem uebernommen (nicht verworfen) und die abgeleiteten
// Werte (Monat/Saldo) neu geladen -- die UI bleibt konsistent, nur der Fehler
// wird zusaetzlich angezeigt.
async function saveBreak(p: BreakPair) {
  if (!selectedDay.value) return;
  const reason = requireCorrectionReason();
  if (!reason) return;
  busy.value = true; actionErr.value = "";
  const changedIds: number[] = [];
  try {
    if (timeChanged(p.start)) {
      dayDetail.value = await apiFetch<DayDetail>(`/clock/events/${p.start.id}/correct`, {
        method: "POST",
        body: { event_time: toIsoUtc(selectedDay.value, editTimes[p.start.id]!), reason },
      });
      changedIds.push(p.start.id);
    }
    if (p.end && timeChanged(p.end)) {
      dayDetail.value = await apiFetch<DayDetail>(`/clock/events/${p.end.id}/correct`, {
        method: "POST",
        body: { event_time: toIsoUtc(selectedDay.value, editTimes[p.end.id]!), reason },
      });
      changedIds.push(p.end.id);
    }
  } catch (e: any) {
    actionErr.value = e?.data?.detail || "Pause konnte nicht gespeichert werden.";
  }
  if (changedIds.length) {
    syncEditTimes(changedIds);
    await loadMonth();
    await reloadOvertime();
  }
  busy.value = false;
}
async function deleteBreak(p: BreakPair) {
  const reason = requireCorrectionReason();
  if (!reason) return;
  busy.value = true; actionErr.value = "";
  const changedIds: number[] = [];
  try {
    dayDetail.value = await apiFetch<DayDetail>(`/clock/events/${p.start.id}/void`, {
      method: "POST", body: { reason },
    });
    changedIds.push(p.start.id);
    if (p.end) {
      dayDetail.value = await apiFetch<DayDetail>(`/clock/events/${p.end.id}/void`, {
        method: "POST", body: { reason },
      });
      changedIds.push(p.end.id);
    }
  } catch (e: any) {
    actionErr.value = e?.data?.detail || "Pause konnte nicht entfernt werden.";
  }
  if (changedIds.length) {
    syncEditTimes(changedIds);
    await loadMonth();
    await reloadOvertime();
  }
  busy.value = false;
}
const pauseChanged = (p: BreakPair) => timeChanged(p.start) || timeChanged(p.end);

// --- Stammdaten bearbeiten ---
const editing = ref(false);
const form = reactive<UserDetail>({});

// Stunden-Bindung eines Profils (Nachtrag 14.08.): Response-String -> Zahl.
// null = Profil bindet nicht (nfc-Bestand ohne Stunden, auto ohne Arbeitstage).
function wtpBoundHours(p?: WorkTimeProfileOpt | null): number | null {
  if (p?.contracted_hours_per_week == null) return null;
  const n = parseFloat(p.contracted_hours_per_week);
  return Number.isFinite(n) ? n : null;
}
// Nur typkompatible, aktive Arbeitszeit-Profile anbieten; das aktuell
// zugewiesene Profil bleibt wählbar, auch wenn es inzwischen inaktiv ist.
// Profile mit Vertragsstunden zusätzlich nur bei exakt passenden Stunden des
// Mitarbeiters (Server erzwingt dasselbe, 422).
const compatibleWtProfiles = computed(() => {
  const cap = form.time_capture || user.value?.time_capture || "";
  const wanted = cap === "auto" ? "auto" : "nfc";
  const hours = Number(form.contracted_hours_per_week) || 0;
  return workTimeProfiles.value.filter((p) => {
    if (p.kind !== wanted) return false;
    if (p.is_active === false && p.id !== form.work_time_profile_id) return false;
    const bound = wtpBoundHours(p);
    // Das bereits GESPEICHERTE Profil bleibt wählbar (Bestandszuweisungen wurden
    // bei Einführung der Stunden-Bindung nicht angefasst; bei einer Stunden-
    // änderung meldet der Server das 422 statt hier still abzuwählen).
    return bound == null || bound === hours || p.id === user.value?.work_time_profile_id;
  });
});
// Modus-/Stundenwechsel im Formular: nicht mehr passendes Profil abwählen (sonst 422).
watch([() => form.time_capture, () => form.contracted_hours_per_week], () => {
  if (!editing.value) return;
  if (form.work_time_profile_id && !compatibleWtProfiles.value.some((p) => p.id === form.work_time_profile_id)) {
    form.work_time_profile_id = "";
  }
});
// NFC-Profil MIT Stundenbindung: dessen Tagessoll gewinnt serverseitig über
// users.weekday_target_minutes — hier fürs Anzeigen/Sperren spiegeln.
function wtpTargetArray(p?: WorkTimeProfileOpt | null): number[] | null {
  if (!p || p.kind !== "nfc" || wtpBoundHours(p) == null) return null;
  return Array.from({ length: 7 }, (_, i) => p.weekday_target_minutes?.[String(i)] ?? 0);
}
// Anzeige (Lesemodus): zugewiesenes Profil des Mitarbeiters.
const assignedWtProfile = computed(() =>
  workTimeProfiles.value.find((p) => p.id === user.value?.work_time_profile_id) || null
);
const assignedWtpTargets = computed(() => wtpTargetArray(assignedWtProfile.value));
// Bearbeiten-Modus: live die Auswahl im Formular.
const formWtProfile = computed(() =>
  workTimeProfiles.value.find((p) => p.id === form.work_time_profile_id) || null
);
const formWtpTargets = computed(() => wtpTargetArray(formWtProfile.value));

// Bereiche gehören zu einer Firma — erst filtern, wenn eine Firma gewählt ist.
// Bereiche sind firmenunabhaengig (#183): das Dropdown zeigt immer alle
// Bereiche, ein Firmenwechsel laesst den Bereich unberuehrt.
const filteredDepartments = computed(() => departments.value);
const saveErr = ref("");
const saving = ref(false);

// Tages-Soll pro Wochentag (Mo..So). Aktiv = eigener Plan; sonst Wochenstunden÷5.
const WEEKDAY_LABELS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const wdEnabled = ref(false);
const wdHM = reactive<{ h: number; m: number }[]>(
  Array.from({ length: 7 }, () => ({ h: 0, m: 0 })),
);
function fmtHM2(h: number, m: number): string {
  return `${h}:${String(m).padStart(2, "0")}`;
}
// Wochensumme des Plans als „H:MM".
const wdWeeklySum = computed(() => {
  const total = wdHM.reduce((s, d) => s + (Number(d.h) || 0) * 60 + (Number(d.m) || 0), 0);
  return fmtHM2(Math.floor(total / 60), total % 60);
});
function loadWeekdayForm(mins: number[] | null | undefined) {
  if (mins && mins.length === 7) {
    wdEnabled.value = true;
    mins.forEach((v, i) => { wdHM[i].h = Math.floor((v || 0) / 60); wdHM[i].m = (v || 0) % 60; });
  } else {
    // Vorbelegung als Startpunkt: Wochenstunden÷5 auf Mo–Fr, Wochenende 0.
    wdEnabled.value = false;
    const perDay = Math.round(((Number(form.contracted_hours_per_week) || 40) / 5) * 60);
    wdHM.forEach((d, i) => {
      const v = i < 5 ? perDay : 0;
      d.h = Math.floor(v / 60); d.m = v % 60;
    });
  }
}
// Aus den 7 Feldern die Minuten-Liste bauen (oder null, wenn deaktiviert).
function weekdayMinutesFromForm(): number[] | null {
  if (!wdEnabled.value) return null;
  return wdHM.map((d) => (Number(d.h) || 0) * 60 + (Number(d.m) || 0));
}
// Lese-Anzeige: „Mo 8:15 · Di 8:15 · … · Fr 5:30" (Tage mit 0 werden ausgelassen).
function weekdayTargetDisplay(mins?: number[] | null): string {
  if (!mins || mins.length !== 7) return "—";
  return mins
    .map((v, i) => (v > 0 ? `${WEEKDAY_LABELS[i]} ${fmtHM2(Math.floor(v / 60), v % 60)}` : null))
    .filter(Boolean)
    .join(" · ") || "—";
}

function startEdit() {
  if (!user.value) return;
  closeOtherEdits("stammdaten");
  saveErr.value = "";
  Object.assign(form, {
    first_name: user.value.first_name ?? "",
    last_name: user.value.last_name ?? "",
    email: user.value.email ?? "",
    employee_number: user.value.employee_number ?? "",
    contracted_hours_per_week: user.value.contracted_hours_per_week ?? null,
    department_id: user.value.department_id ?? "",
    company_id: user.value.company_id ?? "",
    manager_id: user.value.manager_id ?? "",
    time_capture: user.value.time_capture ?? "",
    wage_profile_id: user.value.wage_profile_id ?? "",
    work_time_profile_id: user.value.work_time_profile_id ?? "",
    hire_date: user.value.hire_date ?? "",
    counting_start_date: user.value.counting_start_date ?? "",
    termination_date: user.value.termination_date ?? "",
    termination_reason: user.value.termination_reason ?? "",
    employment_type: user.value.employment_type ?? "",
    vacation_entitlement_days: user.value.vacation_entitlement_days ?? null,
    initial_vacation_remaining_days: user.value.initial_vacation_remaining_days ?? null,
    birth_date: user.value.birth_date ?? "",
    overtime_payout_mode: user.value.overtime_payout_mode ?? "none",
    overtime_cap_hours: user.value.overtime_cap_hours ?? null,
  });
  loadWeekdayForm(user.value.weekday_target_minutes);
  editing.value = true;
}
function cancelEdit() { editing.value = false; saveErr.value = ""; }

async function save() {
  if (!user.value) return;
  saving.value = true;
  saveErr.value = "";
  const fields: (keyof UserDetail)[] = [
    "first_name", "last_name", "email", "employee_number",
    "contracted_hours_per_week", "department_id", "company_id", "manager_id",
    "time_capture", "wage_profile_id", "work_time_profile_id", "hire_date", "counting_start_date", "employment_type",
    "termination_date", "termination_reason",
    "vacation_entitlement_days", "initial_vacation_remaining_days",
    "birth_date", "overtime_payout_mode", "overtime_cap_hours",
  ];
  const body: Record<string, any> = {};
  for (const f of fields) {
    const next = (form as any)[f] === "" ? null : (form as any)[f];
    const prev = (user.value as any)[f] ?? null;
    if (next !== prev) body[f] = next;
  }
  // Wochentags-Soll separat (Array-Vergleich über JSON).
  const wdNext = weekdayMinutesFromForm();
  const wdPrev = user.value.weekday_target_minutes ?? null;
  if (JSON.stringify(wdNext) !== JSON.stringify(wdPrev)) body.weekday_target_minutes = wdNext;
  if (!Object.keys(body).length) { editing.value = false; saving.value = false; return; }
  try {
    user.value = await apiFetch<UserDetail>(`/users/${id}`, { method: "PATCH", body });
    editing.value = false;
    // Soll-relevante Änderung → Saldo/Verlauf neu berechnen lassen.
    if ("weekday_target_minutes" in body || "contracted_hours_per_week" in body) {
      await Promise.all([reloadOvertime(), loadBalance()]);
    }
  } catch (e: any) {
    // 422-Detail kann String (Service, z. B. Typ-Bindung Arbeitszeit-Profil)
    // oder Liste (Pydantic) sein.
    const d = e?.data?.detail;
    saveErr.value = typeof d === "string"
      ? d
      : Array.isArray(d)
        ? d.map((x: any) => x?.msg || "").filter(Boolean).join(" · ") || "Speichern fehlgeschlagen."
        : "Speichern fehlgeschlagen.";
  }
  saving.value = false;
}

function deptName(did?: string | null): string {
  if (!did) return "—";
  return departments.value.find((d) => d.id === did)?.name || "—";
}
function companyName(cid?: string | null): string {
  if (!cid) return "—";
  return companies.value.find((c) => c.id === cid)?.name || "—";
}
function managerName(mid?: string | null): string {
  if (!mid) return "—";
  return managers.value.find((m) => m.id === mid)?.name || "—";
}

// --- Kiosk-Hinweise: persönliche Meldungen am Terminal nach dem Stempeln (#189) ---
interface KioskMsg { id: string; text: string; active_from?: string | null; active_until?: string | null }
const kioskMsgs = ref<KioskMsg[]>([]);
const kioskMsgOpen = ref(false);
const kioskNewText = ref("");
const kioskNewFrom = ref("");
const kioskNewUntil = ref("");
const kioskBusy = ref(false);
const kioskErr = ref("");
const canManageKioskMsgs = computed(() => hasPermission("admin:settings") || canAccessAdmin.value);

async function loadKioskMsgs() {
  try {
    kioskMsgs.value = await apiFetch<KioskMsg[]>(`/admin/kiosk-messages?employee_id=${id}`);
  } catch { /* ohne Recht leer */ }
}
async function addKioskMsg() {
  const text = kioskNewText.value.trim();
  if (!text) { kioskErr.value = "Text ist Pflicht."; return; }
  kioskBusy.value = true; kioskErr.value = "";
  try {
    await apiFetch("/admin/kiosk-messages", {
      method: "POST",
      body: { text, employee_id: id, active_from: kioskNewFrom.value || null, active_until: kioskNewUntil.value || null },
    });
    kioskNewText.value = ""; kioskNewFrom.value = ""; kioskNewUntil.value = "";
    await loadKioskMsgs();
  } catch (e: any) {
    kioskErr.value = e?.data?.detail || "Meldung konnte nicht angelegt werden.";
  }
  kioskBusy.value = false;
}
async function delKioskMsg(m: KioskMsg) {
  kioskBusy.value = true; kioskErr.value = "";
  try {
    await apiFetch(`/admin/kiosk-messages/${m.id}`, { method: "DELETE" });
    await loadKioskMsgs();
  } catch (e: any) {
    kioskErr.value = e?.data?.detail || "Löschen fehlgeschlagen.";
  }
  kioskBusy.value = false;
}
function toggleKioskMsgs() {
  kioskMsgOpen.value = !kioskMsgOpen.value;
  if (kioskMsgOpen.value && !kioskMsgs.value.length) loadKioskMsgs();
}

// --- Stempel-Protokoll: unveränderbares Log aller Roh-Ereignisse (#167) ---
// punch_events ist append-only; Korrekturen/Stornos sind neue Zeilen mit
// Verweis. Dieser Abschnitt ist bewusst read-only — Korrekturen laufen
// weiter über den Tages-Editor und erscheinen hier automatisch.
interface PunchLogRow {
  id: number;
  event_time: string;
  created_at: string;
  event_type: string;
  source: string;
  device_id?: string | null;
  device_name?: string | null;
  is_void: boolean;
  corrects_event_id?: number | null;
  reason?: string | null;
  created_by_name?: string | null;
  created_by_account_email?: string | null;
  effective: boolean;
}
const plOpen = ref(false);
const _plNow = new Date();
const _plToday = `${_plNow.getFullYear()}-${String(_plNow.getMonth() + 1).padStart(2, "0")}-${String(_plNow.getDate()).padStart(2, "0")}`;
// Standard: nur der heutige Tag — Zeitraum jederzeit änderbar.
const plFrom = ref(_plToday);
const plTo = ref(_plToday);
const plOnlyTerminal = ref(false);
const plExtended = ref(false); // Erweiterte Ansicht: Konto, Gespeichert-am, Event-ID
function plShowMonth() {
  plFrom.value = `${_plNow.getFullYear()}-${String(_plNow.getMonth() + 1).padStart(2, "0")}-01`;
  plTo.value = _plToday;
}
const plRows = ref<PunchLogRow[]>([]);
const plLoading = ref(false);
const plErr = ref("");
const plLoaded = ref(false);

const plTypeLabel: Record<string, string> = {
  PUNCH_IN: "Kommen", PUNCH_OUT: "Gehen", BREAK_START: "Pause Beginn", BREAK_END: "Pause Ende",
};
const plShown = computed(() =>
  plOnlyTerminal.value ? plRows.value.filter((r) => r.source === "NFC_TERMINAL") : plRows.value
);
// Häppchenweise Anzeige: lange Zeiträume nicht als Endlos-Liste rendern.
// Daten sind vollständig geladen — nur die ANZEIGE ist gedrosselt.
const PL_PAGE = 20;
const plVisible = ref(PL_PAGE);
const plPage = computed(() => plShown.value.slice(0, plVisible.value));
watch([plShown], () => { plVisible.value = PL_PAGE; });
function plStatus(r: PunchLogRow): string {
  if (r.is_void) return r.corrects_event_id ? `Storno von #${r.corrects_event_id}` : "Storno";
  if (!r.effective) return "Ersetzt / storniert";
  if (r.corrects_event_id) return `Wirksam (Korrektur zu #${r.corrects_event_id})`;
  return "Wirksam";
}
// Offline-Queue: Buchung deutlich später gespeichert als gestempelt.
function plNachgereicht(r: PunchLogRow): string {
  const et = new Date(r.event_time).getTime();
  const ca = new Date(r.created_at).getTime();
  if (isNaN(et) || isNaN(ca) || ca - et < 5 * 60 * 1000) return "";
  return `nachgereicht ${isoToTime(r.created_at)}`;
}
async function loadPunchLog() {
  if (!plFrom.value || !plTo.value) return;
  plLoading.value = true;
  plErr.value = "";
  try {
    plRows.value = await apiFetch<PunchLogRow[]>(
      `/clock/punch-log/${id}?date_from=${plFrom.value}&date_to=${plTo.value}`
    );
    plLoaded.value = true;
  } catch (e: any) {
    plErr.value = e?.data?.detail || "Protokoll konnte nicht geladen werden.";
  }
  plLoading.value = false;
}
function togglePunchLog() {
  plOpen.value = !plOpen.value;
  if (plOpen.value && !plLoaded.value) loadPunchLog();
}
watch([plFrom, plTo], () => { if (plOpen.value) loadPunchLog(); });

// --- Mitarbeiter löschen (Soft-Delete: employment_status="deleted" → inaktiv) ---
const deleting = ref(false);
const confirmDelete = ref(false);
const deleteErr = ref("");
function askDelete() { confirmDelete.value = true; }
function cancelDelete() { confirmDelete.value = false; deleteErr.value = ""; }
async function doDelete() {
  deleting.value = true;
  deleteErr.value = "";
  try {
    await apiFetch(`/users/${id}`, { method: "PATCH", body: { employment_status: "deleted" } });
    await navigateTo("/mitarbeiter");
  } catch (e: any) {
    deleteErr.value = e?.data?.detail || "Löschen fehlgeschlagen.";
    deleting.value = false;
  }
}

const empTypeLabel: Record<string, string> = { salaried: "Gehalt", hourly: "Stundenlohn", werkstudent: "Werkstudent / Aushilfe" };
const captureLabel: Record<string, string> = { auto: "Automatisch", web: "Web", nfc: "NFC" };
const absTypeLabel: Record<string, string> = {
  vacation: "Urlaub", sick_leave: "Krank", sick: "Krank", special: "Sonderurlaub",
  special_leave: "Sonderurlaub", abbau: "Überstundenabbau",
  unbezahlt: "Unbezahlt", unpaid_leave: "Unbezahlt",
  overtime_reduction: "Überstundenabbau",
  unexcused: "Unentschuldigt", unexcused_absence: "Unentschuldigtes Fehlen",
};
const absStatusLabel: Record<string, string> = {
  approved: "Genehmigt", submitted: "Eingereicht", pending: "Offen",
  rejected: "Abgelehnt", cancelled: "Storniert",
};
function absReqTypeLabel(rt?: string): string {
  return absTypeLabel[(rt || "").toLowerCase()] || rt || "—";
}
function absReqStatusLabel(st?: string): string {
  return absStatusLabel[(st || "").toLowerCase()] || st || "—";
}
// Die Art der Krankmeldung (AU/LFZ) wird über das normale „Bearbeiten" der
// Abwesenheit gepflegt (Spalte „Art") — kein separates Inline-Menü mehr.

// --- Abwesenheiten-Liste: Filter, Aufteilung, Storno ---

// --- Überstunden (Saldo, Verlauf, manuelle Korrektur) ---
const otModeLabel: Record<string, string> = {
  none: "Nur ansammeln",
  all: "Monatlich alles auszahlen",
  above_cap: "Monatlich über Grenze",
  // Überlauf über der Grenze verfällt ersatzlos (Altregel aus Altsystem) —
  // klar von "über Grenze auszahlen" unterscheiden, es geht um Geld.
  forfeit_above_cap: "Über Grenze verfallen lassen",
};
// Beide gekappten Modi zeigen ihre Grenze — ohne sie sagt das Label zu wenig.
const otModeMitGrenze = ["above_cap", "forfeit_above_cap"];
function otModeText(m?: string, cap?: number): string {
  const label = otModeLabel[m || ""] || m || "—";
  if (m && otModeMitGrenze.includes(m) && cap != null) return `${label} · Grenze ${cap} h`;
  return label;
}
// Urlaubstage: maximal eine Nachkommastelle — glättet Float-Artefakte wie
// 24.259999999999998 (30 − 5.74) aus der Balance-Berechnung.
function d1(v?: number | null): string {
  if (v == null) return "—";
  return String(Math.round(v * 10) / 10);
}
// Dezimalstunden → "8h 12min" (bzw. "8h" / "12min" / "0h"); negativ mit −.
function hm(h?: number): string {
  if (h == null) return "—";
  const totalMin = Math.round(Math.abs(h) * 60);
  const hh = Math.floor(totalMin / 60);
  const mm = totalMin % 60;
  const body = totalMin === 0 ? "0h" : hh && mm ? `${hh}h ${mm}min` : hh ? `${hh}h` : `${mm}min`;
  return (h < 0 ? "−" : "") + body;
}
// Wie hm(), aber immer mit Vorzeichen (+/−) — für Δ und Saldo im Verlauf.
function hmSigned(h?: number): string {
  if (h == null) return "—";
  return (h < 0 ? "−" : "+") + hm(Math.abs(h));
}

async function reloadOvertime() {
  // Gleiche Regel wie in loadMonth: ein fehlgeschlagener Abruf leert den
  // bisherigen Stand NICHT. overtimeDaily traegt die "Unentschuldigt"-Marken
  // im Kalender — die wortlos verschwinden zu lassen ist genauso irrefuehrend
  // wie ein leerer Urlaubsblock.
  const fehlt: string[] = [];
  try {
    overtime.value = await apiFetch<Overtime>(`/overtime/balance?employee_id=${id}`);
  } catch { fehlt.push("Überstunden-Saldo"); }
  try {
    overtimeLog.value = await apiFetch<OvertimeEntry[]>(`/overtime/ledger?employee_id=${id}`);
  } catch { fehlt.push("Überstunden-Log"); }
  try {
    overtimeDaily.value = await apiFetch<OtDailyRow[]>(`/overtime/daily?employee_id=${id}`);
  } catch { fehlt.push("Tagesverlauf"); }
  if (fehlt.length) {
    monatLadeFehler.value =
      `${fehlt.join(" und ")} konnten nicht geladen werden — die Anzeige kann veraltet sein. Bitte neu laden.`;
  }
}

const canManageOvertime = computed(
  () =>
    hasPermission("overtime:manage") ||
    hasPermission("timesheet:edit_all") ||
    canAccessAdmin.value
);

// Monats-Stundenzettel (StundenzettelSheet): gleiche Rechte wie der
// Endpoint /admin/monthly-timesheet — view_all/Admin alle, view_team Bereich.
const canViewStundenzettel = computed(
  () =>
    hasPermission("timesheet:view_all") ||
    hasPermission("timesheet:view_team") ||
    canAccessAdmin.value
);
const sheetOpen = ref(false);

const otAdjOpen = ref(false);
// Korrektur wird in Vorzeichen + Stunden + Minuten eingegeben (statt Dezimal).
const otAdjSign = ref<"+" | "−">("+");
const otAdjH = ref<number | null>(null);
const otAdjM = ref<number | null>(null);
const otAdjReason = ref("");
const otAdjDate = ref("");
const otAdjErr = ref("");
const otAdjHinweis = ref("");
const otAdjBusy = ref(false);
const otAdjEditId = ref<string | null>(null); // gesetzt → Bearbeiten statt Neuanlage
const otDelId = ref<string | null>(null);      // Lösch-Bestätigung je Zeile

// Dezimalwert der Korrektur aus Vorzeichen/Stunden/Minuten (null = leer/0).
const otAdjHours = computed<number | null>(() => {
  const h = Number(otAdjH.value) || 0;
  const m = Number(otAdjM.value) || 0;
  if (h === 0 && m === 0) return null;
  const val = h + m / 60;
  return otAdjSign.value === "−" ? -val : val;
});
// Dezimalstunden → Formularfelder (für Bearbeiten).
function setOtAdjFields(h?: number | null) {
  if (h == null || h === 0) { otAdjSign.value = "+"; otAdjH.value = null; otAdjM.value = null; return; }
  otAdjSign.value = h < 0 ? "−" : "+";
  const totalMin = Math.round(Math.abs(h) * 60);
  otAdjH.value = Math.floor(totalMin / 60);
  otAdjM.value = totalMin % 60;
}

function resetOtAdj() {
  setOtAdjFields(null);
  otAdjReason.value = "";
  otAdjDate.value = "";
  otAdjErr.value = "";
  otAdjEditId.value = null;
}
function openOtAdj() { resetOtAdj(); otAdjOpen.value = true; }
function cancelOtAdj() { resetOtAdj(); otAdjOpen.value = false; }
// Edit-Formular aus einer Ledger-Korrekturzeile öffnen (delta_hours → Stunden/Minuten).
function openOtDailyEdit(r: OtDailyRow) {
  resetOtAdj();
  otAdjEditId.value = r.id ? String(r.id) : null;
  setOtAdjFields(r.delta_hours ?? null);
  otAdjReason.value = r.reason || "";
  otAdjDate.value = r.date || "";
  otAdjOpen.value = true;
}
// Beschriftung einer Verlaufszeile: Korrektur, oder Arbeitstag (mit Kontext).
function otRowLabel(r: OtDailyRow): string {
  if (r.type === "adjustment") return "Korrektur";
  if (r.type === "accrual") return "Monatsabschluss";
  if (r.type === "payout") return "Auszahlung";
  if (r.type === "forfeit") return "Verfallen";
  const worked = (r.worked_hours || 0) > 0;
  // Geplanter Überstundenabbau: der Tag zieht sein Soll vom Konto ab — als
  // "Arbeit" mit 0h Ist wäre das Minus nicht zu verstehen.
  if (r.reduction && r.reduction > 0 && !worked) return "Überstundenabbau";
  if (r.absent) return "Unentschuldigtes Fehlen";
  if (r.holiday) return "Arbeit (Feiertag)";
  if (r.weekend) return "Arbeit (Wochenende)";
  if (r.absence_fraction && r.absence_fraction >= 1) return worked ? "Arbeit (am Urlaubstag)" : "Urlaub";
  // Halber Urlaubstag: gearbeitet → "Arbeit (halber Urlaubstag)"; nicht gearbeitet → nur der halbe Tag.
  if (r.absence_fraction && r.absence_fraction > 0) return worked ? "Arbeit (halber Urlaubstag)" : "Halber Urlaubstag";
  return "Arbeit";
}
async function deleteOtAdj(entryId: string) {
  try {
    await apiFetch(`/overtime/adjustment/${entryId}`, { method: "DELETE" });
    otDelId.value = null;
    await reloadOvertime();
  } catch (e: any) {
    otAdjErr.value = e?.data?.detail || "Korrektur konnte nicht gelöscht werden.";
  }
}

async function submitOtAdj() {
  otAdjErr.value = "";
  otAdjHinweis.value = "";
  if (otAdjHours.value == null || isNaN(Number(otAdjHours.value))) {
    otAdjErr.value = "Bitte Stunden und/oder Minuten eingeben.";
    return;
  }
  if (!otAdjReason.value.trim()) {
    otAdjErr.value = "Bitte einen Grund angeben.";
    return;
  }
  otAdjBusy.value = true;
  try {
    const editId = otAdjEditId.value;
    const res = await apiFetch<{ hinweis?: string }>(
      editId ? `/overtime/adjustment/${editId}` : "/overtime/adjustment",
      {
        method: editId ? "PATCH" : "POST",
        body: {
          employee_id: id,
          hours: Number(otAdjHours.value),
          reason: otAdjReason.value.trim(),
          date: otAdjDate.value || undefined,
        },
      }
    );
    await reloadOvertime();
    resetOtAdj();
    otAdjOpen.value = false;
    otAdjHinweis.value = res?.hinweis || "";
  } catch (e: any) {
    otAdjErr.value = e?.data?.detail || "Korrektur konnte nicht gespeichert werden.";
  }
  otAdjBusy.value = false;
}
const canEditAbsences = computed(
  () =>
    hasPermission("leave:approve_all") ||
    hasPermission("leave:approve_team") ||
    canAccessAdmin.value
);

// Filter über der Tabelle
const absFilterType = ref<"" | "VACATION" | "SICK_LEAVE" | "SPECIAL_LEAVE" | "OVERTIME_REDUCTION" | "UNPAID_LEAVE">("");
const absFilterFrom = ref("");
const absFilterTo = ref("");
const pastOpen = ref(false); // "Vergangene" standardmäßig eingeklappt

// Nur aktive Einträge (stornierte/abgelehnte raus).
const activeAbs = computed(() => {
  const allowed = new Set(["APPROVED", "SUBMITTED", "ESCALATED"]);
  return (requests.value || []).filter((r) =>
    allowed.has(String(r.status || "").toUpperCase())
  );
});

// Aktuell & kommend: immer alle (ungefiltert).
const upcomingAbs = computed(() =>
  activeAbs.value
    .filter((r) => (r.date_to || r.date_from || "") >= todayIso)
    .slice()
    .sort((a, b) => (a.date_from || "").localeCompare(b.date_from || ""))
);
// Vergangene: hier greifen die Typ-/Zeitraum-Filter.
const pastAbs = computed(() =>
  activeAbs.value
    .filter((r) => (r.date_to || r.date_from || "") < todayIso)
    .filter((r) => {
      if (absFilterType.value && String(r.request_type || "").toUpperCase() !== absFilterType.value) return false;
      // Zeitraum-Überschneidung: [date_from..date_to] ∩ [filterFrom..filterTo]
      const from = r.date_from || "";
      const to = r.date_to || r.date_from || "";
      if (absFilterTo.value && from && from > absFilterTo.value) return false;
      if (absFilterFrom.value && to && to < absFilterFrom.value) return false;
      return true;
    })
    .slice()
    .sort((a, b) => (b.date_from || "").localeCompare(a.date_from || ""))
);

// Storno (zweistufige Bestätigung pro Zeile)
const absRevokeConfirmId = ref<string | null>(null);
const absRevokeBusy = ref(false);
const absRevokeErr = ref("");
function askRevokeAbs(rid?: string) {
  closeOtherEdits("revoke");
  absRevokeErr.value = "";
  absRevokeConfirmId.value = rid || null;
}
function cancelRevokeAbs() {
  absRevokeConfirmId.value = null;
  absRevokeErr.value = "";
}
// Abwesenheit bearbeiten (Zeitraum; bei Krank zusätzlich die Art AU/LFZ).
const absEditId = ref<string | null>(null);
const absEditFrom = ref("");
const absEditTo = ref("");
const absEditIsSick = ref(false);
const absEditSickStatus = ref("");
const absEditBusy = ref(false);
function startAbsEdit(r: any) {
  absEditId.value = r.id;
  absEditFrom.value = (r.date_from || "").slice(0, 10);
  absEditTo.value = (r.date_to || "").slice(0, 10);
  absEditIsSick.value = String(r.request_type || "").toUpperCase() === "SICK_LEAVE";
  absEditSickStatus.value = r.sick_status || "";
}
function cancelAbsEdit() { absEditId.value = null; }
async function saveAbsEdit(rid?: string) {
  if (!rid) return;
  absEditBusy.value = true;
  absDecideErr.value = "";
  try {
    await apiFetch(`/absences/requests/${rid}`, {
      method: "PATCH",
      body: { date_from: absEditFrom.value, date_to: absEditTo.value },
    });
    // Art (AU/LFZ) nur bei Krankmeldungen mitpflegen.
    if (absEditIsSick.value) {
      await apiFetch(`/absences/requests/${rid}/sick-status`, {
        method: "PATCH",
        body: { sick_status: absEditSickStatus.value || null },
      });
    }
    absEditId.value = null;
    await reloadRequests();
    await loadMonth();
    await loadSickDays();
    await loadBalance();
  } catch (e: any) {
    absDecideErr.value = e?.data?.detail || "Änderung konnte nicht gespeichert werden.";
  }
  absEditBusy.value = false;
}

// Eingereichte Anträge direkt hier genehmigen/ablehnen (TL: eigener Bereich,
// GF/HR: alle — der Server prüft Bereich + Vier-Augen-Prinzip).
const absDecideBusy = ref(false);
const absDecideErr = ref("");
function isPendingAbs(r: { status?: string }): boolean {
  const st = String(r.status || "").toUpperCase();
  return st === "SUBMITTED" || st === "ESCALATED";
}
async function decideAbs(rid: string | undefined, action: "approve" | "reject") {
  if (!rid) return;
  absDecideBusy.value = true;
  absDecideErr.value = "";
  try {
    await apiFetch(`/absences/requests/${rid}/${action}`, {
      method: "PATCH",
      body: action === "reject" ? { reason: "Im Mitarbeiterprofil abgelehnt" } : {},
    });
    await reloadRequests();
    await loadMonth();
    await loadSickDays();
    await loadBalance();
    await reloadOvertime();
  } catch (e: any) {
    absDecideErr.value = e?.data?.detail || "Entscheidung konnte nicht gespeichert werden.";
  }
  absDecideBusy.value = false;
}

async function revokeAbs(rid?: string) {
  if (!rid) return;
  absRevokeBusy.value = true;
  absRevokeErr.value = "";
  try {
    await apiFetch(`/absences/requests/${rid}/revoke`, {
      method: "POST",
      body: { reason: "Im Mitarbeiterprofil entfernt" },
    });
    await reloadRequests();
    await loadMonth();
    await loadSickDays();
    await loadBalance();
    await reloadOvertime();
    absRevokeConfirmId.value = null;
  } catch (e: any) {
    absRevokeErr.value = e?.data?.detail || "Abwesenheit konnte nicht entfernt werden.";
  }
  absRevokeBusy.value = false;
}
</script>

<template>
  <div>
    <NuxtLink to="/mitarbeiter" class="muted">‹ Mitarbeiter</NuxtLink>

    <div v-if="err" class="wip-banner" style="margin-top: 12px">{{ err }}</div>
    <div v-else-if="loading" class="empty">Lädt …</div>

    <template v-else>
      <div class="spread" style="margin: 8px 0 16px">
        <h1>{{ user?.full_name || "Mitarbeiterprofil" }}</h1>
        <span class="muted num">{{ user?.employee_number || "—" }}</span>
      </div>

      <!-- Kopfleiste: Status + heutige Stempelzeit — dieselbe Sicht wie in der
           Mitarbeiterliste, damit man beim Wechsel nicht umdenken muss. -->
      <div class="card" style="padding: 12px 16px; margin-bottom: 16px">
        <div class="spread">
          <div class="row" style="gap: 26px; flex-wrap: wrap; align-items: center">
            <div class="row" style="gap: 10px; align-items: center">
              <span class="label">Status</span>
              <StatusBadge :status="heuteStatus" />
            </div>
            <div class="row" style="gap: 10px; align-items: center">
              <span class="label">Heute eingestempelt</span>
              <span class="num" :class="{ muted: heuteEingestempelt === '–' }">{{ heuteEingestempelt }}</span>
              <span v-if="heuteStatus === 'pause' && breakSince" class="muted">
                · Pause seit {{ breakSince }} Uhr
              </span>
            </div>
          </div>
          <button v-if="!isAutoCapture" class="linkbtn" title="Heutigen Tag im Kalender bearbeiten" @click="editToday">
            ✎ Heute bearbeiten
          </button>
        </div>
      </div>

      <!-- Kennzahlen -->
      <div class="row" style="gap: 14px; align-items: stretch; flex-wrap: wrap; margin-bottom: 16px">
        <div class="card" style="flex: 1; min-width: 200px">
          <div class="label">Überstunden-Saldo</div>
          <div class="num" style="font-size: 22px; font-weight: 700; margin: 4px 0">
            {{ hm(overtime?.balance_hours) }}
          </div>
          <span class="muted">{{ otModeText(overtime?.payout_mode, overtime?.cap_hours) }}</span>
        </div>

        <div class="card" style="flex: 1; min-width: 200px">
          <div class="spread" style="align-items: center">
            <div class="label">Resturlaub</div>
            <select v-model.number="selectedVacYear" class="input" style="width: auto; padding: 2px 6px; font-size: 12px">
              <option v-for="y in vacYears" :key="y" :value="y">{{ y }}</option>
            </select>
          </div>
          <div class="num" style="font-size: 22px; font-weight: 700; margin: 4px 0">
            {{ d1(balance?.remaining_days) }}
            <span class="muted" style="font-size: 13px; font-weight: 500">
              von {{ d1(balance?.entitlement_days) }}
              · genommen {{ d1(balance?.past_days ?? 0) }}
            </span>
          </div>
          <div class="muted" style="font-size: 12px">
            geplant <span class="num">{{ d1(balance?.planned_days ?? 0) }}</span>
            · noch frei <span class="num">{{ d1(Math.max(0, (balance?.remaining_days ?? 0) - (balance?.planned_days ?? 0))) }}</span>
          </div>
        </div>

        <div class="card" style="flex: 1; min-width: 200px">
          <div class="spread" style="align-items: center">
            <div class="label">Abwesend</div>
            <select v-model.number="selectedSickYear" class="input" style="width: auto; padding: 2px 6px; font-size: 12px">
              <option v-for="y in sickYears" :key="y" :value="y">{{ y }}</option>
            </select>
          </div>
          <div class="num" style="font-size: 22px; font-weight: 700; margin: 4px 0">
            {{ sickDays + unexcusedDays }} <span class="muted" style="font-size: 13px; font-weight: 400">Werktage</span>
          </div>
          <span class="muted">Krank {{ sickDays }} · Unentschuldigt {{ unexcusedDays }}</span>
        </div>

        <div class="card" style="flex: 1; min-width: 200px">
          <div class="label">Wochenstunden</div>
          <div class="num" style="font-size: 22px; font-weight: 700; margin: 4px 0">
            {{ user?.contracted_hours_per_week ?? "—" }} h
          </div>
          <span class="muted">{{ empTypeLabel[user?.employment_type || ""] || "—" }}</span>
        </div>
      </div>

      <!-- Arbeitszeit-Kalender -->
      <div class="card" style="margin-bottom: 16px">
        <!-- Sichtbar machen, wenn Teile des Kalenders nicht geladen werden konnten:
             ohne den Hinweis sieht ein fehlgeschlagener Abruf aus wie "kein Urlaub". -->
        <div v-if="monatLadeFehler" class="wip-banner" style="margin-bottom: 12px">
          {{ monatLadeFehler }}
        </div>
        <div class="spread" style="margin-bottom: 14px">
          <h2>Arbeitszeit-Kalender</h2>
          <div class="row" style="gap: 8px">
            <button v-if="canViewStundenzettel" class="btn" style="padding: 7px 12px; margin-right: 10px"
              @click="sheetOpen = true">Stundenzettel</button>
            <button class="btn" style="padding: 7px 12px" @click="prevMonth">‹</button>
            <span class="num" style="min-width: 130px; text-align: center; font-weight: 600">{{ monthLabel }}</span>
            <button class="btn" style="padding: 7px 12px" @click="nextMonth">›</button>
          </div>
        </div>

        <div class="row" style="gap: 18px; margin-bottom: 12px">
          <span class="muted">Arbeit gesamt: <strong class="num pos">{{ fmtMinutes(totalWork) }}</strong></span>
          <template v-if="monthSummary">
            <span class="muted" title="Soll = effektives Soll nach Abzug der Fehlzeiten-Anteile (Urlaub, Krank, Feiertag). In Klammern die Bruttosollzeit: Arbeitstage × Tagessoll vor diesem Abzug — der Abgleichswert aus dem Altsystem.">Soll: <strong class="num">{{ fmtMinutes(monthSummary.soll_minutes) }}</strong><template v-if="monthSummary.brutto_soll_minutes != null"><span style="font-size: 12px">&nbsp;(brutto {{ fmtMinutes(monthSummary.brutto_soll_minutes) }})</span></template></span>
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
                'cal-vacation': (c.absence === 'vacation') && !c.isHalfAbsence,
                'cal-vac-half': (c.absence === 'vacation') && c.isHalfAbsence,
                'cal-sick': c.absence === 'sick_leave' || c.absence === 'sick',
                'cal-special': c.absence === 'special',
                'cal-abbau': c.absence === 'abbau',
                'cal-unbezahlt': c.absence === 'unbezahlt',
                'cal-absent': c.isAbsent,
                'cal-sel': c.iso === selectedDay,
              }"
              @click="selectDay(c)">
              <span v-if="c.isBirthday" class="cal-birthday"
                :title="c.birthdayAge ? `Geburtstag (${c.birthdayAge})` : 'Geburtstag'">
                <BirthdayIcon /><span v-if="c.birthdayAge" class="cal-birthday-age">{{ c.birthdayAge }}</span>
              </span>
              <span v-if="c.anniversaryYears" class="cal-anniversary"
                :title="`Firmenjubiläum (${c.anniversaryYears} ${c.anniversaryYears === 1 ? 'Jahr' : 'Jahre'})`">
                <AnniversaryIcon /><span class="cal-anniversary-yrs">{{ c.anniversaryYears }}</span>
              </span>
              <div class="num" style="font-size: 12px; font-weight: 600">{{ c.day }}</div>
              <template v-if="c.inMonth">
                <div v-if="c.isAbsent" class="cal-tag cal-absent-tag" title="Unentschuldigtes Fehlen">
                  Unentschuldigt
                </div>
                <template v-else>
                  <!-- Abwesenheits-Tag (Urlaub/Krank); halbtags mit ½-Hinweis,
                       bei Krank mit Art der Krankmeldung (AU/LFZ) -->
                  <div v-if="c.absence" class="cal-tag">
                    {{ absTypeLabel[c.absence] || c.absence }}<span v-if="c.isHalfAbsence && c.absHours"> ({{ hm(c.absHours) }})</span><span v-if="c.absence === 'sick_leave' && c.sickStatus" class="cal-sick-status"> ({{ sickStatusLabel(c.sickStatus) }})</span>
                  </div>
                  <!-- Erfasste Arbeitszeit — wird AUCH an Urlaubstagen angezeigt -->
                  <template v-if="c.iso === todayIso && clockStatus?.is_clocked_in">
                    <div class="num" style="font-size: 11px">
                      {{ fmtMinutes(clockStatus?.today_work_minutes) }}
                      <span class="muted" style="font-size: 9px">läuft</span>
                    </div>
                  </template>
                  <template
                    v-else-if="c.ts && ((c.ts.net_work_minutes || 0) > 0 || c.ts.start_time || c.ts.flagged_reason)">
                    <!-- leere Tage (0 min, keine Stempel, kein Flag) bleiben leer -->
                    <div class="num" style="font-size: 11px">
                      {{ fmtMinutes(c.ts.net_work_minutes) }}
                      <span v-if="flaggedReasonShown(c.ts.flagged_reason)" :title="flaggedReasonShown(c.ts.flagged_reason)" style="color: #b5651d">⚠</span>
                    </div>
                    <div class="muted" style="font-size: 10px">Pause {{ c.ts.break_minutes ?? 0 }}m</div>
                  </template>
                </template>
              </template>
            </div>
          </template>
        </div>

        <!-- Tages-Editor: manuelle Korrekturen (append-only) -->
        <div v-if="selectedDay" id="tages-editor" class="card" style="margin-top: 14px; background: var(--bg)">
          <div class="spread" style="margin-bottom: 10px">
            <strong class="num">{{ fmtDate(selectedDay) }}</strong>
            <button class="btn" style="padding: 5px 10px" @click="closeDay">Schließen</button>
          </div>

          <div v-if="!canCorrect" class="muted">Keine Berechtigung, diesen Tag zu bearbeiten.</div>
          <template v-else>
            <div v-if="dayErr" class="wip-banner" style="margin-bottom: 10px">{{ dayErr }}</div>
            <div v-if="actionErr" class="wip-banner" style="margin-bottom: 10px">{{ actionErr }}</div>

            <!-- Berechnete Tagessumme -->
            <div class="row" style="gap: 18px; flex-wrap: wrap; margin-bottom: 14px">
              <span class="muted">Kommt: <span class="num">{{ isoToTime(daySummaryTs?.start_time) }}</span><span v-if="gestempelterBeginn" class="num muted" :title="`Gestempelt um ${gestempelterBeginn} — gewertet wird erst ab dem erlaubten Beginn (Arbeitszeit-Profil bzw. frühester Beginn aus dem Lohnprofil).`"> ({{ gestempelterBeginn }})</span></span>
              <span class="muted">Geht: <span class="num">{{ isoToTime(daySummaryTs?.end_time) }}</span></span>
              <span class="muted">Pause: <span class="num">{{ daySummaryTs?.break_minutes ?? 0 }}m</span><span v-if="autoBreakMin > 0" class="muted" :title="`Gestempelt ${stampedBreakMin}m — die Pausenregel (über 4 h Arbeit mind. 45 min) ergänzt die Differenz automatisch.`"> (+{{ autoBreakMin }}m auto)</span></span>
              <span class="muted">Arbeit: <strong class="num pos">{{ fmtMinutes(daySummaryTs?.net_work_minutes) }}</strong></span>
              <span v-if="flaggedReasonShown(daySummaryTs?.flagged_reason)" class="wip">⚠ {{ flaggedReasonShown(daySummaryTs?.flagged_reason) }}</span>
            </div>

            <!-- Wochenend-/Feiertagsarbeit: Verbuchung wählbar (Default Überstunden) -->
            <div v-if="(dayIsWeekend || dayIsHoliday) && (daySummaryTs?.net_work_minutes || 0) > 0"
              class="row" style="gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 14px">
              <span class="label" style="margin: 0">{{ dayIsHoliday ? "Feiertagsarbeit" : "Wochenendarbeit" }} verbuchen als</span>
              <select class="input" style="width: auto" :disabled="weBookingBusy"
                :value="daySummaryTs?.weekend_booking === 'gleitzeit' ? 'gleitzeit' : 'ueberstunden'"
                @change="setWeekendBooking(($event.target as HTMLSelectElement).value)">
                <option value="ueberstunden">Überstunden — wird am Monatsende direkt ausgezahlt</option>
                <option value="gleitzeit">Gleitzeit — zählt ins Überstundenkonto</option>
              </select>
            </div>

            <!-- Auto-Erfassung ODER manuell gesetzter Tag eines Stemplers:
                 keine Stempelung, aber Zeiten tageweise anpassbar -->
            <div v-if="isAutoCapture || dayIsManual">
              <template v-if="!autoEdit">
                <div v-if="autoErr" class="wip-banner" style="margin-bottom: 10px">{{ autoErr }}</div>
                <!-- Krank: keine Zeiterfassung möglich, keine Buttons -->
                <div v-if="dayIsSick" class="muted">
                  Krank — an diesem Tag ist keine Zeiterfassung möglich.
                </div>
                <div v-else-if="effectiveEvents.length" class="muted">
                  Stempelungen vorhanden — der Tag wird aus den Stempeln berechnet, nicht automatisch.
                </div>
                <div v-else class="row" style="gap: 10px; align-items: center; flex-wrap: wrap">
                  <span class="muted">{{ isAutoCapture ? "Automatisch erfasst — keine Stempelung." : "Manuell eingetragener Tag — ohne Stempelung." }}</span>
                  <button v-if="canCorrect" class="btn" style="padding: 5px 10px" :disabled="autoBusy" @click="openAutoEdit">
                    Zeiten bearbeiten
                  </button>
                  <!-- „Nicht gearbeitet": wenn nicht schon leer — bei Abwesenheit nur,
                       falls trotzdem Arbeitszeit auf dem Tag steht (Auto-Füllung vor
                       rückwirkendem Urlaub) und die geleert werden soll. -->
                  <button v-if="canCorrect && !dayIsUnworked && (!dayHasAbsence || dayAbsenceWithWork)"
                    class="btn" style="padding: 5px 10px" :disabled="autoBusy"
                    :title="dayAbsenceWithWork ? 'Entfernt die eingetragene Arbeitszeit — die Abwesenheit deckt den Tag dann wieder allein.' : undefined"
                    @click="markUnworked">
                    Nicht gearbeitet
                  </button>
                </div>
              </template>
              <template v-else>
                <div v-if="autoErr" class="wip-banner" style="margin-bottom: 10px">{{ autoErr }}</div>
                <div class="row" style="gap: 14px; flex-wrap: wrap; align-items: flex-end">
                  <div class="stack"><span class="label">Kommt</span>
                    <input v-model="autoStart" type="time" class="input" style="width: auto" /></div>
                  <div class="stack"><span class="label">Geht</span>
                    <input v-model="autoEnd" type="time" class="input" style="width: auto" /></div>
                  <div class="stack"><span class="label">Pause (min)</span>
                    <input v-model.number="autoBreak" type="number" min="0" step="5" class="input" style="width: 80px" /></div>
                  <button class="btn btn-primary" :disabled="autoBusy" @click="saveAutoDay">
                    {{ autoBusy ? "Speichert …" : "Speichern" }}
                  </button>
                  <button class="btn" :disabled="autoBusy" @click="autoEdit = false">Abbrechen</button>
                </div>
                <div class="muted" style="font-size: 11px; margin-top: 10px">
                  Arbeit = (Geht − Kommt) − Pause. Weicht das vom Tagessoll ab, wirkt es im Überstunden-Saldo.
                </div>
              </template>
            </div>
            <!-- Stempelzeiten auch bei Auto-Erfassung zeigen, sobald echte
                 Stempel existieren (Kiosk lässt Auto-MA stempeln; der Tag wird
                 dann aus den Stempeln berechnet). -->
            <template v-if="!isAutoCapture || effectiveEvents.length > 0">
            <!-- Stempelzeiten: Ansicht ↔ Bearbeiten -->
            <div class="spread" style="margin-bottom: 8px">
              <div class="label">Stempelzeiten</div>
              <button class="btn" style="padding: 5px 10px" :disabled="busy"
                @click="toggleDayEditing">{{ dayEditing ? "Fertig" : "Bearbeiten" }}</button>
            </div>

            <div v-if="dayEditing" class="stack" style="margin-bottom: 12px">
              <span class="label">Grund für die Korrektur</span>
              <input v-model="correctionReason" class="input" placeholder="z. B. Terminal defekt, Vergessen auszustempeln …" />
            </div>

            <div v-if="!effectiveEvents.length" class="empty" style="margin-bottom: 16px">
              Noch keine Stempelungen an diesem Tag.
            </div>

            <!-- Ansicht (schreibgeschützt): chronologisch, eine Zeile pro Ereignis -->
            <div v-else-if="!dayEditing" class="stack" style="gap: 6px; margin-bottom: 16px">
              <div v-for="row in timeline" :key="row.key" class="row" style="gap: 10px">
                <span style="width: 64px; font-weight: 600">{{ row.label }}</span>
                <!-- Kappung (Arbeitszeit-Profil): Original dezent durchgestrichen, gewertete Zeit dahinter -->
                <template v-if="row.capped">
                  <span class="num muted" style="text-decoration: line-through">{{ row.time }}</span>
                  <span class="muted" title="Stempelung außerhalb des erlaubten Fensters — gewertet wird die Fenstergrenze (Arbeitszeit-Profil).">
                    → {{ row.capped.label }} <span class="num">{{ row.capped.time }}</span>
                  </span>
                </template>
                <span v-else class="num">{{ row.time }}</span>
                <span v-if="row.src" class="src-tag" :class="{ 'src-kiosk': row.src === 'NFC_TERMINAL' }">{{ srcTagLabel(row.src, row.device) }}</span>
                <span v-if="row.note" class="muted">({{ row.note }})</span>
              </div>
            </div>

            <!-- Bearbeiten: einzelne Zeiten korrigieren/löschen -->
            <div v-else class="stack" style="gap: 8px; margin-bottom: 16px">
              <div v-for="ev in punchIns" :key="ev.id" class="row" style="gap: 10px; align-items: center; flex-wrap: wrap">
                <span style="width: 64px; font-weight: 600">Kommt</span>
                <input v-model="editTimes[ev.id]" type="time" class="input" style="width: auto; padding: 4px 6px" />
                <span class="src-tag" :class="{ 'src-kiosk': ev.source === 'NFC_TERMINAL' }">{{ srcTagLabel(ev.source, ev.device_name) }}</span>
                <button class="btn" style="padding: 4px 8px" :disabled="busy || !timeChanged(ev)" @click="correctEvent(ev)">Speichern</button>
                <button class="btn btn-danger" style="padding: 4px 8px" :disabled="busy" @click="voidEvent(ev)">Löschen</button>
              </div>
              <div v-for="(p, i) in pausen" :key="p.start.id" class="row" style="gap: 10px; align-items: center; flex-wrap: wrap">
                <span style="width: 64px; font-weight: 600">Pause{{ pausen.length > 1 ? " " + (i + 1) : "" }}</span>
                <input v-model="editTimes[p.start.id]" type="time" class="input" style="width: auto; padding: 4px 6px" />
                <span class="muted">–</span>
                <input v-if="p.end" v-model="editTimes[p.end.id]" type="time" class="input" style="width: auto; padding: 4px 6px" />
                <span v-else class="muted">offen</span>
                <button class="btn" style="padding: 4px 8px" :disabled="busy || !pauseChanged(p)" @click="saveBreak(p)">Speichern</button>
                <button class="btn btn-danger" style="padding: 4px 8px" :disabled="busy" @click="deleteBreak(p)">Löschen</button>
              </div>
              <div v-if="effectiveEvents.length && daySummaryTs?.end_time" class="row" style="gap: 10px; align-items: center; flex-wrap: wrap">
                <span style="width: 64px; font-weight: 600">Pause</span>
                <span class="muted">Aufschlag</span>
                <input v-model.number="breakOverrideInput" type="number" min="0" max="480" step="1" class="input" style="width: 76px; padding: 4px 6px" />
                <span class="muted">min</span>
                <button class="btn" style="padding: 4px 8px" :disabled="breakOverrideBusy || breakOverrideInput === autoBreakMin" @click="saveBreakOverride">Speichern</button>
                <span class="muted" style="font-size: 12px" :title="`Aufschlag zusätzlich zu den gestempelten Pausen (${autoBreakHint}). Wird erst am Tagesende dazugebucht. 0 = kein Aufschlag; die ArbZG-Warnung bleibt, wenn real zu wenig Pause gestempelt wurde.`">automatisch ergänzt — hoch/runter korrigierbar</span>
              </div>
              <div v-for="ev in punchOuts" :key="ev.id" class="row" style="gap: 10px; align-items: center; flex-wrap: wrap">
                <span style="width: 64px; font-weight: 600">Geht</span>
                <input v-model="editTimes[ev.id]" type="time" class="input" style="width: auto; padding: 4px 6px" />
                <span class="src-tag" :class="{ 'src-kiosk': ev.source === 'NFC_TERMINAL' }">{{ srcTagLabel(ev.source, ev.device_name) }}</span>
                <button class="btn" style="padding: 4px 8px" :disabled="busy || !timeChanged(ev)" @click="correctEvent(ev)">Speichern</button>
                <button class="btn btn-danger" style="padding: 4px 8px" :disabled="busy" @click="voidEvent(ev)">Löschen</button>
              </div>
            </div>

            <!-- Hinzufügen: nur im Bearbeiten-Modus, nur sinnvolle Aktionen -->
            <template v-if="dayEditing">
              <div class="label" style="margin-bottom: 6px">Hinzufügen</div>
              <div v-if="dayIsSick" class="muted">
                Dieser Tag ist als <b>Krank</b> eingetragen — hier kann keine Arbeitszeit erfasst werden.
              </div>
              <div v-else class="row" style="gap: 16px; flex-wrap: wrap; align-items: flex-end">
                <div v-if="canEinstempeln" class="stack">
                  <span class="label">Einstempeln</span>
                  <div class="row" style="gap: 6px">
                    <input v-model="newKommt" type="time" class="input" style="width: auto" />
                    <button class="btn btn-primary" :disabled="busy" @click="addEvent('PUNCH_IN', newKommt)">Einstempeln</button>
                  </div>
                </div>
                <div v-if="canAusstempeln" class="stack">
                  <span class="label">Ausstempeln</span>
                  <div class="row" style="gap: 6px">
                    <input v-model="newGeht" type="time" class="input" style="width: auto" />
                    <button class="btn" :disabled="busy" @click="tryAusstempeln">Ausstempeln</button>
                  </div>
                </div>
                <div v-if="canPause" class="stack">
                  <span class="label">Pause (von–bis)</span>
                  <div class="row" style="gap: 6px">
                    <input v-model="breakFrom" type="time" class="input" style="width: auto" />
                    <span class="muted">–</span>
                    <input v-model="breakTo" type="time" class="input" style="width: auto" />
                    <button class="btn" :disabled="busy" @click="addBreak">+ Pause</button>
                  </div>
                </div>
                <span v-if="!canEinstempeln && !canAusstempeln && !canPause" class="muted">Tag vollständig erfasst.</span>
              </div>
              <!-- Nachtschicht über Mitternacht: Rückfrage statt Fehler -->
              <div v-if="nightConfirm" class="night-confirm">
                <div>
                  Ausstempeln <b>{{ nightConfirm.geht }}</b> liegt vor dem Kommt um <b>{{ nightConfirm.kommt }}</b> —
                  als <b>Nachtschicht</b> bis {{ nightConfirm.geht }} am {{ folgetagLabel(nightConfirm.folgetag) }} übernehmen?
                </div>
                <div class="muted" style="font-size: 12px; margin-top: 4px">
                  Angelegt werden: Geht 23:59 heute, Kommt 00:00 und Geht {{ nightConfirm.geht }} am Folgetag.
                </div>
                <div class="row" style="gap: 8px; margin-top: 8px">
                  <button class="btn btn-primary" :disabled="busy" @click="confirmNightShift">
                    {{ busy ? "Lege an …" : "Als Nachtschicht übernehmen" }}
                  </button>
                  <button class="btn" :disabled="busy" @click="nightConfirm = null">Abbrechen</button>
                </div>
              </div>
              <div class="muted" style="font-size: 11px; margin-top: 12px">
                Korrekturen werden als neue Einträge gespeichert; die ursprünglichen Kiosk-Stempel bleiben im Protokoll erhalten.
              </div>
            </template>
            </template>
          </template>
        </div>
      </div>

      <!-- Überstunden-Verlauf (einklappbar) -->
      <div class="card" style="margin-bottom: 16px">
        <div class="spread sec-head" :style="secOpen.ot ? 'margin-bottom: 12px' : ''" @click="secOpen.ot = !secOpen.ot">
          <h2><span class="sec-chev">{{ secOpen.ot ? "▾" : "▸" }}</span>Überstunden-Verlauf</h2>
          <div v-if="secOpen.ot" class="row" style="gap: 8px; align-items: center" @click.stop>
            <select v-model.number="otMonthSel" class="input" style="width: auto">
              <option v-for="(mn, i) in otMonthNames" :key="i" :value="i + 1">{{ mn }}</option>
            </select>
            <select v-model.number="otYearSel" class="input" style="width: auto">
              <option v-for="y in otYears" :key="y" :value="y">{{ y }}</option>
            </select>
            <button v-if="canManageOvertime && !otAdjOpen" class="btn" @click="openOtAdj">+ Korrektur</button>
          </div>
          <span v-else class="muted num" @click.stop="secOpen.ot = true">Saldo {{ hm(overtime?.balance_hours) }}</span>
        </div>

        <template v-if="secOpen.ot">
        <div v-if="otAdjHinweis" class="wip-banner" style="margin-bottom: 14px">{{ otAdjHinweis }}</div>
        <div class="row" style="gap: 18px; margin-bottom: 14px">
          <span class="muted">Aktueller Saldo:
            <strong class="num">{{ hm(overtime?.balance_hours) }}</strong>
          </span>
        </div>

        <!-- Manuelle Korrektur (nur mit Recht) -->
        <div v-if="canManageOvertime && otAdjOpen" class="card" style="background: var(--bg); margin-bottom: 16px">
          <div class="label" style="margin-bottom: 10px">{{ otAdjEditId ? "Korrektur bearbeiten" : "Manuelle Korrektur" }}</div>
          <div v-if="otAdjErr" class="wip-banner" style="margin-bottom: 10px">{{ otAdjErr }}</div>
          <div class="row" style="gap: 14px; flex-wrap: wrap; align-items: flex-end">
            <div class="stack">
              <span class="label">Korrektur (± erlaubt)</span>
              <div class="row" style="gap: 6px; align-items: center">
                <select v-model="otAdjSign" class="input" style="width: auto">
                  <option value="+">+</option>
                  <option value="−">−</option>
                </select>
                <input v-model.number="otAdjH" type="number" min="0" step="1" class="input"
                  style="width: 64px" placeholder="0" />
                <span class="muted">h</span>
                <input v-model.number="otAdjM" type="number" min="0" max="59" step="1" class="input"
                  style="width: 64px" placeholder="0" />
                <span class="muted">min</span>
              </div>
            </div>
            <div class="stack" style="flex: 1; min-width: 180px">
              <span class="label">Grund</span>
              <input v-model="otAdjReason" class="input" />
            </div>
            <div class="stack">
              <span class="label">Datum (optional)</span>
              <input v-model="otAdjDate" type="date" class="input" style="width: auto" />
            </div>
            <div class="row" style="gap: 8px">
              <button class="btn btn-primary" :disabled="otAdjBusy" @click="submitOtAdj">
                {{ otAdjBusy ? "Speichert …" : "Speichern" }}
              </button>
              <button class="btn" :disabled="otAdjBusy" @click="cancelOtAdj">Abbrechen</button>
            </div>
          </div>
        </div>

        <div v-if="!otMonthRows.length" class="empty">
          Keine Saldo-Änderungen im {{ otMonthNames[otMonthSel - 1] }} {{ otYearSel }}.
        </div>
        <table v-else class="data">
          <thead>
            <tr>
              <th>Datum</th>
              <th>Art</th>
              <th class="num">Ist</th>
              <th class="num">Soll</th>
              <th class="num">Δ</th>
              <th class="num">Saldo</th>
              <th v-if="canManageOvertime" style="text-align: right">Aktion</th>
            </tr>
          </thead>
          <tbody>
            <!-- Kopfzeile: Gesamtsaldo des Monats + was mit den Stunden passiert ist -->
            <tr class="ot-month-row">
              <td class="num" style="white-space: nowrap">{{ otMonthNames[otMonthSel - 1] }} {{ otYearSel }}</td>
              <td>
                Monatssaldo
                <!-- Auszahlung/Übertrag erst zeigen, wenn der Monat abgeschlossen
                     ist (accrual-Zeile im Ledger). Vorher ist der Saldo laufend,
                     ein „Übertrag in Folgemonat" wäre irreführend. -->
                <span v-if="otMonthSettled" class="muted" style="font-size: 12px">
                  <template v-if="otMonthSummary.payout < 0">
                    — ausbezahlt: {{ hm(-otMonthSummary.payout) }}
                  </template>
                  <!-- Verfall sichtbar machen: ohne diese Zeile wäre der
                       Saldo-Sprung am Monatsende nicht erklärbar. -->
                  <template v-if="otMonthSummary.forfeit < 0">
                    — verfallen: {{ hm(-otMonthSummary.forfeit) }}
                  </template>
                  · übertragen in Folgemonat: {{ hmSigned(otMonthSummary.endBalance) }}
                </span>
              </td>
              <td class="num">{{ hm(otMonthSummary.worked) }}</td>
              <td class="num muted" title="Effektives Soll des Monats; in Klammern die Bruttosollzeit (vor Fehlzeiten-Abzug).">{{ hm(otMonthSummary.target) }}<div v-if="otBruttoSoll != null" style="font-size: 11px">(brutto {{ hm(otBruttoSoll / 60) }})</div></td>
              <td class="num" :class="otMonthSummary.delta < 0 ? 'ot-neg' : 'ot-pos'">{{ hmSigned(otMonthSummary.delta) }}</td>
              <td class="num">{{ hmSigned(otMonthSummary.endBalance) }}</td>
              <td v-if="canManageOvertime"></td>
            </tr>
            <tr v-for="(r, i) in otMonthRows" :key="r.id ?? (r.date + '-' + i)"
              :class="{ 'ot-adj-row': r.type === 'adjustment' }">
              <td class="num">{{ fmtDate(r.date) }}</td>
              <td>
                {{ otRowLabel(r) }}
                <span v-if="r.type !== 'day' && r.reason" class="muted" style="font-size: 12px">— {{ r.reason }}</span>
                <span v-if="isDirectPayout(r)" class="muted" style="font-size: 12px"
                  :title="r.holiday ? 'Feiertagsarbeit zählt nicht ins Gleitzeitkonto und wird direkt vergütet (Zuschlag 100 %).' : 'Wochenendarbeit zählt nicht ins Gleitzeitkonto und wird direkt vergütet (Sa+So 25 %).'">
                  — zählt nicht ins Konto, wird direkt ausbezahlt</span>
              </td>
              <td class="num">{{ r.type === "day" && !r.absent ? hm(r.worked_hours) : "—" }}</td>
              <td class="num muted">{{ r.type === "day" && !isDirectPayout(r) ? hm(r.target_hours) : "—" }}</td>
              <td class="num" :class="isDirectPayout(r) ? 'muted' : r.delta_hours < 0 ? 'ot-neg' : 'ot-pos'">
                <template v-if="isDirectPayout(r)">→ Auszahlung</template>
                <template v-else>{{ hmSigned(r.delta_hours) }}</template>
              </td>
              <td class="num">{{ hmSigned(r.balance_hours) }}</td>
              <td v-if="canManageOvertime" style="text-align: right; white-space: nowrap">
                <template v-if="r.type === 'adjustment' && r.id">
                  <template v-if="otDelId === String(r.id)">
                    <span class="muted" style="font-size: 12px">Löschen?</span>
                    <button class="linkbtn danger" @click="deleteOtAdj(String(r.id))">Ja</button>
                    <button class="linkbtn" @click="otDelId = null">Nein</button>
                  </template>
                  <template v-else>
                    <button class="linkbtn" @click="openOtDailyEdit(r)">Bearbeiten</button>
                    <button class="linkbtn danger" @click="otDelId = String(r.id)">Löschen</button>
                  </template>
                </template>
              </td>
            </tr>
            <!-- Der Saldovortrag ist das Ereignis am Monatsersten — die Liste
                 läuft neueste-zuerst, also steht er ganz unten. -->
            <tr class="ot-carry-row">
              <td class="num muted">{{ fmtDate(otMonth + "-01") }}</td>
              <td>Übernahme aus Vormonat</td>
              <td class="num">—</td>
              <td class="num">—</td>
              <td class="num">—</td>
              <td class="num">{{ hmSigned(otMonthSummary.carryIn) }}</td>
              <td v-if="canManageOvertime"></td>
            </tr>
          </tbody>
        </table>
        </template>
      </div>

      <!-- Abwesenheiten (einklappbar) -->
      <div class="card" style="margin-bottom: 16px">
        <div class="spread sec-head" :style="secOpen.abs ? 'margin-bottom: 12px' : ''" @click="secOpen.abs = !secOpen.abs">
          <h2><span class="sec-chev">{{ secOpen.abs ? "▾" : "▸" }}</span>Abwesenheiten</h2>
          <button v-if="secOpen.abs && !absFormOpen" class="btn" @click.stop="openAbsForm">+ Abwesenheit</button>
          <span v-else-if="!secOpen.abs" class="muted num" @click.stop="secOpen.abs = true">{{ upcomingAbs.length }} aktuell/kommend</span>
        </div>

        <template v-if="secOpen.abs">
        <!-- Abwesenheit eintragen (auto-genehmigt) — erst nach Klick -->
        <div v-if="absFormOpen" class="card" style="background: var(--bg); margin-bottom: 16px">
          <div class="label" style="margin-bottom: 10px">Abwesenheit eintragen</div>
          <div v-if="absFormErr" class="wip-banner" style="margin-bottom: 10px">{{ absFormErr }}</div>
          <div class="row absform-row" style="gap: 14px; flex-wrap: wrap; align-items: flex-end">
            <div class="stack">
              <span class="label">Typ</span>
              <select v-model="absFormType" class="input" style="width: auto">
                <option value="VACATION">Urlaub</option>
                <option value="SICK_LEAVE">Krank</option>
                <option value="SPECIAL_LEAVE">Sonderurlaub</option>
                <option value="OVERTIME_REDUCTION">Überstunden abbauen</option>
                <option value="UNPAID_LEAVE">Unbezahlt (entschuldigt)</option>
              </select>
            </div>
            <div class="stack">
              <span class="label">Tagesteil</span>
              <select v-model="absFormDayPart" class="input" style="width: auto">
                <option value="FULL">Ganzer Tag</option>
                <option value="HALF">Halber Tag</option>
              </select>
            </div>
            <div v-if="absFormDayPart === 'HALF'" class="stack">
              <span class="label">Stunden</span>
              <input v-model.number="absFormHalfHours" type="number" step="0.5" min="1" max="7" class="input" style="width: 90px" />
            </div>
            <div v-if="absFormType === 'SICK_LEAVE'" class="stack">
              <span class="label">AU-Status</span>
              <select v-model="absFormSickStatus" class="input" style="width: auto">
                <option value="">— nicht gesetzt —</option>
                <option v-for="o in sickStatusOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
            </div>
            <div class="stack">
              <span class="label">{{ absFormDayPart === 'HALF' ? 'Datum' : 'Von' }}</span>
              <input v-model="absFormFrom" type="date" class="input" style="width: auto" />
            </div>
            <div v-if="absFormDayPart === 'FULL'" class="stack">
              <span class="label">Bis</span>
              <input v-model="absFormTo" type="date" class="input" style="width: auto" />
            </div>
            <div class="stack" style="flex: 1; min-width: 180px">
              <span class="label">{{ absFormType === 'SPECIAL_LEAVE' ? 'Grund (Pflicht)' : 'Notiz (optional)' }}</span>
              <input v-model="absFormNotes" class="input"
                :placeholder="absFormType === 'SPECIAL_LEAVE' ? 'z. B. Hochzeit, Beerdigung' : ''" />
            </div>
            <div class="row" style="gap: 8px">
              <button class="btn btn-primary" :disabled="absFormBusy" @click="submitAbsence">
                {{ absFormBusy ? "Speichert …" : "Eintragen" }}
              </button>
              <button class="btn" :disabled="absFormBusy" @click="cancelAbsForm">Abbrechen</button>
            </div>
          </div>
        </div>

        <div v-if="absRevokeErr" class="wip-banner" style="margin-bottom: 12px">{{ absRevokeErr }}</div>

        <div v-if="absDecideErr" class="wip-banner" style="margin-bottom: 10px">{{ absDecideErr }}</div>
        <!-- Aktuell & kommend (immer sichtbar) -->
        <div class="label" style="margin-bottom: 8px">Aktuell &amp; kommend</div>
        <div v-if="!upcomingAbs.length" class="empty" style="margin-bottom: 16px">Keine aktuellen oder kommenden Abwesenheiten.</div>
        <table v-else class="data" style="margin-bottom: 16px">
          <thead>
            <tr>
              <th>Typ</th><th>Art</th><th>Zeitraum</th><th>Tage</th><th>Status</th>
              <th v-if="canEditAbsences"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in upcomingAbs" :key="r.id || ((r.date_from || '') + (r.request_type || ''))">
              <td>{{ absReqTypeLabel(r.request_type) }}</td>
              <td>
                <template v-if="(r.request_type || '').toUpperCase() === 'SICK_LEAVE'">
                  <select v-if="absEditId === r.id" v-model="absEditSickStatus" class="input" style="width: auto; padding: 4px 6px">
                    <option value="">—</option>
                    <option v-for="o in sickStatusOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
                  </select>
                  <template v-else>{{ r.sick_status ? sickStatusLabel(r.sick_status) : "—" }}</template>
                </template>
                <template v-else>—</template>
              </td>
              <td class="num">
                <template v-if="absEditId === r.id">
                  <span class="row" style="gap: 6px; align-items: center; white-space: nowrap">
                    <input v-model="absEditFrom" type="date" class="input" style="width: 140px" />
                    <span class="muted">–</span>
                    <input v-model="absEditTo" type="date" class="input" style="width: 140px" />
                  </span>
                </template>
                <template v-else>{{ fmtDate(r.date_from) }} – {{ fmtDate(r.date_to) }}</template>
              </td>
              <td class="num">{{ r.days_count ?? "—" }}</td>
              <td>{{ absReqStatusLabel(r.status) }}</td>
              <td v-if="canEditAbsences" style="text-align: right; white-space: nowrap">
                <template v-if="absRevokeConfirmId === r.id">
                  <span class="row" style="gap: 8px; justify-content: flex-end; align-items: center">
                    <span class="muted" style="font-size: 12px">Wirklich?</span>
                    <button class="linkbtn danger" :disabled="absRevokeBusy" @click="revokeAbs(r.id)">Ja</button>
                    <button class="linkbtn" :disabled="absRevokeBusy" @click="cancelRevokeAbs">Abbrechen</button>
                  </span>
                </template>
                <template v-else-if="absEditId === r.id">
                  <button class="btn btn-primary" style="padding: 5px 12px" :disabled="absEditBusy"
                    @click="saveAbsEdit(r.id)">Speichern</button>
                  <button class="linkbtn" style="margin-left: 10px" :disabled="absEditBusy" @click="cancelAbsEdit">Abbrechen</button>
                </template>
                <template v-else>
                  <span v-if="isPendingAbs(r)" class="row" style="gap: 10px; justify-content: flex-end; display: inline-flex">
                    <button class="linkbtn" style="color: var(--ok, #1a7f37); font-weight: 600" :disabled="absDecideBusy"
                      @click="decideAbs(r.id, 'approve')">Genehmigen</button>
                    <button class="linkbtn danger" :disabled="absDecideBusy" @click="decideAbs(r.id, 'reject')">Ablehnen</button>
                  </span>
                  <button class="linkbtn" style="margin-left: 10px" @click="startAbsEdit(r)">Bearbeiten</button>
                  <button class="linkbtn danger" style="margin-left: 10px" @click="askRevokeAbs(r.id)">Entfernen</button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>

        <!-- Vergangene (ausklappbar) -->
        <button class="linkbtn" style="margin-bottom: 8px" @click="pastOpen = !pastOpen">
          {{ pastOpen ? "▾" : "▸" }} Vergangene Abwesenheiten ({{ pastAbs.length }})
        </button>
        <template v-if="pastOpen">
          <!-- Filter (nur für vergangene Abwesenheiten) -->
          <div class="row absfilter-row" style="gap: 14px; flex-wrap: wrap; align-items: flex-end; margin-bottom: 14px">
            <div class="stack">
              <span class="label">Typ</span>
              <select v-model="absFilterType" class="input" style="width: auto">
                <option value="">Alle</option>
                <option value="VACATION">Urlaub</option>
                <option value="SICK_LEAVE">Krank</option>
                <option value="SPECIAL_LEAVE">Sonderurlaub</option>
                <option value="OVERTIME_REDUCTION">Überstundenabbau</option>
                <option value="UNPAID_LEAVE">Unbezahlt</option>
              </select>
            </div>
            <div class="stack">
              <span class="label">Von</span>
              <input v-model="absFilterFrom" type="date" class="input" style="width: auto" />
            </div>
            <div class="stack">
              <span class="label">Bis</span>
              <input v-model="absFilterTo" type="date" class="input" style="width: auto" />
            </div>
          </div>
          <div v-if="!pastAbs.length" class="empty">Keine vergangenen Abwesenheiten.</div>
          <table v-else class="data">
            <thead>
              <tr>
                <th>Typ</th><th>Art</th><th>Zeitraum</th><th>Tage</th><th>Status</th>
                <th v-if="canEditAbsences"></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in pastAbs" :key="r.id || ((r.date_from || '') + (r.request_type || ''))">
                <td>{{ absReqTypeLabel(r.request_type) }}</td>
                <td>
                  <template v-if="(r.request_type || '').toUpperCase() === 'SICK_LEAVE'">
                    <select v-if="absEditId === r.id" v-model="absEditSickStatus" class="input" style="width: auto; padding: 4px 6px">
                      <option value="">—</option>
                      <option v-for="o in sickStatusOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
                    </select>
                    <template v-else>{{ r.sick_status ? sickStatusLabel(r.sick_status) : "—" }}</template>
                  </template>
                  <template v-else>—</template>
                </td>
                <td class="num">
                  <template v-if="absEditId === r.id">
                    <span class="row" style="gap: 6px; align-items: center; white-space: nowrap">
                      <input v-model="absEditFrom" type="date" class="input" style="width: 140px" />
                      <span class="muted">–</span>
                      <input v-model="absEditTo" type="date" class="input" style="width: 140px" />
                    </span>
                  </template>
                  <template v-else>{{ fmtDate(r.date_from) }} – {{ fmtDate(r.date_to) }}</template>
                </td>
                <td class="num">{{ r.days_count ?? "—" }}</td>
                <td>{{ absReqStatusLabel(r.status) }}</td>
                <td v-if="canEditAbsences" style="text-align: right; white-space: nowrap">
                  <template v-if="absRevokeConfirmId === r.id">
                    <span class="row" style="gap: 8px; justify-content: flex-end; align-items: center">
                      <span class="muted" style="font-size: 12px">Wirklich?</span>
                      <button class="linkbtn danger" :disabled="absRevokeBusy" @click="revokeAbs(r.id)">Ja</button>
                      <button class="linkbtn" :disabled="absRevokeBusy" @click="cancelRevokeAbs">Abbrechen</button>
                    </span>
                  </template>
                  <template v-else-if="absEditId === r.id">
                    <button class="btn btn-primary" style="padding: 5px 12px" :disabled="absEditBusy"
                      @click="saveAbsEdit(r.id)">Speichern</button>
                    <button class="linkbtn" style="margin-left: 10px" :disabled="absEditBusy" @click="cancelAbsEdit">Abbrechen</button>
                  </template>
                  <template v-else>
                    <button class="linkbtn" @click="startAbsEdit(r)">Bearbeiten</button>
                    <button class="linkbtn danger" style="margin-left: 10px" @click="askRevokeAbs(r.id)">Entfernen</button>
                  </template>
                </td>
              </tr>
            </tbody>
          </table>
        </template>
        </template>
      </div>

      <!-- Kiosk-Hinweise: persönliche Meldungen am Terminal (nach dem Stempeln) -->
      <div v-if="canManageKioskMsgs" class="card" style="margin-bottom: 16px">
        <div class="spread" style="cursor: pointer" @click="toggleKioskMsgs">
          <h2><span class="sec-chev">{{ kioskMsgOpen ? "▾" : "▸" }}</span>Kiosk-Hinweise</h2>
          <span class="muted" style="font-size: 12.5px">Zeigt sich diesem MA nach dem Stempeln</span>
        </div>
        <template v-if="kioskMsgOpen">
          <div class="muted" style="font-size: 12.5px; margin: 10px 0">
            Persönliche Kurzmeldung, die diesem Mitarbeiter direkt nach dem Stempeln am Terminal angezeigt wird
            (z. B. „Betriebsarzt heute 10 Uhr“).
          </div>
          <div v-if="kioskErr" class="wip-banner" style="margin-bottom: 10px">{{ kioskErr }}</div>
          <div v-for="m in kioskMsgs" :key="m.id" class="spread" style="padding: 6px 0; border-bottom: 1px solid var(--divider)">
            <span>{{ m.text }}<span v-if="m.active_from || m.active_until" class="muted" style="font-size: 12px"> —<template v-if="m.active_from"> ab {{ fmtDate(m.active_from?.slice(0, 10)) }}</template><template v-if="m.active_until"> bis {{ fmtDate(m.active_until?.slice(0, 10)) }}</template></span></span>
            <button class="linkbtn danger" :disabled="kioskBusy" @click="delKioskMsg(m)">Entfernen</button>
          </div>
          <div class="row" style="flex-wrap: wrap; gap: 8px; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--divider)">
            <input v-model="kioskNewText" class="input" style="flex: 1; min-width: 220px" maxlength="280" placeholder="Neue persönliche Meldung" />
            <label class="row" style="gap: 6px; align-items: center; font-size: 13px">
              <span class="muted">anzeigen von</span>
              <input v-model="kioskNewFrom" type="date" class="input" style="width: auto" />
            </label>
            <label class="row" style="gap: 6px; align-items: center; font-size: 13px">
              <span class="muted">bis</span>
              <input v-model="kioskNewUntil" type="date" class="input" style="width: auto" />
            </label>
            <button class="btn" :disabled="kioskBusy || !kioskNewText.trim()" @click="addKioskMsg">+ Hinweis</button>
          </div>
        </template>
      </div>

      <!-- Stempel-Protokoll: unveränderbares Log (read-only) -->
      <div class="card" style="margin-bottom: 16px">
        <div class="spread" style="cursor: pointer" @click="togglePunchLog">
          <h2><span class="sec-chev">{{ plOpen ? "▾" : "▸" }}</span>Stempel-Protokoll</h2>
          <span class="muted" style="font-size: 12.5px">Unveränderbares Log aller Buchungen</span>
        </div>
        <template v-if="plOpen">
          <div class="row" style="gap: 12px; flex-wrap: wrap; align-items: center; margin: 12px 0">
            <span class="label" style="margin: 0">Von</span>
            <input v-model="plFrom" type="date" class="input" style="width: auto" />
            <span class="label" style="margin: 0">Bis</span>
            <input v-model="plTo" type="date" class="input" style="width: auto" />
            <button class="btn" style="padding: 4px 10px" @click="plShowMonth">Monat</button>
            <label class="row" style="gap: 6px; align-items: center; font-size: 13px; cursor: pointer">
              <input v-model="plOnlyTerminal" type="checkbox" /> nur Terminal
            </label>
            <label class="row" style="gap: 6px; align-items: center; font-size: 13px; cursor: pointer">
              <input v-model="plExtended" type="checkbox" /> Erweitert
            </label>
            <span v-if="plLoading" class="muted" style="font-size: 12.5px">Lädt …</span>
          </div>
          <div v-if="plErr" class="wip-banner" style="margin-bottom: 10px">{{ plErr }}</div>
          <div v-else-if="plLoaded && !plShown.length" class="empty">Keine Buchungen im Zeitraum.</div>
          <table v-else-if="plShown.length" class="data">
            <thead>
              <tr>
                <th>Zeit</th><th>Aktion</th><th>Quelle</th><th>Status</th><th>Grund</th>
                <template v-if="plExtended"><th>Konto</th><th>Gespeichert</th><th>ID</th></template>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in plPage" :key="r.id" :style="r.effective ? '' : 'opacity: 0.55'">
                <td class="num">
                  {{ fmtDate(r.event_time?.slice(0, 10)) }}, {{ isoToTime(r.event_time) }}
                  <span v-if="plNachgereicht(r)" class="muted" style="font-size: 11.5px"> · {{ plNachgereicht(r) }}</span>
                </td>
                <td>{{ plTypeLabel[r.event_type] || r.event_type }}</td>
                <td>
                  <span class="src-tag" :class="{ 'src-kiosk': r.source === 'NFC_TERMINAL' }" :title="r.device_id || undefined">{{ srcTagLabel(r.source, r.device_name, r.device_id) }}</span>
                  <span v-if="r.created_by_name" class="muted" style="font-size: 11.5px"> — {{ r.created_by_name }}</span>
                </td>
                <td>{{ plStatus(r) }}</td>
                <td class="muted" style="font-size: 12.5px">{{ r.reason || "—" }}</td>
                <template v-if="plExtended">
                  <td class="muted" style="font-size: 12px">{{ r.created_by_account_email || "—" }}</td>
                  <td class="num muted" style="font-size: 12px">{{ fmtDate(r.created_at?.slice(0, 10)) }}, {{ isoToTime(r.created_at) }}</td>
                  <td class="num muted" style="font-size: 12px">#{{ r.id }}</td>
                </template>
              </tr>
            </tbody>
          </table>
          <div v-if="plShown.length > plVisible" class="row" style="justify-content: center; margin-top: 10px">
            <button class="btn" @click="plVisible += PL_PAGE">
              Weitere {{ Math.min(PL_PAGE, plShown.length - plVisible) }} anzeigen
              <span class="muted" style="font-weight: 400">({{ plVisible }} von {{ plShown.length }})</span>
            </button>
          </div>
          <p class="muted" style="margin: 10px 0 0; font-size: 12px">
            Das Protokoll ist unveränderbar: Kiosk-Buchungen werden nie geändert oder gelöscht.
            Korrekturen und Stornos sind eigene Einträge mit Verweis, Autor und Grund.
          </p>
        </template>
      </div>

      <!-- Stammdaten (einklappbar) -->
      <div class="card" style="margin-bottom: 16px">
        <div class="spread sec-head" :style="secOpen.stamm ? 'margin-bottom: 14px' : ''" @click="secOpen.stamm = !secOpen.stamm">
          <h2><span class="sec-chev">{{ secOpen.stamm ? "▾" : "▸" }}</span>Stammdaten</h2>
          <template v-if="secOpen.stamm">
            <button v-if="!editing" class="btn" @click.stop="startEdit">Bearbeiten</button>
            <div v-else class="row" style="gap: 8px" @click.stop>
              <button class="btn" @click="cancelEdit" :disabled="saving">Abbrechen</button>
              <button class="btn btn-primary" @click="save" :disabled="saving">
                {{ saving ? "Speichert …" : "Speichern" }}
              </button>
            </div>
          </template>
        </div>

        <template v-if="secOpen.stamm">
        <div v-if="saveErr" class="wip-banner" style="margin-bottom: 12px">{{ saveErr }}</div>

        <!-- Anzeige -->
        <div v-if="!editing" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px">
          <div class="stack"><span class="label">Vorname</span><span>{{ user?.first_name || "—" }}</span></div>
          <div class="stack"><span class="label">Nachname</span><span>{{ user?.last_name || "—" }}</span></div>
          <div class="stack"><span class="label">E-Mail</span><span>{{ user?.email || "—" }}</span></div>
          <div class="stack"><span class="label">Personalnr.</span><span class="num">{{ user?.employee_number || "—" }}</span></div>
          <div class="stack"><span class="label">Bereich</span><span>{{ deptName(user?.department_id) }}</span></div>
          <div class="stack"><span class="label">Firma</span><span>{{ companyName(user?.company_id) }}</span></div>
          <div class="stack"><span class="label">Vorgesetzter</span><span>{{ managerName(user?.manager_id) }}</span></div>
          <div class="stack"><span class="label">Beschäftigungsart</span><span>{{ empTypeLabel[user?.employment_type || ""] || "—" }}</span></div>
          <div class="stack"><span class="label">Erfassungsmodus</span><span>{{ captureLabel[user?.time_capture || ""] || "—" }}</span></div>
          <div class="stack"><span class="label">Arbeitszeit-Profil</span><span>{{ workTimeProfileName(user?.work_time_profile_id) }}</span></div>
          <div class="stack"><span class="label">Abrechnungsprofil</span><span>{{ wageProfileName(user?.wage_profile_id) }}</span></div>
          <div class="stack"><span class="label">Eintrittsdatum</span><span class="num">{{ fmtDate(user?.hire_date) }}</span></div>
          <div class="stack"><span class="label">Erfassungsbeginn</span><span class="num">{{ fmtDate(user?.counting_start_date) }}</span></div>
          <div class="stack"><span class="label">Austrittsdatum</span><span class="num">{{ fmtDate(user?.termination_date) }}</span></div>
          <div class="stack"><span class="label">Austrittsgrund</span><span>{{ terminationReasonLabel(user?.termination_reason) || "—" }}</span></div>
          <div class="stack"><span class="label">Std/Woche</span><span class="num">{{ user?.contracted_hours_per_week ?? "—" }}</span></div>
          <!-- NFC-Profil mit Stundenbindung: das Profil-Tagessoll gewinnt serverseitig. -->
          <div v-if="assignedWtpTargets" class="stack" style="grid-column: 1 / -1">
            <span class="label">Tages-Soll pro Wochentag</span>
            <span class="num">{{ weekdayTargetDisplay(assignedWtpTargets) }}
              <span class="muted" style="font-size: 12px">(aus Arbeitszeit-Profil {{ assignedWtProfile?.name }})</span>
            </span>
          </div>
          <div v-else-if="user?.weekday_target_minutes" class="stack" style="grid-column: 1 / -1">
            <span class="label">Tages-Soll pro Wochentag</span>
            <span class="num">{{ weekdayTargetDisplay(user?.weekday_target_minutes) }}</span>
          </div>
          <div class="stack"><span class="label">Urlaubsanspruch/Jahr</span><span class="num">{{ user?.vacation_entitlement_days ?? "—" }}</span></div>
          <div v-if="user?.initial_vacation_remaining_days != null" class="stack"><span class="label">Resturlaub Eintrittsjahr</span><span class="num">{{ user?.initial_vacation_remaining_days }}</span></div>
          <div class="stack"><span class="label">Geburtstag</span><span class="num">{{ fmtDate(user?.birth_date) }}</span></div>
          <div class="stack"><span class="label">Überstunden-Auszahlung</span><span>{{ otModeLabel[user?.overtime_payout_mode || ""] || "—" }}</span></div>
          <div v-if="otModeMitGrenze.includes(user?.overtime_payout_mode || '')" class="stack"><span class="label">Kappungsgrenze (h)</span><span class="num">{{ user?.overtime_cap_hours ?? "—" }}</span></div>
        </div>

        <!-- Bearbeiten -->
        <div v-else style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px">
          <div class="stack"><span class="label">Vorname</span><input v-model="form.first_name" class="input" /></div>
          <div class="stack"><span class="label">Nachname</span><input v-model="form.last_name" class="input" /></div>
          <div class="stack"><span class="label">E-Mail</span><input v-model="form.email" type="email" class="input" /></div>
          <div class="stack"><span class="label">Personalnr.</span><input v-model="form.employee_number" class="input" /></div>
          <div class="stack">
            <span class="label">Bereich</span>
            <select v-model="form.department_id" class="input">
              <option value="">—</option>
              <option v-for="d in filteredDepartments" :key="d.id" :value="d.id">{{ d.name }}</option>
            </select>
          </div>
          <div class="stack">
            <span class="label">Firma</span>
            <select v-model="form.company_id" class="input">
              <option value="">—</option>
              <option v-for="c in companies" :key="c.id" :value="c.id">{{ c.name }}</option>
            </select>
          </div>
          <div class="stack">
            <span class="label">Vorgesetzter</span>
            <select v-model="form.manager_id" class="input">
              <option value="">—</option>
              <option v-for="m in managerOptions" :key="m.id" :value="m.id">
                {{ m.name }}{{ m.is_active === false ? " (inaktiv)" : "" }}
              </option>
            </select>
          </div>
          <div class="stack">
            <span class="label">Beschäftigungsart</span>
            <select v-model="form.employment_type" class="input">
              <option value="salaried">Gehalt</option>
              <option value="hourly">Stundenlohn</option>
              <option value="werkstudent">Werkstudent / Aushilfe</option>
            </select>
            <span
              v-if="form.employment_type === 'werkstudent'"
              class="muted"
              style="font-size: 12px"
            >
              Zeiten werden erfasst, aber ohne Tagessoll — kein Minus, kein
              Überstundenkonto. Läuft nicht in den DATEV-Export; die Stunden gehen über
              den Monats-Stundenzettel in die Abrechnung.
            </span>
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
            <span class="label">Arbeitszeit-Profil</span>
            <select v-model="form.work_time_profile_id" class="input"
              title="Stempel-Kappung bzw. Auto-Zeiten (Einstellungen → Arbeitszeit-Profile); angeboten werden nur zum Erfassungsmodus passende Profile">
              <option value="">— kein Profil —</option>
              <option v-for="p in compatibleWtProfiles" :key="p.id" :value="p.id">
                {{ p.name }}{{ p.is_active === false ? " (inaktiv)" : "" }}
              </option>
            </select>
          </div>
          <div class="stack">
            <span class="label">Abrechnungsprofil</span>
            <select v-model="form.wage_profile_id" class="input"
              title="Abweichende Lohnart-Zuordnungen (Einstellungen → Lohnarten → Abrechnungsprofile)">
              <option value="">Standard</option>
              <option v-for="p in wageProfiles" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
          </div>
          <div class="stack"><span class="label">Eintrittsdatum</span><input v-model="form.hire_date" type="date" class="input" /></div>
          <div class="stack"><span class="label">Erfassungsbeginn</span><input v-model="form.counting_start_date" type="date" class="input" /></div>
          <div class="stack"><span class="label">Austrittsdatum</span><input v-model="form.termination_date" type="date" class="input" /></div>
          <div class="stack"><span class="label">Austrittsgrund</span>
            <select v-model="form.termination_reason" class="input">
              <option value="">—</option>
              <option v-for="o in terminationReasons" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </div>
          <div class="stack"><span class="label">Std/Woche</span><input v-model.number="form.contracted_hours_per_week" type="number" step="0.5" class="input" /></div>
          <!-- Tages-Soll pro Wochentag (optional). Bei NFC-Profil mit Stunden-
               bindung deaktiviert — das Profil-Tagessoll gewinnt serverseitig. -->
          <div class="stack" style="grid-column: 1 / -1">
            <label class="row" style="gap: 8px; align-items: center" :style="formWtpTargets ? '' : 'cursor: pointer'">
              <input v-model="wdEnabled" type="checkbox" :disabled="!!formWtpTargets" />
              <span class="label" style="margin: 0">Tages-Soll pro Wochentag festlegen</span>
            </label>
            <span v-if="formWtpTargets" class="muted" style="font-size: 12px">
              {{ weekdayTargetDisplay(formWtpTargets) }} (aus Arbeitszeit-Profil
              {{ formWtProfile?.name }} — das Profil bestimmt das Tages-Soll)
            </span>
            <div v-if="wdEnabled && !formWtpTargets" class="wd-grid">
              <div v-for="(d, i) in wdHM" :key="i" class="wd-day">
                <span class="label" style="width: 26px">{{ WEEKDAY_LABELS[i] }}</span>
                <input v-model.number="d.h" type="number" min="0" max="24" class="input wd-num" /><span class="muted">:</span>
                <input v-model.number="d.m" type="number" min="0" max="59" class="input wd-num" />
              </div>
              <div class="muted" style="grid-column: 1 / -1; font-size: 12px">
                Wochensumme: <strong>{{ wdWeeklySum }}</strong> — leer/deaktiviert = Wochenstunden ÷ 5.
              </div>
            </div>
          </div>
          <div class="stack"><span class="label">Urlaubsanspruch/Jahr</span><input v-model.number="form.vacation_entitlement_days" type="number" step="any" min="0" class="input" /></div>
          <div class="stack"><span class="label">Resturlaub bei Erfassungsbeginn</span><input v-model.number="form.initial_vacation_remaining_days" type="number" step="any" min="0" class="input" /><span class="muted" style="font-size: 11px">übernommener Reststand im Jahr des Erfassungsbeginns; leer = voller Anspruch</span></div>
          <div class="stack"><span class="label">Geburtstag</span><input v-model="form.birth_date" type="date" class="input" /></div>
          <div class="stack">
            <span class="label">Überstunden-Auszahlung</span>
            <select v-model="form.overtime_payout_mode" class="input">
              <option value="none">Nur ansammeln</option>
              <option value="all">Monatlich alles auszahlen</option>
              <option value="above_cap">Monatlich über Grenze</option>
              <option value="forfeit_above_cap">Über Grenze verfallen lassen</option>
            </select>
          </div>
          <div v-if="otModeMitGrenze.includes(form.overtime_payout_mode)" class="stack">
            <span class="label">Kappungsgrenze (h)</span>
            <input v-model.number="form.overtime_cap_hours" type="number" step="0.5" class="input" />
          </div>
        </div>

        <!-- Mitarbeiter löschen (nur im Bearbeiten-Modus, mit Bestätigung) -->
        <div v-if="editing" class="stack"
          style="gap: 10px; margin-top: 18px; border-top: 1px solid var(--border); padding-top: 14px">
          <div v-if="deleteErr" class="wip-banner">{{ deleteErr }}</div>
          <div v-if="!confirmDelete">
            <button class="btn btn-danger" :disabled="deleting" @click="askDelete">Mitarbeiter löschen</button>
          </div>
          <div v-else class="row" style="gap: 10px; align-items: center; flex-wrap: wrap">
            <span><strong>{{ user?.full_name }}</strong> wirklich löschen? Der Mitarbeiter wird ausgeblendet und kann in den Einstellungen wiederhergestellt oder endgültig gelöscht werden. Für reguläre Austritte bitte ein Austrittsdatum setzen (→ Archiv).</span>
            <button class="btn btn-danger" :disabled="deleting" @click="doDelete">
              {{ deleting ? "Löscht …" : "Ja, löschen" }}
            </button>
            <button class="btn" :disabled="deleting" @click="cancelDelete">Abbrechen</button>
          </div>
        </div>
        </template>
      </div>

      <!-- Lohnbuchungen (nur mit Recht payroll:read, einklappbar) -->
      <div v-if="canSeeBookings" class="card" style="margin-top: 16px">
        <div class="spread sec-head" :style="secOpen.lohn ? 'margin-bottom: 6px' : ''" @click="secOpen.lohn = !secOpen.lohn">
          <h2><span class="sec-chev">{{ secOpen.lohn ? "▾" : "▸" }}</span>Lohnbuchungen</h2>
          <div v-if="secOpen.lohn" class="row" style="gap: 8px; align-items: center" @click.stop>
            <select v-model.number="bookMonthSel" class="input" style="width: auto">
              <option v-for="(mn, i) in bookMonthNames" :key="i" :value="i + 1">{{ mn }}</option>
            </select>
            <select v-model.number="bookYearSel" class="input" style="width: auto">
              <option v-for="y in bookYears" :key="y" :value="y">{{ y }}</option>
            </select>
            <button v-if="canBook && !bookFormOpen" class="btn" @click="openBookForm">+ Buchung</button>
          </div>
        </div>
        <template v-if="secOpen.lohn">
        <div class="muted" style="font-size: 12px; margin-bottom: 14px">
          Manuelle Pauschalen/Beträge/Sonderzahlungen für den Lohn-Export — fließen NICHT in die Arbeitszeit.
        </div>

        <!-- Buchung anlegen (nur mit Berechtigung, erst nach Klick) -->
        <div v-if="canBook && bookFormOpen" class="card" style="background: var(--bg); margin-bottom: 16px">
          <div class="label" style="margin-bottom: 10px">Lohnbuchung eintragen</div>
          <div class="row" style="gap: 14px; flex-wrap: wrap; align-items: flex-end">
            <div class="stack" style="flex: 1; min-width: 220px">
              <span class="label">Lohnart</span>
              <select v-model="bookForm.wage_type_code" class="input">
                <option value="" disabled>Lohnart wählen …</option>
                <option v-for="w in bookWageTypes" :key="w.internal_code" :value="w.internal_code">
                  {{ bookWageLabel(w) }}
                </option>
              </select>
            </div>
            <div class="stack">
              <span class="label">Wert (negativ = Abzug)</span>
              <input v-model="bookForm.value" type="number" step="0.01" class="input" style="width: auto" />
            </div>
            <div class="stack">
              <span class="label">Datum (optional)</span>
              <input v-model="bookForm.date" type="date" class="input" style="width: auto" />
            </div>
            <div class="stack" style="flex: 1; min-width: 180px">
              <span class="label">Beschreibung (optional)</span>
              <input v-model="bookForm.note" class="input" />
            </div>
            <div class="row" style="gap: 8px">
              <button class="btn btn-primary" :disabled="bookBusy" @click="submitBooking">
                {{ bookBusy ? "Speichert …" : "Eintragen" }}
              </button>
              <button class="btn" :disabled="bookBusy" @click="cancelBookForm">Abbrechen</button>
            </div>
          </div>
        </div>

        <div v-if="bookErr" class="wip-banner" style="margin-bottom: 12px">{{ bookErr }}</div>

        <div v-if="!bookings.length" class="empty">Keine Lohnbuchungen in diesem Monat.</div>
        <table v-else class="data">
          <thead>
            <tr>
              <th>Lohnart</th>
              <th>Wert</th>
              <th>Datum</th>
              <th>Beschreibung</th>
              <th v-if="canBook"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="b in bookings" :key="b.id">
              <td>{{ b.description || "—" }} <span class="muted">(DATEV {{ b.datev_lohnart || "—" }})</span></td>
              <td class="num">{{ b.value }} {{ bookUnitLabel(b.unit) }}</td>
              <td class="num">{{ fmtDate(b.booking_date) }}</td>
              <td>{{ b.note || "—" }}</td>
              <td v-if="canBook" style="text-align: right">
                <template v-if="bookRemoveConfirmId === b.id">
                  <span class="row" style="gap: 8px; justify-content: flex-end; align-items: center">
                    <span class="muted" style="font-size: 12px">Wirklich?</span>
                    <button class="linkbtn danger" :disabled="bookBusy" @click="removeBooking(b.id)">Ja</button>
                    <button class="linkbtn" :disabled="bookBusy" @click="cancelRemoveBooking">Abbrechen</button>
                  </span>
                </template>
                <button v-else class="linkbtn danger" @click="askRemoveBooking(b.id)">Entfernen</button>
              </td>
            </tr>
          </tbody>
        </table>
        </template>
      </div>
    </template>

    <!-- Monats-Stundenzettel (Monat folgt dem Kalender-Cursor) -->
    <StundenzettelSheet
      v-if="canViewStundenzettel"
      :open="sheetOpen"
      :employee-id="id"
      :year="cursor.getFullYear()"
      :month="cursor.getMonth() + 1"
      @close="sheetOpen = false"
    />
  </div>
</template>

<style scoped>
.night-confirm {
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid var(--feiertag-text);
  background: var(--feiertag-bg);
  border-radius: 8px;
}
.cal-cell {
  position: relative;
  min-height: 62px;
  border: 1px solid var(--divider);
  border-radius: 8px;
  padding: 5px 6px;
  cursor: pointer;
  background: #fff;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.cal-cell:hover { background: var(--bg); }
/* Geburtstags-Kuchen: dezent unten rechts in der Kachel. */
.cal-birthday {
  position: absolute;
  bottom: 4px;
  right: 5px;
  display: flex;
  align-items: center;
  gap: 2px;
  line-height: 0;
  color: #e0669b;
  pointer-events: none;
}
.cal-birthday-age { font-size: 10px; font-weight: 700; line-height: 1; }
/* Firmenjubiläum: Medaille dezent unten links (gegenüber vom Geburtstag). */
/* Firmenjubiläum: Pokal + Jahreszahl unten rechts. */
.cal-anniversary {
  position: absolute;
  bottom: 4px;
  right: 5px;
  display: flex;
  align-items: center;
  gap: 2px;
  line-height: 0;
  color: #c79a2e;
  pointer-events: none;
}
.cal-anniversary-yrs { font-size: 10px; font-weight: 700; line-height: 1; }
.cal-out { opacity: .4; pointer-events: none; }
/* Wochenende: grauer Hintergrund bleibt, aber Schrift dunkel (nicht grau). */
.cal-weekend { background: var(--divider); color: var(--text); }
.cal-weekend .num { color: var(--text); }
/* Überstunden-Verlauf: Δ-Vorzeichen färben, Korrekturzeilen leicht absetzen. */
.ot-pos { color: var(--success, #128a4b); }
.ot-neg { color: var(--danger, #c0362c); }
.ot-adj-row { background: var(--bg); }
.ot-month-row { background: var(--bg); font-weight: 600; }
.ot-carry-row td { color: var(--muted, #888); font-style: italic; }
/* Antragsformular: alle Bedien-Elemente gleiche Höhe (Typ-Select = Datum-Box). */
.absform-row .input,
.absform-row .btn { height: 40px; box-sizing: border-box; }
/* Filter „Vergangene Abwesenheiten": Select + Datumsfelder auf eine (Button-)Höhe. */
.absfilter-row .input { height: 40px; box-sizing: border-box; }
/* Tages-Soll pro Wochentag: kompakte Zeilen Mo–So mit Std:Min. */
/* Einklappbare Bereichs-Header: ganze Zeile klickbar, Chevron vor dem Titel. */
.sec-head { cursor: pointer; user-select: none; }
.sec-chev { display: inline-block; width: 20px; color: var(--text-3); font-size: 14px; }
.wd-grid { display: grid; grid-template-columns: repeat(2, minmax(150px, 1fr)); gap: 8px 18px; margin-top: 8px; }
.wd-day { display: flex; align-items: center; gap: 6px; }
.wd-num { width: 56px; text-align: right; }
.cal-future { color: var(--text-3); }
.cal-holiday { background: var(--feiertag-bg); color: var(--feiertag-text); }
/* Urlaub blau (statt grün) — Grün ist im Team-Kalender die Anwesenheit,
   die beiden Ansichten sollen dieselbe Sprache sprechen (2026-07-22).
   Heller als im Team-Raster, weil diese Zellen beschriftet sind. */
.cal-vacation { background: #e3eefb; border-color: #b7d3ee; }
/* Halbtags-Urlaub: nur die rechte Hälfte blau, links bleibt Platz für die Arbeitszeit. */
.cal-vac-half { background: linear-gradient(to right, transparent 0 50%, #e3eefb 50% 100%); border-color: #b7d3ee; }
.cal-sick { background: var(--red-bg); border-color: #f0c8c6; color: var(--red-text); }
/* Sonderurlaub (zählt wie Arbeit): Türkis; Überstundenabbau: Violett (Spec 2026-08-11). */
.cal-special { background: #daf2ee; border-color: #a9ded4; }
.cal-abbau { background: #ece4f9; border-color: #cbb8ec; }
/* Unbezahlt (entschuldigt, 0 Std): kraeftigeres Blau — klar von Urlaub
   (hellblau) unterscheidbar, aber erkennbar dieselbe Farbfamilie. */
.cal-unbezahlt { background: #b9cfea; border-color: #7ba7d4; }
/* Unentschuldigtes Fehlen: rot schraffiert absetzen. */
.cal-absent { background: var(--red-bg); border-color: #e6a9a5; }
.cal-absent-tag { color: var(--red-text, #c0362c); }
.cal-sel { outline: 2px solid var(--accent); }
.cal-tag { font-size: 11px; font-weight: 600; }
/* Art der Krankmeldung (AU/LFZ) hinter "Krank" — dezent, damit die Zelle lesbar bleibt. */
.cal-sick-status { font-size: 9px; font-weight: 500; }
.src-tag { font-size: 12px; color: var(--text-3); }
/* Kiosk-Stempel deutlich hervorheben (Terminal-Zeiten). */
.src-kiosk {
  color: var(--accent);
  font-weight: 700;
  font-size: 11px;
  border: 1px solid var(--accent);
  border-radius: 999px;
  padding: 1px 8px;
}

</style>
