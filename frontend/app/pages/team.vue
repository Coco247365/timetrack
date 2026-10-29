<script setup lang="ts">
// Teamkalender als Monatsmatrix. Datenquelle ist der schlanke Endpunkt
//   GET /absences/team-calendar (MA-Namen + Abwesenheiten, rechte-gescoped:
//       calendar:view_all = ganze Firma, calendar:view_team = eigener Bereich)
// plus optional:
//   GET /clock/presence      (Zähler heute — nicht jede Rolle darf das)
//   GET /holidays?year=YYYY  (Feiertage)
const { apiFetch } = useApi();
const { canViewOrgChart, featureEnabled } = useAuth();

interface Employee {
  id: string;
  name: string;
  department_name?: string | null;
  company_name?: string | null;
  birthday_md?: string | null; // "MM-DD"
  birth_year?: number | null; // Geburtsjahr (fürs Alter)
  hire_md?: string | null; // "MM-DD" des Firmeneintritts (Jubiläum)
  hire_year?: number | null; // Eintrittsjahr (für "X Jahre")
  // nfc | auto | web — "auto" markiert Zeiten im Tooltip als "(auto)".
  // Kommt nur mit, wenn die Rolle Arbeitszeiten sehen darf.
  time_capture?: string | null;
}
interface Absence {
  employee_id: string;
  kind: "urlaub" | "krank" | "abwesend" | "sonderurlaub" | "abbau" | "unbezahlt"; // Krank ohne medical:read = "abwesend"
  date_from: string;
  date_to: string;
  status: string;
}
interface PresenceEmp {
  employee_id?: string;
  id?: string;
  status: string;
  clocked_in_since?: string | null;
}
interface Presence {
  present_count?: number;
  vacation_count?: number;
  sick_leave_count?: number;
  total_employees?: number;
  employees?: PresenceEmp[];
}

const employees = ref<Employee[]>([]);
const presence = ref<Presence>({});
const holidays = ref<Record<string, string>>({});
// Zellen-Index: `${employee_id}|${YYYY-MM-DD}` -> Abwesenheitsart
const cellMap = ref<Record<string, "urlaub" | "krank" | "abwesend" | "sonderurlaub" | "abbau" | "unbezahlt">>({});
// Vergangene Arbeitstage (Feature team_past_worked): Set aus `${id}|${date}`.
const workedSet = ref<Set<string>>(new Set());
// Beginn/Ende je Arbeitstag (gleicher Schlüssel) für "gearbeitet von … bis …".
// Leer, wenn die Rolle keine Arbeitszeiten sehen darf — dann bleibt es bei
// "gearbeitet" ohne Zeiten.
const workedTimes = ref<Record<string, { start?: string | null; end?: string | null }>>({});
// Tage mit vergessenem Ausstempeln, die HR noch nicht aufgeloest hat
// (Spec 2026-08-03) — Set aus `${id}|${date}`. Nur fuer Rollen befuellt, die
// Arbeitszeiten sehen duerfen (Backend liefert sonst eine leere Liste).
const flaggedSet = ref<Set<string>>(new Set());
function isFlagged(emp: Employee, day: DayCell): boolean {
  return flaggedSet.value.has(`${emp.id}|${day.key}`);
}
// Unentschuldigte Fehltage (rotes „U") — gleiche Regel wie im
// Mitarbeiter-Verlauf; ebenfalls nur fuer Rollen mit Arbeitszeit-Recht.
const unexcusedSet = ref<Set<string>>(new Set());
// Ein-/ausblendbar: solange nicht alle Fehlzeiten nachgetragen sind, wuerde das
// rote „U" den Kalender fluten. Standard AUS; die Wahl bleibt pro Browser
// gespeichert. Ist es aus, berechnet auch das Backend die Fehltage nicht.
const showUnexcused = ref(false);
function isUnexcused(emp: Employee, day: DayCell): boolean {
  return showUnexcused.value && unexcusedSet.value.has(`${emp.id}|${day.key}`);
}
onMounted(() => {
  showUnexcused.value = localStorage.getItem("team_show_unexcused") === "1";
});
watch(showUnexcused, (v) => {
  localStorage.setItem("team_show_unexcused", v ? "1" : "0");
  loadAbsences();
});

const loading = ref(true);
const loadError = ref("");
const icsBusy = ref(false);
async function downloadIcs() {
  icsBusy.value = true;
  try {
    const blob = await apiFetch<Blob>("/absences/calendar.ics", { responseType: "blob" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "abwesenheiten.ics";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch { /* Fehler still — Rechte fehlen o.ä. */ }
  icsBusy.value = false;
}
const absencesError = ref("");

const department = ref("Alle Bereiche");
const company = ref("Alle Firmen");

function pad(n: number): string {
  return String(n).padStart(2, "0");
}
function ymd(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Rollendes Zeitfenster statt Monatsansicht (User-Entscheid 2026-07-22):
// „heute" ist der Anker und beim Öffnen zentriert; das Fenster reicht
// ±WINDOW_DAYS um den Anker, die Pfeile verschieben um SHIFT_DAYS.
const today = new Date();
today.setHours(0, 0, 0, 0);
const todayKey = ymd(today);
const WINDOW_DAYS = 21;
const SHIFT_DAYS = 14;
const anchor = ref(new Date(today));

const MONTHS = [
  "Januar", "Februar", "März", "April", "Mai", "Juni",
  "Juli", "August", "September", "Oktober", "November", "Dezember",
];
const WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];

// Label folgt beim Scrollen der Spalte in der Bildschirmmitte (Scroll-
// Handler unten); Fallback ist der Anker-Monat.
const centerKey = ref("");
const monthLabel = computed(() => {
  const key = centerKey.value || ymd(anchor.value);
  const d = new Date(key + "T00:00:00");
  return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
});

// Tage des angezeigten Monats inkl. Metadaten.
interface DayCell {
  key: string;       // YYYY-MM-DD
  day: number;       // Tagnummer
  weekday: string;   // Mo–So
  weekend: boolean;
  holiday: string;   // Feiertagsname oder ""
  isToday: boolean;
  inMonth: boolean;  // gehört zum gewählten Monat (sonst gedimmter Nachbarmonat)
  monthShort: string; // gesetzt am 1. eines Monats → Monatslabel im Kopf
}
const days = computed<DayCell[]>(() => {
  const start = new Date(anchor.value);
  start.setDate(start.getDate() - WINDOW_DAYS);
  const end = new Date(anchor.value);
  end.setDate(end.getDate() + WINDOW_DAYS);
  const out: DayCell[] = [];
  const cur = new Date(start);
  while (cur <= end) {
    const key = ymd(cur);
    // getDay(): 0=So..6=Sa  ->  Index in WEEKDAYS (Mo=0)
    const wd = (cur.getDay() + 6) % 7;
    out.push({
      key,
      day: cur.getDate(),
      weekday: WEEKDAYS[wd],
      weekend: wd >= 5,
      holiday: holidays.value[key] || "",
      isToday: key === todayKey,
      inMonth: true, // rollendes Fenster: kein "Nachbarmonat"-Dimmen mehr
      monthShort: cur.getDate() === 1 ? MONTHS[cur.getMonth()].slice(0, 3) : "",
    });
    cur.setDate(cur.getDate() + 1);
  }
  return out;
});

// Bereichsliste passend zur gewählten Firma filtern (kein Firmenfilter aktiv = alle).
const departments = computed(() => {
  const set = new Set<string>();
  for (const e of employees.value) {
    if (company.value !== "Alle Firmen" && e.company_name !== company.value) continue;
    if (e.department_name) set.add(e.department_name);
  }
  return ["Alle Bereiche", ...[...set].sort()];
});

const companies = computed(() => {
  const set = new Set<string>();
  for (const e of employees.value) if (e.company_name) set.add(e.company_name);
  return ["Alle Firmen", ...[...set].sort()];
});

// Sortierung umschaltbar (User-Wunsch 11.08.): nach BEREICH (Kollegen eines
// Bereichs stehen zusammen, innerhalb weiter nach Name) oder nach NAME über
// alle hinweg. Die Wahl bleibt pro Browser gespeichert.
const sortMode = ref<"bereich" | "name">("bereich");
// Richtung je Spalte: erneuter Klick auf denselben Kopf dreht sie um.
const sortDir = ref<1 | -1>(1);
onMounted(() => {
  const v = localStorage.getItem("team_sort_mode");
  if (v === "name" || v === "bereich") sortMode.value = v;
  sortDir.value = localStorage.getItem("team_sort_dir") === "-1" ? -1 : 1;
});
watch(sortMode, (v) => localStorage.setItem("team_sort_mode", v));
watch(sortDir, (v) => localStorage.setItem("team_sort_dir", String(v)));

function sortByColumn(mode: "bereich" | "name") {
  if (sortMode.value === mode) sortDir.value = sortDir.value === 1 ? -1 : 1;
  else {
    sortMode.value = mode;
    sortDir.value = 1;
  }
}
// Pfeil nur an der aktiven Spalte; ▲ aufsteigend, ▼ absteigend.
function sortArrow(mode: "bereich" | "name"): string {
  if (sortMode.value !== mode) return "";
  return sortDir.value === 1 ? "▲" : "▼";
}

const filteredEmployees = computed(() =>
  employees.value
    .filter(
      (e) =>
        (department.value === "Alle Bereiche" || e.department_name === department.value) &&
        (company.value === "Alle Firmen" || e.company_name === company.value)
    )
    .slice()
    .sort((a, b) => {
      const dir = sortDir.value;
      if (sortMode.value === "name") return dir * a.name.localeCompare(b.name, "de");
      const da = a.department_name || "";
      const db = b.department_name || "";
      if (da === db) return 0; // stabile Sortierung -> API-Reihenfolge (Nachname) bleibt
      if (!da) return 1; // ohne Bereich immer ans Ende, unabhaengig von der Richtung
      if (!db) return -1;
      return dir * da.localeCompare(db, "de");
    })
);

// Firmenwechsel: gewählten Bereich zurücksetzen, wenn er zur neuen Firma nicht mehr passt.
watch(company, () => {
  if (!departments.value.includes(department.value)) department.value = "Alle Bereiche";
});

// Expandiert eine Abwesenheit auf Einzeltage und füllt die Map.
function expand(a: Absence, map: Record<string, "urlaub" | "krank" | "abwesend" | "sonderurlaub" | "abbau" | "unbezahlt">) {
  const kind = a.kind;
  if (!kind) return;
  const cur = new Date(a.date_from + "T00:00:00");
  const end = new Date(a.date_to + "T00:00:00");
  // Defensive Schranke gegen kaputte Datumsangaben.
  let guard = 0;
  while (cur <= end && guard < 400) {
    map[`${a.employee_id}|${ymd(cur)}`] = kind;
    cur.setDate(cur.getDate() + 1);
    guard++;
  }
}

// Wochenend-Brücke: endet ein Block am Freitag und geht am Montag mit
// gleicher Art weiter (z. B. zwei getrennte Anträge Mo–Fr + Mo–Fr), werden
// Sa/So mitmarkiert — rein visuell, zählt in keine Berechnung. Damit sieht
// es identisch aus wie EIN Antrag, der Fr–Mo übers Wochenende läuft.
function bridgeWeekends(map: Record<string, "urlaub" | "krank" | "abwesend" | "sonderurlaub" | "abbau" | "unbezahlt">) {
  for (const key of Object.keys(map)) {
    const sep = key.lastIndexOf("|");
    const emp = key.slice(0, sep);
    const d = new Date(key.slice(sep + 1) + "T00:00:00");
    if (d.getDay() !== 5) continue; // nur Freitage als Brückenkopf
    const at = (n: number) => {
      const x = new Date(d);
      x.setDate(d.getDate() + n);
      return `${emp}|${ymd(x)}`;
    };
    if (map[at(3)] === map[key]) {
      map[at(1)] ??= map[key];
      map[at(2)] ??= map[key];
    }
  }
}

// Heute eingestempelte Mitarbeiter (inkl. Pause): id -> "seit HH:MM".
// Wer gerade in Pause ist -> gelbe Zelle statt gruen (User-Wunsch 14.08.).
// Eigene Ableitung statt Seiteneffekt in presentSince.
const onBreakSet = computed<Set<string>>(() => {
  const s = new Set<string>();
  for (const e of presence.value.employees || []) {
    const key = e.employee_id || e.id;
    if (key && e.status === "on_break") s.add(key);
  }
  return s;
});

const presentSince = computed<Record<string, string>>(() => {
  const m: Record<string, string> = {};
  for (const e of presence.value.employees || []) {
    const key = e.employee_id || e.id;
    if (!key || !["clocked_in", "on_break"].includes(e.status)) continue;
    let since = "";
    if (e.clocked_in_since) {
      const d = new Date(e.clocked_in_since);
      if (!isNaN(d.getTime())) since = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
    m[key] = since;
  }
  return m;
});

// "(auto)" hinter der Zeit: die Zeit stammt aus der automatischen Erfassung,
// nicht aus echten Stempelungen (gleiche Kennzeichnung wie in der Liste, #237).
function autoMarker(emp: Employee): string {
  return emp.time_capture === "auto" ? " (auto)" : "";
}
// " von HH:MM bis HH:MM" für einen erfassten Arbeitstag; leer, wenn keine
// Zeiten mitkamen (Rolle ohne Arbeitszeit-Recht) oder der Tag offen ist.
function vonBis(emp: Employee, day: DayCell): string {
  const t = workedTimes.value[`${emp.id}|${day.key}`];
  const uhr = (v?: string | null) => {
    if (!v) return "";
    const d = new Date(v);
    return isNaN(d.getTime()) ? "" : `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const von = uhr(t?.start);
  const bis = uhr(t?.end);
  if (von && bis) return ` von ${von} bis ${bis}`;
  if (von) return ` seit ${von}`;
  return "";
}

// Zelltyp für MA+Tag (Priorität: Anwesend/gearbeitet > Wochenende >
// Krank/Urlaub > Feiertag). Anwesend zeigt den Live-Zustand — wer trotz
// eingetragener Abwesenheit stempelt, ist real da. Das Wochenend-Grau steht
// bewusst VOR Urlaub/Krank/Feiertag: Sa+So sollen als durchgehend ruhige
// Spalten lesbar bleiben (User-Wunsch 29.07.); nur echte Arbeit sticht durch.
function cellOf(emp: Employee, day: DayCell): { cls: string; title: string } | null {
  if (day.isToday && emp.id in presentSince.value) {
    const since = presentSince.value[emp.id];
    return {
      cls: onBreakSet.value.has(emp.id) ? "c-pause" : "c-anwesend",
      title: `${emp.name} · Anwesend${since ? ` seit ${since}` : ""}${autoMarker(emp)}`,
    };
  }
  // Vergangener Tag mit erfasster Arbeitszeit: grün wie die Live-Anwesenheit
  // (Feature team_past_worked, #187).
  if (workedSet.value.has(`${emp.id}|${day.key}`)) {
    return {
      cls: "c-anwesend",
      title: `${emp.name} · gearbeitet${vonBis(emp, day)}${autoMarker(emp)}`,
    };
  }
  const abs = cellMap.value[`${emp.id}|${day.key}`];
  const ABS_LABELS: Record<string, string> = {
    krank: "Krank", abwesend: "Abwesend", urlaub: "Urlaub",
    sonderurlaub: "Sonderurlaub", abbau: "Überstundenabbau",
    unbezahlt: "Unbezahlt",
  };
  const absLabel = abs ? ABS_LABELS[abs] || "" : "";
  // Wochenende grau — der Eintrag bleibt im Tooltip erhalten, damit z. B. ein
  // über das Wochenende laufender Urlaub nachvollziehbar ist.
  if (day.weekend) {
    const extra = absLabel || day.holiday || "";
    return { cls: "c-weekend", title: extra ? `Wochenende · ${extra}` : "Wochenende" };
  }
  if (absLabel) {
    const ABS_CLS: Record<string, string> = {
      urlaub: "c-urlaub", krank: "c-krank", abwesend: "c-krank",
      sonderurlaub: "c-sonder", abbau: "c-abbau", unbezahlt: "c-unbezahlt",
    };
    return { cls: ABS_CLS[abs!] || "c-krank", title: `${emp.name} · ${absLabel}` };
  }
  if (day.holiday) return { cls: "c-feiertag", title: day.holiday };
  return null;
}

// Klick auf eine Tageszelle: den persönlichen Kalender dieses Mitarbeiters
// mit genau diesem Tag ausgewählt öffnen (`?tag=` liest `mitarbeiter/[id]`).
function tagOeffnen(emp: Employee, day: DayCell) {
  navigateTo(`/mitarbeiter/${emp.id}?tag=${day.key}`);
}

// Titel der Zelle: Abwesenheitsgrund, sonst Name + Datum. Ohne Klick-Hinweis —
// dass eine Zelle anklickbar ist, zeigt schon der Mauszeiger.
function zellenTitel(emp: Employee, day: DayCell): string {
  const grund = cellOf(emp, day)?.title;
  const datum = day.key.split("-").reverse().join(".");
  return grund ?? `${emp.name} · ${datum}`;
}

// Hat dieser MA an diesem Tag Geburtstag? MM-DD vergleichen (Jahr egal).
function isBirthday(emp: Employee, day: DayCell): boolean {
  return !!emp.birthday_md && emp.birthday_md === day.key.slice(5);
}
// Alter am Geburtstag (0 = unbekannt, wenn kein Geburtsjahr).
function birthdayAge(emp: Employee, day: DayCell): number {
  if (!isBirthday(emp, day) || !emp.birth_year) return 0;
  return Number(day.key.slice(0, 4)) - emp.birth_year;
}
// Firmenjubiläum an diesem Tag? Gibt die Jahre der Betriebszugehörigkeit zurück (0 = keins).
function anniversaryYears(emp: Employee, day: DayCell): number {
  if (!emp.hire_md || !emp.hire_year || emp.hire_md !== day.key.slice(5)) return 0;
  const y = Number(day.key.slice(0, 4));
  return y > emp.hire_year ? y - emp.hire_year : 0;
}

async function loadAbsences() {
  absencesError.value = "";
  const f = new Date(anchor.value);
  f.setDate(f.getDate() - WINDOW_DAYS);
  const t = new Date(anchor.value);
  t.setDate(t.getDate() + WINDOW_DAYS);
  const from = ymd(f);
  const to = ymd(t);
  try {
    const data = await apiFetch<{
      employees: Employee[];
      absences: Absence[];
      worked?: { employee_id: string; date: string; start?: string | null; end?: string | null }[];
      flagged?: { employee_id: string; date: string }[];
      unexcused?: { employee_id: string; date: string }[];
    }>(
      `/absences/team-calendar?date_from=${from}&date_to=${to}` +
        (showUnexcused.value ? "&include_unexcused=true" : "")
    );
    employees.value = data.employees || [];
    const map: Record<string, "urlaub" | "krank" | "abwesend" | "sonderurlaub" | "abbau" | "unbezahlt"> = {};
    for (const a of data.absences || []) expand(a, map);
    bridgeWeekends(map);
    cellMap.value = map;
    workedSet.value = new Set((data.worked || []).map((w) => `${w.employee_id}|${w.date}`));
    workedTimes.value = Object.fromEntries(
      (data.worked || []).map((w) => [`${w.employee_id}|${w.date}`, { start: w.start, end: w.end }])
    );
    flaggedSet.value = new Set((data.flagged || []).map((f) => `${f.employee_id}|${f.date}`));
    unexcusedSet.value = new Set((data.unexcused || []).map((u) => `${u.employee_id}|${u.date}`));
  } catch (e: any) {
    cellMap.value = {};
    flaggedSet.value = new Set();
    unexcusedSet.value = new Set();
    absencesError.value =
      e?.data?.detail || "Abwesenheiten konnten nicht geladen werden.";
  }
}

async function loadHolidays() {
  // Fenster kann eine Jahresgrenze überspannen -> beide Jahre laden.
  const y1 = new Date(anchor.value.getTime() - WINDOW_DAYS * 86400000).getFullYear();
  const y2 = new Date(anchor.value.getTime() + WINDOW_DAYS * 86400000).getFullYear();
  try {
    const parts = await Promise.all(
      [...new Set([y1, y2])].map((y) =>
        apiFetch<Record<string, string>>(`/holidays?year=${y}`)
      )
    );
    holidays.value = Object.assign({}, ...parts);
  } catch {
    holidays.value = {}; // Feiertage optional
  }
}

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    presence.value = await apiFetch<Presence>("/clock/presence");
  } catch { /* Zähler optional — nicht jede Rolle darf Präsenz sehen */ }
  await Promise.all([loadHolidays(), loadAbsences()]);
  if (!employees.value.length && absencesError.value) {
    loadError.value = absencesError.value;
  }
  loading.value = false;
  // Anker-Spalte (beim Start: heute) mittig ins Sichtfeld rücken.
  await nextTick();
  centerDate(ymd(anchor.value));
}
onMounted(load);

function centerDate(key: string) {
  document
    .querySelector(`.day-head[data-key="${key}"]`)
    ?.scrollIntoView({ inline: "center", block: "nearest", behavior: "instant" as ScrollBehavior });
}

// Fenster verschieben: Anker ±SHIFT_DAYS, Daten nachladen, Anker zentrieren.
async function shiftWindow(direction: number) {
  const next = new Date(anchor.value);
  next.setDate(next.getDate() + direction * SHIFT_DAYS);
  anchor.value = next;
  await Promise.all([loadAbsences(), loadHolidays()]);
  await nextTick();
  centerDate(ymd(anchor.value));
  // Label sofort auf den neuen Anker ziehen: bleibt die Scroll-Position beim
  // programmatischen Zentrieren gleich, feuert KEIN Scroll-Event — centerKey
  // hinge sonst am alten Monat fest (Bug: Label blieb "Juni 2026").
  centerKey.value = ymd(anchor.value);
}

// Zurück zu heute (Anker + Zentrierung).
async function goToday() {
  anchor.value = new Date(today);
  await Promise.all([loadAbsences(), loadHolidays()]);
  await nextTick();
  centerDate(todayKey);
  centerKey.value = todayKey;
}

// Beim Scrollen die Spalte in der Bildschirmmitte bestimmen -> Monatslabel.
let scrollRaf = 0;
function onMatrixScroll(ev: Event) {
  if (scrollRaf) return;
  scrollRaf = requestAnimationFrame(() => {
    scrollRaf = 0;
    const scroller = ev.target as HTMLElement;
    const mid = scroller.getBoundingClientRect().left + scroller.clientWidth / 2;
    let best: { d: number; key: string } | null = null;
    for (const el of scroller.querySelectorAll<HTMLElement>(".day-head[data-key]")) {
      const r = el.getBoundingClientRect();
      const d = Math.abs((r.left + r.right) / 2 - mid);
      if (!best || d < best.d) best = { d, key: el.dataset.key || "" };
    }
    if (best?.key) centerKey.value = best.key;
  });
}
</script>

<template>
  <div>
    <div class="spread" style="margin-bottom: 18px">
      <div class="row" style="gap: 18px; align-items: baseline">
        <h1>Team</h1>
        <NuxtLink v-if="canViewOrgChart && featureEnabled('org_chart')" to="/organigramm"
          class="linkbtn" style="font-size: 15px">Organigramm ›</NuxtLink>
      </div>
      <div class="row" style="gap: 8px">
        <span class="badge green">
          <span class="dot" />{{ presence.present_count ?? 0 }} anwesend
        </span>
        <span class="badge blue">
          <span class="dot" />{{ presence.vacation_count ?? 0 }} im Urlaub
        </span>
        <span class="badge red">
          <span class="dot" />{{ presence.sick_leave_count ?? 0 }} krank
        </span>
      </div>
    </div>

    <!-- Kompakte Steuerleiste: schmale Paddings, damit sie nicht unnötig viel
         vertikalen Platz vor dem Kalender wegnimmt. -->
    <div class="card" style="padding: 6px 10px; margin-bottom: 10px">
      <div class="spread" style="flex-wrap: wrap; gap: 6px">
        <div class="row" style="gap: 6px">
          <button class="btn" style="padding: 3px 9px" title="2 Wochen zurück" @click="shiftWindow(-1)">‹</button>
          <span class="num" style="font-weight: 650; min-width: 150px; text-align: center">
            {{ monthLabel }}
          </span>
          <button class="btn" style="padding: 3px 9px" title="2 Wochen vor" @click="shiftWindow(1)">›</button>
          <button class="btn" style="padding: 3px 9px" @click="goToday">Heute</button>
        </div>
        <div class="row" style="gap: 6px">
          <label class="row" style="gap: 5px; font-size: 12px; cursor: pointer"
            title="Rotes U bei unentschuldigtem Fehlen anzeigen. Solange nicht alle Fehlzeiten nachgetragen sind, kann das den Kalender überfüllen.">
            <input v-model="showUnexcused" type="checkbox" style="cursor: pointer" />
            <span class="muted">Unentschuldigt</span>
          </label>
          <!-- Firmen-Filter nur zeigen, wenn es mehrere Firmen gibt. -->
          <select v-if="companies.length > 2" v-model="company" class="input" style="width: auto; padding: 3px 8px">
            <option v-for="c in companies" :key="c">{{ c }}</option>
          </select>
          <select v-model="department" class="input" style="width: auto; padding: 3px 8px">
            <option v-for="d in departments" :key="d">{{ d }}</option>
          </select>
        </div>
      </div>
    </div>

    <div class="card" style="padding: 0">
      <div v-if="loadError" class="wip-banner" style="margin: 14px">{{ loadError }}</div>
      <div v-else-if="loading" class="empty">Lädt …</div>
      <div v-else-if="!filteredEmployees.length" class="empty">
        Keine Mitarbeiter gefunden.
      </div>
      <template v-else>
        <div v-if="absencesError" class="wip-banner" style="margin: 14px">
          {{ absencesError }}
        </div>
        <div class="matrix-scroll" @scroll="onMatrixScroll">
          <table class="matrix">
            <thead>
              <tr>
                <th class="sticky-col head-corner th-sort" :class="{ 'is-sorted': sortMode === 'name' }"
                  title="Nach Name sortieren (nochmal klicken kehrt um)" @click="sortByColumn('name')">
                  Mitarbeiter<span class="sort-arrow">{{ sortArrow("name") }}</span>
                </th>
                <th class="sticky-col sticky-col-2 head-corner th-sort" :class="{ 'is-sorted': sortMode === 'bereich' }"
                  title="Nach Bereich sortieren (nochmal klicken kehrt um)" @click="sortByColumn('bereich')">
                  Bereich<span class="sort-arrow">{{ sortArrow("bereich") }}</span>
                </th>
                <th
                  v-for="d in days"
                  :key="d.key"
                  :data-key="d.key"
                  class="day-head"
                  :class="{ 'is-weekend': d.weekend, 'is-holiday': !!d.holiday, 'other-month': !d.inMonth, 'month-start': d.day === 1, 'is-today': d.isToday }"
                  :title="d.holiday || undefined"
                >
                  <div v-if="d.monthShort" class="mon">{{ d.monthShort }}</div>
                  <div class="wd">{{ d.weekday }}</div>
                  <div class="dn" :class="{ 'dn-today': d.isToday }">{{ d.day }}</div>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="e in filteredEmployees" :key="e.id">
                <th class="sticky-col emp-cell">
                  <NuxtLink :to="`/mitarbeiter/${e.id}`" class="emp-link">
                    <div class="emp-name">{{ e.name }}</div>
                  </NuxtLink>
                </th>
                <th class="sticky-col sticky-col-2 emp-cell">
                  <span class="emp-dep muted">{{ e.department_name || "—" }}</span>
                </th>
                <td
                  v-for="d in days"
                  :key="d.key"
                  class="cell"
                  :class="[cellOf(e, d)?.cls, { 'other-month': !d.inMonth, 'month-start': d.day === 1, 'is-today': d.isToday }]"
                  :title="zellenTitel(e, d)"
                  role="link"
                  tabindex="0"
                  @click="tagOeffnen(e, d)"
                  @keydown.enter.prevent="tagOeffnen(e, d)"
                  @keydown.space.prevent="tagOeffnen(e, d)"
                >
                  <span
                    v-if="isBirthday(e, d)"
                    class="birthday"
                    :title="birthdayAge(e, d) ? `${e.name} · Geburtstag (${birthdayAge(e, d)})` : `${e.name} · Geburtstag`"
                  ><BirthdayIcon /><span v-if="birthdayAge(e, d)" class="bd-age">{{ birthdayAge(e, d) }}</span></span>
                  <span
                    v-if="isFlagged(e, d)"
                    class="flagwarn"
                    :title="`${e.name} · Ausstempeln vergessen — noch nicht korrigiert`"
                  >⚠</span>
                  <span
                    v-else-if="isUnexcused(e, d)"
                    class="unexcused-u"
                    :title="`${e.name} · Unentschuldigt gefehlt`"
                  >U</span>
                  <span
                    v-if="anniversaryYears(e, d)"
                    class="anniversary"
                    :title="`${e.name} · Firmenjubiläum (${anniversaryYears(e, d)} ${anniversaryYears(e, d) === 1 ? 'Jahr' : 'Jahre'})`"
                  ><AnniversaryIcon /><span class="ann-yrs">{{ anniversaryYears(e, d) }}</span></span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="spread" style="align-items: center">
          <div class="legend">
            <span class="lg"><i class="sw c-anwesend" />Heute anwesend</span>
            <span class="lg"><i class="sw c-pause" />In Pause</span>
            <span class="lg"><i class="sw c-urlaub" />Urlaub</span>
            <span class="lg"><i class="sw c-krank" />Krank</span>
            <span class="lg"><i class="sw c-sonder" />Sonderurlaub</span>
            <span class="lg"><i class="sw c-abbau" />Überstundenabbau</span>
            <span class="lg"><i class="sw c-unbezahlt" />Unbezahlt</span>
            <span class="lg"><i class="sw c-feiertag" />Feiertag</span>
            <span class="lg"><i class="sw c-weekend" />Wochenende</span>
            <span class="lg"><BirthdayIcon class="cake-ic" /> Geburtstag</span>
            <span class="lg"><AnniversaryIcon class="ann-ic" /> Firmenjubiläum</span>
            <span v-if="flaggedSet.size" class="lg"><span class="flagwarn-lg">⚠</span> Ausstempeln vergessen</span>
            <span v-if="unexcusedSet.size" class="lg"><span class="unexcused-u-lg">U</span> Unentschuldigt</span>
          </div>
          <button class="ics-btn" :disabled="icsBusy"
            title="Lädt die genehmigten Abwesenheiten (letzter Monat bis ein Jahr voraus) als Kalenderdatei herunter. Per Doppelklick in Outlook oder Apple Kalender importieren — Krank erscheint ohne Berechtigung nur als Abwesend. Für einen sich selbst aktualisierenden Kalender: Abo-Link in Einstellungen → Funktionen."
            @click="downloadIcs">{{ icsBusy ? "Erstellt …" : "Kalender exportieren (.ics)" }}</button>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.ics-btn {
  padding: 5px 12px;
  font-size: 12.5px;
  white-space: nowrap;
  color: var(--muted, #6b7280);
  background: transparent;
  border: 1px solid var(--divider, #e5e7eb);
  border-radius: 8px;
  cursor: pointer;
  box-shadow: none;
}
.ics-btn:hover {
  color: var(--text, #111827);
  border-color: var(--muted, #9ca3af);
}
.ics-btn:disabled {
  opacity: 0.5;
  cursor: default;
}

.matrix-scroll {
  overflow: auto;
  -webkit-overflow-scrolling: touch;
  /* Eigener Scroll-Bereich, damit die Kopfzeile (sticky top) beim vertikalen
     Scrollen stehen bleibt (#192). */
  max-height: calc(100vh - 210px);
}
.matrix {
  border-collapse: separate;
  border-spacing: 0;
  width: max-content;
  min-width: 100%;
}
.matrix th,
.matrix td {
  border-bottom: 1px solid var(--divider);
  border-right: 1px solid var(--divider);
}

/* Sticky Spalten links: Name (breit genug fuer "Sebastian Weidenbecher"
   in EINER Zeile) + Bereich als eigene Spalte (User-Wunsch 29.07.). */
.sticky-col {
  position: sticky;
  left: 0;
  z-index: 2;
  background: var(--card);
  text-align: left;
  min-width: 210px;
  max-width: 210px;
}
.sticky-col-2 {
  left: 210px;
  min-width: 110px;
  max-width: 110px;
  border-right: 2px solid var(--divider);
}
.head-corner {
  /* Ecke oben-links: bleibt sowohl beim horizontalen (left) als auch beim
     vertikalen (top) Scrollen stehen, über allem. */
  position: sticky;
  top: 0;
  z-index: 4;
  font-size: 11.5px;
  text-transform: uppercase;
  letter-spacing: .3px;
  color: var(--label);
  font-weight: 600;
  padding: 8px 12px;
}
.emp-cell {
  /* Kompakt gehalten, damit moeglichst viele MA auf einen Bildschirm passen
     (User-Wunsch 29.07.) — die Zeilenhoehe gibt .cell vor. */
  padding: 3px 12px;
  font-weight: 500;
  vertical-align: middle;
}
.emp-name { font-weight: 600; line-height: 1.15; white-space: nowrap; font-size: 13px; }
.emp-dep { font-size: 11.5px; }
/* Name ist ein Link aufs Profil, sieht aber aus wie Text (kein Blau/Unterstrich). */
.emp-link { text-decoration: none; color: inherit; display: block; }
.emp-link:hover .emp-name { text-decoration: underline; }

/* Kopfzeile: Tage — bleibt beim vertikalen Scrollen oben stehen (#192). */
.day-head {
  position: sticky;
  top: 0;
  z-index: 3;
  padding: 6px 0;
  min-width: 34px;
  text-align: center;
  background: var(--card);
}
.day-head.is-weekend { background: var(--neutral-bg); }
.day-head.is-holiday { background: var(--feiertag-bg); }
/* Nachbarmonate gedimmt, Monatswechsel mit Trennlinie + Kürzel sichtbar. */
.other-month { opacity: .45; }
.day-head.month-start,
.cell.month-start { border-left: 2px solid var(--text-3); }
.mon {
  font-size: 9px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .3px;
  color: var(--accent);
}
.wd {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: .3px;
  color: var(--label);
  font-weight: 600;
}
.dn {
  font-size: 13px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--text);
  margin-top: 2px;
}
/* Heute-Spalte: durchgezogene Linie links und rechts, oben und unten
   geschlossen — sonst nichts. KEIN Schleier über der Spalte: der färbte auch
   die leeren Zellen hellgrün und ließ sie wie ein Zustand aussehen, den es
   nicht gibt (weiß heißt "nichts eingetragen" und muss weiß bleiben).
   Deckende Farbe (Akzent eine Spur Richtung Kartenfarbe aufgehellt) statt
   halbtransparent, damit die Linie überall gleich hell wirkt und nicht je
   nach Zellfarbe anders.
   Die Linie liegt in einem Overlay-Pseudoelement, nicht in einem Inset-
   Schatten: der wurde an jeder Zeilengrenze vom 1px-Zeilentrenner
   überzeichnet, die Linie sah gestrichelt aus. Das Overlay reicht mit
   bottom: -1px über den Trenner hinweg — dadurch ist der Strich über die
   ganze Spalte durchgezogen. Overlay statt border, damit sich die
   Spaltenbreite nicht ändert. */
.matrix { --heute-rail: color-mix(in srgb, var(--accent) 82%, var(--card)); }
.matrix .is-today::after {
  content: "";
  position: absolute;
  top: 0;
  bottom: -1px;
  left: 0;
  right: 0;
  border-left: 2px solid var(--heute-rail);
  border-right: 2px solid var(--heute-rail);
  pointer-events: none;
}
/* .day-head ist sticky und damit selbst schon positioniert — kein
   position: relative setzen, das würde die Kopfzeile entkleben. */
.matrix .day-head.is-today::after { border-top: 2px solid var(--heute-rail); }
.matrix tbody tr:last-child .cell.is-today::after {
  bottom: 0;
  border-bottom: 2px solid var(--heute-rail);
}
.dn-today {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
}

/* Zellen */
/* Zellen sind anklickbar: führen in den persönlichen Kalender des
   Mitarbeiters an genau diesem Tag (siehe `tagOeffnen`). */
.cell {
  position: relative;
  min-width: 34px;
  height: 26px;
  background: var(--card);
  cursor: pointer;
}
.cell:hover,
.cell:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
/* Geburtstags-Kuchen (+ Alter): klein unten rechts in der Tages-Kachel. */
/* Warnsymbol „Ausstempeln vergessen" — oben links, damit es nicht mit
   Geburtstag/Jubiläum (unten rechts) kollidiert. Bleibt sichtbar, bis HR den
   Tag korrigiert hat. */
/* Warnsymbol „Ausstempeln vergessen": fuellt die ganze Zelle, damit es im
   Kalenderraster sofort auffaellt. */
.flagwarn {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  line-height: 1;
  color: #eab308;
  pointer-events: none;
}
.flagwarn-lg { color: #eab308; }

/* Unentschuldigt gefehlt: rotes „U" mittig in der Zelle. */
.unexcused-u {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 800;
  line-height: 1;
  color: #dc2626;
  pointer-events: none;
}
.unexcused-u-lg { color: #dc2626; font-weight: 800; }

/* Icon + Zahl liegen auf einem Chip in Kartenfarbe: Direkt auf einer
   gefüllten Zelle (Anwesend-Grün, Krank, Urlaub …) ging das Rosa bzw. Gold
   fast unter — schlechtester Kontrast 1,04:1 auf Grün. Mit Chip sind es
   konstant 3,7–4,3:1, unabhängig von der Zellfarbe; auf weißen Zellen ist
   der Chip unsichtbar. Die Töne sind dafür eine Spur dunkler als zuvor. */
.birthday {
  position: absolute;
  bottom: 0;
  right: 0;
  display: flex;
  align-items: center;
  gap: 1px;
  line-height: 0;
  color: #d1477e;
  background: var(--card);
  border-radius: 3px;
  padding: 1px 2px;
  pointer-events: none;
}
.birthday svg { width: 10px; height: 10px; }
.bd-age { font-size: 8px; font-weight: 700; line-height: 1; }
.cake-ic { color: #d1477e; }
/* Firmenjubiläum: Rosette + Jahre, oben links (gegenüber vom Geburtstag) —
   derselbe Chip, aus demselben Grund. */
.anniversary {
  position: absolute;
  top: 0;
  left: 0;
  display: flex;
  align-items: center;
  gap: 1px;
  line-height: 0;
  color: #a87d16;
  background: var(--card);
  border-radius: 3px;
  padding: 1px 2px;
  pointer-events: none;
}
.anniversary svg { width: 10px; height: 10px; }
.ann-yrs { font-size: 8px; font-weight: 700; line-height: 1; }
.ann-ic { color: #a87d16; }
/* Kräftigere Töne als die globalen Pastell-Tokens: auf kleinen Rasterzellen
   waren --urlaub-bg & Co. kaum von Weiß unterscheidbar (#158).
   Urlaub = blasses Blau, damit es sich klar vom Anwesend-Grün unterscheidet
   (User-Feedback 2026-07-22). Wochenende bleibt bewusst dezent. */
.cell.c-urlaub { background: #a9c9ec; }
.cell.c-sonder { background: #8fd8c9; }
.cell.c-abbau { background: #c5aef0; }
.cell.c-unbezahlt { background: #6f9fd8; }
.cell.c-krank { background: #f0a49c; }
.cell.c-weekend { background: var(--neutral-bg); }
.cell.c-feiertag { background: #f0d488; }
/* Anwesend/gearbeitet: vollflaechig wie die Abwesenheiten (User-Wunsch
   29.07.) — Urlaub ist inzwischen blau, daher keine Verwechslung mehr. */
.cell.c-anwesend { background: #4db878; }
.cell.c-pause { background: #f2c744; }

/* Legende */
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  padding: 14px;
  border-top: 1px solid var(--divider);
}
.lg {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 12.5px;
  color: var(--text-2);
}
.sw {
  width: 16px;
  height: 16px;
  border-radius: 4px;
  border: 1px solid var(--border);
  display: inline-block;
}
.sw.c-urlaub { background: #a9c9ec; }
.sw.c-sonder { background: #8fd8c9; }
.sw.c-abbau { background: #c5aef0; }
.sw.c-unbezahlt { background: #6f9fd8; }
.sw.c-krank { background: #f0a49c; }
.sw.c-weekend { background: var(--neutral-bg); }
.sw.c-feiertag { background: #f0d488; }
.sw.c-anwesend { background: #4db878; }
.sw.c-pause { background: #f2c744; }

/* Sortierbare Spaltenkoepfe: Klick sortiert, erneuter Klick dreht um. Der
   Pfeil steht nur an der aktiven Spalte (User-Wunsch 14.08., ersetzt das
   frühere Sortierung-Dropdown). */
.th-sort { cursor: pointer; user-select: none; white-space: nowrap; }
.th-sort:hover { color: var(--text); }
.th-sort.is-sorted { color: var(--text); font-weight: 700; }
.sort-arrow { margin-left: 4px; font-size: 9px; opacity: 0.75; }
</style>
