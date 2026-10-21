<!-- What this file does: one plant in the tile view: name, type, status, open alarms, last data. -->
<script setup lang="ts">
import type { PlantSummary } from "../api/client";
import { ago, label } from "../format";
import StatusBadge from "./StatusBadge.vue";

defineProps<{ plant: PlantSummary }>();
</script>

<template>
  <RouterLink :to="`/plants/${plant.key}`" class="tile card" :aria-label="`Plant ${plant.name ?? plant.key}`">
    <div class="row spread">
      <strong>{{ plant.name ?? plant.key }}</strong>
      <StatusBadge :status="plant.status" />
    </div>
    <div class="muted">{{ label(plant.plant_type) }} · <span class="mono">{{ plant.key }}</span></div>
    <div class="row spread">
      <span class="muted">Last data {{ ago(plant.last_seen_at) }}</span>
      <span v-if="plant.open_alarms" class="badge bad">▲ {{ plant.open_alarms }} alarm{{ plant.open_alarms > 1 ? "s" : "" }}</span>
    </div>
  </RouterLink>
</template>

<style scoped>
.tile { display: grid; gap: .4rem; text-decoration: none; transition: border-color .15s; }
.tile:hover { border-color: var(--accent); }
</style>
