<script setup lang="ts">
// Einstieg → je nach Rolle: Mitarbeiterliste (Admin/HR) oder Meine Zeit (Self-Service).
definePageMeta({ layout: false });
const auth = useAuth();
onMounted(async () => {
  if (!auth.user.value) {
    try { await auth.fetchMe(); } catch { return navigateTo("/login"); }
  }
  navigateTo(auth.canViewEmployees.value ? "/mitarbeiter" : "/meine-zeit");
});
</script>

<template>
  <div class="empty">Weiterleitung …</div>
</template>
