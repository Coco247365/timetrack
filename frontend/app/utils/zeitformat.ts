// Zeit-Formatierung fuer Monatsabgleich (Bildschirm + Druck).

/** Minuten -> "h:mm"; negativ mit "−", optional "+" bei positiven Werten. */
export function hmm(min: number, signed = false): string {
  const neg = min < 0;
  const a = Math.abs(Math.round(min));
  const s = `${Math.floor(a / 60)}:${String(a % 60).padStart(2, "0")}`;
  return neg ? `−${s}` : signed && min > 0 ? `+${s}` : s;
}

/** Dezimalstunden -> "h:mm" (siehe hmm). */
export function hmmH(hoursDec: number, signed = false): string {
  return hmm(Math.round(hoursDec * 60), signed);
}

/** Tage mit einer Nachkommastelle (deutsch). */
export function tage1(n: number): string {
  return n.toLocaleString("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

/** Zahl mit zwei Nachkommastellen (deutsch), wie im Export. */
export function dez2(n: number): string {
  return n.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export const payoutModeLabels: Record<string, string> = {
  none: "Konto, keine Auszahlung",
  all: "alles auszahlen",
  above_cap: "über Kappe auszahlen",
  forfeit_above_cap: "über Kappe verfällt",
};

export function payoutModeLabel(mode: string | null): string {
  return payoutModeLabels[mode ?? ""] ?? (mode || "—");
}
