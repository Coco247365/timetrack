<script setup lang="ts">
// Monatsabgleich-Block (Altsystem-Fussblock): Soll-Kette · Überstunden · Lohnarten.
// Reine Anzeige; wird in der Auswertung aufgeklappt UND im Druck je Seite genutzt.
import type { Reconciliation } from "~/types/monatsabgleich";
import { dez2 as num2, hmm, hmmH, payoutModeLabel as modeLabel, tage1 as tage } from "~/utils/zeitformat";

defineProps<{ data: Reconciliation; periodLabel: string }>();
</script>

<template>
  <div class="recon">
    <div class="recon-col">
      <div class="recon-head">Stunden · {{ periodLabel }}</div>
      <div class="recon-line"><span>Bruttosollzeit</span><b>{{ hmm(data.stunden.brutto_soll_minutes) }}</b></div>
      <div class="recon-line sub"><span>− Feiertage</span><span class="muted">{{ tage(data.stunden.feiertag_tage) }} T</span><b>{{ hmm(data.stunden.feiertag_minutes) }}</b></div>
      <div class="recon-line sub"><span>− Urlaub</span><span class="muted">{{ tage(data.stunden.urlaub_tage) }} T</span><b>{{ hmm(data.stunden.urlaub_minutes) }}</b></div>
      <div class="recon-line sub"><span>− Krank</span><span class="muted">{{ tage(data.stunden.krank_tage) }} T</span><b>{{ hmm(data.stunden.krank_minutes) }}</b></div>
      <div v-if="data.stunden.sonstige_minutes" class="recon-line sub"><span>− Sonstige (Sonderurlaub/unbezahlt)</span><span class="muted">{{ tage(data.stunden.sonstige_tage) }} T</span><b>{{ hmm(data.stunden.sonstige_minutes) }}</b></div>
      <div class="recon-line total"><span>effektive Sollzeit</span><b>{{ hmm(data.stunden.effektiv_soll_minutes) }}</b></div>
      <div class="recon-line"><span>Istzeit</span><b>{{ hmm(data.stunden.ist_minutes) }}</b></div>
      <div class="recon-line total"><span>Saldo Monat</span><b>{{ hmm(data.stunden.saldo_minutes, true) }}</b></div>
      <div class="recon-head" style="margin-top: 12px">Zuschläge</div>
      <div class="recon-line"><span>Nacht</span><b>{{ hmm(data.zuschlaege.nacht_minutes) }}</b></div>
      <div class="recon-line"><span>Samstag</span><b>{{ hmm(data.zuschlaege.samstag_minutes) }}</b></div>
      <div class="recon-line"><span>Sonntag</span><b>{{ hmm(data.zuschlaege.sonntag_minutes) }}</b></div>
      <div class="recon-line"><span>Feiertag (gearbeitet)</span><b>{{ hmm(data.zuschlaege.feiertag_minutes) }}</b></div>
    </div>
    <div class="recon-col">
      <div class="recon-head">Überstunden</div>
      <div class="recon-line"><span>Gleitzeit-Vortrag</span><b>{{ hmmH(data.ueberstunden.vortrag_hours, true) }}</b></div>
      <div class="recon-line"><span>Saldo Monat</span><b>{{ hmmH(data.ueberstunden.monat_hours, true) }}</b></div>
      <div class="recon-line sub"><span>− ausbezahlt</span><b>{{ hmmH(data.ueberstunden.ausgezahlt_hours) }}</b></div>
      <div v-if="data.ueberstunden.verfall_hours" class="recon-line sub"><span>− verfallen</span><b>{{ hmmH(data.ueberstunden.verfall_hours) }}</b></div>
      <div class="recon-line sub"><span>± Korrekturen</span><b>{{ hmmH(data.ueberstunden.korrektur_hours, true) }}</b></div>
      <div class="recon-line total"><span>Gleitzeit-Saldo</span><b>{{ hmmH(data.ueberstunden.saldo_hours, true) }}</b></div>
      <div class="muted" style="margin-top: 8px; font-size: 12px">
        Modus: {{ modeLabel(data.ueberstunden.payout_mode) }}<template v-if="data.ueberstunden.cap_hours != null"> · Kappe {{ num2(data.ueberstunden.cap_hours) }} h</template>
      </div>
      <div v-if="!data.ueberstunden.abgeschlossen" class="badge amber" style="margin-top: 8px">
        Monat noch nicht abgeschlossen — Überstundenzeile ist eine Vorschau
      </div>
    </div>
    <div class="recon-col">
      <div class="recon-head">
        Lohnarten (Export)
        <span v-if="data.export_status" class="badge green" style="margin-left: 6px">Datei {{ data.export_status === "DELIVERED" ? "übermittelt" : "erzeugt" }}</span>
        <span v-else class="badge neutral" style="margin-left: 6px">noch keine Datei</span>
      </div>
      <div v-if="!data.lohnarten.length" class="muted">Keine Export-Zeilen für diesen Monat.</div>
      <div v-for="(la, i) in data.lohnarten" :key="i" class="recon-line" :title="la.bezeichnung || undefined">
        <span class="num">{{ String(la.lohnart).padStart(3, "0") }}</span>
        <b>{{ num2(la.wert) }}</b>
      </div>
    </div>
  </div>
</template>

<style scoped>
.recon {
  display: grid;
  grid-template-columns: repeat(3, minmax(220px, 1fr));
  gap: 24px;
}
@media (max-width: 900px) {
  .recon {
    grid-template-columns: 1fr;
  }
}
.recon-head {
  font-weight: 700;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-2);
  margin-bottom: 6px;
}
.recon-line {
  display: flex;
  gap: 10px;
  align-items: baseline;
  padding: 3px 0;
  font-variant-numeric: tabular-nums;
}
.recon-line > span:first-child {
  flex: 1;
}
.recon-line > b {
  min-width: 64px;
  text-align: right;
}
.recon-line.sub {
  padding-left: 12px;
  color: var(--text-2);
}
.recon-line.total {
  border-top: 1px solid var(--divider);
  margin-top: 2px;
  padding-top: 5px;
}
@media print {
  .recon {
    grid-template-columns: repeat(3, 1fr);
    gap: 18px;
  }
  .recon-line {
    font-size: 11px;
    padding: 2px 0;
  }
}
</style>
