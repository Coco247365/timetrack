<script setup lang="ts">
// Ein Knoten im Organigramm-Baum: Karte + eingerückte Unterknoten.
// Rekursive Komponente (ruft sich selbst für die Kinder auf).
export interface OrgTreeNode {
  id: string;
  name: string;
  department_name?: string | null;
  children: OrgTreeNode[];
}

defineProps<{ node: OrgTreeNode; depth: number }>();
</script>

<template>
  <div>
    <div class="org-card" :style="{ marginLeft: `${depth * 34}px` }">
      <span v-if="depth > 0" class="org-connector" aria-hidden="true">└</span>
      <div class="stack" style="gap: 1px">
        <strong style="font-size: 14.5px">{{ node.name }}</strong>
        <span class="muted" style="font-size: 12px">{{ node.department_name || "—" }}</span>
      </div>
      <span v-if="node.children.length" class="badge green" style="margin-left: auto">
        <span class="dot" />{{ node.children.length }} direkt unterstellt
      </span>
    </div>
    <OrgNode v-for="c in node.children" :key="c.id" :node="c" :depth="depth + 1" />
  </div>
</template>

<style scoped>
.org-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  margin-bottom: 8px;
  border: 1px solid var(--divider, #e5e7eb);
  border-radius: 10px;
  background: var(--surface, #fff);
}
.org-connector {
  color: var(--muted, #9ca3af);
  font-size: 13px;
}
</style>
