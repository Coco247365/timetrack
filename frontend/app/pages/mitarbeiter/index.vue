<script setup lang="ts">
// Mitarbeiterliste. Quelle: GET /admin/employees (Stammdaten) gemerged mit
// GET /clock/presence (heutiger Status). Überstunden-Saldo pro MA = Backend-Blocker.
const { apiFetch } = useApi();

interface Employee {
  id: string;
  name: string;
  role_name?: string | null;
  employee_number?: string | null;
  department_name?: string | null;
  company_id?: string | null;
  company_name?: string | null;
  time_capture?: string | null;
  employment_type?: string | null;
  is_active?: boolean;
  employment_status?: string | null;
  termination_date?: string | null;
  termination_reason?: string | null;
}
interface PresenceEmp {
  employee_id?: string; id?: string; status: string;
  clocked_in_since?: string | null; today_start?: string | null; today_end?: string | null;
}
interface Presence { present_count?: number; employees?: PresenceEmp[] }

const employees = ref<Employee[]>([]);
const otBalances = ref<Record<string, number>>({});

// Dezimalstunden -> "±Xh Ymin" (gleiche Darstellung wie die Profil-Kachel).
function hmSigned(h: number): string {
  const totalMin = Math.round(Math.abs(h) * 60);
  const hh = Math.floor(totalMin / 60);
  const mm = totalMin % 60;
  const body = totalMin === 0 ? "0h" : hh && mm ? `${hh}h ${mm}min` : hh ? `${hh}h` : `${mm}min`;
  return (h < 0 ? "−" : "+") + body;
}
// Resturlaub (Tage) je Mitarbeiter, laufendes Jahr.
const vacRemaining = ref<Record<string, number>>({});
const statusById = ref<Record<string, string>>({});
const sinceById = ref<Record<string, string>>({});
const startById = ref<Record<string, string>>({});
const endById = ref<Record<string, string>>({});
const loading = ref(true);
const loadError = ref("");

// Filter überleben die Navigation ins Mitarbeiter-Profil und zurück
// (useState statt ref: seitenübergreifender Zustand innerhalb der Sitzung;
// erst ein Neuladen der App setzt sie zurück).
const search = useState("ma-filter-suche", () => "");

// Offene Anträge (nur für Genehmiger sichtbar) — Link + Zähler im Kopf.
const { hasPermission: _hasPerm, canAccessAdmin: _canAdmin, featureEnabled: _featureOn } = useAuth();
const canDecide = computed(
  () => _hasPerm("leave:approve_all") || _hasPerm("leave:approve_team") || _canAdmin.value
);
const canSeeSickMails = computed(
  () => (_hasPerm("medical:read") || _canAdmin.value) && _featureOn("sick_mail")
);
const sickMailCount = ref(0);
onMounted(async () => {
  if (!canSeeSickMails.value) return;
  try {
    const mails = await apiFetch<{ status: string }[]>("/absences/sick-mails");
    sickMailCount.value = (mails || []).filter((m) => m.status === "NEW").length;
  } catch { /* Zähler optional */ }
});
const pendingCount = ref(0);
onMounted(async () => {
  if (!canDecide.value) return;
  try {
    // Status-gefiltert zählen — das tenant-weite Fenster (limit) würde bei
    // viel Betrieb offene Anträge unterschlagen (#165).
    const [submitted, escalated] = await Promise.all([
      apiFetch<{ id: string }[]>("/absences/requests?status=SUBMITTED&limit=500"),
      apiFetch<{ id: string }[]>("/absences/requests?status=ESCALATED&limit=500"),
    ]);
    pendingCount.value = (submitted || []).length + (escalated || []).length;
  } catch { /* Zähler optional */ }
});
const statusFilter = useState<"alle" | "anwesend" | "urlaub" | "krank">(
  "ma-filter-status", () => "alle"
);
// Erfassungsmodus (users.time_capture): nfc | auto | web; "" = kein Filter.
const capture = useState<"" | "nfc" | "auto" | "web">("ma-filter-erfassung", () => "");
const captureOptions = [
  { value: "", label: "Alle Erfassungen" },
  { value: "nfc", label: "NFC" },
  { value: "auto", label: "Automatisch" },
  { value: "web", label: "Web" },
] as const;
const company = useState("ma-filter-firma", () => "Alle Firmen");
// Gehalt (salaried) vs. Lohn (hourly) — Werte aus users.employment_type.
const payType = useState<"Gehalt & Lohn" | "Gehalt" | "Lohn">(
  "ma-filter-gehaltlohn", () => "Gehalt & Lohn"
);
const payTypes = ["Gehalt & Lohn", "Gehalt", "Lohn"] as const;

// Archiv: Ausgetretene bleiben gespeichert (Aufbewahrung), tauchen aber nur
// noch hier unten auf — nirgendwo sonst im System.
const archived = ref<Employee[]>([]);
const archiveOpen = ref(false);

// Archiv-Filter: Suche, Austrittsgrund, Austrittsjahr (client-seitig,
// Optionen nur aus tatsächlich vorkommenden Werten).
const archiveSearch = ref("");
const archiveReason = ref("");
const archiveYear = ref("");
const archiveReasonOptions = computed(() =>
  [...new Set(archived.value.map((e) => e.termination_reason).filter(Boolean))] as string[]
);
const archiveYearOptions = computed(() =>
  [...new Set(archived.value.map((e) => (e.termination_date || "").slice(0, 4)).filter(Boolean))].sort().reverse()
);
const filteredArchived = computed(() => {
  const q = archiveSearch.value.trim().toLowerCase();
  return archived.value.filter((e) => {
    if (archiveReason.value && e.termination_reason !== archiveReason.value) return false;
    if (archiveYear.value && (e.termination_date || "").slice(0, 4) !== archiveYear.value) return false;
    if (q && !`${e.name} ${e.employee_number || ""}`.toLowerCase().includes(q)) return false;
    return true;
  });
});

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const all = await apiFetch<Employee[]>("/admin/employees");
    // Gelöschte/inaktive Mitarbeiter ausblenden (Soft-Delete = is_active false).
    employees.value = all.filter((e) => e.is_active !== false);
    archived.value = all
      .filter((e) => e.employment_status === "archived")
      .sort((a, b) => String(b.termination_date || "").localeCompare(String(a.termination_date || "")));
  } catch (e: any) {
    loadError.value = e?.data?.detail || "Mitarbeiter konnten nicht geladen werden.";
  }
  // Status separat — falls Recht/Endpunkt fehlt, Liste trotzdem zeigen.
  try {
    const p = await apiFetch<Presence>("/clock/presence");
    const map: Record<string, string> = {};
    const since: Record<string, string> = {};
    const starts: Record<string, string> = {};
    const ends: Record<string, string> = {};
    for (const e of p.employees || []) {
      const key = e.employee_id || e.id;
      if (key) {
        map[key] = e.status;
        if (e.clocked_in_since) since[key] = e.clocked_in_since;
        if (e.today_start) starts[key] = e.today_start;
        if (e.today_end) ends[key] = e.today_end;
      }
    }
    statusById.value = map;
    sinceById.value = since;
    startById.value = starts;
    endById.value = ends;
  } catch { /* Status optional */ }
  // Überstunden-Salden separat — bei fehlendem Recht/Endpunkt Liste trotzdem zeigen.
  try {
    otBalances.value = await apiFetch<Record<string, number>>("/overtime/balances");
  } catch { otBalances.value = {}; }
  // Resturlaub separat — bei fehlendem Recht/Endpunkt Liste trotzdem zeigen.
  try {
    vacRemaining.value = await apiFetch<Record<string, number>>(
      `/absences/balances?year=${new Date().getFullYear()}`
    );
  } catch { vacRemaining.value = {}; }
  loading.value = false;
}
onMounted(load);

const companies = computed(() => {
  const set = new Set<string>();
  for (const e of employees.value) if (e.company_name) set.add(e.company_name);
  return ["Alle Firmen", ...[...set].sort()];
});

// Uhrzeit (HH:MM) aus ISO-Zeitstempel, lokale Zeit.
function hhmm(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}
// "Heute eingestempelt": seit … (noch drin) / von … bis … (war da) / – (noch gar nicht).
function todayStamp(e: Employee): string {
  const s = statusById.value[e.id] || "";
  const inNow = s === "clocked_in" || s === "on_break";
  if (inNow && sinceById.value[e.id]) {
    const d = new Date(sinceById.value[e.id]);
    // Offene Schicht von einem früheren Tag: Datum mit anzeigen.
    if (!isNaN(d.getTime()) && d.toDateString() !== new Date().toDateString()) {
      return `seit ${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}., ${hhmm(sinceById.value[e.id])} Uhr`;
    }
    return `seit ${hhmm(sinceById.value[e.id])} Uhr`;
  }
  const start = startById.value[e.id];
  const end = endById.value[e.id];
  if (start && end) return `von ${hhmm(start)} bis ${hhmm(end)} Uhr`;
  if (start && inNow) return `seit ${hhmm(start)} Uhr`;
  return "–";
}

// Anzeige-Status. "Pause" ist ein eigener Zustand (gelb) — fuer Zaehler und
// Filter zaehlt sie aber als anwesend (siehe istAnwesend), denn der Mitarbeiter
// ist ja da.
function uiStatus(e: Employee): string {
  const s = statusById.value[e.id] || "";
  if (s === "vacation") return "urlaub";
  if (s === "sick_leave") return "krank";
  if (s === "on_break") return "pause";
  if (s === "clocked_in") return "anwesend";
  return "abwesend";
}
function istAnwesend(e: Employee): boolean {
  const s = uiStatus(e);
  return s === "anwesend" || s === "pause";
}

// Sortierung: alphabetisch nach VORNAME (name = "Vorname Nachname"). Das Backend
// liefert nach Nachname sortiert (order_by last_name, first_name) — das bleibt so,
// weil daran auch Export und andere Listen hängen; hier wird nur die Anzeige
// umsortiert. localeCompare("de"): Umlaute einsortiert (Ä bei A), Groß/klein egal.
const filtered = computed(() =>
  employees.value.filter((e) => {
    const q = search.value.trim().toLowerCase();
    if (q) {
      const hay = `${e.name} ${e.employee_number || ""} ${e.department_name || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    if (capture.value && e.time_capture !== capture.value) return false;
    if (company.value !== "Alle Firmen" && e.company_name !== company.value) return false;
    if (payType.value === "Gehalt" && e.employment_type !== "salaried") return false;
    if (payType.value === "Lohn" && e.employment_type !== "hourly") return false;
    if (statusFilter.value === "anwesend" && !istAnwesend(e)) return false;
    if (statusFilter.value !== "alle" && statusFilter.value !== "anwesend"
      && uiStatus(e) !== statusFilter.value) return false;
    return true;
  }).sort((a, b) => a.name.localeCompare(b.name, "de", { sensitivity: "base" }))
);

const presentCount = computed(() => employees.value.filter(istAnwesend).length);

const chips: { key: typeof statusFilter.value; label: string }[] = [
  { key: "alle", label: "Alle" },
  { key: "anwesend", label: "Anwesend" },
  { key: "urlaub", label: "Urlaub" },
  { key: "krank", label: "Krank" },
];

// Austrittsgründe (kanonischer Wert → Label), wie in den Stammdaten.
const terminationReasons: Record<string, string> = {
  AUFHEBUNGSVERTRAG: "Aufhebungsvertrag",
  AUSBILDUNG_ENDE: "Ende der Ausbildung",
  BEFRISTUNG_ENDE: "Ende der Befristung",
  KUENDIGUNG: "Kündigung",
  KUENDIGUNG_MITARBEITER: "Kündigung durch Mitarbeiter",
  KUENDIGUNG_UNTERNEHMEN: "Kündigung durch Unternehmen",
  RENTE: "Rente",
  TOD: "Tod",
};
// Urlaubstage: maximal eine Nachkommastelle (glättet Float-Artefakte).
function d1(v?: number | null): string {
  if (v == null) return "—";
  return String(Math.round(v * 10) / 10);
}
function fmtDate(iso?: string | null): string {
  if (!iso) return "—";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}.${m}.${y}`;
}
</script>

<template>
  <div>
    <div class="spread" style="margin-bottom: 18px">
      <div class="row" style="gap: 18px; align-items: baseline">
        <h1>Mitarbeiter</h1>
        <NuxtLink v-if="canDecide" to="/mitarbeiter/antraege" class="linkbtn" style="font-size: 15px">
          Anträge<span v-if="pendingCount > 0"> ({{ pendingCount }})</span> ›
        </NuxtLink>
        <NuxtLink v-if="canSeeSickMails" to="/mitarbeiter/krankmeldungen" class="linkbtn" style="font-size: 15px">
          Krankmeldungen<span v-if="sickMailCount > 0"> ({{ sickMailCount }})</span> ›
        </NuxtLink>
      </div>
      <span class="muted num">
        {{ employees.length }} Mitarbeiter · {{ presentCount }} anwesend
      </span>
    </div>

    <div class="card" style="padding: 14px; margin-bottom: 16px">
      <div class="row filterbar" style="flex-wrap: wrap; gap: 10px">
        <input v-model="search" class="input" style="flex: 1; min-width: 220px"
          placeholder="Suche: Name, Personalnr. oder Bereich" />
        <div class="row" style="gap: 6px">
          <button v-for="c in chips" :key="c.key" class="btn"
            :class="{ 'btn-primary': statusFilter === c.key }"
            style="padding: 7px 12px" @click="statusFilter = c.key">{{ c.label }}</button>
        </div>
        <select v-model="capture" class="input" style="width: auto">
          <option v-for="o in captureOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
        <select v-model="company" class="input" style="width: auto">
          <option v-for="c in companies" :key="c">{{ c }}</option>
        </select>
        <select v-model="payType" class="input" style="width: auto">
          <option v-for="p in payTypes" :key="p">{{ p }}</option>
        </select>
        <NuxtLink to="/mitarbeiter/neu" class="btn btn-primary">+ Mitarbeiter</NuxtLink>
      </div>
    </div>

    <div class="card" style="padding: 0">
      <div v-if="loadError" class="wip-banner" style="margin: 14px">{{ loadError }}</div>
      <div v-else-if="loading" class="empty">Lädt …</div>
      <div v-else-if="!filtered.length" class="empty">Keine Mitarbeiter gefunden.</div>
      <table v-else class="data ma-table">
        <thead>
          <tr>
            <th>Mitarbeiter</th>
            <th>Personalnr.</th>
            <th>Bereich</th>
            <th>Status</th>
            <th>Heute eingestempelt</th>
            <th>Überstunden</th>
            <th>Resturlaub</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in filtered" :key="e.id" class="clickable"
            @click="navigateTo(`/mitarbeiter/${e.id}`)">
            <td>
              <div style="font-weight: 600">{{ e.name }}</div>
            </td>
            <td class="num">{{ e.employee_number || "—" }}</td>
            <td>
              <div>{{ e.department_name || "—" }}</div>
              <div v-if="e.company_name" class="muted" style="font-size: 12px">{{ e.company_name }}</div>
            </td>
            <td><StatusBadge :status="uiStatus(e)" /></td>
            <td class="num" :class="{ muted: todayStamp(e) === '–' }">
              {{ todayStamp(e) }}
              <!-- Auto-Erfasste: kennzeichnen, dass die Zeit aus der automatischen
                   Erfassung stammt, nicht aus echten Stempelungen -->
              <span v-if="e.time_capture === 'auto' && todayStamp(e) !== '–'" class="muted" style="font-size: 12px">(auto)</span>
            </td>
            <td class="num" :class="{ muted: otBalances[e.id] == null }">
              {{ otBalances[e.id] != null ? hmSigned(otBalances[e.id]) : '—' }}
            </td>
            <td class="num" :class="{ muted: vacRemaining[e.id] == null }">
              {{ vacRemaining[e.id] != null ? d1(vacRemaining[e.id]) + ' Tage' : '—' }}
            </td>
            <td class="muted" style="text-align: right">›</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Archiv: ausgetretene Mitarbeiter — aufbewahrt, aber aus dem aktiven System raus. -->
    <div v-if="archived.length" style="margin-top: 16px">
      <button class="linkbtn muted" style="font-size: 13px" @click="archiveOpen = !archiveOpen">
        {{ archiveOpen ? "▾" : "▸" }} Archiv ({{ archived.length }})
      </button>
      <div v-if="archiveOpen" class="card" style="padding: 0; margin-top: 8px">
        <div class="row" style="gap: 10px; flex-wrap: wrap; padding: 12px 18px 0">
          <input v-model="archiveSearch" class="input" style="width: 200px" placeholder="Name / Personalnr. suchen" />
          <select v-model="archiveReason" class="input" style="width: auto">
            <option value="">Alle Gründe</option>
            <option v-for="r in archiveReasonOptions" :key="r" :value="r">{{ terminationReasons[r] || r }}</option>
          </select>
          <select v-model="archiveYear" class="input" style="width: auto">
            <option value="">Alle Jahre</option>
            <option v-for="y in archiveYearOptions" :key="y" :value="y">{{ y }}</option>
          </select>
          <span v-if="filteredArchived.length !== archived.length" class="muted" style="align-self: center; font-size: 12.5px">
            {{ filteredArchived.length }} von {{ archived.length }}
          </span>
        </div>
        <table class="data">
          <thead>
            <tr>
              <th>Mitarbeiter</th>
              <th>Personalnr.</th>
              <th>Ausgetreten am</th>
              <th>Austrittsgrund</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="e in filteredArchived" :key="e.id" class="clickable"
              @click="navigateTo(`/mitarbeiter/${e.id}`)">
              <td style="font-weight: 600">{{ e.name }}</td>
              <td class="num">{{ e.employee_number || "—" }}</td>
              <td class="num">{{ fmtDate(e.termination_date) }}</td>
              <td>{{ e.termination_reason ? (terminationReasons[e.termination_reason] || e.termination_reason) : "—" }}</td>
              <td class="muted" style="text-align: right">›</td>
            </tr>
          </tbody>
        </table>
        <p class="muted" style="padding: 10px 18px; margin: 0; font-size: 12.5px">
          Archivierte Mitarbeiter bleiben mit ihrer kompletten Historie gespeichert
          (Aufbewahrungspflicht), sind aber aus Listen, Auswertungen und Export raus.
          Login und NFC-Karte sind deaktiviert.
        </p>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* Toolbar: alle Bedien-Elemente (Suche, Chips, Selects, +Mitarbeiter) gleiche Höhe. */
.filterbar .btn,
.filterbar .input { height: 40px; box-sizing: border-box; }

/* Spaltenbreiten fest verteilen — sonst zieht die automatische Verteilung die
   Namensspalte unnötig breit und die Zahlenspalten rücken weit nach rechts. */
.ma-table { table-layout: fixed; }
.ma-table th:nth-child(1) { width: 19%; }  /* Mitarbeiter */
.ma-table th:nth-child(2) { width: 10%; }  /* Personalnr. */
.ma-table th:nth-child(3) { width: 17%; }  /* Bereich */
.ma-table th:nth-child(4) { width: 11%; }  /* Status */
.ma-table th:nth-child(5) { width: 19%; }  /* Heute eingestempelt */
.ma-table th:nth-child(6) { width: 11%; }  /* Überstunden */
.ma-table th:nth-child(7) { width: 11%; }  /* Resturlaub */
.ma-table th:nth-child(8) { width: 2%; }   /* Chevron */
</style>
