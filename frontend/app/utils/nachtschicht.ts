// Nachtschicht-Eingabe im Tages-Editor (Spec 2026-09-15): reine Helfer ohne
// Nuxt-Abhaengigkeiten, damit sie sich isoliert testen lassen. Die Schicht
// wird an Mitternacht in zwei Sitzungen getrennt; die Rechenwege bleiben
// unangetastet (Issue #96 bleibt fuer die Anfangstag-Zuordnung offen).

/** "HH:MM" -> Minuten seit Mitternacht. */
export function hhmmToMinutes(hhmm: string): number {
  const [h, m] = hhmm.split(":").map(Number);
  return (h || 0) * 60 + (m || 0);
}

/** Lokale "HH:MM" eines ISO-Zeitstempels (Browser-Zeitzone = Mandantenzeit). */
export function localHHMM(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** Folgetag eines "YYYY-MM-DD" (Monats- und Jahreswechsel inklusive). */
export function nextDayIso(dayIso: string): string {
  const [Y, Mo, D] = dayIso.split("-").map(Number);
  const d = new Date(Y!, Mo! - 1, D! + 1, 12, 0, 0);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Lokaler Zeitpunkt (Tag + h:m:s) als UTC-ISO — wie toIsoUtc, nur sekundengenau. */
export function toIsoUtcSec(dayIso: string, h: number, m: number, s: number): string {
  const [Y, Mo, D] = dayIso.split("-").map(Number);
  return new Date(Y!, Mo! - 1, D!, h, m, s).toISOString();
}

/** Ist das eingegebene Geht eine Nachtschicht (liegt nicht nach dem offenen Kommt)? */
export function istNachtschicht(kommtHHMM: string, gehtHHMM: string): boolean {
  return hhmmToMinutes(gehtHHMM) <= hhmmToMinutes(kommtHHMM);
}

export interface NachtschichtStempel {
  /** Geht 23:59:59 am Anfangstag */
  gehtAnfangstag: string;
  /** Kommt 00:00:00 am Folgetag */
  kommtFolgetag: string;
  /** Geht HH:MM am Folgetag */
  gehtFolgetag: string;
  folgetag: string;
}

/** Die drei Stempel, die eine Nachtschicht ueber Mitternacht abbilden. */
export function nachtschichtStempel(dayIso: string, gehtHHMM: string): NachtschichtStempel {
  const folgetag = nextDayIso(dayIso);
  const [h, m] = gehtHHMM.split(":").map(Number);
  return {
    gehtAnfangstag: toIsoUtcSec(dayIso, 23, 59, 59),
    kommtFolgetag: toIsoUtcSec(folgetag, 0, 0, 0),
    gehtFolgetag: toIsoUtcSec(folgetag, h || 0, m || 0, 0),
    folgetag,
  };
}

export interface FolgetagEvent {
  event_type: string;
  event_time: string;
  effective: boolean;
}

/**
 * Konflikt am Folgetag: ein wirksamer Stempel, der VOR dem Geht liegt (oder
 * genau darauf), wuerde die neue Sitzung 00:00–HH:MM zerschneiden. Liefert
 * eine Beschreibung des ersten Stoerers oder null.
 */
export function folgetagKonflikt(events: FolgetagEvent[], gehtHHMM: string): string | null {
  const grenze = hhmmToMinutes(gehtHHMM);
  const labels: Record<string, string> = {
    PUNCH_IN: "Kommt",
    PUNCH_OUT: "Geht",
    BREAK_START: "Pause von",
    BREAK_END: "Pause bis",
  };
  const stoerer = events
    .filter((e) => e.effective)
    .map((e) => ({ e, hhmm: localHHMM(e.event_time) }))
    .filter(({ hhmm }) => hhmmToMinutes(hhmm) <= grenze)
    .sort((a, b) => hhmmToMinutes(a.hhmm) - hhmmToMinutes(b.hhmm))[0];
  if (!stoerer) return null;
  return `${labels[stoerer.e.event_type] || stoerer.e.event_type} ${stoerer.hhmm}`;
}
