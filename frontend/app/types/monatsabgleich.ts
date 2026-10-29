// Antwort von GET /admin/month-reconciliation (Monatsabgleich je Mitarbeiter,
// Spec 2026-09-09). Geteilt zwischen Auswertung (Aufklapp-Zeile, Druck) und
// der Block-Komponente MonatsabgleichBlock.

export interface ReconStunden {
  brutto_soll_minutes: number;
  feiertag_minutes: number;
  feiertag_tage: number;
  urlaub_minutes: number;
  urlaub_tage: number;
  krank_minutes: number;
  krank_tage: number;
  sonstige_minutes: number;
  sonstige_tage: number;
  effektiv_soll_minutes: number;
  ist_minutes: number;
  saldo_minutes: number;
}

export interface ReconUeberstunden {
  payout_mode: string | null;
  cap_hours: number | null;
  vortrag_hours: number;
  monat_hours: number;
  ausgezahlt_hours: number;
  korrektur_hours: number;
  verfall_hours: number;
  saldo_hours: number;
  abgeschlossen: boolean;
}

export interface ReconZuschlaege {
  nacht_minutes: number;
  sonntag_minutes: number;
  feiertag_minutes: number;
  samstag_minutes: number;
}

export interface ReconLohnart {
  lohnart: number;
  bezeichnung: string | null;
  wert: number;
  tage: number | null;
}

export interface Reconciliation {
  employee_id: string;
  employee_name: string;
  employee_number: string | null;
  year: number;
  month: number;
  stunden: ReconStunden;
  ueberstunden: ReconUeberstunden;
  zuschlaege: ReconZuschlaege;
  lohnarten: ReconLohnart[];
  export_status: string | null;
  urlaub_rest_tage: number | null;
}
