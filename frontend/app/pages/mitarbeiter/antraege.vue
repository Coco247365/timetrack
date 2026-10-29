<script setup lang="ts">
// Zentrale Antrags-Übersicht für Genehmiger (TL: eigener Bereich, GF/HR: alle).
// Quelle: GET /absences/requests (server-seitig scope-gefiltert, Namen angereichert).
// Aktionen: PATCH /absences/requests/{id}/approve | /reject — Server prüft
// Bereich + Vier-Augen-Prinzip.
const { apiFetch } = useApi();
const { hasPermission, canAccessAdmin } = useAuth();

const canDecide = computed(
  () =>
    hasPermission("leave:approve_all") ||
    hasPermission("leave:approve_team") ||
    canAccessAdmin.value
);

interface Req {
  id: string;
  employee_id: string;
  employee_name?: string | null;
  employee_number?: string | null;
  request_type: string;
  status: string;
  date_from: string;
  date_to: string;
  days_count?: number | string | null;
  submitted_at?: string | null;
  decided_at?: string | null;
  decision_reason?: string | null;
}

const requests = ref<Req[]>([]);
const loading = ref(true);
const loadError = ref("");
const busyId = ref<string | null>(null);
const actionErr = ref("");
const decidedOpen = ref(false);

const typeLabels: Record<string, string> = {
  VACATION: "Urlaub",
  SICK_LEAVE: "Krank",
  OVERTIME_REDUCTION: "Überstundenabbau",
  SPECIAL_LEAVE: "Sonderurlaub",
};
const statusLabels: Record<string, string> = {
  SUBMITTED: "Eingereicht",
  ESCALATED: "Eskaliert",
  APPROVED: "Genehmigt",
  REJECTED: "Abgelehnt",
  CANCELLED: "Storniert",
};
function typeLabel(t: string) { return typeLabels[String(t || "").toUpperCase()] || t; }
function statusLabel(st: string) { return statusLabels[String(st || "").toUpperCase()] || st; }
function fmtDate(iso?: string | null) {
  if (!iso) return "—";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}.${m}.${y}`;
}
function fmtDateTime(iso?: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}.${d.getFullYear()}, ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

const isPending = (r: Req) => ["SUBMITTED", "ESCALATED"].includes(String(r.status).toUpperCase());
const pending = computed(() =>
  requests.value.filter(isPending).slice().sort((a, b) => (a.date_from || "").localeCompare(b.date_from || ""))
);
const decided = computed(() =>
  requests.value
    .filter((r) => !isPending(r))
    .slice()
    .sort((a, b) => (b.decided_at || b.date_from || "").localeCompare(a.decided_at || a.date_from || ""))
    .slice(0, 30)
);

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    // Offene Anträge VOLLSTÄNDIG über den Status-Filter laden — die
    // tenant-weite Liste ist gekappt (created_at DESC); ein alter offener
    // Antrag fiele sonst still aus der Genehmigungsliste (#165).
    // Historie (entschieden) bewusst weiter als "letzte 200".
    const [submitted, escalated, recent] = await Promise.all([
      apiFetch<Req[]>("/absences/requests?status=SUBMITTED&limit=500"),
      apiFetch<Req[]>("/absences/requests?status=ESCALATED&limit=500"),
      apiFetch<Req[]>("/absences/requests?limit=200"),
    ]);
    const byId = new Map<string, Req>();
    for (const r of [...(submitted || []), ...(escalated || []), ...(recent || [])]) byId.set(r.id, r);
    requests.value = [...byId.values()];
  } catch (e: any) {
    loadError.value = e?.data?.detail || "Anträge konnten nicht geladen werden.";
  }
  loading.value = false;
}
onMounted(load);

async function decide(r: Req, action: "approve" | "reject") {
  busyId.value = r.id;
  actionErr.value = "";
  try {
    await apiFetch(`/absences/requests/${r.id}/${action}`, {
      method: "PATCH",
      body: action === "reject" ? { reason: "In der Antragsübersicht abgelehnt" } : {},
    });
    await load();
  } catch (e: any) {
    actionErr.value = e?.data?.detail || "Entscheidung konnte nicht gespeichert werden.";
  }
  busyId.value = null;
}
</script>

<template>
  <div>
    <div class="spread" style="margin-bottom: 18px">
      <div class="row" style="gap: 18px; align-items: baseline">
        <NuxtLink to="/mitarbeiter" class="linkbtn" style="font-size: 15px">‹ Mitarbeiter</NuxtLink>
        <h1>Anträge</h1>
      </div>
      <span v-if="!loading" class="muted">{{ pending.length }} offen</span>
    </div>

    <div v-if="!canDecide" class="card">
      <div class="empty">Keine Berechtigung, Anträge zu bearbeiten.</div>
    </div>

    <template v-else>
      <div v-if="loadError" class="wip-banner" style="margin-bottom: 14px">{{ loadError }}</div>
      <div v-if="actionErr" class="wip-banner" style="margin-bottom: 14px">{{ actionErr }}</div>

      <div class="card" style="margin-bottom: 16px">
        <h2 style="margin-bottom: 12px">Offene Anträge</h2>
        <div v-if="loading" class="empty">Lädt …</div>
        <div v-else-if="!pending.length" class="empty">Keine offenen Anträge. 🎉</div>
        <table v-else class="data">
          <thead>
            <tr>
              <th>Mitarbeiter</th><th>Typ</th><th>Zeitraum</th><th>Tage</th><th>Eingereicht</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in pending" :key="r.id">
              <td>
                {{ r.employee_name || "—" }}
                <span v-if="r.employee_number" class="muted" style="font-size: 12px"> · {{ r.employee_number }}</span>
              </td>
              <td>{{ typeLabel(r.request_type) }}</td>
              <td class="num">{{ fmtDate(r.date_from) }} – {{ fmtDate(r.date_to) }}</td>
              <td class="num">{{ r.days_count ?? "—" }}</td>
              <td class="num">{{ fmtDateTime(r.submitted_at) }}</td>
              <td style="text-align: right; white-space: nowrap">
                <button class="linkbtn" style="color: var(--ok, #1a7f37); font-weight: 600"
                  :disabled="busyId === r.id" @click="decide(r, 'approve')">Genehmigen</button>
                <button class="linkbtn danger" style="margin-left: 12px"
                  :disabled="busyId === r.id" @click="decide(r, 'reject')">Ablehnen</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="card" style="padding-bottom: 10px">
        <button class="linkbtn" style="margin-bottom: 8px" @click="decidedOpen = !decidedOpen">
          {{ decidedOpen ? "▾" : "▸" }} Zuletzt entschieden ({{ decided.length }})
        </button>
        <table v-if="decidedOpen && decided.length" class="data" style="margin-bottom: 8px">
          <thead>
            <tr><th>Mitarbeiter</th><th>Typ</th><th>Zeitraum</th><th>Status</th><th>Entschieden</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in decided" :key="r.id">
              <td>{{ r.employee_name || "—" }}</td>
              <td>{{ typeLabel(r.request_type) }}</td>
              <td class="num">{{ fmtDate(r.date_from) }} – {{ fmtDate(r.date_to) }}</td>
              <td>{{ statusLabel(r.status) }}</td>
              <td class="num">{{ fmtDateTime(r.decided_at) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else-if="decidedOpen" class="empty" style="margin-bottom: 8px">Noch nichts entschieden.</div>
      </div>
    </template>
  </div>
</template>
