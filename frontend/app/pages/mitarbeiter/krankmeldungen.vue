<script setup lang="ts">
// Krankmeldungen, die per E-Mail eingegangen sind (krank@…-Postfach).
// Bekannte Absender werden automatisch zugeordnet und als Antrag eingereicht
// (-> Anträge-Übersicht); unbekannte landen hier zum manuellen Zuordnen.
// Gesundheitsdaten: nur mit medical:read (bzw. Admin) sichtbar.
const { apiFetch } = useApi();
const { hasPermission, canAccessAdmin, featureEnabled } = useAuth();


const canView = computed(
  () => (hasPermission("medical:read") || canAccessAdmin.value) && featureEnabled("sick_mail")
);

interface SickMail {
  id: string;
  sender_email: string;
  subject?: string | null;
  body_preview?: string | null;
  received_at: string;
  parsed_date_from?: string | null;
  parsed_date_to?: string | null;
  parsed_hint?: string | null;
  employee_id?: string | null;
  employee_name?: string | null;
  absence_request_id?: string | null;
  status: "NEW" | "LINKED" | "DISMISSED" | string;
  assignment?: "auto" | "manual" | string | null;
}
interface Employee { id: string; name: string; is_active?: boolean }

const reports = ref<SickMail[]>([]);
const employees = ref<Employee[]>([]);
const loading = ref(true);
const loadError = ref("");
const busyId = ref<string | null>(null);
const actionErr = ref("");
const pollBusy = ref(false);
const pollInfo = ref("");
const assignSel = ref<Record<string, string>>({});
// Korrigierbarer Zeitraum pro offener Meldung (vorbefuellt mit dem Parser-Vorschlag).
const dateDraft = ref<Record<string, { from: string; to: string }>>({});
function draftFor(r: SickMail) {
  if (!dateDraft.value[r.id]) {
    dateDraft.value[r.id] = {
      from: (r.parsed_date_from || "").slice(0, 10),
      to: (r.parsed_date_to || "").slice(0, 10),
    };
  }
  return dateDraft.value[r.id];
}
const doneOpen = ref(false);

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    reports.value = await apiFetch<SickMail[]>("/absences/sick-mails");
    // Namens-Vorschlag: erkannten Mitarbeiter im Dropdown vorauswählen.
    for (const r of reports.value) {
      if (r.status === "NEW" && r.employee_id && !assignSel.value[r.id]) {
        assignSel.value[r.id] = r.employee_id;
      }
    }
    try {
      const emps = await apiFetch<Employee[]>("/admin/employees");
      employees.value = emps.filter((e) => e.is_active !== false);
    } catch { /* Zuordnen-Dropdown dann leer */ }
  } catch (e: any) {
    loadError.value = e?.data?.detail || "Krankmeldungen konnten nicht geladen werden.";
  }
  loading.value = false;
}
onMounted(load);

const open = computed(() => reports.value.filter((r) => r.status === "NEW"));

// Zeitraum-Filter für die verarbeiteten Meldungen (nach Eingangsdatum).
const doneFrom = ref("");
const doneTo = ref("");
const doneStatus = ref("");
const done = computed(() =>
  reports.value
    .filter((r) => r.status !== "NEW")
    .filter((r) => {
      const day = (r.received_at || "").slice(0, 10);
      if (doneFrom.value && day < doneFrom.value) return false;
      if (doneTo.value && day > doneTo.value) return false;
      if (doneStatus.value === "LINKED_AUTO") return r.status === "LINKED" && r.assignment !== "manual";
      if (doneStatus.value === "LINKED_MANUAL") return r.status === "LINKED" && r.assignment === "manual";
      if (doneStatus.value && r.status !== doneStatus.value) return false;
      return true;
    })
    .slice(0, 100)
);

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
const statusLabels: Record<string, string> = {
  NEW: "Offen", LINKED: "Zugeordnet", DISMISSED: "Verworfen",
};
function assignmentLabel(r: SickMail): string | null {
  if (r.status !== "LINKED") return null;
  return r.assignment === "manual" ? "manuell" : r.assignment === "auto" ? "automatisch" : null;
}

async function assign(r: SickMail) {
  const emp = assignSel.value[r.id];
  if (!emp) return;
  busyId.value = r.id;
  actionErr.value = "";
  try {
    const d = draftFor(r);
    await apiFetch(`/absences/sick-mails/${r.id}/assign`, {
      method: "POST",
      body: { employee_id: emp, date_from: d.from || undefined, date_to: d.to || undefined },
    });
    await load();
  } catch (e: any) {
    actionErr.value = e?.data?.detail || "Zuordnung fehlgeschlagen.";
  }
  busyId.value = null;
}
async function dismiss(r: SickMail) {
  busyId.value = r.id;
  actionErr.value = "";
  try {
    await apiFetch(`/absences/sick-mails/${r.id}/dismiss`, { method: "POST" });
    await load();
  } catch (e: any) {
    actionErr.value = e?.data?.detail || "Verwerfen fehlgeschlagen.";
  }
  busyId.value = null;
}
// Pop-up mit der vollstaendigen Nachricht.
const viewMail = ref<SickMail | null>(null);
function previewText(r: SickMail): string {
  const t = (r.body_preview || "").replace(/\s+/g, " ").trim();
  return t.length > 110 ? t.slice(0, 110) + " …" : t;
}
async function deleteReport(r: SickMail) {
  busyId.value = r.id;
  actionErr.value = "";
  try {
    await apiFetch(`/absences/sick-mails/${r.id}`, { method: "DELETE" });
    await load();
  } catch (e: any) {
    actionErr.value = e?.data?.detail || "Löschen fehlgeschlagen.";
  }
  busyId.value = null;
}
async function resetReport(r: SickMail) {
  busyId.value = r.id;
  actionErr.value = "";
  try {
    await apiFetch(`/absences/sick-mails/${r.id}/reset`, { method: "POST" });
    delete dateDraft.value[r.id]; // frischer Vorschlag beim erneuten Zuordnen
    await load();
  } catch (e: any) {
    actionErr.value = e?.data?.detail || "Zurücksetzen fehlgeschlagen.";
  }
  busyId.value = null;
}
async function pollNow() {
  pollBusy.value = true;
  pollInfo.value = "";
  actionErr.value = "";
  try {
    const res = await apiFetch<{ imported: number }>("/absences/sick-mails/poll", { method: "POST" });
    pollInfo.value = res.imported ? `${res.imported} neue Meldung(en) abgerufen.` : "Keine neuen Mails im Postfach.";
    await load();
  } catch (e: any) {
    actionErr.value = e?.data?.detail || "Postfach-Abruf fehlgeschlagen (M365 konfiguriert?).";
  }
  pollBusy.value = false;
}
</script>

<template>
  <div>
    <div class="spread" style="margin-bottom: 18px">
      <div class="row" style="gap: 18px; align-items: baseline">
        <NuxtLink to="/mitarbeiter" class="linkbtn" style="font-size: 15px">‹ Mitarbeiter</NuxtLink>
        <h1>Krankmeldungen</h1>
      </div>
      <button class="btn" :disabled="pollBusy" @click="pollNow">
        {{ pollBusy ? "Ruft ab …" : "Postfach jetzt abrufen" }}
      </button>
    </div>

    <div v-if="!canView" class="card">
      <div class="empty">Keine Berechtigung (medical:read erforderlich — Gesundheitsdaten).</div>
    </div>

    <template v-else>
      <div v-if="loadError" class="wip-banner" style="margin-bottom: 14px">{{ loadError }}</div>
      <div v-if="actionErr" class="wip-banner" style="margin-bottom: 14px">{{ actionErr }}</div>
      <div v-if="pollInfo" class="card" style="padding: 10px 14px; margin-bottom: 14px">
        <span class="muted">{{ pollInfo }}</span>
      </div>

      <div class="card" style="margin-bottom: 16px">
        <h2 style="margin-bottom: 4px">Offene Meldungen</h2>
        <div class="muted" style="font-size: 12.5px; margin-bottom: 12px">
          Bekannte Absender werden automatisch zugeordnet und landen als „Eingereicht"
          in den <NuxtLink to="/mitarbeiter/antraege" class="linkbtn">Anträgen</NuxtLink>.
          Hier steht, was nicht automatisch zuzuordnen war.
        </div>
        <div v-if="loading" class="empty">Lädt …</div>
        <div v-else-if="!open.length" class="empty">Nichts offen. 🎉</div>
        <div v-else class="stack" style="gap: 12px">
          <div v-for="r in open" :key="r.id"
            style="border: 1px solid var(--divider); border-radius: 12px; padding: 14px 16px">
            <!-- Kopfzeile: wann · von wem · Aktionen -->
            <div class="spread" style="flex-wrap: wrap; gap: 8px; margin-bottom: 8px">
              <div class="row" style="gap: 14px; flex-wrap: wrap; align-items: baseline">
                <strong class="num" style="font-size: 13.5px">{{ fmtDateTime(r.received_at) }}</strong>
                <span>
                  {{ r.sender_email || "(kein Absender)" }}
                  <span v-if="r.employee_name" class="muted" style="font-size: 12.5px"> → {{ r.employee_name }} (Vorschlag)</span>
                </span>
              </div>
              <button class="linkbtn danger" :disabled="busyId === r.id"
                title="Endgültig löschen (z.B. Spam/Newsletter)" @click="deleteReport(r)">Löschen</button>
            </div>

            <!-- Inhalt: kompakter Anriss — Klick oeffnet die ganze Nachricht -->
            <div style="cursor: pointer" title="Nachricht anzeigen" @click="viewMail = r">
              <strong v-if="r.subject" style="font-size: 14px">{{ r.subject }}</strong>
              <span v-if="r.body_preview" class="muted" style="font-size: 13px"> — {{ previewText(r) }}</span>
              <span class="linkbtn" style="font-size: 12.5px; margin-left: 8px">Nachricht anzeigen</span>
            </div>

            <!-- Bearbeiten/Zuordnen -->
            <div class="row" style="gap: 10px; flex-wrap: wrap; align-items: flex-end; margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--divider)">
              <div class="stack" style="gap: 4px">
                <span class="label">Krank von</span>
                <input v-model="draftFor(r).from" type="date" class="input" style="width: 145px" />
              </div>
              <div class="stack" style="gap: 4px">
                <span class="label">bis</span>
                <input v-model="draftFor(r).to" type="date" class="input" style="width: 145px" />
              </div>
              <div class="stack" style="gap: 4px">
                <span class="label">Mitarbeiter</span>
                <select v-model="assignSel[r.id]" class="input" style="width: 210px">
                  <option value="">wählen …</option>
                  <option v-for="e in employees" :key="e.id" :value="e.id">{{ e.name }}</option>
                </select>
              </div>
              <button class="btn btn-primary" :disabled="busyId === r.id || !assignSel[r.id]"
                @click="assign(r)">Als Krankmeldung erfassen</button>
              <span v-if="r.parsed_hint" class="muted" style="font-size: 11.5px">erkannt: {{ r.parsed_hint }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="card" style="padding-bottom: 10px">
        <button class="linkbtn" style="margin-bottom: 8px" @click="doneOpen = !doneOpen">
          {{ doneOpen ? "▾" : "▸" }} Verarbeitete Meldungen ({{ done.length }})
        </button>
        <div v-if="doneOpen" class="row" style="gap: 12px; flex-wrap: wrap; align-items: flex-end; margin-bottom: 12px">
          <div class="stack" style="gap: 4px">
            <span class="label">Eingegangen von</span>
            <input v-model="doneFrom" type="date" class="input" style="width: 150px" />
          </div>
          <div class="stack" style="gap: 4px">
            <span class="label">bis</span>
            <input v-model="doneTo" type="date" class="input" style="width: 150px" />
          </div>
          <div class="stack" style="gap: 4px">
            <span class="label">Status</span>
            <select v-model="doneStatus" class="input" style="width: 210px">
              <option value="">Alle</option>
              <option value="LINKED">Zugeordnet (alle)</option>
              <option value="LINKED_AUTO">Zugeordnet — automatisch</option>
              <option value="LINKED_MANUAL">Zugeordnet — manuell</option>
              <option value="DISMISSED">Verworfen</option>
            </select>
          </div>
          <button v-if="doneFrom || doneTo || doneStatus" class="linkbtn"
            @click="doneFrom = ''; doneTo = ''; doneStatus = ''">Filter zurücksetzen</button>
        </div>
        <table v-if="doneOpen && done.length" class="data" style="margin-bottom: 8px">
          <thead>
            <tr><th>Eingegangen</th><th>Absender</th><th>Mitarbeiter</th><th>Inhalt</th><th>Zeitraum</th><th>Status</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="r in done" :key="r.id">
              <td class="num">{{ fmtDateTime(r.received_at) }}</td>
              <td class="muted">{{ r.sender_email }}</td>
              <td>{{ r.employee_name || "—" }}</td>
              <td style="max-width: 380px; min-width: 220px; cursor: pointer" title="Nachricht anzeigen" @click="viewMail = r">
                <span style="display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">
                  <strong v-if="r.subject" style="font-size: 13px">{{ r.subject }}</strong>
                  <span v-if="r.body_preview" class="muted" style="font-size: 12.5px"> — {{ previewText(r) }}</span>
                </span>
              </td>
              <td class="num">{{ fmtDate(r.parsed_date_from) }} – {{ fmtDate(r.parsed_date_to) }}</td>
              <td style="white-space: nowrap">
                {{ statusLabels[r.status] || r.status }}
                <span v-if="assignmentLabel(r)" class="badge" :class="r.assignment === 'manual' ? 'blue' : 'green'"
                  style="margin-left: 6px"><span class="dot" />{{ assignmentLabel(r) }}</span>
              </td>
              <td style="text-align: right; white-space: nowrap">
                <button class="linkbtn" :disabled="busyId === r.id"
                  :title="r.status === 'LINKED' ? 'Storniert den erzeugten Antrag und legt die Meldung zurück in Offen — dort mit korrigiertem Zeitraum neu zuordnen.' : 'Meldung zurück in die Offen-Liste holen.'"
                  @click="resetReport(r)">
                  {{ r.status === "LINKED" ? "Zurücksetzen" : "Wiederherstellen" }}
                </button>
                <button class="linkbtn danger" style="margin-left: 10px" :disabled="busyId === r.id"
                  @click="deleteReport(r)">Löschen</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else-if="doneOpen" class="empty" style="margin-bottom: 8px">Noch nichts verarbeitet.</div>
      </div>
    </template>

    <!-- Pop-up: vollstaendige Nachricht -->
    <div v-if="viewMail" @click.self="viewMail = null"
      style="position: fixed; inset: 0; background: rgba(15, 23, 42, 0.4); z-index: 60; display: flex; align-items: center; justify-content: center; padding: 24px">
      <div class="card" style="max-width: 680px; width: 100%; max-height: 80vh; overflow-y: auto; padding: 22px 24px">
        <div class="spread" style="margin-bottom: 10px; align-items: baseline">
          <h2 style="margin: 0">{{ viewMail.subject || "(kein Betreff)" }}</h2>
          <button class="btn" style="padding: 5px 12px" @click="viewMail = null">Schließen</button>
        </div>
        <div class="muted" style="font-size: 12.5px; margin-bottom: 12px">
          Von {{ viewMail.sender_email }} · {{ fmtDateTime(viewMail.received_at) }}
          <template v-if="viewMail.employee_name"> · zugeordnet: {{ viewMail.employee_name }}</template>
        </div>
        <div style="font-size: 14px; line-height: 1.6; white-space: pre-line; overflow-wrap: anywhere">
          {{ viewMail.body_preview || "(kein Text)" }}
        </div>
      </div>
    </div>
  </div>
</template>
