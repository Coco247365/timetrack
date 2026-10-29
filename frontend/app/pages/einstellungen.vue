<script setup lang="ts">
// Einstellungen: Lohnarten, Bereiche, NFC-Karten.
// Quellen: GET /admin/wage-types, GET /departments,
// GET /admin/scanned-cards (freie Karten) gemerged mit GET /admin/employees
// (Feld nfc_badge_id => zugewiesene Karten). Es gibt KEINEN kombinierten
// "alle Karten"-Endpunkt; die NFC-Sicht ist daher zusammengesetzt.
import { onBeforeRouteLeave } from "vue-router";
const { apiFetch } = useApi();

/* ---------- Typen ---------- */
interface WageType {
  internal_code: string;
  datev_lohnart: string | number | null;
  description: string;
  is_active: boolean;
  unit?: string;
}

// Einheit-Optionen für Lohnarten (hours|amount|days).
const wageUnitOptions: { value: string; label: string }[] = [
  { value: "hours", label: "Stunden" },
  { value: "amount", label: "Betrag" },
  { value: "days", label: "Tage" },
];
const wageUnitShort: Record<string, string> = { hours: "Std", amount: "Betrag", days: "Tage" };
interface Department {
  id: string;
  name: string;
  code: string;
  teamleiter_user_id?: string | null;
  teamleiter_name?: string | null;
  is_active?: boolean;
  company_id?: string | null;
  company_name?: string | null;
}
interface Employee {
  id: string;
  name: string;
  employee_number?: string | null;
  is_active?: boolean;
  department_name?: string | null;
  nfc_badge_id?: string | null;
  nfc_chip_uid?: string | null;
}
interface ScannedCard {
  uid: string;
  chip_uid?: string | null;
  [k: string]: any;
}

/* ---------- Lohnarten ---------- */
// Feinere Verwaltungs-Rechte: steuern, welche Sektionen bearbeitbar sind
// (Server erzwingt dieselben Regeln — das hier ist nur Sichtbarkeit/UX).
const { canManageRoles, canManageAccounts, canManageCards, canManageSettings, canManageFeatures, fetchFeatures: refreshGlobalFeatures } = useAuth();

/* ---------- Funktions-Schalter (optionale Bereiche) ---------- */
const featureFlags = ref<Record<string, boolean>>({});
const featuresOpen = ref(false); // Funktionen-Karte ein-/ausklappbar

/* ---------- Kalender-Abo (.ics-Feed) ---------- */
const icsFeedUrl = ref("");
const icsFeedOpen = ref(false); // Abo-Block ein-/ausklappbar
const icsFeedBusy = ref(false);
const icsFeedInfo = ref("");
async function loadIcsFeed() {
  try {
    const d = await apiFetch<{ path: string }>("/admin/ics-feed");
    icsFeedUrl.value = `${window.location.origin}${d.path}`;
  } catch { /* ohne admin:settings */ }
}
async function copyIcsFeed() {
  try {
    await navigator.clipboard.writeText(icsFeedUrl.value);
    icsFeedInfo.value = "Link kopiert.";
    setTimeout(() => { icsFeedInfo.value = ""; }, 3000);
  } catch { icsFeedInfo.value = "Kopieren fehlgeschlagen — Link manuell markieren."; }
}
async function rotateIcsFeed() {
  icsFeedBusy.value = true;
  try {
    const d = await apiFetch<{ path: string }>("/admin/ics-feed/rotate", { method: "POST" });
    icsFeedUrl.value = `${window.location.origin}${d.path}`;
    icsFeedInfo.value = "Neuer Link erzeugt — alte Abos funktionieren nicht mehr.";
  } catch (e: any) {
    icsFeedInfo.value = e?.data?.detail || "Fehler beim Erzeugen.";
  }
  icsFeedBusy.value = false;
}

/* ---------- Krankmeldungs-Postfaecher (mehrere moeglich) ---------- */
interface MailBox {
  id: string;
  provider: "m365" | "imap" | string;
  host?: string; port?: number | string; user?: string; folder?: string;
  tenant_id?: string; client_id?: string; mailbox?: string;
  has_password?: boolean; has_client_secret?: boolean; complete?: boolean;
  intake?: boolean;
}
const mailBoxes = ref<MailBox[]>([]);
const mailBoxesOpen = ref(false); // Postfach-Block ein-/ausklappbar
const mailEnvFallback = ref(false);
const mailCfgBusy = ref(false);
const mailCfgError = ref("");
const mailCfgInfo = ref("");
// Formular: null = zu, "new" = neues Postfach, sonst id des bearbeiteten.
const mailEditId = ref<string | null>(null);
const mailForm = ref<Record<string, any>>({ provider: "imap" });
const mailSecret = ref(""); // write-only

async function loadMailCfg() {
  try {
    const res = await apiFetch<{ boxes: MailBox[]; env_fallback: boolean }>("/admin/sickmail-config");
    mailBoxes.value = res.boxes || [];
    mailEnvFallback.value = !!res.env_fallback;
  } catch { /* ohne admin:settings nicht sichtbar */ }
}
function mailBoxLabel(b: MailBox): string {
  return b.provider === "m365" ? `M365 · ${b.mailbox || "?"}` : `IMAP · ${b.user || "?"} @ ${b.host || "?"}`;
}
function startMailEdit(b?: MailBox) {
  mailCfgError.value = ""; mailCfgInfo.value = ""; mailSecret.value = "";
  if (b) { mailEditId.value = b.id; mailForm.value = { ...b }; }
  else { mailEditId.value = "new"; mailForm.value = { provider: "imap", port: 993, folder: "INBOX", intake: true }; }
}
function cancelMailEdit() { mailEditId.value = null; mailSecret.value = ""; }
async function saveMailBox() {
  mailCfgBusy.value = true; mailCfgError.value = ""; mailCfgInfo.value = "";
  try {
    const f = mailForm.value;
    const body: Record<string, any> = { provider: f.provider, intake: f.intake !== false };
    if (mailEditId.value && mailEditId.value !== "new") body.id = mailEditId.value;
    if (f.provider === "imap") {
      body.host = f.host || ""; body.port = f.port || 993;
      body.user = f.user || ""; body.folder = f.folder || "INBOX";
      if (mailSecret.value) body.password = mailSecret.value;
    } else {
      body.tenant_id = f.tenant_id || ""; body.client_id = f.client_id || ""; body.mailbox = f.mailbox || "";
      if (mailSecret.value) body.client_secret = mailSecret.value;
    }
    const res = await apiFetch<{ box: MailBox }>("/admin/sickmail-config", { method: "PATCH", body });
    mailCfgInfo.value = !res.box?.complete
      ? "Gespeichert — noch unvollständig (Passwort/Secret fehlt?)."
      : res.box?.intake === false
        ? "Gespeichert — vollständig, nur zum Versand (kein Krankmail-Abruf)."
        : "Gespeichert — vollständig, wird für den Krankmail-Abruf gelesen.";
    cancelMailEdit();
    await loadMailCfg();
  } catch (e: any) {
    mailCfgError.value = e?.data?.detail || "Speichern fehlgeschlagen.";
  }
  mailCfgBusy.value = false;
}
async function deleteMailBox(b: MailBox) {
  mailCfgBusy.value = true; mailCfgError.value = ""; mailCfgInfo.value = "";
  try {
    await apiFetch(`/admin/sickmail-config/${b.id}`, { method: "DELETE" });
    if (mailEditId.value === b.id) cancelMailEdit();
    await loadMailCfg();
  } catch (e: any) {
    mailCfgError.value = e?.data?.detail || "Löschen fehlgeschlagen.";
  }
  mailCfgBusy.value = false;
}
async function testMailBox(b: MailBox) {
  mailCfgBusy.value = true; mailCfgError.value = ""; mailCfgInfo.value = "";
  try {
    const res = await apiFetch<{ detail: string }>(`/admin/sickmail-config/${b.id}/test`, { method: "POST" });
    mailCfgInfo.value = `${mailBoxLabel(b)}: ${res.detail}`;
  } catch (e: any) {
    mailCfgError.value = e?.data?.detail || "Test fehlgeschlagen.";
  }
  mailCfgBusy.value = false;
}
const featureBusy = ref(false);
const featureError = ref("");
async function loadFeatures() {
  try {
    featureFlags.value = await apiFetch<Record<string, boolean>>("/admin/features");
  } catch { /* optional */ }
}
async function setFeature(key: string, on: boolean) {
  featureBusy.value = true;
  featureError.value = "";
  try {
    featureFlags.value = await apiFetch<Record<string, boolean>>("/admin/features", {
      method: "PATCH",
      body: { [key]: on },
    });
    await refreshGlobalFeatures(); // Navigation sofort aktualisieren
  } catch (e: any) {
    featureError.value = e?.data?.detail || "Einstellung konnte nicht gespeichert werden.";
  }
  featureBusy.value = false;
}

// Teamkalender-Markierung als 3-Stufen-Auswahl über zwei Flags:
// off = team_past_worked aus; all = an + stamped_only aus;
// stamped = an + stamped_only an (#191).
const teamPastWorkedMode = computed(() => {
  if (featureFlags.value.team_past_worked === false) return "off";
  return featureFlags.value.team_past_worked_stamped_only === true ? "stamped" : "all";
});
async function setTeamPastWorkedMode(mode: string) {
  featureBusy.value = true;
  featureError.value = "";
  try {
    featureFlags.value = await apiFetch<Record<string, boolean>>("/admin/features", {
      method: "PATCH",
      body: {
        team_past_worked: mode !== "off",
        team_past_worked_stamped_only: mode === "stamped",
      },
    });
    await refreshGlobalFeatures();
  } catch (e: any) {
    featureError.value = e?.data?.detail || "Einstellung konnte nicht gespeichert werden.";
  }
  featureBusy.value = false;
}

/* ---------- Urlaubsanspruch-Default (7tu: mandantenweit konfigurierbar) ---------- */
interface VacationSettings {
  default_vacation_entitlement_days: number;
  is_custom: boolean;
  fallback_default: number;
}
const vacationSettings = ref<VacationSettings | null>(null);
const vacationDraft = ref("");
const vacationBusy = ref(false);
const vacationError = ref("");

async function loadVacationSettings() {
  try {
    vacationSettings.value = await apiFetch<VacationSettings>("/admin/vacation-settings");
    vacationDraft.value = String(vacationSettings.value.default_vacation_entitlement_days);
  } catch { /* optional */ }
}
async function saveVacationDefault() {
  vacationBusy.value = true;
  vacationError.value = "";
  try {
    vacationSettings.value = await apiFetch<VacationSettings>("/admin/vacation-settings", {
      method: "PATCH",
      body: { default_vacation_entitlement_days: Number(vacationDraft.value) },
    });
    vacationDraft.value = String(vacationSettings.value.default_vacation_entitlement_days);
  } catch (e: any) {
    vacationError.value = e?.data?.detail || "Einstellung konnte nicht gespeichert werden.";
  }
  vacationBusy.value = false;
}
async function resetVacationDefault() {
  vacationBusy.value = true;
  vacationError.value = "";
  try {
    vacationSettings.value = await apiFetch<VacationSettings>("/admin/vacation-settings", {
      method: "PATCH",
      body: { default_vacation_entitlement_days: null },
    });
    vacationDraft.value = String(vacationSettings.value.default_vacation_entitlement_days);
  } catch (e: any) {
    vacationError.value = e?.data?.detail || "Einstellung konnte nicht zurückgesetzt werden.";
  }
  vacationBusy.value = false;
}

const wageTypes = ref<WageType[]>([]);
const wageError = ref("");
const wageLoading = ref(true);
const wageEdit = ref(false);
const wageDraft = ref<WageType[]>([]);
const wageSaving = ref(false);
const wageOpen = ref(false); // Lohnarten-Karte ein-/ausklappbar

async function loadWageTypes() {
  wageLoading.value = true;
  wageError.value = "";
  try {
    wageTypes.value = await apiFetch<WageType[]>("/admin/wage-types");
  } catch (e: any) {
    wageError.value = e?.data?.detail || "Lohnarten konnten nicht geladen werden.";
  }
  wageLoading.value = false;
}

// Immer nur EINE Sektion im Bearbeiten-Modus: andere beim Öffnen schließen.
function closeOtherSettingsEdits(keep: "wage" | "surcharge" | "wtprofiles" | "dep" | "company" | "roles" | "accounts" | "features") {
  if (keep !== "wage" && wageEdit.value) cancelWageEdit();
  if (keep !== "surcharge" && surchargeEdit.value) cancelSurchargeEdit();
  if (keep !== "wtprofiles" && wtpEditId.value) cancelWtpEdit();
  if (keep !== "dep" && depEdit.value) cancelDepEdit();
  if (keep !== "company" && companyEdit.value) cancelCompanyEdit();
  if (keep !== "roles" && rolesEdit.value) cancelRolesEdit();
  if (keep !== "accounts" && accountsEdit.value) cancelAccountsEdit();
}

// Akkordeon: immer nur EINE Karte offen. Andere klappen ein -> deren
// Collapse-Watches beenden automatisch deren Bearbeiten-Modus.
function toggleSection(name: "wage" | "surcharge" | "wtprofiles" | "dep" | "company" | "cards" | "roles" | "accounts" | "features") {
  const isOpen = {
    wage: wageOpen.value, surcharge: surchargeOpen.value, wtprofiles: wtpOpen.value, dep: depOpen.value,
    company: companiesOpen.value, cards: cardsOpen.value, roles: rolesOpen.value,
    accounts: accountsOpen.value, features: featuresOpen.value,
  }[name];
  wageOpen.value = false;
  surchargeOpen.value = false;
  wtpOpen.value = false;
  depOpen.value = false;
  companiesOpen.value = false;
  cardsOpen.value = false;
  rolesOpen.value = false;
  accountsOpen.value = false;
  featuresOpen.value = false;
  if (isOpen) return; // war offen -> jetzt zu (alles bleibt zu)
  if (name === "wage") wageOpen.value = true;
  else if (name === "surcharge") surchargeOpen.value = true;
  else if (name === "wtprofiles") wtpOpen.value = true;
  else if (name === "dep") depOpen.value = true;
  else if (name === "company") companiesOpen.value = true;
  else if (name === "cards") cardsOpen.value = true;
  else if (name === "roles") rolesOpen.value = true;
  else if (name === "accounts") accountsOpen.value = true;
  else if (name === "features") featuresOpen.value = true;
}

function startWageEdit() {
  closeOtherSettingsEdits("wage");
  wageDraft.value = wageTypes.value.map((w) => ({ ...w }));
  wageEdit.value = true;
}
function addWageRow() {
  wageDraft.value.push({ internal_code: "", datev_lohnart: "", description: "", is_active: true, unit: "hours" });
}
async function saveWageType(w: WageType) {
  wageSaving.value = true;
  wageError.value = "";
  try {
    await apiFetch("/admin/wage-types", {
      method: "POST",
      body: {
        internal_code: w.internal_code,
        // Nummer ist optional: leer/fehlend -> null (Lohnart wird dann nicht exportiert).
        datev_lohnart: w.datev_lohnart === "" || w.datev_lohnart == null ? null : w.datev_lohnart,
        description: w.description,
        is_active: w.is_active,
        unit: w.unit || "hours",
      },
    });
    await loadWageTypes();
    if (wageEdit.value) wageDraft.value = wageTypes.value.map((x) => ({ ...x }));
  } catch (e: any) {
    wageError.value = e?.data?.detail || "Lohnart konnte nicht gespeichert werden.";
  }
  wageSaving.value = false;
}
async function deactivateWageType(w: WageType) {
  await saveWageType({ ...w, is_active: false });
}
async function deleteWageType(w: WageType) {
  if (!w.internal_code) return;
  wageSaving.value = true;
  wageError.value = "";
  try {
    await apiFetch(`/admin/wage-types/${encodeURIComponent(w.internal_code)}`, { method: "DELETE" });
    await loadWageTypes();
    if (wageEdit.value) wageDraft.value = wageTypes.value.map((x) => ({ ...x }));
  } catch (e: any) {
    wageError.value = e?.data?.detail || "Lohnart konnte nicht gelöscht werden.";
  }
  wageSaving.value = false;
}
function finishWageEdit() {
  wageEdit.value = false;
  wageDraft.value = [];
}
function cancelWageEdit() {
  // Bearbeiten verlassen + nicht gespeicherte Entwürfe verwerfen.
  wageDraft.value = [];
  wageEdit.value = false;
}

/* ---------- Zuschlags-Zeiten ---------- */
interface SurchargeConfig {
  night_start: number;
  night_end: number;
  saturday_from: number;
  sunday_from: number;
  export_basis?: string;
  mehrarbeit_frei_wochen_std?: number;
}
const surchargeConfig = ref<SurchargeConfig | null>(null);
const surchargeError = ref("");
const surchargeLoading = ref(true);
const surchargeEdit = ref(false);
const surchargeDraft = ref<SurchargeConfig>({
  night_start: 23, night_end: 6, saturday_from: 0, sunday_from: 0,
  export_basis: "ist", mehrarbeit_frei_wochen_std: 1.5,
});
const surchargeSaving = ref(false);
const surchargeOpen = ref(false); // Zuschlags-Zeiten-Karte ein-/ausklappbar

// Stunden-Optionen 0–23 für die Selects ("0 Uhr" … "23 Uhr").
const hourOptions: { value: number; label: string }[] = Array.from(
  { length: 24 },
  (_, h) => ({ value: h, label: `${h} Uhr` })
);

async function loadSurchargeConfig() {
  surchargeLoading.value = true;
  surchargeError.value = "";
  try {
    surchargeConfig.value = await apiFetch<SurchargeConfig>("/admin/surcharge-config");
  } catch (e: any) {
    surchargeError.value = e?.data?.detail || "Zuschlags-Zeiten konnten nicht geladen werden.";
  }
  surchargeLoading.value = false;
}

function startSurchargeEdit() {
  closeOtherSettingsEdits("surcharge");
  if (surchargeConfig.value) surchargeDraft.value = { ...surchargeConfig.value };
  surchargeEdit.value = true;
}
async function saveSurchargeConfig() {
  surchargeSaving.value = true;
  surchargeError.value = "";
  try {
    await apiFetch("/admin/surcharge-config", {
      method: "PATCH",
      body: {
        night_start: surchargeDraft.value.night_start,
        night_end: surchargeDraft.value.night_end,
        saturday_from: surchargeDraft.value.saturday_from,
        sunday_from: surchargeDraft.value.sunday_from,
      },
    });
    await loadSurchargeConfig();
    surchargeEdit.value = false;
  } catch (e: any) {
    surchargeError.value = e?.data?.detail || "Zuschlags-Zeiten konnten nicht gespeichert werden.";
  }
  surchargeSaving.value = false;
}
function cancelSurchargeEdit() {
  // Bearbeiten verlassen + nicht gespeicherten Entwurf verwerfen.
  if (surchargeConfig.value) surchargeDraft.value = { ...surchargeConfig.value };
  surchargeEdit.value = false;
}

/* ---------- Arbeitszeit-Profile ---------- */
// Ersetzt die frühere Karte „Arbeitszeitmodelle" (informative Tenant-Settings
// work_schedules). Zwei streng getrennte Typen: 'nfc' = Kappungsfenster für
// Stempler (NFC/Web), 'auto' = Wochentagsplan für Auto-Erfassung. Zuweisung
// beim Mitarbeiter (Stammdaten → Arbeitszeit-Profil), typgebunden (Server: 422).
// Quelle: GET/POST /admin/work-time-profiles, PATCH/DELETE …/{id} (409 bei
// Löschung mit zugewiesenen Mitarbeitern).
interface WtpWeekdayTime { start: string; end: string; break_start?: string | null; break_end?: string | null }
interface WorkTimeProfile {
  id: string;
  name: string;
  kind: "nfc" | "auto";
  is_active: boolean;
  clock_start?: string | null;
  clock_end?: string | null;
  weekday_times?: Record<string, WtpWeekdayTime | null> | null;
  // Nachtrag 14.08.: JSON-String mit 2 Nachkommastellen (z. B. "38.50").
  // nfc = gespeichert (null = Bestandsprofil ohne Stundenbindung),
  // auto = vom Server BERECHNET aus weekday_times (readonly, null ohne Arbeitstage).
  contracted_hours_per_week?: string | null;
  // Nur nfc: Tages-Soll je Wochentag in Minuten ("0".."6", fehlender Key = 0).
  weekday_target_minutes?: Record<string, number> | null;
  assigned_count?: number;
}
interface WtpDayForm { enabled: boolean; start: string; end: string; break_start: string; break_end: string }

const WTP_WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const WTP_HHMM_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
const wtProfiles = ref<WorkTimeProfile[]>([]);
const wtpOpen = ref(false); // Karte ein-/ausklappbar
const wtpLoading = ref(true);
const wtpError = ref(""); // Fehler der Liste (Löschen, z. B. 409)
const wtpFormError = ref(""); // Fehler im Editor (Validierung, 422)
const wtpBusy = ref(false);
const wtpEditId = ref<string | null>(null); // null = zu, "new" = anlegen, sonst Profil-Id
const wtpDeleteConfirmId = ref<string | null>(null);
// Mitglieder-Panel je Profil (Muster Abrechnungsprofile): Chips + Zuweisen.
const wtpMembersOpen = ref<Record<string, boolean>>({});
const wtpAddSel = ref<Record<string, string>>({});
const wtpRemoveConfirmId = ref<string | null>(null); // Mitarbeiter-Id mit offener Rückfrage

// Mitglieder eines Arbeitszeit-Profils (nutzt dieselbe Mitarbeiterliste wie
// die Abrechnungsprofile — /admin/employees liefert work_time_profile_id mit).
function wtpMembers(p: WorkTimeProfile): ProfileEmployee[] {
  return profileEmployees.value
    .filter((e) => e.work_time_profile_id === p.id)
    .sort((a, b) => a.name.localeCompare(b.name, "de"));
}
// Stunden-Bindung eines Profils (Nachtrag 14.08.): Response-String -> Zahl.
// null = Profil bindet nicht (nfc-Bestand ohne Stunden, auto ohne Arbeitstage).
function wtpBoundHours(p: WorkTimeProfile): number | null {
  if (p.contracted_hours_per_week == null) return null;
  const n = parseFloat(p.contracted_hours_per_week);
  return Number.isFinite(n) ? n : null;
}
// Stunden deutsch formatieren: 40 -> "40", 38.5 -> "38,5".
function wtpFmtHours(n: number): string {
  return String(Math.round(n * 100) / 100).replace(".", ",");
}
// Zusatz für die Hinweiszeile unterm Zuweisen-Dropdown ("… und 40 h/Woche").
function wtpHoursHint(p: WorkTimeProfile): string {
  const bound = wtpBoundHours(p);
  return bound != null ? ` und ${wtpFmtHours(bound)} h/Woche` : "";
}
// Typ-Bindung im Dropdown spiegeln: NFC-Profile für Stempler (nfc/web),
// Auto-Profile nur für Auto-Erfasste. Profile mit Vertragsstunden zusätzlich
// nur für Mitarbeiter mit exakt passenden Stunden. Der Server erzwingt beides (422).
function wtpCandidates(p: WorkTimeProfile): ProfileEmployee[] {
  const bound = wtpBoundHours(p);
  return profileEmployees.value
    .filter((e) => {
      if (e.work_time_profile_id === p.id) return false;
      const tc = (e.time_capture || "").toLowerCase();
      const modeOk = p.kind === "auto" ? tc === "auto" : tc === "nfc" || tc === "web";
      if (!modeOk) return false;
      return bound == null || Number(e.contracted_hours_per_week) === bound;
    })
    .sort((a, b) => a.name.localeCompare(b.name, "de"));
}
function wtpCurrentName(e: ProfileEmployee): string {
  if (!e.work_time_profile_id) return "—";
  return wtProfiles.value.find((p) => p.id === e.work_time_profile_id)?.name || "—";
}
async function wtpAssign(empId: string, profile: WorkTimeProfile | null) {
  wtpBusy.value = true;
  wtpError.value = "";
  try {
    await apiFetch(`/users/${empId}`, {
      method: "PATCH",
      body: { work_time_profile_id: profile ? profile.id : null },
    });
    await Promise.all([loadWtProfiles(), loadWageProfiles()]); // Zähler + Mitarbeiterliste
  } catch (e: any) {
    wtpError.value = wtpDetailText(e, "Zuweisung konnte nicht gespeichert werden.");
  }
  wtpBusy.value = false;
}

// Startwerte für den Wochentagsplan: Mo–Fr aktiv, Wochenende frei.
function emptyWtpDays(): WtpDayForm[] {
  return Array.from({ length: 7 }, (_, i) => ({
    enabled: i < 5, start: "07:00", end: "15:30", break_start: "", break_end: "",
  }));
}
// Tages-Soll (nfc) als Std:Min je Wochentag — gleiche Eingabeform wie das
// Stammdaten-Wochentagssoll (mitarbeiter/[id].vue), intern Minuten.
function emptyWtpTargets(): { h: number; m: number }[] {
  return Array.from({ length: 7 }, () => ({ h: 0, m: 0 }));
}
const wtpForm = ref({
  name: "",
  kind: "nfc" as "nfc" | "auto",
  is_active: true,
  clock_start: "",
  clock_end: "",
  contracted_hours: "" as string | number, // Vertragsstunden/Woche (nur nfc)
  targets: emptyWtpTargets(), // Tages-Soll Mo–So (nur nfc)
  days: emptyWtpDays(),
});
// Bestandsprofil ohne Stundenbindung darf beim Bearbeiten ohne Stunden bleiben;
// bei Neuanlage und bei Profilen MIT Stunden sind die Felder Pflicht.
const wtpEditHadHours = ref(false);

function fmtMinHM(min: number): string {
  const neg = min < 0 ? "-" : "";
  const v = Math.abs(Math.round(min));
  return `${neg}${Math.floor(v / 60)}:${String(v % 60).padStart(2, "0")}`;
}
// Summe der Tagessoll-Eingaben (Minuten) — Live-Anzeige + Validierung.
const wtpTargetSumMin = computed(() =>
  wtpForm.value.targets.reduce((s, d) => s + (Number(d.h) || 0) * 60 + (Number(d.m) || 0), 0)
);
// Vertragsstunden-Eingabe -> Zahl (Komma erlaubt); null = leer/ungültig.
const wtpContractedNum = computed<number | null>(() => {
  const raw = String(wtpForm.value.contracted_hours ?? "").trim().replace(",", ".");
  if (!raw) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
});
// Weicht die Summe der Tagessolls von Vertragsstunden × 60 ab? (Warnfarbe)
const wtpSumMismatch = computed(() => {
  const n = wtpContractedNum.value;
  return n != null && wtpTargetSumMin.value !== Math.round(n * 60);
});

// ArbZG §4 wie backend policy/statutory.effective_break_minutes (netto-basiert):
// >6 h Arbeitszeit -> 30 min, >9 h -> 45 min; angehoben nur so weit wie nötig.
function wtpEffectiveBreak(presenceMin: number, breakWindowMin: number): number {
  for (const cand of [breakWindowMin, Math.max(breakWindowMin, 30), Math.max(breakWindowMin, 45)]) {
    const net = presenceMin - cand;
    if (net <= 360 || (net <= 540 && cand >= 30) || cand >= 45) return cand;
  }
  return Math.max(breakWindowMin, 45);
}
function wtpHhmmToMin(t: string): number | null {
  if (!WTP_HHMM_RE.test(t)) return null;
  return Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5));
}
// Auto-Profil: Wochenstunden live aus den Tagesfenstern — gleiche Rechnung wie
// das Backend (AutoDay.target_minutes: Fenster − Pausenfenster, mindestens die
// gesetzliche Pflichtpause). null = kein gültiger Arbeitstag.
const wtpAutoWeekHours = computed<string | null>(() => {
  let total = 0;
  let any = false;
  for (const d of wtpForm.value.days) {
    if (!d.enabled) continue;
    const s = wtpHhmmToMin(d.start);
    const e = wtpHhmmToMin(d.end);
    if (s == null || e == null || e <= s) continue;
    any = true;
    const presence = e - s;
    const bs = d.break_start ? wtpHhmmToMin(d.break_start) : null;
    const be = d.break_end ? wtpHhmmToMin(d.break_end) : null;
    const breakWindow = bs != null && be != null && be > bs ? be - bs : 0;
    total += Math.max(0, presence - wtpEffectiveBreak(presence, breakWindow));
  }
  return any ? wtpFmtHours(total / 60) : null;
});

// 422/409-Detail kann String (Service) oder Liste (Pydantic) sein.
function wtpDetailText(e: any, fallback: string): string {
  const d = e?.data?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x: any) => x?.msg || "").filter(Boolean).join(" · ") || fallback;
  return fallback;
}

async function loadWtProfiles() {
  wtpLoading.value = true;
  try {
    const d = await apiFetch<any>("/admin/work-time-profiles");
    // Tolerant gegenüber Liste bzw. { profiles: [...] } als Antwortform.
    wtProfiles.value = Array.isArray(d) ? d : (d?.profiles || []);
  } catch { /* ohne admin:settings */ }
  wtpLoading.value = false;
}

// Zeiten-Kurzform für die Liste: NFC = Fenster (ggf. Nachtschicht),
// Auto = aufeinanderfolgende Tage mit gleichen Zeiten gruppiert.
function wtpTimesSummary(p: WorkTimeProfile): string {
  if (p.kind === "nfc") {
    if (!p.clock_start && !p.clock_end) return "keine Kappung";
    const night = !!(p.clock_start && p.clock_end && p.clock_end < p.clock_start);
    return `${p.clock_start || "—"}–${p.clock_end || "—"}${night ? " (Nachtschicht)" : ""}`;
  }
  const keyOf = (i: number): string | null => {
    const d = p.weekday_times?.[String(i)];
    if (!d) return null;
    const br = d.break_start && d.break_end ? ` (Pause ${d.break_start}–${d.break_end})` : "";
    return `${d.start}–${d.end}${br}`;
  };
  const parts: string[] = [];
  let i = 0;
  while (i < 7) {
    const k = keyOf(i);
    if (!k) { i++; continue; }
    let j = i;
    while (j + 1 < 7 && keyOf(j + 1) === k) j++;
    parts.push(`${j > i ? `${WTP_WEEKDAYS[i]}–${WTP_WEEKDAYS[j]}` : WTP_WEEKDAYS[i]} ${k}`);
    i = j + 1;
  }
  return parts.join(" · ") || "keine Arbeitstage";
}

// Stunden-Kurzform für die Liste: "· 40 h/Woche" bzw. bei NFC-Bestand ohne
// Stunden "· ohne Stundenbindung" (auto ohne Arbeitstage: nichts).
function wtpHoursSummary(p: WorkTimeProfile): string {
  const bound = wtpBoundHours(p);
  if (bound != null) return `· ${wtpFmtHours(bound)} h/Woche`;
  return p.kind === "nfc" ? "· ohne Stundenbindung" : "";
}

function startWtpEdit(p?: WorkTimeProfile) {
  closeOtherSettingsEdits("wtprofiles");
  wtpFormError.value = "";
  wtpDeleteConfirmId.value = null;
  if (p) {
    const days = emptyWtpDays().map((d) => ({ ...d, enabled: false }));
    for (let i = 0; i < 7; i++) {
      const t = p.weekday_times?.[String(i)];
      if (t) days[i] = { enabled: true, start: t.start, end: t.end, break_start: t.break_start || "", break_end: t.break_end || "" };
    }
    const targets = emptyWtpTargets();
    if (p.kind === "nfc" && p.weekday_target_minutes) {
      for (let i = 0; i < 7; i++) {
        const v = p.weekday_target_minutes[String(i)] || 0;
        targets[i] = { h: Math.floor(v / 60), m: v % 60 };
      }
    }
    const bound = p.kind === "nfc" ? wtpBoundHours(p) : null;
    wtpEditHadHours.value = bound != null;
    wtpForm.value = {
      name: p.name, kind: p.kind, is_active: p.is_active,
      clock_start: p.clock_start || "", clock_end: p.clock_end || "",
      contracted_hours: bound != null ? String(bound) : "",
      targets, days,
    };
    wtpEditId.value = p.id;
  } else {
    wtpEditHadHours.value = false;
    wtpForm.value = {
      name: "", kind: "nfc", is_active: true, clock_start: "", clock_end: "",
      contracted_hours: "", targets: emptyWtpTargets(), days: emptyWtpDays(),
    };
    wtpEditId.value = "new";
  }
}
function cancelWtpEdit() { wtpEditId.value = null; wtpFormError.value = ""; }

// Client-Validierung — spiegelt die Server-Regeln (Pydantic), ersetzt sie nicht.
function wtpValidate(): string | null {
  const f = wtpForm.value;
  if (!f.name.trim()) return "Name erforderlich.";
  if (f.kind === "nfc") {
    if (f.clock_start && !WTP_HHMM_RE.test(f.clock_start)) return `„Stempelung ab": Zeit im Format HH:MM angeben.`;
    if (f.clock_end && !WTP_HHMM_RE.test(f.clock_end)) return `„Stempelung bis": Zeit im Format HH:MM angeben.`;
    // über Mitternacht (bis < ab) ist erlaubt = Nachtschicht
    // Vertragsstunden + Tagessoll (Nachtrag 14.08.) — spiegelt die Server-Regel.
    const hasHours = String(f.contracted_hours ?? "").trim() !== "";
    const anyTarget = wtpTargetSumMin.value > 0;
    if (!hasHours && !anyTarget && wtpEditId.value !== "new" && !wtpEditHadHours.value) {
      return null; // Bestandsprofil ohne Stundenbindung darf so bleiben
    }
    if (!hasHours) return `„Vertragsstunden/Woche" erforderlich (zusammen mit dem Tagessoll Mo–So).`;
    const n = wtpContractedNum.value;
    if (n == null || n <= 0 || n > 80) return `„Vertragsstunden/Woche": Wert über 0 bis 80 angeben.`;
    if (Math.round(n * 100) !== n * 100) return `„Vertragsstunden/Woche": höchstens 2 Nachkommastellen.`;
    for (let i = 0; i < 7; i++) {
      const d = f.targets[i]!;
      const h = Number(d.h), m = Number(d.m);
      if (!Number.isInteger(h) || h < 0 || h > 24 || !Number.isInteger(m) || m < 0 || m > 59) {
        return `Tagessoll ${WTP_WEEKDAYS[i]}: Stunden 0–24 und Minuten 0–59 angeben.`;
      }
    }
    const expected = Math.round(n * 60);
    const sum = wtpTargetSumMin.value;
    if (sum !== expected) {
      const diff = expected - sum;
      const hint = diff > 0 ? `fehlen ${fmtMinHM(diff)}` : `${fmtMinHM(-diff)} zu viel`;
      return `Tagessoll-Summe ${fmtMinHM(sum)} h passt nicht zu ${wtpFmtHours(n)} h Vertragsstunden (${hint}).`;
    }
    return null;
  }
  let anyDay = false;
  for (let i = 0; i < 7; i++) {
    const d = f.days[i]!;
    if (!d.enabled) continue;
    anyDay = true;
    const wd = WTP_WEEKDAYS[i];
    if (!WTP_HHMM_RE.test(d.start) || !WTP_HHMM_RE.test(d.end)) return `${wd}: von/bis im Format HH:MM angeben.`;
    if (d.start >= d.end) return `${wd}: „von" muss vor „bis" liegen.`;
    if (!!d.break_start !== !!d.break_end) return `${wd}: Pause braucht von UND bis (oder beide Felder leer).`;
    if (d.break_start) {
      if (!WTP_HHMM_RE.test(d.break_start) || !WTP_HHMM_RE.test(d.break_end)) return `${wd}: Pausenzeiten im Format HH:MM angeben.`;
      if (d.break_start >= d.break_end) return `${wd}: Pause „von" muss vor „bis" liegen.`;
      if (d.break_start < d.start || d.break_end > d.end) return `${wd}: Pause muss im Arbeitsfenster liegen.`;
    }
  }
  return anyDay ? null : "Mindestens einen Wochentag aktivieren.";
}

async function saveWtp() {
  const err = wtpValidate();
  if (err) { wtpFormError.value = err; return; }
  wtpBusy.value = true;
  wtpFormError.value = "";
  const f = wtpForm.value;
  const body: Record<string, any> = { name: f.name.trim(), is_active: f.is_active };
  if (f.kind === "nfc") {
    body.clock_start = f.clock_start || null; // leer = keine Kappung an dieser Seite
    body.clock_end = f.clock_end || null;
    // Vertragsstunden + Tagessoll nur gemeinsam senden (Server-Regel); leer =
    // Bestandsprofil ohne Stundenbindung bleibt unangetastet (PATCH lässt weg).
    if (String(f.contracted_hours ?? "").trim() !== "") {
      body.contracted_hours_per_week = String(f.contracted_hours).trim().replace(",", ".");
      const tm: Record<string, number> = {};
      f.targets.forEach((d, i) => { tm[String(i)] = (Number(d.h) || 0) * 60 + (Number(d.m) || 0); });
      body.weekday_target_minutes = tm;
    }
  } else {
    const wt: Record<string, WtpWeekdayTime | null> = {};
    f.days.forEach((d, i) => {
      wt[String(i)] = d.enabled
        ? { start: d.start, end: d.end, break_start: d.break_start || null, break_end: d.break_end || null }
        : null; // null = frei (Soll 0)
    });
    body.weekday_times = wt;
  }
  try {
    if (wtpEditId.value === "new") {
      body.kind = f.kind; // Typ nur beim Anlegen — danach fest
      await apiFetch("/admin/work-time-profiles", { method: "POST", body });
    } else {
      await apiFetch(`/admin/work-time-profiles/${wtpEditId.value}`, { method: "PATCH", body });
    }
    wtpEditId.value = null;
    await loadWtProfiles();
  } catch (e: any) {
    wtpFormError.value = wtpDetailText(e, "Profil konnte nicht gespeichert werden.");
  }
  wtpBusy.value = false;
}

async function deleteWtp(p: WorkTimeProfile) {
  wtpBusy.value = true;
  wtpError.value = "";
  try {
    await apiFetch(`/admin/work-time-profiles/${p.id}`, { method: "DELETE" });
    if (wtpEditId.value === p.id) cancelWtpEdit();
    // Löschen löst die Zuweisungen (wie Abrechnungsprofile) → auch die
    // Mitarbeiterliste der Chips neu laden.
    await Promise.all([loadWtProfiles(), loadWageProfiles()]);
  } catch (e: any) {
    wtpError.value = wtpDetailText(e, "Profil konnte nicht gelöscht werden.");
  }
  wtpBusy.value = false;
}

/* ---------- Monatsabschluss zurücksetzen & neu ausführen ---------- */
const monthNames = [
  "Januar", "Februar", "März", "April", "Mai", "Juni",
  "Juli", "August", "September", "Oktober", "November", "Dezember",
];
// Default = Vormonat.
const _resettleDefault = new Date();
_resettleDefault.setMonth(_resettleDefault.getMonth() - 1);
const resettleMonth = ref(_resettleDefault.getMonth() + 1);
const resettleYear = ref(_resettleDefault.getFullYear());
const resettleConfirm = ref(false);
// Erst NACH Monatsende: der laufende Monat rechnet ohnehin laufend mit, ein Lauf
// mitten im Monat wuerde Teilwerte einfrieren (Server lehnt das ebenfalls ab).
const _jetzt = new Date();
const neuberechnenGesperrt = computed(
  () => resettleYear.value > _jetzt.getFullYear()
    || (resettleYear.value === _jetzt.getFullYear()
        && resettleMonth.value >= _jetzt.getMonth() + 1)
);
const neuberechnenHinweis = computed(() =>
  neuberechnenGesperrt.value
    ? "Nur abgelaufene Monate — der laufende Monat rechnet automatisch mit und lässt sich ab dem 1. des Folgemonats neu berechnen."
    : "Wertet die Zeiten des Monats noch einmal aus — nötig, wenn Stempelzeiten, Abwesenheiten oder Anfangsbestände nachträglich geändert wurden."
);
const resettleBusy = ref(false);
const resettleError = ref("");
const resettleDone = ref(false);
interface ResettleResult {
  employee_id: string;
  employee_number: string | null;
  alte_auszahlung: number;
  neue_auszahlung: number;
}
const resettleResult = ref<ResettleResult[]>([]);
const resettleChanged = computed(() =>
  resettleResult.value.filter((r) => Math.abs(r.alte_auszahlung - r.neue_auszahlung) > 0.005)
);

async function runResettle() {
  resettleBusy.value = true;
  resettleError.value = "";
  resettleDone.value = false;
  try {
    resettleResult.value = await apiFetch<ResettleResult[]>("/admin/overtime/resettle", {
      method: "POST",
      body: { year: resettleYear.value, month: resettleMonth.value, employee_id: null },
    });
    resettleDone.value = true;
  } catch (e: any) {
    resettleError.value = e?.data?.detail || "Überstunden konnten nicht neu berechnet werden.";
  }
  resettleConfirm.value = false;
  resettleBusy.value = false;
}

/* ---------- Lohnart-Zuordnung (System-Kategorien -> Katalog) ---------- */
interface AssignCategory { code: string; label: string; unit?: string; assigned: string | null }
interface AssignOption { internal_code: string; datev_lohnart: number; description?: string | null }
const assignCategories = ref<AssignCategory[]>([]);
const assignOptions = ref<AssignOption[]>([]);
const assignError = ref("");
const assignBusy = ref(false);

async function loadAssignments() {
  try {
    const d = await apiFetch<{ categories: AssignCategory[]; options: AssignOption[] }>(
      "/admin/wage-type-assignments"
    );
    assignCategories.value = d.categories || [];
    assignOptions.value = d.options || [];
  } catch (e: any) {
    assignError.value = e?.data?.detail || "Zuordnung konnte nicht geladen werden.";
  }
}
/* ---------- Abrechnungsprofile (abweichende Zuordnungen pro Gruppe) ---------- */
interface WageProfile {
  id: string;
  name: string;
  mapping: Record<string, string>;
  employee_count?: number;
  is_standard?: boolean;
  fruehester_beginn?: string | null;
  mehrarbeit_frei_wochen_std?: number | null;
  wochenende_ins_konto?: boolean;
}
const wageProfiles = ref<WageProfile[]>([]);
const profileEditId = ref<string | null>(null); // "new" oder Profil-id
const profileForm = ref<{
  name: string;
  mapping: Record<string, string>;
  fruehester_beginn: string;
  mehrarbeit_frei_wochen_std: string;
  wochenende_ins_konto: boolean;
}>({ name: "", mapping: {}, fruehester_beginn: "", mehrarbeit_frei_wochen_std: "", wochenende_ins_konto: false });
const profileBusy = ref(false);
const profileError = ref("");

interface ProfileEmployee {
  id: string;
  name: string;
  is_active?: boolean;
  wage_profile_id?: string | null;
  // Für die Arbeitszeit-Profil-Karte (gleiche Liste, gleiche Chips-Mechanik):
  time_capture?: string | null;
  work_time_profile_id?: string | null;
  // Stunden-Bindung: Profile mit Vertragsstunden nur bei exakter Übereinstimmung.
  contracted_hours_per_week?: number | null;
}
const profileEmployees = ref<ProfileEmployee[]>([]);
const profMembersOpen = ref<Record<string, boolean>>({});
const profAddSel = ref<Record<string, string>>({});
const profRemoveConfirmId = ref<string | null>(null); // Mitarbeiter-Id mit offener Rückfrage
const profDeleteConfirmId = ref<string | null>(null); // Profil-Id mit offener Lösch-Rückfrage

async function loadWageProfiles() {
  try {
    const d = await apiFetch<{ profiles: WageProfile[] }>("/admin/wage-profiles");
    wageProfiles.value = d.profiles || [];
    const emps = await apiFetch<ProfileEmployee[]>("/admin/employees");
    profileEmployees.value = emps.filter((e) => e.is_active !== false);
  } catch { /* ohne admin:settings */ }
}
// Mitglieder eines Profils; Standard = alle ohne eigenes Profil.
function profileMembers(p: WageProfile): ProfileEmployee[] {
  return profileEmployees.value
    .filter((e) => (p.is_standard ? !e.wage_profile_id : e.wage_profile_id === p.id))
    .sort((a, b) => a.name.localeCompare(b.name, "de"));
}
function currentProfileName(e: ProfileEmployee): string {
  if (!e.wage_profile_id) return "Standard";
  return wageProfiles.value.find((p) => p.id === e.wage_profile_id)?.name || "Standard";
}
// Label fuer eine Zuordnungs-Option (Nummer + Bezeichnung), z.B. fuer die
// Anzeige des vom Standard-Profil geerbten Werts in anderen Profilen.
function wageOptionLabel(code: string): string {
  const o = assignOptions.value.find((x) => x.internal_code === code);
  if (!o) return "";
  return `${o.datev_lohnart} · ${o.description || o.internal_code}`;
}
// Text fuer die leere Auswahl-Option im Profil-Editor: bei Nicht-Standard-Profilen
// den tatsaechlich geerbten Wert aus dem Standard-Profil anzeigen statt "wie Standard".
function inheritedMappingLabel(categoryCode: string): string {
  const standardCode = wageProfiles.value.find((p) => p.is_standard)?.mapping?.[categoryCode];
  if (!standardCode) return "— nicht zugeordnet —";
  return wageOptionLabel(standardCode) || "— nicht zugeordnet —";
}
async function moveEmployeeToProfile(empId: string, profile: WageProfile | null) {
  profileBusy.value = true;
  profileError.value = "";
  try {
    await apiFetch(`/users/${empId}`, {
      method: "PATCH",
      body: { wage_profile_id: profile && !profile.is_standard ? profile.id : null },
    });
    await loadWageProfiles(); // Zaehler + Mitglieder aktualisieren
  } catch (e: any) {
    profileError.value = e?.data?.detail || "Zuweisung fehlgeschlagen.";
  }
  profileBusy.value = false;
}
function startProfileEdit(p?: WageProfile) {
  profileError.value = "";
  if (p) {
    profileEditId.value = p.id;
    profileForm.value = {
      name: p.name,
      mapping: { ...(p.mapping || {}) },
      fruehester_beginn: p.fruehester_beginn || "",
      mehrarbeit_frei_wochen_std:
        p.mehrarbeit_frei_wochen_std === null || p.mehrarbeit_frei_wochen_std === undefined
          ? ""
          : String(p.mehrarbeit_frei_wochen_std),
      wochenende_ins_konto: p.wochenende_ins_konto ?? false,
    };
  } else {
    profileEditId.value = "new";
    profileForm.value = {
      name: "",
      mapping: {},
      fruehester_beginn: "",
      mehrarbeit_frei_wochen_std: "",
      wochenende_ins_konto: false,
    };
  }
}
function cancelProfileEdit() { profileEditId.value = null; }
function setProfileMapping(category: string, code: string) {
  if (code) profileForm.value.mapping[category] = code;
  else delete profileForm.value.mapping[category];
}
async function saveWageProfile() {
  if (!profileForm.value.name.trim()) { profileError.value = "Name erforderlich."; return; }
  profileBusy.value = true;
  profileError.value = "";
  try {
    const body: Record<string, any> = { name: profileForm.value.name.trim(), mapping: profileForm.value.mapping };
    if (profileEditId.value && profileEditId.value !== "new") body.id = profileEditId.value;
    if (profileEditId.value !== "standard") {
      body.fruehester_beginn = profileForm.value.fruehester_beginn || null;
      body.mehrarbeit_frei_wochen_std =
        profileForm.value.mehrarbeit_frei_wochen_std === ""
          ? null
          : Number(profileForm.value.mehrarbeit_frei_wochen_std);
      body.wochenende_ins_konto = profileForm.value.wochenende_ins_konto;
    }
    await apiFetch("/admin/wage-profiles", { method: "PATCH", body });
    profileEditId.value = null;
    await loadWageProfiles();
  } catch (e: any) {
    profileError.value = e?.data?.detail || "Profil konnte nicht gespeichert werden.";
  }
  profileBusy.value = false;
}
async function deleteWageProfile(p: WageProfile) {
  profileBusy.value = true;
  profileError.value = "";
  try {
    await apiFetch(`/admin/wage-profiles/${p.id}`, { method: "DELETE" });
    if (profileEditId.value === p.id) profileEditId.value = null;
    await loadWageProfiles();
  } catch (e: any) {
    profileError.value = e?.data?.detail || "Profil konnte nicht gelöscht werden.";
  }
  profileBusy.value = false;
}

async function setAssignment(code: string, internalCode: string) {
  assignBusy.value = true;
  assignError.value = "";
  try {
    await apiFetch("/admin/wage-type-assignments", {
      method: "PATCH",
      body: { assignments: { [code]: internalCode || null } },
    });
    await loadAssignments();
  } catch (e: any) {
    assignError.value = e?.data?.detail || "Zuordnung konnte nicht gespeichert werden.";
  }
  assignBusy.value = false;
}

/* ---------- Bereiche ---------- */
const departments = ref<Department[]>([]);
const depError = ref("");
const depLoading = ref(true);
const depEdit = ref(false);
const depDraft = ref<Department[]>([]);
const newDep = ref({ name: "", company_id: "" });
const depSaving = ref(false);
const depOpen = ref(false); // Bereiche-Karte ein-/ausklappbar
const depDeleteConfirmId = ref<string | null>(null); // zweistufige Rückfrage vorm Löschen

async function loadDepartments() {
  depLoading.value = true;
  depError.value = "";
  try {
    departments.value = await apiFetch<Department[]>("/departments");
  } catch (e: any) {
    depError.value = e?.data?.detail || "Bereiche konnten nicht geladen werden.";
  }
  depLoading.value = false;
}

function startDepEdit() {
  closeOtherSettingsEdits("dep");
  depDraft.value = departments.value.map((d) => ({ ...d }));
  newDep.value = { name: "", company_id: "" };
  depEdit.value = true;
}
async function saveDep(d: Department) {
  depSaving.value = true;
  depError.value = "";
  try {
    await apiFetch(`/departments/${d.id}`, {
      method: "PATCH",
      body: { name: d.name },
    });
    await loadDepartments();
    depDraft.value = departments.value.map((x) => ({ ...x }));
  } catch (e: any) {
    depError.value = e?.data?.detail || "Bereich konnte nicht gespeichert werden.";
  }
  depSaving.value = false;
}
async function createDep() {
  const name = newDep.value.name.trim();
  if (!name) {
    depError.value = "Name ist Pflicht.";
    return;
  }
  depSaving.value = true;
  depError.value = "";
  try {
    // Bereiche sind firmenunabhaengig (#183) — ein Eintrag gilt fuer
    // Mitarbeiter beider Firmen.
    await apiFetch("/departments", { method: "POST", body: { name } });
    newDep.value = { name: "", company_id: "" };
    await loadDepartments();
    depDraft.value = departments.value.map((x) => ({ ...x }));
  } catch (e: any) {
    depError.value = e?.data?.detail || "Bereich konnte nicht angelegt werden.";
  }
  depSaving.value = false;
}
async function deleteDep(d: Department) {
  depSaving.value = true;
  depError.value = "";
  try {
    await apiFetch(`/departments/${d.id}`, { method: "DELETE" });
    await loadDepartments();
    depDraft.value = departments.value.map((x) => ({ ...x }));
  } catch (e: any) {
    depError.value = e?.data?.detail || "Bereich konnte nicht gelöscht werden.";
  }
  depDeleteConfirmId.value = null;
  depSaving.value = false;
}
function finishDepEdit() {
  depEdit.value = false;
  depDraft.value = [];
}
function cancelDepEdit() {
  // Bearbeiten verlassen + nicht gespeicherte Entwürfe verwerfen.
  depDraft.value = [];
  newDep.value = { name: "", company_id: "" };
  depEdit.value = false;
}

// Bereiche sind tenant-weit und firmenunabhaengig (#183) — keine
// Gruppierung nach Firma mehr, einfache alphabetische Liste.
const sortedDepartments = computed(() =>
  [...departments.value].sort((a, b) => a.name.localeCompare(b.name, "de"))
);

/* ---------- Firmen ---------- */
interface Company {
  id: string;
  name: string;
  is_active?: boolean;
  advisor_number?: string | null;
  client_number?: string | null;
}
const companies = ref<Company[]>([]);
const companyError = ref("");
const companyLoading = ref(true);
const companyEdit = ref(false);
const companyDraft = ref<Company[]>([]);
const newCompany = ref({ name: "", advisor_number: "", client_number: "" });
const companySaving = ref(false);
const companiesOpen = ref(false); // Firmen-Karte ein-/ausklappbar
const companyDeleteConfirmId = ref<string | null>(null); // zweistufige Rückfrage vorm Löschen

async function loadCompanies() {
  companyLoading.value = true;
  companyError.value = "";
  try {
    companies.value = await apiFetch<Company[]>("/companies");
  } catch (e: any) {
    companyError.value = e?.data?.detail || "Firmen konnten nicht geladen werden.";
  }
  companyLoading.value = false;
}

function startCompanyEdit() {
  closeOtherSettingsEdits("company");
  companyDraft.value = companies.value.map((c) => ({ ...c }));
  newCompany.value = { name: "", advisor_number: "", client_number: "" };
  companyEdit.value = true;
}
async function saveCompany(c: Company) {
  companySaving.value = true;
  companyError.value = "";
  try {
    await apiFetch(`/companies/${c.id}`, {
      method: "PATCH",
      body: {
        name: c.name,
        advisor_number: c.advisor_number ?? "",
        client_number: c.client_number ?? "",
      },
    });
    await loadCompanies();
    companyDraft.value = companies.value.map((x) => ({ ...x }));
  } catch (e: any) {
    companyError.value = e?.data?.detail || "Firma konnte nicht gespeichert werden.";
  }
  companySaving.value = false;
}
async function createCompany() {
  if (!newCompany.value.name.trim()) {
    companyError.value = "Name ist Pflicht.";
    return;
  }
  companySaving.value = true;
  companyError.value = "";
  try {
    await apiFetch("/companies", {
      method: "POST",
      body: {
        name: newCompany.value.name.trim(),
        advisor_number: newCompany.value.advisor_number || null,
        client_number: newCompany.value.client_number || null,
      },
    });
    newCompany.value = { name: "", advisor_number: "", client_number: "" };
    await loadCompanies();
    companyDraft.value = companies.value.map((x) => ({ ...x }));
  } catch (e: any) {
    companyError.value = e?.data?.detail || "Firma konnte nicht angelegt werden.";
  }
  companySaving.value = false;
}
async function deleteCompany(c: Company) {
  companySaving.value = true;
  companyError.value = "";
  try {
    await apiFetch(`/companies/${c.id}`, { method: "DELETE" });
    await loadCompanies();
    companyDraft.value = companies.value.map((x) => ({ ...x }));
  } catch (e: any) {
    companyError.value = e?.data?.detail || "Firma konnte nicht gelöscht werden.";
  }
  companyDeleteConfirmId.value = null;
  companySaving.value = false;
}
function finishCompanyEdit() {
  companyEdit.value = false;
  companyDraft.value = [];
}
function cancelCompanyEdit() {
  // Bearbeiten verlassen + nicht gespeicherte Entwürfe verwerfen.
  companyDraft.value = [];
  newCompany.value = { name: "", advisor_number: "", client_number: "" };
  companyEdit.value = false;
}

/* ---------- NFC-Karten (zusammengesetzte Sicht) ---------- */
interface CardRow {
  uid: string;
  assignedTo: string | null;
  assignedUserId: string | null;
  free: boolean;
  // Chip-UID (physische NFC-UID des Tags) — nur für freie/gescannte Karten
  // bekannt, da sie aus dem scanned-cards-Pool stammt. Zum Abgleich mit einer
  // Handy-NFC-App, wenn der Admin die richtige Karte einem Mitarbeiter
  // zuweisen will.
  chipUid: string | null;
}
const scannedCards = ref<ScannedCard[]>([]);
const cardEmployees = ref<Employee[]>([]);
const cardError = ref("");
const cardLoading = ref(true);
const cardBusy = ref(false);
const cardsOpen = ref(false); // NFC-Karten-Karte ein-/ausklappbar
// Zweistufige Rückfrage vorm Löschen/Lösen -- destruktive Aktionen (siehe
// removeCard/unassignCard), analog zu dep-/companyDeleteConfirmId.
const cardRemoveConfirmUid = ref<string | null>(null);
const cardUnassignConfirmUserId = ref<string | null>(null);

async function loadCards() {
  cardLoading.value = true;
  cardError.value = "";
  try {
    const [scanned, emps] = await Promise.all([
      apiFetch<ScannedCard[]>("/admin/scanned-cards"),
      apiFetch<Employee[]>("/admin/employees"),
    ]);
    scannedCards.value = scanned;
    cardEmployees.value = emps;
  } catch (e: any) {
    cardError.value = e?.data?.detail || "NFC-Karten konnten nicht geladen werden.";
  }
  cardLoading.value = false;
}

const cardRows = computed<CardRow[]>(() => {
  const rows: CardRow[] = [];
  for (const e of cardEmployees.value) {
    if (e.nfc_badge_id) {
      const label = e.department_name ? `${e.name} · ${e.department_name}` : e.name;
      // Chip-UID zugewiesener Pool-Karten kommt aus assigned_card_meta
      // (Backend /admin/employees, Feld nfc_chip_uid).
      rows.push({
        uid: e.nfc_badge_id,
        assignedTo: label,
        assignedUserId: e.id,
        free: false,
        chipUid: e.nfc_chip_uid || null,
      });
    }
  }
  for (const c of scannedCards.value) {
    if (!c.uid) continue;
    rows.push({ uid: c.uid, assignedTo: null, assignedUserId: null, free: true, chipUid: c.chip_uid || null });
  }
  return rows;
});

async function removeCard(uid: string) {
  cardBusy.value = true;
  cardError.value = "";
  try {
    await apiFetch("/admin/scanned-cards/remove", { method: "POST", body: { uid } });
    await loadCards();
  } catch (e: any) {
    cardError.value = e?.data?.detail || "Karte konnte nicht gelöscht werden.";
  }
  cardRemoveConfirmUid.value = null;
  cardBusy.value = false;
}
async function unassignCard(userId: string) {
  cardBusy.value = true;
  cardError.value = "";
  try {
    await apiFetch(`/users/${userId}`, { method: "PATCH", body: { nfc_badge_id: null } });
    await loadCards();
  } catch (e: any) {
    cardError.value = e?.data?.detail || "Zuweisung konnte nicht gelöst werden.";
  }
  cardUnassignConfirmUserId.value = null;
  cardBusy.value = false;
}

// Mitarbeiter ohne eigene Karte — Auswahl für "Zuweisen" bei freien Karten.
const unbadgedEmployees = computed(() =>
  cardEmployees.value.filter((e) => !e.nfc_badge_id && e.is_active !== false)
);
// uid -> ausgewählte employee_id im Zuweisen-Dropdown der jeweiligen Zeile.
const cardAssignSelection = ref<Record<string, string>>({});

async function assignCard(uid: string) {
  const userId = cardAssignSelection.value[uid];
  if (!userId) return;
  cardBusy.value = true;
  cardError.value = "";
  try {
    await apiFetch(`/users/${userId}`, { method: "PATCH", body: { nfc_badge_id: uid } });
    delete cardAssignSelection.value[uid];
    await loadCards();
  } catch (e: any) {
    cardError.value = e?.data?.detail || "Karte konnte nicht zugewiesen werden.";
  }
  cardBusy.value = false;
}

/* ---------- Rollen & Rechte ---------- */
interface Role {
  id: string;
  name: string;
  role_type: string;
  data_scope: string;
  permissions: string[];
}
type DataScope = "self" | "department" | "global" | "anonymized_global";

// Vollständiger Rechte-Katalog, gruppiert (technische Strings exakt wie Backend).
// Vollständiger Rechte-Katalog, gruppiert (technische Strings exakt wie Backend).
// desc = verständliche Erklärung; wird unter dem Label angezeigt.
const permissionGroups: {
  group: string;
  desc?: string;
  items: { key: string; label: string; desc?: string }[];
}[] = [
  {
    group: "Zeiterfassung",
    desc: "Stempelzeiten ansehen und korrigieren.",
    items: [
      { key: "timesheet:view_own", label: "Eigene Zeiten ansehen", desc: "Meine Zeit: eigene Stempel, Kalender, Salden." },
      { key: "timesheet:view_team", label: "Zeiten des Bereichs ansehen", desc: "Arbeitszeiten aller im eigenen Bereich." },
      { key: "timesheet:view_all", label: "Zeiten aller ansehen", desc: "Arbeitszeiten der ganzen Firma." },
      { key: "timesheet:edit_team", label: "Zeiten des Bereichs korrigieren", desc: "Stempel im eigenen Bereich nachtragen/ändern." },
      { key: "timesheet:edit_all", label: "Zeiten aller korrigieren", desc: "Stempel firmenweit nachtragen/ändern." },
      { key: "overtime:manage", label: "Überstundenkonto verwalten", desc: "Manuelle Konto-Korrekturen buchen." },
      { key: "timesheet:view_aggregates", label: "Nur Summen ansehen", desc: "Auswertungen ohne Personenbezug (z. B. Buchhaltung)." },
      { key: "clock:all", label: "Stempeln", desc: "Kommen/Gehen/Pause erfassen (Kiosk/Web)." },
    ],
  },
  {
    group: "Kalender & Organigramm",
    desc: "Urlaubs-/Abwesenheitskalender und Hierarchie-Ansicht — ohne Arbeitszeiten, ohne Stammdaten.",
    items: [
      { key: "calendar:view_team", label: "Teamkalender (eigener Bereich)", desc: "Sieht Urlaub/Abwesenheit der Kollegen im Bereich." },
      { key: "calendar:view_all", label: "Teamkalender (ganze Firma)", desc: "Sieht Urlaub/Abwesenheit aller." },
      { key: "orgchart:view_team", label: "Organigramm (eigener Bereich)", desc: "Sieht die Hierarchie des eigenen Bereichs." },
      { key: "orgchart:view_all", label: "Organigramm (ganze Firma)", desc: "Sieht die komplette Firmen-Hierarchie." },
    ],
  },
  {
    group: "Anträge & Urlaub",
    desc: "Urlaubsanträge stellen und entscheiden.",
    items: [
      { key: "leave:request_own", label: "Eigenen Urlaub beantragen", desc: "Anträge einreichen und eigene zurückziehen." },
      { key: "leave:approve_team", label: "Anträge des Bereichs entscheiden", desc: "Genehmigen/Ablehnen im eigenen Bereich (Vier-Augen: nie eigene)." },
      { key: "leave:approve_all", label: "Alle Anträge entscheiden", desc: "Genehmigen/Ablehnen firmenweit." },
      { key: "medical:read", label: "Krankmeldungs-Details sehen", desc: "Gesundheitsdaten! Ohne dieses Recht erscheint Krank nur als Abwesend." },
    ],
  },
  {
    group: "Mitarbeiter (Stammdaten)",
    desc: "Personalakten lesen und pflegen.",
    items: [
      { key: "users:read_team", label: "Bereich lesen", desc: "Stammdaten im eigenen Bereich einsehen." },
      { key: "users:read_all", label: "Alle lesen", desc: "Stammdaten firmenweit einsehen." },
      { key: "users:write", label: "Mitarbeiter verwalten", desc: "Anlegen, ändern, deaktivieren — schließt NFC-Karten ein." },
    ],
  },
  {
    group: "Export & Lohn",
    desc: "DATEV-Export und Lohnbuchungen.",
    items: [
      { key: "export:datev", label: "DATEV-Export", desc: "LODAS-Dateien erzeugen und herunterladen." },
      { key: "reports:view", label: "Auswertung ansehen", desc: "Monatsreport/Stundenzettel." },
      { key: "payroll:read", label: "Lohnbuchungen ansehen", desc: "Manuelle Pauschalen/Beträge einsehen." },
      { key: "payroll:write", label: "Lohnbuchungen bearbeiten", desc: "Pauschalen/Beträge anlegen und löschen." },
    ],
  },
  {
    group: "Administration",
    desc: "Systemverwaltung — sparsam vergeben.",
    items: [
      { key: "admin:dashboard", label: "Admin-Dashboard", desc: "Verwaltungs-Übersicht sehen." },
      { key: "admin:settings", label: "Einstellungen verwalten", desc: "Lohnarten, Zuschläge, Firmen, Bereiche." },
      { key: "admin:features", label: "Funktionen schalten", desc: "Haupt-Schalter der Firma (Organigramm, Krankmeldungen, Lohnbuchungen) und Kalender-Abo-Link erneuern." },
      { key: "admin:roles", label: "Rollen & Rechte verwalten", desc: "Darf diese Rechte hier ändern — nur GF/Admin!" },
      { key: "admin:accounts", label: "Benutzerkonten verwalten", desc: "Logins anlegen, Passwörter zurücksetzen." },
      { key: "admin:cards", label: "NFC-Karten verwalten", desc: "Karten zuweisen/löschen — z. B. für HR, ohne weitere Rechte." },
    ],
  },
  {
    group: "Aufsicht & Audit",
    desc: "Nachvollziehbarkeit.",
    items: [
      { key: "audit:read", label: "Audit-Log lesen", desc: "Wer hat wann was geändert (mit Namen)." },
      { key: "audit:read_anonymized", label: "Audit-Log anonymisiert", desc: "Wie Audit-Log, aber ohne Namen (Betriebsrat)." },
    ],
  },
];

// „Alle" schließt „Bereich" ein: Wer das linke Recht hat, hat das rechte
// automatisch mit — die Checkbox wird dann ausgegraut angezeigt.
const PERM_IMPLIES: Record<string, string[]> = {
  "timesheet:view_all": ["timesheet:view_team"],
  "timesheet:edit_all": ["timesheet:edit_team"],
  "leave:approve_all": ["leave:approve_team"],
  "users:read_all": ["users:read_team"],
  "calendar:view_all": ["calendar:view_team"],
  "orgchart:view_all": ["orgchart:view_team"],
  "users:write": ["admin:cards"],
  "audit:read": ["audit:read_anonymized"],
};
function permImplied(perms: string[], key: string): boolean {
  return perms.some((p) => p !== key && (PERM_IMPLIES[p] || []).includes(key));
}
function permChecked(perms: string[], key: string): boolean {
  return perms.includes(key) || permImplied(perms, key);
}
function togglePerm(perms: string[], key: string, on: boolean) {
  const i = perms.indexOf(key);
  if (on && i < 0) {
    perms.push(key);
    // Redundante engere Rechte aufräumen — sie sind jetzt enthalten.
    for (const implied of PERM_IMPLIES[key] || []) {
      const j = perms.indexOf(implied);
      if (j >= 0) perms.splice(j, 1);
    }
  } else if (!on && i >= 0) {
    perms.splice(i, 1);
  }
}

const dataScopeOptions: { value: DataScope; label: string }[] = [
  { value: "self", label: "Sieht nur sich selbst" },
  { value: "department", label: "Sieht den eigenen Bereich" },
  { value: "global", label: "Sieht die ganze Firma" },
  { value: "anonymized_global", label: "Nur anonymisiert (Betriebsrat)" },
];

// Ein-Klick-Vorlagen für neue Rollen.
const roleTemplates: { label: string; role: Omit<Role, "id"> }[] = [
  {
    label: "Mitarbeiter",
    role: {
      name: "Mitarbeiter", role_type: "mitarbeiter", data_scope: "self",
      permissions: ["timesheet:view_own", "clock:all", "leave:request_own", "calendar:view_team", "orgchart:view_team"],
    },
  },
  {
    label: "Teamleiter",
    role: {
      name: "Teamleiter", role_type: "teamleiter", data_scope: "department",
      permissions: [
        "timesheet:view_own", "timesheet:view_team", "timesheet:edit_team", "clock:all",
        "leave:request_own", "leave:approve_team", "users:read_team", "calendar:view_team",
        "orgchart:view_team",
      ],
    },
  },
  {
    label: "Personal (HR)",
    role: {
      name: "Personalleitung", role_type: "hr", data_scope: "global",
      permissions: [
        "timesheet:view_own", "timesheet:view_all", "timesheet:edit_all", "leave:request_own",
        "leave:approve_all", "medical:read", "users:read_all", "users:write",
        "calendar:view_all", "orgchart:view_all", "reports:view", "export:datev", "admin:dashboard",
      ],
    },
  },
  {
    label: "Geschäftsführung",
    role: {
      name: "Geschäftsführung", role_type: "gf", data_scope: "global",
      permissions: [
        "timesheet:view_own", "timesheet:view_all", "timesheet:edit_all", "overtime:manage",
        "clock:all", "leave:request_own", "leave:approve_all", "users:read_all", "users:write",
        "calendar:view_all", "orgchart:view_all", "export:datev", "reports:view", "payroll:read", "payroll:write",
        "admin:dashboard", "admin:settings", "admin:features", "admin:roles", "admin:accounts", "audit:read",
      ],
    },
  },
];
function applyTemplate(t: { label: string; role: Omit<Role, "id"> }) {
  newRole.value = { id: "", ...t.role, permissions: [...t.role.permissions] };
}

const roles = ref<Role[]>([]);
const roleError = ref("");
const roleLoading = ref(true);
const roleSaving = ref(false);
const roleDrafts = ref<Record<string, Role>>({});
const newRole = ref<Role>({ id: "", name: "", role_type: "", data_scope: "self", permissions: [] });
const showNewRole = ref(false);
const rolesOpen = ref(false); // Rollen-Karte ein-/ausklappbar
const rolesEdit = ref(false); // true, sobald irgendeine Rolle bearbeitet wird (fuer Akkordeon-Logik)
const editingRoleId = ref<string | null>(null); // genau EINE Rolle im Edit-Modus

async function loadRoles() {
  roleLoading.value = true;
  roleError.value = "";
  try {
    roles.value = await apiFetch<Role[]>("/roles");
  } catch (e: any) {
    roleError.value = e?.data?.detail || "Rollen konnten nicht geladen werden.";
  }
  roleLoading.value = false;
}

function startRoleEdit(r: Role) {
  closeOtherSettingsEdits("roles");
  roleDrafts.value = {
    [r.id]: { ...r, permissions: Array.isArray(r.permissions) ? [...r.permissions] : [] },
  };
  editingRoleId.value = r.id;
  rolesEdit.value = true;
}
function cancelRolesEdit() {
  // Bearbeiten verlassen + nicht gespeicherte Entwürfe verwerfen.
  roleDrafts.value = {};
  editingRoleId.value = null;
  newRole.value = { id: "", name: "", role_type: "", data_scope: "self", permissions: [] };
  showNewRole.value = false;
  rolesEdit.value = false;
}
async function saveRole(id: string) {
  const d = roleDrafts.value[id];
  if (!d) return;
  roleSaving.value = true;
  roleError.value = "";
  try {
    await apiFetch(`/roles/${id}`, {
      method: "PATCH",
      body: {
        name: d.name,
        role_type: d.role_type,
        data_scope: d.data_scope,
        permissions: d.permissions,
      },
    });
    await loadRoles();
    cancelRolesEdit();
  } catch (e: any) {
    roleError.value = e?.data?.detail || "Rolle konnte nicht gespeichert werden.";
  }
  roleSaving.value = false;
}
async function deleteRole(id: string) {
  roleSaving.value = true;
  roleError.value = "";
  try {
    await apiFetch(`/roles/${id}`, { method: "DELETE" });
    await loadRoles();
    cancelRolesEdit();
  } catch (e: any) {
    roleError.value = e?.data?.detail || "Rolle konnte nicht gelöscht werden.";
  }
  roleSaving.value = false;
}
async function createRole() {
  if (!newRole.value.name.trim()) {
    roleError.value = "Rollenname ist Pflicht.";
    return;
  }
  roleSaving.value = true;
  roleError.value = "";
  try {
    await apiFetch("/roles", {
      method: "POST",
      body: {
        name: newRole.value.name.trim(),
        role_type: newRole.value.role_type.trim(),
        data_scope: newRole.value.data_scope,
        permissions: newRole.value.permissions,
      },
    });
    newRole.value = { id: "", name: "", role_type: "", data_scope: "self", permissions: [] };
    showNewRole.value = false;
    await loadRoles();
  } catch (e: any) {
    roleError.value = e?.data?.detail || "Rolle konnte nicht angelegt werden.";
  }
  roleSaving.value = false;
}

/* ---------- Benutzerkonten ---------- */
interface AccountRow {
  id: string;
  email: string;
  employee_id?: string | null;
  employee_name?: string | null;
  role_name?: string | null;
  role_id?: string | null;
  is_active?: boolean;
  is_superadmin?: boolean;
  scope_department_ids?: string[];
}
const accounts = ref<AccountRow[]>([]);
const accError = ref("");
const accLoading = ref(true);
const accBusy = ref(false);
const showNewAccount = ref(false);
const accountsOpen = ref(false); // Benutzerkonten-Karte ein-/ausklappbar
const accountsEdit = ref(false); // karten-weiter Edit-Modus
const newAccount = ref({
  email: "",
  password: "",
  role_id: "",
  employee_id: "",
});

const activeAccounts = computed(() => accounts.value.filter((a) => a.is_active !== false));
const inactiveAccounts = computed(() => accounts.value.filter((a) => a.is_active === false));
const deactivatedOpen = ref(false);

// Verknüpfbare Mitarbeiter: nutzt die bereits in loadCards() geladene
// Liste aus /admin/employees (cardEmployees), nur aktive.
const linkableEmployees = computed(() =>
  cardEmployees.value.filter((e) => e.is_active !== false)
);
function employeeOptionLabel(e: Employee): string {
  return e.employee_number ? `${e.name} (${e.employee_number})` : e.name;
}

function accountName(a: AccountRow): string {
  return a.employee_name || "—";
}
function accountRoleName(a: AccountRow): string {
  if (a.role_name) return a.role_name;
  const r = roles.value.find((x) => x.id === a.role_id);
  return r?.name || "—";
}

function startAccountsEdit() {
  closeOtherSettingsEdits("accounts");
  accountsEdit.value = true;
}
function finishAccountsEdit() {
  accountsEdit.value = false;
  showNewAccount.value = false;
}
function cancelAccountsEdit() {
  // Bearbeiten verlassen + nicht gespeichertes Neu-Konto-Formular zurücksetzen.
  newAccount.value = { email: "", password: "", role_id: "", employee_id: "" };
  showNewAccount.value = false;
  pwResetConfirmId.value = null;
  pwResetInfo.value = null;
  accountsEdit.value = false;
}

// --- Kiosk-Kurzmeldungen: Rundmeldungen für den Terminal-Ruhebildschirm (#189) ---
interface KioskMsg { id: string; text: string; active_from?: string | null; active_until?: string | null }
const kioskMsgs = ref<KioskMsg[]>([]);
// ISO YYYY-MM-DD → TT.MM.JJJJ. War im Template referenziert, aber auf dieser
// Seite nie definiert → sobald eine Meldung mit "anzeigen bis" existierte,
// crashte das Rendern und die GANZE Einstellungen-Seite blieb weiß (#194).
function fmtDate(v?: string | null): string {
  if (!v) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(v);
  return m ? `${m[3]}.${m[2]}.${m[1]}` : v;
}
const kioskMsgOpen = ref(false);
const kioskNewText = ref("");
const kioskNewFrom = ref("");
const kioskNewUntil = ref("");
const kioskBusy = ref(false);
const kioskErr = ref("");

async function loadKioskMsgs() {
  try {
    kioskMsgs.value = await apiFetch<KioskMsg[]>("/admin/kiosk-messages?broadcasts_only=true");
  } catch { /* ohne Recht bleibt die Karte leer */ }
}
async function addKioskMsg() {
  const text = kioskNewText.value.trim();
  if (!text) { kioskErr.value = "Text ist Pflicht."; return; }
  kioskBusy.value = true; kioskErr.value = "";
  try {
    await apiFetch("/admin/kiosk-messages", {
      method: "POST",
      body: { text, active_from: kioskNewFrom.value || null, active_until: kioskNewUntil.value || null },
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

// --- Stundenzettel-Versand: Vormonats-PDF per Mail (Spec 2026-08-07) ---
interface TsMailRecipient { employee_id: string; name: string; email?: string | null; enabled: boolean }
interface TsMailLogRow { employee_name: string; year_month: string; sent_to?: string | null; status: string; detail?: string | null; created_at?: string | null }
interface TsMailSmtp { host: string; port: number; user: string; starttls?: boolean; password_set: boolean }
interface TsMailData {
  enabled: boolean; sender: string; body_text?: string;
  send_day: number; transport: "m365" | "smtp"; smtp: TsMailSmtp; problem?: string | null;
  mailbox_id?: string | null; mailboxes?: { id: string; mailbox: string }[];
  recipients: TsMailRecipient[]; log: TsMailLogRow[];
}
const tsMail = ref<TsMailData | null>(null);
const tsMailOpen = ref(false);
const tsMailLogOpen = ref(false);
const tsMailBusy = ref(false);
const tsMailErr = ref("");
const tsMailResult = ref("");
const tsMailSender = ref("");
const tsMailBodyText = ref("");
const tsMailSendDay = ref(1);
const tsMailSmtpHost = ref("");
const tsMailSmtpPort = ref(587);
const tsMailSmtpUser = ref("");
// Immer leer geladen; nur ein neu eingetipptes Passwort wird gesendet —
// leer lassen behaelt das gespeicherte (Backend ignoriert Leerstring).
const tsMailSmtpPassword = ref("");

async function loadTsMail() {
  try {
    tsMail.value = await apiFetch<TsMailData>("/admin/timesheet-mail");
    tsMailSender.value = tsMail.value.sender;
    tsMailBodyText.value = tsMail.value.body_text || "";
    tsMailSendDay.value = tsMail.value.send_day || 1;
    tsMailSmtpHost.value = tsMail.value.smtp?.host || "";
    tsMailSmtpPort.value = tsMail.value.smtp?.port || 587;
    tsMailSmtpUser.value = tsMail.value.smtp?.user || "";
    tsMailSmtpPassword.value = "";
  } catch { /* ohne Recht bleibt die Karte leer */ }
}
async function saveTsMailCfg(patch: {
  enabled?: boolean; sender?: string; body_text?: string;
  send_day?: number; transport?: string; smtp?: Record<string, unknown>; mailbox_id?: string | null;
}) {
  tsMailBusy.value = true; tsMailErr.value = "";
  try {
    await apiFetch("/admin/timesheet-mail", { method: "POST", body: patch });
    await loadTsMail();
  } catch (e: any) {
    tsMailErr.value = e?.data?.detail || "Speichern fehlgeschlagen.";
  }
  tsMailBusy.value = false;
}
async function toggleTsMailRecipient(r: TsMailRecipient) {
  tsMailBusy.value = true; tsMailErr.value = "";
  try {
    await apiFetch("/admin/timesheet-mail/recipients", {
      method: "POST", body: { employee_id: r.employee_id, enabled: !r.enabled },
    });
    r.enabled = !r.enabled;
  } catch (e: any) {
    tsMailErr.value = e?.data?.detail || "Änderung fehlgeschlagen.";
  }
  tsMailBusy.value = false;
}
function saveTsMailSmtp() {
  const smtp: Record<string, unknown> = {
    host: tsMailSmtpHost.value.trim(),
    port: Number(tsMailSmtpPort.value) || 587,
    user: tsMailSmtpUser.value.trim(),
  };
  if (tsMailSmtpPassword.value) smtp.password = tsMailSmtpPassword.value;
  return saveTsMailCfg({ smtp });
}
async function tsMailSendNow() {
  tsMailBusy.value = true; tsMailErr.value = ""; tsMailResult.value = "";
  try {
    const c = await apiFetch<{ sent: number; skipped: number; errors: number }>(
      "/admin/timesheet-mail/send-now", { method: "POST" }
    );
    tsMailResult.value = `Gesendet: ${c.sent} · Übersprungen (keine E-Mail): ${c.skipped} · Fehler: ${c.errors}`;
    await loadTsMail();
  } catch (e: any) {
    tsMailErr.value = e?.data?.detail || "Versand fehlgeschlagen.";
  }
  tsMailBusy.value = false;
}
// "2026-07-01" -> "Juli 2026" fürs Versandprotokoll.
function tsMailMonthLabel(ym: string): string {
  const m = /^(\d{4})-(\d{2})/.exec(ym);
  if (!m) return ym;
  const names = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"];
  return `${names[Number(m[2]) - 1]} ${m[1]}`;
}
const TSMAIL_STATUS: Record<string, string> = {
  sent: "gesendet", skipped_no_email: "keine E-Mail", error: "Fehler",
};

// --- Gelöschte Mitarbeiter (Soft-Delete): wiederherstellen oder endgültig löschen ---
interface DeletedEmployee {
  id: string;
  name: string;
  employee_number?: string | null;
  company_name?: string | null;
  employment_status?: string | null;
}
const deletedEmployees = ref<DeletedEmployee[]>([]);
const deletedEmpOpen = ref(false);
const deletedEmpBusy = ref(false);
const deletedEmpErr = ref("");

async function loadDeletedEmployees() {
  try {
    const all = await apiFetch<DeletedEmployee[]>("/admin/employees");
    deletedEmployees.value = all.filter((e) => e.employment_status === "deleted");
  } catch { /* Karte bleibt einfach leer, wenn das Recht fehlt */ }
}

async function restoreEmployee(e: DeletedEmployee) {
  deletedEmpBusy.value = true;
  deletedEmpErr.value = "";
  try {
    await apiFetch(`/users/${e.id}`, { method: "PATCH", body: { employment_status: "active" } });
    await loadDeletedEmployees();
  } catch (err: any) {
    deletedEmpErr.value = err?.data?.detail || "Wiederherstellen fehlgeschlagen.";
  }
  deletedEmpBusy.value = false;
}

async function destroyEmployee(e: DeletedEmployee) {
  if (!window.confirm(`${e.name} endgültig löschen? Das ist nur für falsch angelegte Datensätze ohne Zeitdaten möglich und kann nicht rückgängig gemacht werden.`)) return;
  deletedEmpBusy.value = true;
  deletedEmpErr.value = "";
  try {
    await apiFetch(`/users/${e.id}`, { method: "DELETE" });
    await loadDeletedEmployees();
  } catch (err: any) {
    deletedEmpErr.value = err?.data?.detail || "Endgültiges Löschen fehlgeschlagen.";
  }
  deletedEmpBusy.value = false;
}

async function loadAccounts() {
  accLoading.value = true;
  accError.value = "";
  try {
    accounts.value = await apiFetch<AccountRow[]>("/accounts");
  } catch (e: any) {
    accError.value = e?.data?.detail || "Benutzerkonten konnten nicht geladen werden.";
  }
  accLoading.value = false;
}

async function createAccount() {
  const a = newAccount.value;
  if (!a.email.trim()) {
    accError.value = "E-Mail ist Pflicht.";
    return;
  }
  if (!a.password || a.password.length < 8) {
    accError.value = "Passwort ist Pflicht (mindestens 8 Zeichen).";
    return;
  }
  if (!a.role_id) {
    accError.value = "Bitte eine Rolle auswählen.";
    return;
  }
  accBusy.value = true;
  accError.value = "";
  try {
    await apiFetch("/accounts", {
      method: "POST",
      body: {
        email: a.email.trim(),
        password: a.password,
        role_id: a.role_id,
        employee_id: a.employee_id || undefined,
      },
    });
    newAccount.value = {
      email: "",
      password: "",
      role_id: "",
      employee_id: "",
    };
    showNewAccount.value = false;
    await loadAccounts();
  } catch (e: any) {
    accError.value = e?.data?.detail || "Konto konnte nicht angelegt werden.";
  }
  accBusy.value = false;
}

async function changeAccountRole(a: AccountRow, roleId: string) {
  accBusy.value = true;
  accError.value = "";
  try {
    await apiFetch(`/accounts/${a.id}`, { method: "PATCH", body: { role_id: roleId } });
    await loadAccounts();
  } catch (e: any) {
    accError.value = e?.data?.detail || "Rolle konnte nicht geändert werden.";
  }
  accBusy.value = false;
}

// Zuständige Bereiche eines Kontos umschalten (leer = Bereich der Personalakte).
async function toggleAccountDepartment(a: AccountRow, deptId: string, on: boolean) {
  const cur = new Set(a.scope_department_ids || []);
  if (on) cur.add(deptId);
  else cur.delete(deptId);
  accBusy.value = true;
  accError.value = "";
  try {
    await apiFetch(`/accounts/${a.id}`, {
      method: "PATCH",
      body: { scope_department_ids: [...cur] },
    });
    await loadAccounts();
  } catch (e: any) {
    accError.value = e?.data?.detail || "Bereiche konnten nicht geändert werden.";
  }
  accBusy.value = false;
}
function accountDeptSummary(a: AccountRow): string {
  const ids = a.scope_department_ids || [];
  if (!ids.length) return "wie Personalakte";
  const names = ids
    .map((id) => departments.value.find((d) => d.id === id)?.name || "?")
    .sort((x, y) => x.localeCompare(y, "de"));
  return names.join(", ");
}

async function changeAccountEmployee(a: AccountRow, employeeId: string) {
  accBusy.value = true;
  accError.value = "";
  try {
    await apiFetch(`/accounts/${a.id}`, {
      method: "PATCH",
      body: { employee_id: employeeId || null },
    });
    await loadAccounts();
  } catch (e: any) {
    accError.value = e?.data?.detail || "Mitarbeiter-Verknüpfung konnte nicht geändert werden.";
  }
  accBusy.value = false;
}

async function deactivateAccount(a: AccountRow) {
  accBusy.value = true;
  accError.value = "";
  try {
    await apiFetch(`/accounts/${a.id}`, { method: "PATCH", body: { is_active: false } });
    await loadAccounts();
  } catch (e: any) {
    accError.value = e?.data?.detail || "Konto konnte nicht deaktiviert werden.";
  }
  accBusy.value = false;
}

async function reactivateAccount(a: AccountRow) {
  accBusy.value = true;
  accError.value = "";
  try {
    await apiFetch(`/accounts/${a.id}`, { method: "PATCH", body: { is_active: true } });
    await loadAccounts();
  } catch (e: any) {
    accError.value = e?.data?.detail || "Konto konnte nicht aktiviert werden.";
  }
  accBusy.value = false;
}

async function deleteAccount(a: AccountRow) {
  if (!confirm(
    `Konto ${a.email} endgültig löschen? Der Mitarbeiter und seine Zeitdaten ` +
    `bleiben erhalten, nur der Login wird entfernt.`
  )) return;
  accBusy.value = true;
  accError.value = "";
  try {
    await apiFetch(`/accounts/${a.id}`, { method: "DELETE" });
    await loadAccounts();
  } catch (e: any) {
    accError.value = e?.data?.detail || "Konto konnte nicht gelöscht werden.";
  }
  accBusy.value = false;
}

// Passwort zurücksetzen (Admin) — zweistufige Bestätigung wie sonst.
const pwResetConfirmId = ref<string | null>(null);
const pwResetInfo = ref<{ email: string; password: string } | null>(null);

async function resetAccountPw(a: AccountRow) {
  accBusy.value = true;
  accError.value = "";
  try {
    const res = await apiFetch<{ ok: boolean; password: string }>(
      `/accounts/${a.id}/reset-password`,
      { method: "POST" }
    );
    pwResetInfo.value = { email: a.email, password: res.password };
  } catch (e: any) {
    accError.value = e?.data?.detail || "Passwort konnte nicht zurückgesetzt werden.";
  }
  pwResetConfirmId.value = null;
  accBusy.value = false;
}

/* ---------- Auto-Abbruch beim Einklappen ---------- */
// Wird eine Sektion zugeklappt, Bearbeiten-Modus beenden + Entwurf verwerfen.
watch(wageOpen, (v) => { if (!v) cancelWageEdit(); });
watch(surchargeOpen, (v) => { if (!v) { cancelSurchargeEdit(); resettleConfirm.value = false; } });
watch(wtpOpen, (v) => { if (!v) { cancelWtpEdit(); wtpDeleteConfirmId.value = null; } });
watch(depOpen, (v) => { if (!v) cancelDepEdit(); });
watch(companiesOpen, (v) => { if (!v) cancelCompanyEdit(); });
watch(rolesOpen, (v) => { if (!v) cancelRolesEdit(); });
watch(accountsOpen, (v) => { if (!v) cancelAccountsEdit(); });

/* ---------- Auto-Abbruch beim Verlassen der Seite ---------- */
// Beim Navigieren weg von dieser Seite alle Bearbeiten-Modi beenden
// und nicht gespeicherte Entwürfe verwerfen.
onBeforeRouteLeave(() => {
  cancelWageEdit();
  cancelSurchargeEdit();
  cancelWtpEdit();
  cancelDepEdit();
  cancelCompanyEdit();
  cancelRolesEdit();
  cancelAccountsEdit();
});

onMounted(() => {
  loadFeatures();
  loadVacationSettings();
  loadMailCfg();
  loadWageProfiles();
  loadIcsFeed();
  loadWageTypes();
  loadAssignments();
  loadSurchargeConfig();
  loadWtProfiles();
  loadDepartments();
  loadCompanies();
  loadCards();
  loadRoles();
  loadAccounts();
  loadDeletedEmployees();
  loadKioskMsgs();
  loadTsMail();
});
</script>

<template>
  <div>
    <h1 style="margin-bottom: 18px">Einstellungen</h1>

    <!-- 1) Lohnarten -->
    <div class="card" style="padding: 0; margin-bottom: 16px">
      <div class="spread" style="padding: 16px 18px; cursor: pointer" @click="toggleSection('wage')">
        <h2>Lohnarten <span class="muted" style="font-weight: 400; font-size: 13px">({{ wageTypes.length }})</span></h2>
        <div class="row" style="gap: 8px">
          <span v-if="wageOpen" class="row" style="gap: 8px" @click.stop>
            <template v-if="wageEdit">
              <button class="btn" @click="addWageRow">+ Lohnart</button>
              <button class="btn" @click="cancelWageEdit">Abbrechen</button>
              <button class="btn btn-primary" @click="finishWageEdit">Fertig</button>
            </template>
            <button v-else class="btn" @click="startWageEdit">Bearbeiten</button>
          </span>
          <span class="muted" style="font-size: 13px">{{ wageOpen ? "▾" : "▸" }}</span>
        </div>
      </div>

      <template v-if="wageOpen">
        <div v-if="wageError" class="wip-banner" style="margin: 0 18px 14px">{{ wageError }}</div>
        <div v-if="wageLoading" class="empty">Lädt …</div>

        <div v-if="!wageLoading" class="muted" style="font-size: 12px; padding: 0 18px 12px">
          Die DATEV-Nummern kommen von eurem Steuerberater und werden hier gepflegt — exportiert wird nur, was eine Nummer hat.
        </div>

        <table v-if="!wageLoading" class="data">
          <thead>
            <tr>
              <th>Nummer</th>
              <th>Bezeichnung</th>
              <th>Code</th>
              <th>Einheit</th>
              <th>Aktiv</th>
              <th></th>
            </tr>
          </thead>

          <!-- Leseansicht: keine Aktionen -->
          <tbody v-if="!wageEdit">
            <tr v-if="!wageTypes.length">
              <td colspan="6" class="empty">Keine Lohnarten vorhanden.</td>
            </tr>
            <tr v-for="w in wageTypes" :key="w.internal_code">
              <td class="num">
                <template v-if="w.datev_lohnart !== null && w.datev_lohnart !== ''">{{ w.datev_lohnart }}</template>
                <template v-else>
                  —
                  <span class="muted" style="font-size: 11px; white-space: nowrap">ohne Nummer → wird nicht exportiert</span>
                </template>
              </td>
              <td>{{ w.description || "—" }}</td>
              <td class="num muted">{{ w.internal_code || "—" }}</td>
              <td class="muted">{{ wageUnitShort[w.unit || "hours"] || "Std" }}</td>
              <td>
                <span class="badge" :class="w.is_active ? 'green' : 'neutral'">
                  <span class="dot" />{{ w.is_active ? "Aktiv" : "Inaktiv" }}
                </span>
              </td>
              <td></td>
            </tr>
          </tbody>

          <!-- Bearbeiten: Speichern + Deaktivieren/Aktivieren pro Zeile -->
          <tbody v-else>
            <tr v-for="(w, i) in wageDraft" :key="i">
              <td><input v-model="w.datev_lohnart" class="input" style="min-width: 90px" placeholder="Nummer eintragen" /></td>
              <td><input v-model="w.description" class="input" placeholder="Bezeichnung" /></td>
              <td><input v-model="w.internal_code" class="input" style="min-width: 90px" placeholder="Code" /></td>
              <td>
                <select v-model="w.unit" class="input" style="min-width: 100px">
                  <option v-for="o in wageUnitOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
                </select>
              </td>
              <td>
                <span class="badge" :class="w.is_active ? 'green' : 'neutral'">
                  <span class="dot" />{{ w.is_active ? "Aktiv" : "Inaktiv" }}
                </span>
              </td>
              <td style="text-align: right; white-space: nowrap">
                <button class="btn btn-primary" :disabled="wageSaving" style="padding: 6px 12px"
                  @click="saveWageType(w)">Speichern</button>
                <button v-if="w.is_active" class="linkbtn danger" :disabled="wageSaving"
                  style="margin-left: 8px" @click="deactivateWageType(w)">Deaktivieren</button>
                <button v-else class="linkbtn" :disabled="wageSaving"
                  style="margin-left: 8px" @click="saveWageType({ ...w, is_active: true })">Aktivieren</button>
                <button v-if="w.internal_code" class="linkbtn danger" :disabled="wageSaving"
                  style="margin-left: 8px" @click="deleteWageType(w)">Löschen</button>
              </td>
            </tr>
          </tbody>
        </table>

        <!-- Abrechnungsprofile: abweichende Zuordnungen pro Mitarbeiter-Gruppe -->
        <div style="padding: 14px 18px 18px; border-top: 1px solid var(--divider)">
          <div class="spread" style="margin-bottom: 4px">
            <div class="label">Abrechnungsprofile — was wohin gebucht wird</div>
            <button class="btn" style="padding: 5px 10px" @click="startProfileEdit()">+ Profil</button>
          </div>
          <div class="muted" style="font-size: 12.5px; margin-bottom: 12px">
            Der Export berechnet die Kategorien automatisch aus den Stempeldaten und bucht sie auf die
            im Profil zugeordnete Lohnart. „Standard" gilt für alle Mitarbeiter ohne eigenes Profil;
            weitere Profile überschreiben einzelne Zuordnungen (Rest = wie Standard). Zugewiesen wird
            das Profil beim Mitarbeiter (Stammdaten → Abrechnungsprofil).
          </div>
          <div v-if="profileError" class="wip-banner" style="margin-bottom: 10px">{{ profileError }}</div>

          <div v-for="p in wageProfiles" :key="p.id"
            style="border: 1px solid var(--divider); border-radius: 10px; padding: 10px 14px; margin-bottom: 8px">
            <div class="spread" style="flex-wrap: wrap; gap: 8px">
              <div class="row" style="gap: 10px; align-items: baseline">
                <strong style="font-size: 13.5px">{{ p.name }}</strong>
                <span class="muted" style="font-size: 12px">
                  {{ Object.keys(p.mapping || {}).length }} Zuordnung(en) · {{ p.employee_count || 0 }} Mitarbeiter
                </span>
              </div>
              <span class="row" style="gap: 12px; align-items: center">
                <template v-if="profDeleteConfirmId === p.id">
                  <span class="muted" style="font-size: 12.5px">
                    Profil wirklich löschen{{ (p.employee_count || 0) > 0 ? ` (${p.employee_count} Mitarbeiter fallen auf Standard zurück)` : "" }}?
                  </span>
                  <button class="linkbtn danger" :disabled="profileBusy"
                    @click="deleteWageProfile(p); profDeleteConfirmId = null">Ja, löschen</button>
                  <button class="linkbtn" :disabled="profileBusy" @click="profDeleteConfirmId = null">Abbrechen</button>
                </template>
                <template v-else>
                  <button class="linkbtn" :disabled="profileBusy"
                    @click="profMembersOpen[p.id] = !profMembersOpen[p.id]">
                    Mitarbeiter {{ profMembersOpen[p.id] ? "▾" : "▸" }}
                  </button>
                  <button class="linkbtn" :disabled="profileBusy" @click="startProfileEdit(p)">Bearbeiten</button>
                  <button v-if="!p.is_standard" class="linkbtn danger" :disabled="profileBusy"
                    @click="profDeleteConfirmId = p.id">Löschen</button>
                </template>
              </span>
            </div>

            <!-- Mitglieder: Chips + Hinzufügen -->
            <div v-if="profMembersOpen[p.id]" style="margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--divider)">
              <div class="row" style="flex-wrap: wrap; gap: 8px; margin-bottom: 10px">
                <span v-if="!profileMembers(p).length" class="muted" style="font-size: 12.5px">
                  {{ p.is_standard ? "Niemand auf Standard." : "Noch keine Mitarbeiter in diesem Profil." }}
                </span>
                <span v-for="e in profileMembers(p)" :key="e.id" class="row"
                  style="gap: 6px; align-items: center; border: 1px solid var(--divider); border-radius: 999px; padding: 4px 10px; font-size: 13px">
                  {{ e.name }}
                  <template v-if="!p.is_standard">
                    <template v-if="profRemoveConfirmId === e.id">
                      <span class="muted" style="font-size: 12px">zurück auf Standard?</span>
                      <button class="linkbtn danger" style="font-size: 12.5px; line-height: 1" :disabled="profileBusy"
                        @click="moveEmployeeToProfile(e.id, null); profRemoveConfirmId = null">Ja</button>
                      <button class="linkbtn" style="font-size: 12.5px; line-height: 1" :disabled="profileBusy"
                        @click="profRemoveConfirmId = null">Nein</button>
                    </template>
                    <button v-else class="linkbtn danger" style="font-size: 13px; line-height: 1"
                      title="Zurück auf Standard" :disabled="profileBusy"
                      @click="profRemoveConfirmId = e.id">✕</button>
                  </template>
                </span>
              </div>
              <div v-if="!p.is_standard" class="row" style="gap: 8px; align-items: center">
                <select v-model="profAddSel[p.id]" class="input" style="width: 300px">
                  <option value="">Mitarbeiter hinzufügen …</option>
                  <option v-for="e in profileEmployees.filter(x => x.wage_profile_id !== p.id)" :key="e.id" :value="e.id">
                    {{ e.name }} — aktuell: {{ currentProfileName(e) }}
                  </option>
                </select>
                <button class="btn btn-primary" style="padding: 6px 12px"
                  :disabled="profileBusy || !profAddSel[p.id]"
                  @click="moveEmployeeToProfile(profAddSel[p.id]!, p); profAddSel[p.id] = ''">Zuweisen</button>
              </div>
              <div v-else class="muted" style="font-size: 12px">
                Standard-Mitglieder wechseln automatisch, sobald sie einem anderen Profil zugewiesen werden.
              </div>
            </div>
          </div>

          <div v-if="profileEditId" style="border: 1px dashed var(--divider); border-radius: 10px; padding: 12px 14px; margin-top: 4px">
            <div class="row" style="gap: 12px; flex-wrap: wrap; align-items: flex-end; margin-bottom: 12px">
              <div class="stack" style="gap: 4px">
                <span class="label">Profil-Name</span>
                <input v-model="profileForm.name" class="input" style="width: 260px"
                  placeholder="z.B. Schichtarbeiter" />
              </div>
            </div>
            <div class="stack" style="gap: 8px; margin-bottom: 12px">
              <div v-for="c in assignCategories" :key="c.code" class="row" style="gap: 12px; flex-wrap: wrap">
                <span style="min-width: 220px">{{ c.label }}</span>
                <select class="input" style="width: auto; min-width: 280px"
                  :value="profileForm.mapping[c.code] ?? ''"
                  @change="setProfileMapping(c.code, ($event.target as HTMLSelectElement).value)">
                  <option value="">{{ profileEditId === "standard" ? "— nicht zugeordnet —" : inheritedMappingLabel(c.code) }}</option>
                  <option v-for="o in assignOptions" :key="o.internal_code" :value="o.internal_code">
                    {{ o.datev_lohnart }} · {{ o.description || o.internal_code }}
                  </option>
                </select>
              </div>
            </div>
            <div v-if="profileEditId !== 'standard'" class="stack" style="gap: 10px; margin-bottom: 12px">
              <div class="row" style="gap: 12px; flex-wrap: wrap; align-items: center">
                <div class="stack" style="gap: 4px">
                  <span class="label">Arbeitszeit frühestens ab</span>
                  <input v-model="profileForm.fruehester_beginn" type="time" class="input" style="width: 140px" />
                </div>
                <span class="muted" style="font-size: 12px; max-width: 320px">
                  Stempelzeiten vor dieser Uhrzeit werden ab dieser Uhrzeit gewertet. Leer = keine Kappung.
                </span>
              </div>
              <div class="row" style="gap: 12px; flex-wrap: wrap; align-items: center">
                <div class="stack" style="gap: 4px">
                  <span class="label">Mehrarbeit ohne Zuschlag (Std/Woche)</span>
                  <input v-model="profileForm.mehrarbeit_frei_wochen_std" type="number" step="0.25" min="0"
                    class="input" style="width: 140px" />
                </div>
                <span class="muted" style="font-size: 12px; max-width: 320px">
                  Leer = wie Mandanten-Einstellung (Einstellungen → Zuschläge).
                </span>
              </div>
              <div class="stack" style="gap: 4px">
                <label class="row" style="gap: 8px; align-items: center">
                  <input v-model="profileForm.wochenende_ins_konto" type="checkbox" />
                  <span>Wochenend-/Feiertagsarbeit zählt ins Überstundenkonto</span>
                </label>
                <span class="muted" style="font-size: 12px">
                  Sonst laufen diese Stunden nur über die Zuschlags-Lohnarten.
                </span>
              </div>
            </div>
            <div class="row" style="gap: 8px">
              <button class="btn btn-primary" :disabled="profileBusy" @click="saveWageProfile">Speichern</button>
              <button class="btn" :disabled="profileBusy" @click="cancelProfileEdit">Abbrechen</button>
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- 1b) Zuschlags-Zeiten -->
    <div class="card" style="padding: 0; margin-bottom: 16px">
      <div class="spread" style="padding: 16px 18px; cursor: pointer" @click="toggleSection('surcharge')">
        <h2>Zuschlags-Zeiten</h2>
        <div class="row" style="gap: 8px">
          <span v-if="surchargeOpen" class="row" style="gap: 8px" @click.stop>
            <template v-if="surchargeEdit">
              <button class="btn" @click="cancelSurchargeEdit">Abbrechen</button>
              <button class="btn btn-primary" :disabled="surchargeSaving" @click="saveSurchargeConfig">Speichern</button>
            </template>
            <button v-else class="btn" :disabled="surchargeLoading || !surchargeConfig" @click="startSurchargeEdit">Bearbeiten</button>
          </span>
          <span class="muted" style="font-size: 13px">{{ surchargeOpen ? "▾" : "▸" }}</span>
        </div>
      </div>

      <template v-if="surchargeOpen">
        <div v-if="surchargeError" class="wip-banner" style="margin: 0 18px 14px">{{ surchargeError }}</div>
        <div v-if="surchargeLoading" class="empty">Lädt …</div>

        <div v-else style="padding: 0 18px 18px">
          <div class="muted" style="font-size: 12px; margin-bottom: 12px">
            Steuert die Lohnarten-Zuordnung beim DATEV-Export: ab wann Arbeit als
            Samstags-/Sonntags-Zuschlag zählt und wann Nachtarbeit beginnt/endet.
          </div>

          <!-- Leseansicht -->
          <div v-if="!surchargeEdit && surchargeConfig">
            <div>
              Nachtarbeit: {{ surchargeConfig.night_start }}–{{ surchargeConfig.night_end }} Uhr ·
              Samstag ab {{ surchargeConfig.saturday_from }} Uhr ·
              Sonntag ab {{ surchargeConfig.sunday_from }} Uhr
            </div>
            <div style="margin-top: 6px">
              Export-Basis:
              {{ surchargeConfig.export_basis === "soll" ? "Sollzeit + Mehrarbeit" : "Istzeit" }}
              <template v-if="surchargeConfig.export_basis === 'soll'">
                · Mehrarbeit ohne Zuschlag: {{ surchargeConfig.mehrarbeit_frei_wochen_std }} Std/Woche
              </template>
            </div>
          </div>
          <div v-else-if="!surchargeEdit" class="empty" style="padding: 0">Keine Konfiguration vorhanden.</div>

          <!-- Bearbeiten -->
          <div v-else class="row" style="flex-wrap: wrap; gap: 12px; align-items: flex-end">
            <div class="stack" style="gap: 4px; width: 150px">
              <span class="label">Nachtarbeit ab</span>
              <select v-model.number="surchargeDraft.night_start" class="input">
                <option v-for="o in hourOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
            </div>
            <div class="stack" style="gap: 4px; width: 150px">
              <span class="label">Nachtarbeit bis</span>
              <select v-model.number="surchargeDraft.night_end" class="input">
                <option v-for="o in hourOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
            </div>
            <div class="stack" style="gap: 4px; width: 150px">
              <span class="label">Samstag ab</span>
              <select v-model.number="surchargeDraft.saturday_from" class="input">
                <option v-for="o in hourOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
            </div>
            <div class="stack" style="gap: 4px; width: 150px">
              <span class="label">Sonntag ab</span>
              <select v-model.number="surchargeDraft.sunday_from" class="input">
                <option v-for="o in hourOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
            </div>
            <div class="stack" style="gap: 4px; width: 220px">
              <span class="label">Export-Basis</span>
              <select
                v-model="surchargeDraft.export_basis"
                class="input"
                title="Sollzeit + Mehrarbeit: Zeitlohn = Tagessoll, Differenzen laufen als Mehrarbeit-Staffel bzw. Minusstunden (wie das Altsystem). Istzeit: gearbeitete Stunden werden direkt gebucht."
              >
                <option value="soll">Sollzeit + Mehrarbeit</option>
                <option value="ist">Istzeit</option>
              </select>
            </div>
            <div v-if="surchargeDraft.export_basis === 'soll'" class="stack" style="gap: 4px; width: 220px">
              <span class="label">Mehrarbeit ohne Zuschlag (Std/Woche)</span>
              <input
                v-model.number="surchargeDraft.mehrarbeit_frei_wochen_std"
                type="number"
                step="0.25"
                min="0"
                class="input"
                title="Wochenkontingent an Mehrarbeit ohne Zuschlag, z.B. 1,5 Std (Zone 38,5 bis 40 Std) — erst darüber greift die Zuschlags-Lohnart"
              />
            </div>
          </div>
        </div>

        <!-- Monatsabschluss zurücksetzen: nach nachträglichen Korrekturen neu rechnen -->
        <div style="padding: 14px 18px 18px; border-top: 1px solid var(--divider)">
          <div class="label" style="margin-bottom: 4px">Überstunden neu berechnen</div>
          <div class="muted" style="font-size: 12px; margin-bottom: 10px">
            {{ neuberechnenHinweis }}
          </div>
          <div class="row" style="flex-wrap: wrap; gap: 10px; align-items: flex-end">
            <div class="stack" style="gap: 4px; width: 140px">
              <span class="label">Monat</span>
              <select v-model.number="resettleMonth" class="input">
                <option v-for="m in 12" :key="m" :value="m">{{ monthNames[m - 1] }}</option>
              </select>
            </div>
            <div class="stack" style="gap: 4px; width: 110px">
              <span class="label">Jahr</span>
              <input v-model.number="resettleYear" type="number" class="input" />
            </div>
            <template v-if="resettleConfirm">
              <span class="muted" style="font-size: 12.5px">Überstunden für {{ monthNames[resettleMonth - 1] }} {{ resettleYear }} wirklich neu berechnen?</span>
              <button class="linkbtn danger" :disabled="resettleBusy" @click="runResettle">Ja, neu berechnen</button>
              <button class="linkbtn" :disabled="resettleBusy" @click="resettleConfirm = false">Abbrechen</button>
            </template>
            <button v-else class="btn" :disabled="resettleBusy || neuberechnenGesperrt"
              :title="neuberechnenHinweis" @click="resettleConfirm = true">Überstunden neu berechnen</button>
          </div>
          <div v-if="resettleError" class="wip-banner" style="margin-top: 10px">{{ resettleError }}</div>
          <div v-if="resettleDone" class="stack" style="gap: 4px; margin-top: 12px">
            <div class="muted" style="font-size: 12px">Änderungen:</div>
            <div v-if="!resettleChanged.length" class="muted" style="font-size: 12.5px">
              Keine Änderungen — Auszahlungen waren bereits aktuell.
            </div>
            <div v-for="r in resettleChanged" :key="r.employee_id" style="font-size: 13px">
              {{ r.employee_number || r.employee_id }}: {{ r.alte_auszahlung.toFixed(2) }} h → {{ r.neue_auszahlung.toFixed(2) }} h
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- 1c) Arbeitszeit-Profile -->
    <div class="card" style="padding: 0; margin-bottom: 16px">
      <div class="spread" style="padding: 16px 18px; cursor: pointer" @click="toggleSection('wtprofiles')">
        <h2>Arbeitszeit-Profile <span class="muted" style="font-weight: 400; font-size: 13px">({{ wtProfiles.length }})</span></h2>
        <div class="row" style="gap: 8px">
          <span v-if="wtpOpen" class="row" style="gap: 8px" @click.stop>
            <button v-if="!wtpEditId" class="btn" @click="startWtpEdit()">+ Profil</button>
          </span>
          <span class="muted" style="font-size: 13px">{{ wtpOpen ? "▾" : "▸" }}</span>
        </div>
      </div>

      <template v-if="wtpOpen">
        <div style="padding: 0 18px 18px">
          <div class="muted" style="font-size: 12.5px; margin-bottom: 12px">
            NFC-Profile kappen Stempelzeiten außerhalb des erlaubten Fensters (die Roh-Stempel
            bleiben erhalten); Auto-Profile bestimmen die automatisch erfassten Zeiten und das
            Tages-Soll je Wochentag. Zugewiesen wird das Profil beim Mitarbeiter
            (Stammdaten → Arbeitszeit-Profil) — passend zum Erfassungsmodus.
          </div>
          <div v-if="wtpError" class="wip-banner" style="margin-bottom: 10px">{{ wtpError }}</div>
          <div v-if="wtpLoading" class="empty" style="padding: 8px 0">Lädt …</div>
          <div v-else-if="!wtProfiles.length && !wtpEditId" class="empty" style="padding: 8px 0">Keine Profile vorhanden.</div>

          <div v-for="p in wtProfiles" :key="p.id"
            style="border: 1px solid var(--divider); border-radius: 10px; padding: 10px 14px; margin-bottom: 8px">
            <div class="spread" style="flex-wrap: wrap; gap: 8px">
              <div class="row" style="gap: 10px; align-items: baseline; flex-wrap: wrap">
                <strong style="font-size: 13.5px">{{ p.name }}</strong>
                <span class="badge neutral">{{ p.kind === "auto" ? "Automatisch" : "NFC" }}</span>
                <span class="muted" style="font-size: 12px">{{ wtpTimesSummary(p) }}</span>
                <span v-if="wtpHoursSummary(p)" class="muted" style="font-size: 12px">{{ wtpHoursSummary(p) }}</span>
                <span class="muted" style="font-size: 12px">· {{ p.assigned_count || 0 }} Mitarbeiter</span>
                <span class="badge" :class="p.is_active ? 'green' : 'neutral'">
                  <span class="dot" />{{ p.is_active ? "Aktiv" : "Inaktiv" }}
                </span>
              </div>
              <span class="row" style="gap: 12px; align-items: center">
                <template v-if="wtpDeleteConfirmId === p.id">
                  <span class="muted" style="font-size: 12.5px">
                    Profil wirklich löschen{{ (p.assigned_count || 0) > 0 ? ` (${p.assigned_count} Mitarbeiter verlieren das Profil)` : "" }}?
                  </span>
                  <button class="linkbtn danger" :disabled="wtpBusy"
                    @click="deleteWtp(p); wtpDeleteConfirmId = null">Ja, löschen</button>
                  <button class="linkbtn" :disabled="wtpBusy" @click="wtpDeleteConfirmId = null">Abbrechen</button>
                </template>
                <template v-else>
                  <button class="linkbtn" :disabled="wtpBusy"
                    @click="wtpMembersOpen[p.id] = !wtpMembersOpen[p.id]">
                    Mitarbeiter {{ wtpMembersOpen[p.id] ? "▾" : "▸" }}
                  </button>
                  <button class="linkbtn" :disabled="wtpBusy" @click="startWtpEdit(p)">Bearbeiten</button>
                  <button class="linkbtn danger" :disabled="wtpBusy" @click="wtpDeleteConfirmId = p.id">Löschen</button>
                </template>
              </span>
            </div>

            <!-- Mitglieder: Chips + Zuweisen (Muster Abrechnungsprofile) -->
            <div v-if="wtpMembersOpen[p.id]" style="margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--divider)">
              <div class="row" style="flex-wrap: wrap; gap: 8px; margin-bottom: 10px">
                <span v-if="!wtpMembers(p).length" class="muted" style="font-size: 12.5px">
                  Noch keine Mitarbeiter in diesem Profil.
                </span>
                <span v-for="e in wtpMembers(p)" :key="e.id" class="row"
                  style="gap: 6px; align-items: center; border: 1px solid var(--divider); border-radius: 999px; padding: 4px 10px; font-size: 13px">
                  {{ e.name }}
                  <template v-if="wtpRemoveConfirmId === e.id">
                    <span class="muted" style="font-size: 12px">Profil entfernen?</span>
                    <button class="linkbtn danger" style="font-size: 12.5px; line-height: 1" :disabled="wtpBusy"
                      @click="wtpAssign(e.id, null); wtpRemoveConfirmId = null">Ja</button>
                    <button class="linkbtn" style="font-size: 12.5px; line-height: 1" :disabled="wtpBusy"
                      @click="wtpRemoveConfirmId = null">Nein</button>
                  </template>
                  <button v-else class="linkbtn danger" style="font-size: 13px; line-height: 1"
                    title="Profil entfernen" :disabled="wtpBusy"
                    @click="wtpRemoveConfirmId = e.id">✕</button>
                </span>
              </div>
              <div class="row" style="gap: 8px; align-items: center">
                <select v-model="wtpAddSel[p.id]" class="input" style="width: 300px">
                  <option value="">Mitarbeiter hinzufügen …</option>
                  <option v-for="e in wtpCandidates(p)" :key="e.id" :value="e.id">
                    {{ e.name }} — aktuell: {{ wtpCurrentName(e) }}
                  </option>
                </select>
                <button class="btn btn-primary" style="padding: 6px 12px"
                  :disabled="wtpBusy || !wtpAddSel[p.id]"
                  @click="wtpAssign(wtpAddSel[p.id]!, p); wtpAddSel[p.id] = ''">Zuweisen</button>
              </div>
              <div class="muted" style="font-size: 12px; margin-top: 6px">
                Angeboten werden nur Mitarbeiter mit passendem Erfassungsmodus
                ({{ p.kind === "auto" ? "Automatisch" : "NFC/Web" }}){{ wtpHoursHint(p) }}.
              </div>
            </div>
          </div>

          <!-- Editor: Anlegen (Typ wählbar) / Bearbeiten (Typ fest) -->
          <div v-if="wtpEditId" style="border: 1px dashed var(--divider); border-radius: 10px; padding: 12px 14px; margin-top: 4px">
            <div v-if="wtpFormError" class="wip-banner" style="margin-bottom: 10px">{{ wtpFormError }}</div>
            <div class="row" style="gap: 14px; flex-wrap: wrap; align-items: flex-end; margin-bottom: 12px">
              <div class="stack" style="gap: 4px">
                <span class="label">Name</span>
                <input v-model="wtpForm.name" class="input" style="width: 240px" placeholder="z. B. Frühschicht" />
              </div>
              <div class="stack" style="gap: 4px">
                <span class="label">Typ</span>
                <select v-if="wtpEditId === 'new'" v-model="wtpForm.kind" class="input" style="width: 230px">
                  <option value="nfc">NFC — Stempler (NFC/Web)</option>
                  <option value="auto">Automatisch — Auto-Erfassung</option>
                </select>
                <span v-else style="padding: 6px 0">
                  {{ wtpForm.kind === "auto" ? "Automatisch" : "NFC" }}
                  <span class="muted" style="font-size: 12px">(nach dem Anlegen fest)</span>
                </span>
              </div>
              <label class="row" style="gap: 8px; align-items: center; padding-bottom: 6px; cursor: pointer">
                <input v-model="wtpForm.is_active" type="checkbox" />
                <span>Aktiv</span>
              </label>
            </div>

            <!-- NFC: Kappungsfenster -->
            <div v-if="wtpForm.kind === 'nfc'" class="stack" style="gap: 8px; margin-bottom: 12px">
              <div class="row" style="gap: 14px; flex-wrap: wrap; align-items: flex-end">
                <div class="stack" style="gap: 4px">
                  <span class="label">Stempelung ab</span>
                  <input v-model="wtpForm.clock_start" type="time" class="input" style="width: 140px" />
                </div>
                <div class="stack" style="gap: 4px">
                  <span class="label">Stempelung bis</span>
                  <input v-model="wtpForm.clock_end" type="time" class="input" style="width: 140px" />
                </div>
              </div>
              <span class="muted" style="font-size: 12px">
                Stempel außerhalb des Fensters werden auf die Grenze gewertet. Beide Felder optional —
                leer = keine Kappung an dieser Seite. „Bis" vor „ab" = über Mitternacht (Nachtschicht, z. B. 22:00–06:15).
              </span>

              <!-- Vertragsstunden + Tagessoll (Nachtrag 14.08.): nur gemeinsam,
                   Summe der Tagessolls muss exakt Stunden × 60 ergeben (Server 422). -->
              <div class="stack" style="gap: 4px; margin-top: 8px">
                <span class="label">Vertragsstunden/Woche</span>
                <input v-model="wtpForm.contracted_hours" type="number" step="0.5" min="0.5" max="80"
                  class="input" style="width: 140px" placeholder="z. B. 40" />
              </div>
              <div class="stack" style="gap: 6px">
                <span class="label">Tagessoll</span>
                <div class="row" style="gap: 12px 18px; flex-wrap: wrap">
                  <div v-for="(d, i) in wtpForm.targets" :key="i" class="row" style="gap: 6px; align-items: center">
                    <span class="label" style="margin: 0; width: 26px">{{ WTP_WEEKDAYS[i] }}</span>
                    <input v-model.number="d.h" type="number" min="0" max="24" class="input"
                      style="width: 56px; text-align: right; padding: 4px 6px" /><span class="muted">:</span>
                    <input v-model.number="d.m" type="number" min="0" max="59" class="input"
                      style="width: 56px; text-align: right; padding: 4px 6px" />
                  </div>
                </div>
                <span style="font-size: 12.5px" :class="wtpSumMismatch ? 'neg' : 'muted'">
                  Summe: <strong>{{ fmtMinHM(wtpTargetSumMin) }}</strong>
                  <template v-if="wtpContractedNum != null"> von {{ fmtMinHM(Math.round(wtpContractedNum * 60)) }} h</template>
                  <template v-else> h — Vertragsstunden angeben</template>
                  <template v-if="wtpSumMismatch"> — Summe muss den Vertragsstunden entsprechen</template>
                </span>
                <span v-if="wtpEditId !== 'new' && !wtpEditHadHours" class="muted" style="font-size: 12px">
                  Bestandsprofil ohne Stundenbindung — Felder leer lassen oder beide ausfüllen.
                </span>
              </div>
            </div>

            <!-- Automatisch: Wochentagsplan Mo–So -->
            <div v-else class="stack" style="gap: 6px; margin-bottom: 12px">
              <div v-for="(d, i) in wtpForm.days" :key="i" class="row" style="gap: 10px; flex-wrap: wrap; align-items: center">
                <label class="row" style="gap: 8px; align-items: center; width: 64px; cursor: pointer">
                  <input v-model="d.enabled" type="checkbox" />
                  <span class="label" style="margin: 0">{{ WTP_WEEKDAYS[i] }}</span>
                </label>
                <template v-if="d.enabled">
                  <input v-model="d.start" type="time" class="input" style="width: auto; padding: 4px 6px" />
                  <span class="muted">–</span>
                  <input v-model="d.end" type="time" class="input" style="width: auto; padding: 4px 6px" />
                  <span class="muted" style="font-size: 12px; margin-left: 8px">Pause</span>
                  <input v-model="d.break_start" type="time" class="input" style="width: auto; padding: 4px 6px" />
                  <span class="muted">–</span>
                  <input v-model="d.break_end" type="time" class="input" style="width: auto; padding: 4px 6px" />
                </template>
                <span v-else class="muted" style="font-size: 12.5px">frei</span>
              </div>
              <span class="muted" style="font-size: 12px">
                Tages-Soll = Arbeitsfenster − Pause. Pause optional (leer = keine feste Pause);
                die gesetzliche Mindestpause bleibt Untergrenze.
              </span>
              <!-- Vertragsstunden bei 'auto': readonly, live aus den Tagesfenstern
                   berechnet (Backend rechnet identisch und liefert den Wert mit). -->
              <span style="font-size: 12.5px">
                ergibt <strong>{{ wtpAutoWeekHours != null ? `${wtpAutoWeekHours} h/Woche` : "— (keine Arbeitstage)" }}</strong>
              </span>
            </div>

            <div class="row" style="gap: 8px">
              <button class="btn btn-primary" :disabled="wtpBusy" @click="saveWtp">
                {{ wtpBusy ? "Speichert …" : "Speichern" }}
              </button>
              <button class="btn" :disabled="wtpBusy" @click="cancelWtpEdit">Abbrechen</button>
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- 2) Bereiche -->
    <div class="card" style="padding: 0; margin-bottom: 16px">
      <div class="spread" style="padding: 16px 18px; cursor: pointer" @click="toggleSection('dep')">
        <h2>Bereiche <span class="muted" style="font-weight: 400; font-size: 13px">({{ departments.length }})</span></h2>
        <div class="row" style="gap: 8px">
          <span v-if="depOpen" class="row" style="gap: 8px" @click.stop>
            <template v-if="depEdit">
              <button class="btn" @click="cancelDepEdit">Abbrechen</button>
              <button class="btn btn-primary" @click="finishDepEdit">Fertig</button>
            </template>
            <button v-else class="btn" @click="startDepEdit">Bearbeiten</button>
          </span>
          <span class="muted" style="font-size: 13px">{{ depOpen ? "▾" : "▸" }}</span>
        </div>
      </div>

      <template v-if="depOpen">
        <div v-if="depError" class="wip-banner" style="margin: 0 18px 14px">{{ depError }}</div>
        <div v-if="depLoading" class="empty">Lädt …</div>

        <!-- Leseansicht: Chips, nach Firma gruppiert -->
        <div v-else-if="!depEdit" style="padding: 0 18px 18px">
          <div v-if="!departments.length" class="empty">Keine Bereiche vorhanden.</div>
          <div v-else class="row" style="flex-wrap: wrap; gap: 8px">
            <span v-for="d in sortedDepartments" :key="d.id" class="badge neutral">
              <span class="dot" />{{ d.name }}
            </span>
          </div>
        </div>

        <!-- Bearbeiten -->
        <div v-else style="padding: 0 18px 18px">
          <div class="stack" style="gap: 10px">
            <div v-for="d in depDraft" :key="d.id" class="row" style="flex-wrap: wrap; gap: 8px">
              <input v-model="d.name" class="input" style="flex: 1; min-width: 160px" placeholder="Name" />
              <button class="btn btn-primary" :disabled="depSaving" @click="saveDep(d)">Speichern</button>
              <template v-if="depDeleteConfirmId === d.id">
                <span class="muted" style="font-size: 12.5px">Wirklich entfernen?</span>
                <button class="linkbtn danger" :disabled="depSaving" @click="deleteDep(d)">Ja, entfernen</button>
                <button class="linkbtn" :disabled="depSaving" @click="depDeleteConfirmId = null">Abbrechen</button>
              </template>
              <button v-else class="linkbtn danger" :disabled="depSaving" @click="depDeleteConfirmId = d.id">Entfernen</button>
            </div>
          </div>

          <div class="row" style="flex-wrap: wrap; gap: 8px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--divider)">
            <input v-model="newDep.name" class="input" style="flex: 1; min-width: 160px" placeholder="Neuer Bereich – Name" />
            <button class="btn" :disabled="depSaving || !newDep.name.trim()" @click="createDep">+ Bereich</button>
          </div>
        </div>
      </template>
    </div>

    <!-- 3) Firmen -->
    <div class="card" style="padding: 0; margin-bottom: 16px">
      <div class="spread" style="padding: 16px 18px; cursor: pointer" @click="toggleSection('company')">
        <h2>Firmen <span class="muted" style="font-weight: 400; font-size: 13px">({{ companies.length }})</span></h2>
        <div class="row" style="gap: 8px">
          <span v-if="companiesOpen" class="row" style="gap: 8px" @click.stop>
            <template v-if="companyEdit">
              <button class="btn" @click="cancelCompanyEdit">Abbrechen</button>
              <button class="btn btn-primary" @click="finishCompanyEdit">Fertig</button>
            </template>
            <button v-else class="btn" @click="startCompanyEdit">Bearbeiten</button>
          </span>
          <span class="muted" style="font-size: 13px">{{ companiesOpen ? "▾" : "▸" }}</span>
        </div>
      </div>

      <template v-if="companiesOpen">
        <div v-if="companyError" class="wip-banner" style="margin: 0 18px 14px">{{ companyError }}</div>
        <div v-if="companyLoading" class="empty">Lädt …</div>

        <!-- Leseansicht: Chips -->
        <div v-else-if="!companyEdit" style="padding: 0 18px 18px">
          <div v-if="!companies.length" class="empty">Keine Firmen vorhanden.</div>
          <div v-else class="stack" style="gap: 8px">
            <div v-for="c in companies" :key="c.id" class="row" style="gap: 10px; align-items: baseline">
              <span class="badge neutral"><span class="dot" />{{ c.name }}</span>
              <span class="muted" style="font-size: 12px">
                Berater-Nr. {{ c.advisor_number || "—" }} · Mandanten-Nr. {{ c.client_number || "—" }}
              </span>
            </div>
          </div>
        </div>

        <!-- Bearbeiten -->
        <div v-else style="padding: 0 18px 18px">
          <div class="muted" style="font-size: 12px; margin-bottom: 12px">
            Berater-Nr. und Mandanten-Nr. werden für den DATEV-LODAS-Export verwendet (DATEV-Mandant pro Firma).
          </div>
          <div class="stack" style="gap: 10px">
            <div v-for="c in companyDraft" :key="c.id" class="row" style="flex-wrap: wrap; gap: 8px">
              <input v-model="c.name" class="input" style="flex: 1; min-width: 160px" placeholder="Name" />
              <input v-model="c.advisor_number" class="input" style="width: 140px" placeholder="Berater-Nr."
                title="DATEV-LODAS: Berater-Nummer für den Lohnexport" />
              <input v-model="c.client_number" class="input" style="width: 140px" placeholder="Mandanten-Nr."
                title="DATEV-LODAS: Mandanten-Nummer für den Lohnexport" />
              <button class="btn btn-primary" :disabled="companySaving" @click="saveCompany(c)">Speichern</button>
              <template v-if="companyDeleteConfirmId === c.id">
                <span class="muted" style="font-size: 12.5px">Wirklich entfernen?</span>
                <button class="linkbtn danger" :disabled="companySaving" @click="deleteCompany(c)">Ja, entfernen</button>
                <button class="linkbtn" :disabled="companySaving" @click="companyDeleteConfirmId = null">Abbrechen</button>
              </template>
              <button v-else class="linkbtn danger" :disabled="companySaving" @click="companyDeleteConfirmId = c.id">Entfernen</button>
            </div>
          </div>

          <div class="row" style="flex-wrap: wrap; gap: 8px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--divider)">
            <input v-model="newCompany.name" class="input" style="flex: 1; min-width: 160px" placeholder="Neue Firma – Name" />
            <input v-model="newCompany.advisor_number" class="input" style="width: 140px" placeholder="Berater-Nr."
              title="DATEV-LODAS: Berater-Nummer für den Lohnexport" />
            <input v-model="newCompany.client_number" class="input" style="width: 140px" placeholder="Mandanten-Nr."
              title="DATEV-LODAS: Mandanten-Nummer für den Lohnexport" />
            <button class="btn" :disabled="companySaving" @click="createCompany">+ Firma</button>
          </div>
        </div>
      </template>
    </div>

    <!-- 4) NFC-Karten -->
    <div class="card" style="padding: 0; margin-bottom: 16px">
      <div class="spread" style="padding: 16px 18px; cursor: pointer" @click="toggleSection('cards')">
        <h2>NFC-Karten <span class="muted" style="font-weight: 400; font-size: 13px">({{ cardRows.length }})</span></h2>
        <span class="muted" style="font-size: 13px">{{ cardsOpen ? "▾" : "▸" }}</span>
      </div>

      <template v-if="cardsOpen">
        <div class="wip-banner" style="margin: 0 18px 14px">
          Es gibt keinen kombinierten „alle Karten“-Endpunkt. Diese Sicht ist aus
          freien Karten (/admin/scanned-cards) und zugewiesenen Karten
          (nfc_badge_id aus /admin/employees) zusammengesetzt.
        </div>

        <div v-if="cardError" class="wip-banner" style="margin: 0 18px 14px">{{ cardError }}</div>
        <div v-if="cardLoading" class="empty">Lädt …</div>

        <template v-else>
          <div v-if="cardRows.length" class="muted" style="margin: 0 18px 8px; font-size: 13px">
            Chip-UID per Handy-NFC-App auslesen und hier abgleichen, um die richtige Karte zuzuweisen.
          </div>

          <table class="data">
            <thead>
              <tr>
                <th>Karten-ID</th>
                <th>Chip-UID</th>
                <th>Zugewiesen an</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!cardRows.length">
                <td colspan="5" class="empty">Keine Karten vorhanden.</td>
              </tr>
              <tr v-for="(c, i) in cardRows" :key="`${c.uid}-${i}`">
                <td class="num">{{ c.uid }}</td>
                <td class="num" style="font-family: monospace">{{ c.chipUid || "—" }}</td>
                <td>{{ c.assignedTo || "—" }}</td>
                <td>
                  <span class="badge" :class="c.free ? 'amber' : 'green'">
                    <span class="dot" />{{ c.free ? "Nicht zugewiesen" : "Zugewiesen" }}
                  </span>
                </td>
                <td style="text-align: right; white-space: nowrap">
                  <span v-if="c.free && canManageCards" class="row" style="gap: 8px; justify-content: flex-end">
                    <select v-model="cardAssignSelection[c.uid]" class="input" style="width: 200px">
                      <option value="">Mitarbeiter wählen …</option>
                      <option v-for="e in unbadgedEmployees" :key="e.id" :value="e.id">
                        {{ e.department_name ? `${e.name} · ${e.department_name}` : e.name }}
                      </option>
                    </select>
                    <button class="btn btn-primary" :disabled="cardBusy || !cardAssignSelection[c.uid]"
                      style="padding: 6px 12px" @click="assignCard(c.uid)">Zuweisen</button>
                    <template v-if="cardRemoveConfirmUid === c.uid">
                      <span class="muted" style="font-size: 12.5px">Wirklich löschen?</span>
                      <button class="btn btn-danger" :disabled="cardBusy"
                        style="padding: 6px 12px" @click="removeCard(c.uid)">Ja, löschen</button>
                      <button class="btn" :disabled="cardBusy"
                        style="padding: 6px 12px" @click="cardRemoveConfirmUid = null">Abbrechen</button>
                    </template>
                    <button v-else class="btn btn-danger" :disabled="cardBusy"
                      style="padding: 6px 12px" @click="cardRemoveConfirmUid = c.uid">Löschen</button>
                  </span>
                  <span v-else-if="c.assignedUserId && canManageCards" class="row" style="gap: 8px; justify-content: flex-end">
                    <template v-if="cardUnassignConfirmUserId === c.assignedUserId">
                      <span class="muted" style="font-size: 12.5px">Zuweisung wirklich lösen?</span>
                      <button class="btn btn-danger" :disabled="cardBusy"
                        style="padding: 6px 12px" @click="unassignCard(c.assignedUserId)">Ja, lösen</button>
                      <button class="btn" :disabled="cardBusy"
                        style="padding: 6px 12px" @click="cardUnassignConfirmUserId = null">Abbrechen</button>
                    </template>
                    <button v-else class="btn" :disabled="cardBusy"
                      style="padding: 6px 12px" @click="cardUnassignConfirmUserId = c.assignedUserId">Zuweisung lösen</button>
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </template>
      </template>
    </div>

    <!-- 4) Rollen & Rechte -->
    <div class="card" style="padding: 0; margin-bottom: 16px">
      <div class="spread" style="padding: 16px 18px; cursor: pointer" @click="toggleSection('roles')">
        <h2>Rollen &amp; Rechte <span class="muted" style="font-weight: 400; font-size: 13px">({{ roles.length }})</span></h2>
        <div class="row" style="gap: 8px">
          <span v-if="rolesOpen && canManageRoles" class="row" style="gap: 8px" @click.stop>
            <button class="btn" @click="showNewRole = !showNewRole; if (showNewRole) closeOtherSettingsEdits('roles')">+ Rolle</button>
          </span>
          <span class="muted" style="font-size: 13px">{{ rolesOpen ? "▾" : "▸" }}</span>
        </div>
      </div>

      <template v-if="rolesOpen">
        <div v-if="roleError" class="wip-banner" style="margin: 0 18px 14px">{{ roleError }}</div>
        <div v-if="roleLoading" class="empty">Lädt …</div>

        <template v-else>
          <!-- Neue Rolle (nur Edit-Modus) -->
          <div v-if="showNewRole && canManageRoles" style="padding: 0 18px 18px; border-bottom: 1px solid var(--divider)">
            <div class="stack" style="gap: 10px">
              <div class="row" style="gap: 8px; flex-wrap: wrap; align-items: center">
                <span class="label" style="margin: 0">Vorlage:</span>
                <button v-for="t in roleTemplates" :key="t.label" class="btn" style="padding: 5px 10px"
                  @click="applyTemplate(t)">{{ t.label }}</button>
                <span class="muted" style="font-size: 12px">füllt Name, Sichtbarkeit und Rechte vor — danach frei anpassbar</span>
              </div>
              <div class="row" style="flex-wrap: wrap; gap: 8px; align-items: flex-end">
                <div class="stack" style="gap: 4px; flex: 1; min-width: 160px">
                  <span class="label">Name</span>
                  <input v-model="newRole.name" class="input" placeholder="Rollenname" />
                </div>
                <div class="stack" style="gap: 4px; width: 170px">
                  <span class="label">Typ</span>
                  <input v-model="newRole.role_type" class="input" list="role-type-suggestions" placeholder="z. B. mitarbeiter" />
                  <datalist id="role-type-suggestions">
                    <option value="mitarbeiter" /><option value="teamleiter" /><option value="hr" />
                    <option value="gf" /><option value="admin" /><option value="betriebsrat" />
                  </datalist>
                </div>
                <div class="stack" style="gap: 4px; width: 230px">
                  <span class="label">Sichtbarkeit (Datensicht)</span>
                  <select v-model="newRole.data_scope" class="input">
                    <option v-for="o in dataScopeOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
                  </select>
                </div>
              </div>
              <div class="stack" style="gap: 12px">
                <div v-for="g in permissionGroups" :key="g.group" class="stack" style="gap: 6px">
                  <div class="stack" style="gap: 0">
                    <span class="label">{{ g.group }}</span>
                    <span v-if="g.desc" class="muted" style="font-size: 12px">{{ g.desc }}</span>
                  </div>
                  <div class="row" style="flex-wrap: wrap; gap: 8px 18px">
                    <label v-for="p in g.items" :key="p.key" class="row"
                      :title="p.desc" style="gap: 6px; align-items: flex-start; width: 300px"
                      :style="permImplied(newRole.permissions, p.key) ? 'opacity: 0.55' : ''">
                      <input type="checkbox" style="margin-top: 3px"
                        :checked="permChecked(newRole.permissions, p.key)"
                        :disabled="permImplied(newRole.permissions, p.key)"
                        @change="togglePerm(newRole.permissions, p.key, ($event.target as HTMLInputElement).checked)" />
                      <span class="stack" style="gap: 0">
                        <span>{{ p.label }}
                          <span v-if="permImplied(newRole.permissions, p.key)" class="muted" style="font-size: 11px">— in „Alle" enthalten</span>
                        </span>
                        <span class="muted" style="font-size: 11.5px">{{ p.desc }}</span>
                      </span>
                    </label>
                  </div>
                </div>
              </div>
              <div class="row" style="gap: 8px">
                <button class="btn btn-primary" :disabled="roleSaving" @click="createRole">Anlegen</button>
                <button class="linkbtn" @click="showNewRole = false">Abbrechen</button>
              </div>
            </div>
          </div>

          <!-- Liste -->
          <div style="padding: 0 18px 18px">
            <div v-if="!roles.length" class="empty">Keine Rollen vorhanden.</div>
            <div class="stack" style="gap: 0">
              <div v-for="r in roles" :key="r.id" style="padding: 14px 0; border-bottom: 1px solid var(--divider)">
                <!-- Leseansicht mit eigenem Bearbeiten-Knopf pro Rolle -->
                <div v-if="editingRoleId !== r.id" class="spread" style="align-items: center">
                  <div class="stack" style="gap: 2px">
                    <strong>{{ r.name }}</strong>
                    <span class="muted" style="font-size: 12.5px">
                      {{ r.role_type || "—" }} ·
                      {{ dataScopeOptions.find(o => o.value === r.data_scope)?.label || r.data_scope || "—" }} ·
                      {{ (r.permissions?.length || 0) }} Rechte
                    </span>
                  </div>
                  <button v-if="canManageRoles" class="btn" style="padding: 6px 12px" @click="startRoleEdit(r)">Bearbeiten</button>
                </div>

                <!-- Editor (nur die eine, gerade bearbeitete Rolle) -->
                <div v-else-if="roleDrafts[r.id]" class="stack" style="gap: 10px">
                  <div class="row" style="flex-wrap: wrap; gap: 8px; align-items: flex-end">
                    <div class="stack" style="gap: 4px; flex: 1; min-width: 160px">
                      <span class="label">Name</span>
                      <input v-model="roleDrafts[r.id].name" class="input" placeholder="Rollenname" />
                    </div>
                    <div class="stack" style="gap: 4px; width: 170px">
                      <span class="label">Typ</span>
                      <input v-model="roleDrafts[r.id].role_type" class="input" list="role-type-suggestions" placeholder="z. B. mitarbeiter" />
                    </div>
                    <div class="stack" style="gap: 4px; width: 230px">
                      <span class="label">Sichtbarkeit (Datensicht)</span>
                      <select v-model="roleDrafts[r.id].data_scope" class="input">
                        <option v-for="o in dataScopeOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
                      </select>
                    </div>
                  </div>
                  <div class="stack" style="gap: 12px">
                    <div v-for="g in permissionGroups" :key="g.group" class="stack" style="gap: 6px">
                      <div class="stack" style="gap: 0">
                        <span class="label">{{ g.group }}</span>
                        <span v-if="g.desc" class="muted" style="font-size: 12px">{{ g.desc }}</span>
                      </div>
                      <div class="row" style="flex-wrap: wrap; gap: 8px 18px">
                        <label v-for="p in g.items" :key="p.key" class="row"
                          :title="p.desc" style="gap: 6px; align-items: flex-start; width: 300px"
                          :style="permImplied(roleDrafts[r.id]!.permissions, p.key) ? 'opacity: 0.55' : ''">
                          <input type="checkbox" style="margin-top: 3px"
                            :checked="permChecked(roleDrafts[r.id]!.permissions, p.key)"
                            :disabled="permImplied(roleDrafts[r.id]!.permissions, p.key)"
                            @change="togglePerm(roleDrafts[r.id]!.permissions, p.key, ($event.target as HTMLInputElement).checked)" />
                          <span class="stack" style="gap: 0">
                            <span>{{ p.label }}
                              <span v-if="permImplied(roleDrafts[r.id]!.permissions, p.key)" class="muted" style="font-size: 11px">— in „Alle" enthalten</span>
                            </span>
                            <span class="muted" style="font-size: 11.5px">{{ p.desc }}</span>
                          </span>
                        </label>
                      </div>
                    </div>
                  </div>
                  <div class="row" style="gap: 8px">
                    <button class="btn btn-primary" :disabled="roleSaving" @click="saveRole(r.id)">Speichern</button>
                    <button class="btn" :disabled="roleSaving" @click="cancelRolesEdit">Abbrechen</button>
                    <button class="linkbtn danger" :disabled="roleSaving" @click="deleteRole(r.id)">Entfernen</button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </template>
      </template>
    </div>

    <!-- Funktionen (optionale Bereiche) -->
    <div class="card" style="padding: 0; margin-bottom: 16px">
      <div class="spread" style="padding: 16px 18px; cursor: pointer" @click="toggleSection('features')">
        <h2>Funktionen</h2>
        <span class="muted" style="font-size: 13px">{{ featuresOpen ? "▾" : "▸" }}</span>
      </div>
      <template v-if="featuresOpen">
        <div style="padding: 0 18px 16px">
          <div class="muted" style="font-size: 12.5px; margin-bottom: 10px">
            Optionale Bereiche komplett ein-/ausschalten (Haupt-Schalter für die ganze Firma).
            WER etwas sieht, stellst du zusätzlich pro Rolle unter „Rollen &amp; Rechte" ein.
          </div>
          <div v-if="featureError" class="wip-banner" style="margin-bottom: 10px">{{ featureError }}</div>
          <label class="row" style="gap: 8px; align-items: center; margin-bottom: 8px">
            <input type="checkbox" :checked="featureFlags.org_chart !== false"
              :disabled="featureBusy || !canManageFeatures"
              @change="setFeature('org_chart', ($event.target as HTMLInputElement).checked)" />
            <span>Organigramm anzeigen</span>
            <span class="muted" style="font-size: 12px">— wer es sieht: Rechte „Organigramm (Bereich/Firma)" pro Rolle</span>
          </label>
          <label class="row" style="gap: 8px; align-items: center">
            <input type="checkbox" :checked="featureFlags.sick_mail !== false"
              :disabled="featureBusy || !canManageFeatures"
              @change="setFeature('sick_mail', ($event.target as HTMLInputElement).checked)" />
            <span>Krankmeldungen per E-Mail</span>
            <span class="muted" style="font-size: 12px">— Postfach-Eingang unter Mitarbeiter → Krankmeldungen (nur medical:read)</span>
          </label>
          <label v-if="featureFlags.sick_mail !== false" class="row"
            style="gap: 8px; align-items: center; margin-left: 26px; margin-top: 6px">
            <input type="checkbox" :checked="featureFlags.sick_mail_auto_approve === true"
              :disabled="featureBusy || !canManageFeatures"
              @change="setFeature('sick_mail_auto_approve', ($event.target as HTMLInputElement).checked)" />
            <span>Direkt genehmigen</span>
            <span class="muted" style="font-size: 12px">— erkannte Krankmeldungen sofort eintragen, ohne Genehmigungsschritt in den Anträgen</span>
          </label>
          <label class="row" style="gap: 8px; align-items: center; margin-top: 8px">
            <input type="checkbox" :checked="featureFlags.payroll_bookings === true"
              :disabled="featureBusy || !canManageFeatures"
              @change="setFeature('payroll_bookings', ($event.target as HTMLInputElement).checked)" />
            <span>Lohnbuchungen</span>
            <span class="muted" style="font-size: 12px">— Manuelle Pauschalen/Beträge je Mitarbeiter für den Lohn-Export</span>
          </label>
          <label class="row" style="gap: 8px; align-items: center; margin-top: 8px; flex-wrap: wrap">
            <span>Vergangene Arbeitstage im Teamkalender markieren</span>
            <select class="input" style="width: auto" :disabled="featureBusy || !canManageFeatures"
              :value="teamPastWorkedMode" @change="setTeamPastWorkedMode(($event.target as HTMLSelectElement).value)">
              <option value="off">Aus</option>
              <option value="all">Alle erfassten Tage (inkl. Automatik)</option>
              <option value="stamped">Nur echte Stempeltage</option>
            </select>
            <span class="muted" style="font-size: 12px">— grüner Punkt an vergangenen Arbeitstagen (heute = Live-Anwesenheit)</span>
          </label>

          <!-- Urlaubsanspruch-Default (7tu) -->
          <div style="margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--divider)">
            <div class="label">Urlaubsanspruch-Default (Tage/Jahr)</div>
            <div class="muted" style="font-size: 12.5px; margin: 4px 0 8px">
              Gilt für neue Mitarbeiter ohne eigenen Wert. Bestehende Mitarbeiter mit individuellem
              Urlaubsanspruch (Personalakte) sind davon nicht betroffen.
            </div>
            <div v-if="vacationError" class="wip-banner" style="margin-bottom: 10px">{{ vacationError }}</div>
            <div class="row" style="gap: 8px; flex-wrap: wrap; align-items: center">
              <input class="input" type="number" min="0" max="365" step="0.5" style="width: 100px"
                v-model="vacationDraft" :disabled="vacationBusy || !canManageSettings" />
              <button class="btn" style="padding: 6px 12px" :disabled="vacationBusy || !canManageSettings"
                @click="saveVacationDefault">Speichern</button>
              <button v-if="vacationSettings?.is_custom" class="linkbtn" :disabled="vacationBusy || !canManageSettings"
                @click="resetVacationDefault">Zurücksetzen (Standard {{ vacationSettings?.fallback_default }})</button>
              <span v-else class="muted" style="font-size: 12px">Standardwert, nicht angepasst</span>
            </div>
          </div>

          <!-- Urlaubsanspruch-Default (7tu) -->
          <div style="margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--divider)">
            <div class="label">Urlaubsanspruch-Default (Tage/Jahr)</div>
            <div class="muted" style="font-size: 12.5px; margin: 4px 0 8px">
              Gilt für neue Mitarbeiter ohne eigenen Wert. Bestehende Mitarbeiter mit individuellem
              Urlaubsanspruch (Personalakte) sind davon nicht betroffen.
            </div>
            <div v-if="vacationError" class="wip-banner" style="margin-bottom: 10px">{{ vacationError }}</div>
            <div class="row" style="gap: 8px; flex-wrap: wrap; align-items: center">
              <input class="input" type="number" min="0" max="365" step="0.5" style="width: 100px"
                v-model="vacationDraft" :disabled="vacationBusy || !canManageSettings" />
              <button class="btn" style="padding: 6px 12px" :disabled="vacationBusy || !canManageSettings"
                @click="saveVacationDefault">Speichern</button>
              <button v-if="vacationSettings?.is_custom" class="linkbtn" :disabled="vacationBusy || !canManageSettings"
                @click="resetVacationDefault">Zurücksetzen (Standard {{ vacationSettings?.fallback_default }})</button>
              <span v-else class="muted" style="font-size: 12px">Standardwert, nicht angepasst</span>
            </div>
          </div>

          <!-- Kalender-Abo (.ics) -->
          <div style="margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--divider)">
            <div class="spread" style="cursor: pointer" @click="icsFeedOpen = !icsFeedOpen">
              <div class="label">Team-Kalender-Abo (Outlook / Apple Kalender)</div>
              <span class="muted" style="font-size: 13px">{{ icsFeedOpen ? "▾" : "▸" }}</span>
            </div>
            <template v-if="icsFeedOpen">
            <div class="muted" style="font-size: 12.5px; margin: 4px 0 8px">
              Diese URL in Outlook (Desktop) oder Apple Kalender als Abo hinzufügen — Urlaub &amp;
              Abwesenheiten der ganzen Firma bleiben automatisch aktuell (Krank erscheint nur als
              „Abwesend"). Funktioniert nur im Firmennetz/VPN; das neue Outlook (Web) kann VPN-Adressen
              nicht abonnieren.
            </div>
            <div v-if="icsFeedInfo" class="muted" style="font-size: 12px; margin-bottom: 6px">{{ icsFeedInfo }}</div>
            <div class="row" style="gap: 8px; flex-wrap: wrap; align-items: center">
              <input class="input" style="flex: 1; min-width: 320px; font-size: 12px" readonly :value="icsFeedUrl" />
              <button class="btn" style="padding: 6px 12px" :disabled="!icsFeedUrl" @click="copyIcsFeed">Link kopieren</button>
              <button class="linkbtn" :disabled="icsFeedBusy || !canManageFeatures" title="Alte Abo-Links werden ungültig"
                @click="rotateIcsFeed">Neu erzeugen</button>
            </div>
            </template>
          </div>

          <!-- E-Mail-Postfaecher: generische Registry, von Krankmail-Abruf UND Stundenzettel-Versand nutzbar -->
          <div style="margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--divider)">
            <div class="spread" style="margin-bottom: 2px; cursor: pointer" @click="mailBoxesOpen = !mailBoxesOpen">
              <div class="label">E-Mail-Postfächer <span class="muted" style="font-weight: 400">({{ mailBoxes.length }})</span></div>
              <span class="row" style="gap: 10px; align-items: center">
                <button v-if="canManageSettings && mailBoxesOpen" class="btn" style="padding: 5px 10px"
                  @click.stop="startMailEdit()">+ Postfach</button>
                <span class="muted" style="font-size: 13px">{{ mailBoxesOpen ? "▾" : "▸" }}</span>
              </span>
            </div>
            <template v-if="mailBoxesOpen">
            <div class="muted" style="font-size: 12.5px; margin-bottom: 10px">
              Einmal einrichten, an mehreren Stellen nutzbar: Krankmeldungs-Abruf und Stundenzettel-Versand.
              Ein Postfach anzulegen aktiviert den Krankmeldungs-Abruf <strong>nicht</strong> — dafür ist der
              Haken „Für Krankmeldungs-Abruf verwenden" am Postfach zuständig. Passwörter werden gespeichert,
              aber nie wieder angezeigt.
            </div>
            <div v-if="mailCfgError" class="wip-banner" style="margin-bottom: 10px">{{ mailCfgError }}</div>
            <div v-if="mailCfgInfo" class="card" style="padding: 8px 12px; margin-bottom: 10px"><span class="muted">{{ mailCfgInfo }}</span></div>
            <div v-if="mailEnvFallback" class="muted" style="font-size: 12px; margin-bottom: 10px">
              Aktiv über Server-Umgebungsvariablen (M365) — hier gepflegte Postfächer haben Vorrang.
            </div>

            <!-- Liste -->
            <div v-if="!mailBoxes.length && !mailEditId" class="empty" style="margin-bottom: 4px">
              Noch kein Postfach eingerichtet.
            </div>
            <div v-for="b in mailBoxes" :key="b.id"
              style="border: 1px solid var(--divider); border-radius: 10px; padding: 10px 14px; margin-bottom: 8px">
              <div class="spread" style="flex-wrap: wrap; gap: 8px">
                <div class="row" style="gap: 10px; align-items: baseline">
                  <strong style="font-size: 13.5px">{{ mailBoxLabel(b) }}</strong>
                  <span class="badge" :class="b.complete ? 'green' : 'amber'">
                    <span class="dot" />{{ b.complete ? "aktiv" : "unvollständig" }}
                  </span>
                  <span class="muted" style="font-size: 12px">{{ b.intake === false ? "nur Versand" : "Krankmail-Abruf" }}</span>
                </div>
                <span v-if="canManageSettings" class="row" style="gap: 12px">
                  <button class="linkbtn" :disabled="mailCfgBusy" @click="testMailBox(b)">Verbindung testen</button>
                  <button class="linkbtn" :disabled="mailCfgBusy" @click="startMailEdit(b)">Bearbeiten</button>
                  <button class="linkbtn danger" :disabled="mailCfgBusy" @click="deleteMailBox(b)">Löschen</button>
                </span>
              </div>
            </div>

            <!-- Formular (neu/bearbeiten) -->
            <div v-if="mailEditId" style="border: 1px dashed var(--divider); border-radius: 10px; padding: 12px 14px; margin-top: 4px">
              <div class="label" style="margin-bottom: 8px">{{ mailEditId === "new" ? "Neues Postfach" : "Postfach bearbeiten" }}</div>
              <div class="row" style="gap: 12px; flex-wrap: wrap; align-items: flex-end; margin-bottom: 10px">
                <div class="stack" style="gap: 4px">
                  <span class="label">Anbieter</span>
                  <select v-model="mailForm.provider" class="input" style="width: 240px">
                    <option value="imap">IMAP (z.B. GMX, web.de)</option>
                    <option value="m365">Microsoft 365 (Graph)</option>
                  </select>
                </div>
              </div>
              <div v-if="mailForm.provider === 'imap'" class="row" style="gap: 12px; flex-wrap: wrap; margin-bottom: 10px">
                <div class="stack" style="gap: 4px">
                  <span class="label">IMAP-Server</span>
                  <input v-model="mailForm.host" class="input" style="width: 210px" placeholder="imap.gmx.net" />
                </div>
                <div class="stack" style="gap: 4px">
                  <span class="label">Port</span>
                  <input v-model="mailForm.port" class="input" style="width: 90px" placeholder="993" />
                </div>
                <div class="stack" style="gap: 4px">
                  <span class="label">Benutzer (E-Mail)</span>
                  <input v-model="mailForm.user" class="input" style="width: 230px" placeholder="krank@firma.de" />
                </div>
                <div class="stack" style="gap: 4px">
                  <span class="label">Passwort{{ mailForm.has_password ? " (gespeichert)" : "" }}</span>
                  <input v-model="mailSecret" type="password" class="input" style="width: 200px"
                    :placeholder="mailForm.has_password ? '•••••••• (leer = unverändert)' : 'Passwort'" />
                </div>
                <div class="stack" style="gap: 4px">
                  <span class="label">Ordner</span>
                  <input v-model="mailForm.folder" class="input" style="width: 120px" placeholder="INBOX" />
                </div>
              </div>
              <div v-else class="row" style="gap: 12px; flex-wrap: wrap; margin-bottom: 10px">
                <div class="stack" style="gap: 4px">
                  <span class="label">Tenant-ID</span>
                  <input v-model="mailForm.tenant_id" class="input" style="width: 290px" />
                </div>
                <div class="stack" style="gap: 4px">
                  <span class="label">Client-ID</span>
                  <input v-model="mailForm.client_id" class="input" style="width: 290px" />
                </div>
                <div class="stack" style="gap: 4px">
                  <span class="label">Geheimschlüssel{{ mailForm.has_client_secret ? " (gespeichert)" : "" }}</span>
                  <input v-model="mailSecret" type="password" class="input" style="width: 230px"
                    :placeholder="mailForm.has_client_secret ? '•••••••• (leer = unverändert)' : 'Client Secret'" />
                </div>
                <div class="stack" style="gap: 4px">
                  <span class="label">Postfach</span>
                  <input v-model="mailForm.mailbox" class="input" style="width: 230px" placeholder="krank@example.com" />
                </div>
              </div>
              <label class="row" style="gap: 8px; align-items: center; margin-bottom: 12px">
                <input type="checkbox" :checked="mailForm.intake !== false"
                  @change="mailForm.intake = ($event.target as HTMLInputElement).checked" />
                <span style="font-size: 13px">Für Krankmeldungs-Abruf verwenden
                  <span class="muted">(aus = nur zum Versand, wird nicht gelesen)</span></span>
              </label>
              <div class="row" style="gap: 8px">
                <button class="btn btn-primary" :disabled="mailCfgBusy" @click="saveMailBox">Speichern</button>
                <button class="btn" :disabled="mailCfgBusy" @click="cancelMailEdit">Abbrechen</button>
              </div>
            </div>
            </template>
          </div>
        </div>
      </template>
    </div>

    <!-- Kiosk-Kurzmeldungen (Rundmeldungen für alle) -->
    <div v-if="canManageSettings" class="card" style="padding: 0; margin-bottom: 16px">
      <div class="spread" style="padding: 16px 18px; cursor: pointer" @click="kioskMsgOpen = !kioskMsgOpen">
        <h2>Kiosk-Meldungen <span class="muted" style="font-weight: 400; font-size: 13px">({{ kioskMsgs.length }})</span></h2>
        <span class="muted" style="font-size: 13px">{{ kioskMsgOpen ? "▾" : "▸" }}</span>
      </div>
      <template v-if="kioskMsgOpen">
        <div style="padding: 0 18px 18px">
          <div class="muted" style="font-size: 12.5px; margin-bottom: 10px">
            Diese Meldungen erscheinen für alle dauerhaft auf dem Terminal-Ruhebildschirm
            (z. B. „Weihnachtsfeier am 24.12.“). Persönliche Hinweise je Mitarbeiter gibt es auf der Mitarbeiter-Detailseite.
          </div>
          <div v-if="kioskErr" class="wip-banner" style="margin-bottom: 10px">{{ kioskErr }}</div>
          <div v-for="m in kioskMsgs" :key="m.id" class="spread" style="padding: 6px 0; border-bottom: 1px solid var(--divider)">
            <span>{{ m.text }}<span v-if="m.active_from || m.active_until" class="muted" style="font-size: 12px"> —<template v-if="m.active_from"> ab {{ fmtDate(m.active_from) }}</template><template v-if="m.active_until"> bis {{ fmtDate(m.active_until) }}</template></span></span>
            <button class="linkbtn danger" :disabled="kioskBusy" @click="delKioskMsg(m)">Entfernen</button>
          </div>
          <div class="row" style="flex-wrap: wrap; gap: 8px; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--divider)">
            <input v-model="kioskNewText" class="input" style="flex: 1; min-width: 220px" maxlength="280" placeholder="Neue Meldung für alle" />
            <label class="row" style="gap: 6px; align-items: center; font-size: 13px">
              <span class="muted">anzeigen von</span>
              <input v-model="kioskNewFrom" type="date" class="input" style="width: auto" />
            </label>
            <label class="row" style="gap: 6px; align-items: center; font-size: 13px">
              <span class="muted">bis</span>
              <input v-model="kioskNewUntil" type="date" class="input" style="width: auto" />
            </label>
            <button class="btn" :disabled="kioskBusy || !kioskNewText.trim()" @click="addKioskMsg">+ Meldung</button>
          </div>
        </div>
      </template>
    </div>

    <!-- Stundenzettel-Versand (Vormonats-PDF per Mail) -->
    <div v-if="canManageSettings && tsMail" class="card" style="padding: 0; margin-bottom: 16px">
      <div class="spread" style="padding: 16px 18px; cursor: pointer" @click="tsMailOpen = !tsMailOpen">
        <h2>Stundenzettel-Versand
          <span class="muted" style="font-weight: 400; font-size: 13px">
            ({{ tsMail.enabled ? "aktiv" : "aus" }} · {{ tsMail.recipients.filter(r => r.enabled).length }} Empfänger)
          </span>
        </h2>
        <span class="muted" style="font-size: 13px">{{ tsMailOpen ? "▾" : "▸" }}</span>
      </div>
      <template v-if="tsMailOpen">
        <div style="padding: 0 18px 18px">
          <div class="muted" style="font-size: 12.5px; margin-bottom: 10px">
            Verschickt automatisch den Stundenzettel des Vormonats als PDF an die hinterlegte
            E-Mail der angehakten Mitarbeiter — ab dem eingestellten Versandtag.
          </div>
          <div v-if="tsMailErr" class="wip-banner" style="margin-bottom: 10px">{{ tsMailErr }}</div>
          <div v-if="tsMailResult" class="wip-banner" style="margin-bottom: 10px">{{ tsMailResult }}</div>
          <div v-if="tsMail.problem" class="wip-banner" style="margin-bottom: 10px">
            Versand derzeit nicht möglich: {{ tsMail.problem }}
          </div>

          <div class="row" style="flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 14px">
            <label class="row" style="gap: 8px; align-items: center; cursor: pointer">
              <input type="checkbox" :checked="tsMail.enabled" :disabled="tsMailBusy"
                @change="saveTsMailCfg({ enabled: !tsMail.enabled })" />
              <span>Automatischer Versand aktiv</span>
            </label>
            <label class="row" style="gap: 6px; align-items: center; flex: 1; min-width: 260px">
              <span class="muted" style="font-size: 13px">Absender-Postfach</span>
              <input v-model="tsMailSender" class="input" style="flex: 1" placeholder="z. B. zeiterfassung@firma.de"
                @blur="tsMailSender.trim() !== tsMail.sender && saveTsMailCfg({ sender: tsMailSender.trim() })" />
            </label>
            <button class="btn" :disabled="tsMailBusy || !tsMail.enabled" @click="tsMailSendNow">
              Vormonat jetzt senden
            </button>
          </div>

          <!-- Versandtag + Versandweg -->
          <div class="row" style="flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 14px">
            <label class="row" style="gap: 6px; align-items: center">
              <span class="muted" style="font-size: 13px">Versand am</span>
              <input v-model.number="tsMailSendDay" type="number" min="1" max="28" class="input" style="width: 64px"
                :disabled="tsMailBusy"
                @blur="tsMailSendDay !== tsMail.send_day && saveTsMailCfg({ send_day: tsMailSendDay })" />
              <span class="muted" style="font-size: 13px">. des Monats</span>
            </label>
            <label class="row" style="gap: 6px; align-items: center">
              <span class="muted" style="font-size: 13px">Versandweg</span>
              <select class="input" style="width: auto" :value="tsMail.transport" :disabled="tsMailBusy"
                @change="saveTsMailCfg({ transport: ($event.target as HTMLSelectElement).value })">
                <option value="m365">Microsoft 365 (Postfach)</option>
                <option value="smtp">Eigener Mailserver (SMTP)</option>
              </select>
            </label>
            <label v-if="tsMail.transport === 'm365'" class="row" style="gap: 6px; align-items: center">
              <span class="muted" style="font-size: 13px">Versenden über</span>
              <select class="input" style="width: auto" :value="tsMail.mailbox_id || ''" :disabled="tsMailBusy"
                @change="saveTsMailCfg({ mailbox_id: ($event.target as HTMLSelectElement).value || null })">
                <option value="">— Postfach wählen —</option>
                <option v-for="mb in tsMail.mailboxes || []" :key="mb.id" :value="mb.id">{{ mb.mailbox || mb.id }}</option>
              </select>
            </label>
          </div>
          <div v-if="tsMail.transport === 'm365' && !(tsMail.mailboxes || []).length"
            class="muted" style="font-size: 12px; margin-bottom: 12px">
            Noch kein M365-Postfach eingerichtet — lege oben unter „E-Mail-Postfächer" eins an
            (für reinen Versand den Haken „Für Krankmeldungs-Abruf verwenden" weglassen).
          </div>

          <!-- SMTP-Zugangsdaten (nur bei Versandweg SMTP) -->
          <div v-if="tsMail.transport === 'smtp'"
            class="row" style="flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 14px; padding: 12px; border: 1px solid var(--divider); border-radius: 8px">
            <label class="stack" style="gap: 4px; flex: 2; min-width: 200px">
              <span class="muted" style="font-size: 13px">SMTP-Server</span>
              <input v-model="tsMailSmtpHost" class="input" placeholder="z. B. mail.firma.de" :disabled="tsMailBusy" />
            </label>
            <label class="stack" style="gap: 4px; width: 90px">
              <span class="muted" style="font-size: 13px">Port</span>
              <input v-model.number="tsMailSmtpPort" type="number" min="1" max="65535" class="input" :disabled="tsMailBusy" />
            </label>
            <label class="stack" style="gap: 4px; flex: 2; min-width: 180px">
              <span class="muted" style="font-size: 13px">Benutzer</span>
              <input v-model="tsMailSmtpUser" class="input" autocomplete="off" :disabled="tsMailBusy" />
            </label>
            <label class="stack" style="gap: 4px; flex: 2; min-width: 180px">
              <span class="muted" style="font-size: 13px">Passwort</span>
              <input v-model="tsMailSmtpPassword" type="password" class="input" autocomplete="new-password"
                :placeholder="tsMail.smtp?.password_set ? 'gespeichert — leer lassen zum Behalten' : ''"
                :disabled="tsMailBusy" />
            </label>
            <button class="btn" :disabled="tsMailBusy" @click="saveTsMailSmtp">Speichern</button>
            <span class="muted" style="font-size: 11.5px; flex-basis: 100%">
              Port 465 = SSL, andere Ports mit STARTTLS. Das Passwort wird nie wieder angezeigt.
            </span>
          </div>

          <label class="stack" style="gap: 4px; margin-bottom: 14px">
            <span class="muted" style="font-size: 13px">Text in der E-Mail (optional)</span>
            <textarea v-model="tsMailBodyText" class="input" rows="2" maxlength="1000"
              placeholder="z. B. anbei dein Stundenzettel. Bei Fragen melde dich im Büro."
              style="resize: vertical; font: inherit"
              @blur="tsMailBodyText.trim() !== (tsMail.body_text || '') && saveTsMailCfg({ body_text: tsMailBodyText.trim() })"></textarea>
            <span class="muted" style="font-size: 11.5px">Steht zwischen der Anrede („Hallo Vorname,") und dem automatischen Abbinder. Leer = Standardtext.</span>
          </label>

          <div style="max-height: 300px; overflow-y: auto; border: 1px solid var(--divider); border-radius: 8px; padding: 4px 12px">
            <div v-for="r in tsMail.recipients" :key="r.employee_id" class="spread" style="padding: 5px 0; border-bottom: 1px solid var(--divider)">
              <label class="row" style="gap: 8px; align-items: center; cursor: pointer" :style="!r.email ? 'opacity: 0.5' : ''">
                <input type="checkbox" :checked="r.enabled" :disabled="tsMailBusy || !r.email"
                  @change="toggleTsMailRecipient(r)" />
                <span>{{ r.name }}</span>
              </label>
              <span class="muted" style="font-size: 12.5px">{{ r.email || "keine E-Mail hinterlegt" }}</span>
            </div>
          </div>

          <div style="margin-top: 12px">
            <button class="linkbtn" @click="tsMailLogOpen = !tsMailLogOpen">
              Versandprotokoll {{ tsMailLogOpen ? "▾" : "▸" }}
            </button>
            <template v-if="tsMailLogOpen">
              <div v-if="!tsMail.log.length" class="muted" style="font-size: 12.5px; padding: 6px 0">Noch keine Einträge.</div>
              <div v-for="(entry, i) in tsMail.log" :key="i" class="spread" style="padding: 4px 0; border-bottom: 1px solid var(--divider); font-size: 12.5px">
                <span>{{ entry.employee_name }} · {{ tsMailMonthLabel(entry.year_month) }}<span v-if="entry.sent_to" class="muted"> → {{ entry.sent_to }}</span></span>
                <span :class="entry.status === 'sent' ? 'muted' : 'neg'">
                  {{ TSMAIL_STATUS[entry.status] || entry.status }}<template v-if="entry.detail"> — {{ entry.detail }}</template>
                </span>
              </div>
            </template>
          </div>
        </div>
      </template>
    </div>

    <!-- 5) Benutzerkonten -->
    <div class="card" style="padding: 0">
      <div class="spread" style="padding: 16px 18px; cursor: pointer" @click="toggleSection('accounts')">
        <h2>Benutzerkonten <span class="muted" style="font-weight: 400; font-size: 13px">({{ activeAccounts.length }})</span></h2>
        <div class="row" style="gap: 8px">
          <span v-if="accountsOpen && canManageAccounts" class="row" style="gap: 8px" @click.stop>
            <template v-if="accountsEdit">
              <button class="btn" @click="showNewAccount = !showNewAccount">+ Konto</button>
              <button class="btn" @click="cancelAccountsEdit">Abbrechen</button>
              <button class="btn btn-primary" @click="finishAccountsEdit">Fertig</button>
            </template>
            <button v-else class="btn" @click="startAccountsEdit">Bearbeiten</button>
          </span>
          <span class="muted" style="font-size: 13px">{{ accountsOpen ? "▾" : "▸" }}</span>
        </div>
      </div>

      <template v-if="accountsOpen">
        <div v-if="accError" class="wip-banner" style="margin: 0 18px 14px">{{ accError }}</div>
        <div v-if="pwResetInfo" class="wip-banner" style="margin: 0 18px 14px">
          Passwort von {{ pwResetInfo.email }} zurückgesetzt auf:
          <strong>{{ pwResetInfo.password }}</strong>
          — bitte dem Nutzer mitteilen, er kann es danach selbst ändern.
          <button class="linkbtn" style="margin-left: 8px" @click="pwResetInfo = null">Schließen</button>
        </div>

        <!-- Neues Konto (nur Edit-Modus) -->
        <div v-if="accountsEdit && showNewAccount" style="padding: 0 18px 18px; border-bottom: 1px solid var(--divider)">
          <div class="stack" style="gap: 10px">
            <div class="row" style="flex-wrap: wrap; gap: 8px">
              <input v-model="newAccount.email" type="email" class="input" style="flex: 1; min-width: 200px" placeholder="E-Mail" />
            </div>
            <div class="row" style="flex-wrap: wrap; gap: 8px">
              <input v-model="newAccount.password" type="password" class="input" style="flex: 1; min-width: 200px" placeholder="Passwort (min. 8 Zeichen)" />
              <select v-model="newAccount.role_id" class="input" style="width: 230px">
                <option value="" disabled>Rolle wählen …</option>
                <option v-for="r in roles" :key="r.id" :value="r.id">{{ r.name }}</option>
              </select>
            </div>
            <div class="row" style="flex-wrap: wrap; gap: 8px">
              <select v-model="newAccount.employee_id" class="input" style="width: 230px"
                title="Mitarbeiter verknüpfen (optional)">
                <option value="">— kein Mitarbeiter —</option>
                <option v-for="e in linkableEmployees" :key="e.id" :value="e.id">{{ employeeOptionLabel(e) }}</option>
              </select>
            </div>
            <div class="row" style="gap: 8px">
              <button class="btn btn-primary" :disabled="accBusy" @click="createAccount">Anlegen</button>
              <button class="linkbtn" @click="showNewAccount = false">Abbrechen</button>
            </div>
          </div>
        </div>

        <div v-if="accLoading" class="empty">Lädt …</div>

        <table v-else class="data">
          <thead>
            <tr>
              <th>Name</th>
              <th>E-Mail</th>
              <th>Rolle</th>
              <th v-if="accountsEdit">Mitarbeiter</th>
              <th v-if="accountsEdit">Zuständig für</th>
              <th v-if="accountsEdit"></th>
            </tr>
          </thead>

          <!-- Leseansicht: Rolle als Text, keine Aktionen -->
          <tbody v-if="!accountsEdit">
            <tr v-if="!activeAccounts.length">
              <td colspan="3" class="empty">Keine aktiven Konten vorhanden.</td>
            </tr>
            <tr v-for="a in activeAccounts" :key="a.id">
              <td>{{ accountName(a) }}</td>
              <td class="muted">{{ a.email || "—" }}</td>
              <td>{{ accountRoleName(a) }}</td>
            </tr>
          </tbody>

          <!-- Bearbeiten: Rolle als Select + Deaktivieren -->
          <tbody v-else>
            <tr v-if="!activeAccounts.length">
              <td colspan="5" class="empty">Keine aktiven Konten vorhanden.</td>
            </tr>
            <tr v-for="a in activeAccounts" :key="a.id">
              <td>{{ accountName(a) }}</td>
              <td class="muted">{{ a.email || "—" }}</td>
              <td>
                <select class="input" style="min-width: 160px" :value="a.role_id || ''" :disabled="accBusy"
                  @change="changeAccountRole(a, ($event.target as HTMLSelectElement).value)">
                  <option value="" disabled>{{ a.role_name || "Rolle wählen …" }}</option>
                  <option v-for="r in roles" :key="r.id" :value="r.id">{{ r.name }}</option>
                </select>
              </td>
              <td>
                <select class="input" style="min-width: 180px" :value="a.employee_id ?? ''" :disabled="accBusy"
                  title="Mitarbeiter verknüpfen (optional)"
                  @change="changeAccountEmployee(a, ($event.target as HTMLSelectElement).value)">
                  <option value="">— kein Mitarbeiter —</option>
                  <option v-for="e in linkableEmployees" :key="e.id" :value="e.id">{{ employeeOptionLabel(e) }}</option>
                </select>
              </td>
              <td>
                <details style="position: relative">
                  <summary class="input" style="cursor: pointer; min-width: 170px; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; list-style: none"
                    :title="'Bereiche, für die dieses Konto zuständig ist (Genehmigen, Team-Sicht). Leer = Bereich der eigenen Personalakte. ' + accountDeptSummary(a)">
                    {{ accountDeptSummary(a) }}
                  </summary>
                  <div class="card" style="position: absolute; z-index: 20; padding: 10px 12px; min-width: 210px; max-height: 240px; overflow-y: auto">
                    <label v-for="d in departments" :key="d.id" class="row" style="gap: 6px; padding: 3px 0">
                      <input type="checkbox" :disabled="accBusy"
                        :checked="(a.scope_department_ids || []).includes(d.id)"
                        @change="toggleAccountDepartment(a, d.id, ($event.target as HTMLInputElement).checked)" />
                      {{ d.name }}
                    </label>
                    <div class="muted" style="font-size: 11.5px; margin-top: 6px">
                      Nichts angehakt = Bereich der Personalakte gilt.
                    </div>
                  </div>
                </details>
              </td>
              <td style="text-align: right; white-space: nowrap">
                <template v-if="pwResetConfirmId === a.id">
                  <span class="muted" style="font-size: 12.5px; margin-right: 8px">Wirklich?</span>
                  <button class="linkbtn danger" :disabled="accBusy" @click="resetAccountPw(a)">Ja</button>
                  <button class="linkbtn" :disabled="accBusy" style="margin-left: 8px" @click="pwResetConfirmId = null">Abbrechen</button>
                </template>
                <template v-else>
                  <button class="linkbtn" :disabled="accBusy" @click="pwResetConfirmId = a.id">Passwort zurücksetzen</button>
                  <button class="linkbtn danger" :disabled="accBusy" style="margin-left: 8px" @click="deactivateAccount(a)">Deaktivieren</button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>

        <div v-if="inactiveAccounts.length" style="margin: 0 18px 18px; padding-top: 12px; border-top: 1px solid var(--divider)">
          <div class="spread" style="cursor: pointer" @click="deactivatedOpen = !deactivatedOpen">
            <div class="label">Deaktivierte Konten <span class="muted" style="font-weight: 400">({{ inactiveAccounts.length }})</span></div>
            <span class="muted" style="font-size: 13px">{{ deactivatedOpen ? "▾" : "▸" }}</span>
          </div>
          <template v-if="deactivatedOpen">
            <div class="muted" style="font-size: 12.5px; margin: 4px 0 8px">
              Reaktivieren gibt der Person ihr Login zurück. Löschen entfernt nur den Login — der Mitarbeiter und seine Zeitdaten bleiben.
            </div>
            <div v-for="a in inactiveAccounts" :key="a.id" class="spread" style="padding: 6px 0; border-bottom: 1px solid var(--divider)">
              <span>{{ accountName(a) }} <span class="muted" style="font-size: 12px">— {{ a.email || "—" }} — {{ accountRoleName(a) }}</span></span>
              <span v-if="accountsEdit" class="row" style="gap: 10px">
                <button class="btn" :disabled="accBusy" @click="reactivateAccount(a)">Wieder aktivieren</button>
                <button class="linkbtn danger" :disabled="accBusy" @click="deleteAccount(a)">Endgültig löschen</button>
              </span>
            </div>
          </template>
        </div>
      </template>
    </div>

    <!-- 6) Gelöschte Mitarbeiter: ausgeblendete Datensätze wiederherstellen oder endgültig entfernen -->
    <div v-if="deletedEmployees.length" class="card" style="padding: 0; margin-top: 16px">
      <div class="spread" style="padding: 16px 18px; cursor: pointer" @click="deletedEmpOpen = !deletedEmpOpen">
        <h2>Gelöschte Mitarbeiter <span class="muted" style="font-weight: 400; font-size: 13px">({{ deletedEmployees.length }})</span></h2>
        <span class="muted" style="font-size: 13px">{{ deletedEmpOpen ? "▾" : "▸" }}</span>
      </div>
      <template v-if="deletedEmpOpen">
        <div style="padding: 0 18px 18px">
          <div class="muted" style="font-size: 12.5px; margin-bottom: 8px">
            Diese Mitarbeiter wurden über die Detailseite gelöscht (ausgeblendet). Wiederherstellen bringt sie
            in die Mitarbeiterliste zurück. Endgültiges Löschen ist nur ohne Zeit-/Lohndaten möglich —
            reguläre Austritte laufen über das Austrittsdatum ins Archiv.
          </div>
          <div v-if="deletedEmpErr" class="wip-banner" style="margin-bottom: 10px">{{ deletedEmpErr }}</div>
          <div v-for="e in deletedEmployees" :key="e.id" class="spread" style="padding: 6px 0; border-bottom: 1px solid var(--divider)">
            <span>{{ e.name }} <span class="muted" style="font-size: 12px">— {{ e.employee_number || "—" }}<template v-if="e.company_name"> — {{ e.company_name }}</template></span></span>
            <span class="row" style="gap: 10px">
              <button class="btn" :disabled="deletedEmpBusy" @click="restoreEmployee(e)">Wiederherstellen</button>
              <button class="linkbtn danger" :disabled="deletedEmpBusy" @click="destroyEmployee(e)">Endgültig löschen</button>
            </span>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>
