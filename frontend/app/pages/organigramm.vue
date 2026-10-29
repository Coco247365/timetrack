<script setup lang="ts">
// Organigramm: Hierarchie aus Vorgesetzten-Kette (manager_id), gruppiert
// nach Wurzeln. Quelle: GET /org-chart (rechte-gescoped wie der Teamkalender —
// nur Name/Bereich/Vorgesetzter, keine Stammdaten).
import type { OrgTreeNode } from "~/components/OrgNode.vue";

const { apiFetch } = useApi();

interface OrgEmployee {
  id: string;
  name: string;
  department_name?: string | null;
  manager_id?: string | null;
}

const employees = ref<OrgEmployee[]>([]);
const loading = ref(true);
const loadError = ref("");

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const data = await apiFetch<{ employees: OrgEmployee[] }>("/org-chart");
    employees.value = data.employees || [];
  } catch (e: any) {
    loadError.value = e?.data?.detail || "Organigramm konnte nicht geladen werden.";
  }
  loading.value = false;
}
onMounted(load);

// Baum aus manager_id bauen. Wurzeln = ohne Vorgesetzten (oder Vorgesetzter
// außerhalb der sichtbaren Menge). Zyklen werden defensiv aufgebrochen.
const roots = computed<OrgTreeNode[]>(() => {
  const byId = new Map<string, OrgTreeNode>();
  for (const e of employees.value) {
    byId.set(e.id, { id: e.id, name: e.name, department_name: e.department_name, children: [] });
  }
  const rootList: OrgTreeNode[] = [];
  for (const e of employees.value) {
    const node = byId.get(e.id)!;
    const parent = e.manager_id ? byId.get(e.manager_id) : undefined;
    if (parent && parent !== node) parent.children.push(node);
    else rootList.push(node);
  }
  // Zyklen-Schutz: Knoten, die durch einen Zyklus nirgends als Wurzel landen
  // und nie erreicht werden, hier nicht nötig — parent!==node bricht Selbstbezug,
  // gegenseitige Bezüge landen als eigene Wurzeln, sobald einer fehlt.
  const sortRec = (nodes: OrgTreeNode[]) => {
    nodes.sort((a, b) => b.children.length - a.children.length || a.name.localeCompare(b.name, "de"));
    for (const n of nodes) sortRec(n.children);
  };
  sortRec(rootList);
  return rootList;
});

const withReports = computed(() => roots.value.filter((r) => r.children.length));
const standalone = computed(() => roots.value.filter((r) => !r.children.length));

// Ohne gepflegte Vorgesetzten: Hinweis + Gruppierung nach Bereich als Fallback.
const hasHierarchy = computed(() => employees.value.some((e) => e.manager_id));
const byDepartment = computed(() => {
  const groups = new Map<string, OrgEmployee[]>();
  for (const e of employees.value) {
    const key = e.department_name || "Ohne Bereich";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(e);
  }
  return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0], "de"));
});
</script>

<template>
  <div>
    <div class="spread" style="margin-bottom: 18px">
      <div class="row" style="gap: 18px; align-items: baseline">
        <NuxtLink to="/team" class="linkbtn" style="font-size: 15px">‹ Team</NuxtLink>
        <h1>Organigramm</h1>
      </div>
      <span v-if="!loading" class="muted num">{{ employees.length }} Mitarbeiter</span>
    </div>

    <div v-if="loadError" class="wip-banner" style="margin-bottom: 14px">{{ loadError }}</div>
    <div v-if="loading" class="card"><div class="empty">Lädt …</div></div>

    <template v-else-if="hasHierarchy">
      <!-- Hierarchie-Bäume -->
      <div v-for="r in withReports" :key="r.id" class="card" style="margin-bottom: 16px">
        <OrgNode :node="r" :depth="0" />
      </div>
      <!-- Personen ohne Team und ohne Vorgesetzten -->
      <div v-if="standalone.length" class="card">
        <div class="label" style="margin-bottom: 10px">Ohne zugeordneten Vorgesetzten</div>
        <div class="row" style="flex-wrap: wrap; gap: 8px">
          <div v-for="p in standalone" :key="p.id"
            style="padding: 8px 12px; border: 1px solid var(--divider); border-radius: 10px">
            <strong style="font-size: 13.5px">{{ p.name }}</strong>
            <span class="muted" style="font-size: 12px"> · {{ p.department_name || "—" }}</span>
          </div>
        </div>
        <div class="muted" style="font-size: 12px; margin-top: 10px">
          Vorgesetzte werden in der Personalakte gepflegt (Mitarbeiter → Stammdaten → Vorgesetzter).
        </div>
      </div>
    </template>

    <!-- Fallback: keine Vorgesetzten gepflegt -> nach Bereichen gruppieren -->
    <template v-else>
      <div class="wip-banner" style="margin-bottom: 14px">
        Noch keine Vorgesetzten gepflegt — Anzeige nach Bereichen. Hierarchie entsteht,
        sobald in den Personalakten „Vorgesetzter" gesetzt ist.
      </div>
      <div v-for="[dep, members] in byDepartment" :key="dep" class="card" style="margin-bottom: 14px">
        <div class="label" style="margin-bottom: 10px">{{ dep }} ({{ members.length }})</div>
        <div class="row" style="flex-wrap: wrap; gap: 8px">
          <div v-for="m in members" :key="m.id"
            style="padding: 8px 12px; border: 1px solid var(--divider); border-radius: 10px">
            <strong style="font-size: 13.5px">{{ m.name }}</strong>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
