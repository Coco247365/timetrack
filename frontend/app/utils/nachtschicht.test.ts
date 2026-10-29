import { describe, expect, it } from "vitest";
import {
  folgetagKonflikt,
  istNachtschicht,
  localHHMM,
  nachtschichtStempel,
  nextDayIso,
} from "./nachtschicht";

describe("nachtschicht", () => {
  it("erkennt Geht vor oder gleich Kommt als Nachtschicht", () => {
    expect(istNachtschicht("20:30", "06:15")).toBe(true);
    expect(istNachtschicht("20:30", "20:30")).toBe(true);
    expect(istNachtschicht("08:00", "16:15")).toBe(false);
  });

  it("Folgetag inkl. Monats- und Jahreswechsel", () => {
    expect(nextDayIso("2026-09-08")).toBe("2026-09-09");
    expect(nextDayIso("2026-09-30")).toBe("2026-10-01");
    expect(nextDayIso("2026-12-31")).toBe("2027-01-01");
    expect(nextDayIso("2028-02-28")).toBe("2028-02-29");
  });

  it("baut die drei Stempel sekundengenau an Mitternacht", () => {
    const s = nachtschichtStempel("2026-09-08", "06:15");
    expect(s.folgetag).toBe("2026-09-09");
    expect(localHHMM(s.gehtAnfangstag)).toBe("23:59");
    expect(new Date(s.gehtAnfangstag).getSeconds()).toBe(59);
    expect(localHHMM(s.kommtFolgetag)).toBe("00:00");
    expect(localHHMM(s.gehtFolgetag)).toBe("06:15");
    // Geht am Anfangstag liegt genau eine Sekunde vor dem Kommt am Folgetag.
    expect(new Date(s.kommtFolgetag).getTime() - new Date(s.gehtAnfangstag).getTime()).toBe(1000);
  });

  it("meldet einen Stempel am Folgetag vor dem Geht als Konflikt", () => {
    const morgens = new Date(2026, 8, 9, 5, 30, 0).toISOString();
    const abends = new Date(2026, 8, 9, 20, 30, 0).toISOString();
    expect(folgetagKonflikt([{ event_type: "PUNCH_IN", event_time: abends, effective: true }], "06:15")).toBeNull();
    expect(folgetagKonflikt([{ event_type: "PUNCH_IN", event_time: morgens, effective: true }], "06:15")).toBe("Kommt 05:30");
    // Unwirksame (korrigierte/geloeschte) Stempel stoeren nicht.
    expect(folgetagKonflikt([{ event_type: "PUNCH_IN", event_time: morgens, effective: false }], "06:15")).toBeNull();
  });
});
