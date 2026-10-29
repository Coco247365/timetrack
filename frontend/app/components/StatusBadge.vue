<!-- Status-Pille: Anwesend / Pause / Urlaub / Krank / Abwesend.
     Pause ist ein eigener (gelber) Zustand, kein Unterfall von "Anwesend" —
     wer in der Pause ist, ist da, arbeitet aber gerade nicht. -->
<script setup lang="ts">
const props = defineProps<{ status: string }>();

const MAP: Record<string, { cls: string; label: string }> = {
  clocked_in: { cls: "green", label: "Anwesend" },
  on_break: { cls: "pause", label: "Pause" },
  pause: { cls: "pause", label: "Pause" },
  anwesend: { cls: "green", label: "Anwesend" },
  vacation: { cls: "blue", label: "Urlaub" },
  urlaub: { cls: "blue", label: "Urlaub" },
  sick_leave: { cls: "red", label: "Krank" },
  krank: { cls: "red", label: "Krank" },
  clocked_out: { cls: "neutral", label: "Abwesend" },
  abwesend: { cls: "neutral", label: "Abwesend" },
};

const meta = computed(() => MAP[props.status] || { cls: "neutral", label: "Abwesend" });
</script>

<template>
  <span class="badge" :class="meta.cls"><span class="dot" />{{ meta.label }}</span>
</template>
