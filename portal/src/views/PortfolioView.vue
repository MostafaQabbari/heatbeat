<!-- What this file does: all plants of the customer as tiles or on a map, with filters. -->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, unwrap, type PlantSummary } from "../api/client";
import PlantTile from "../components/PlantTile.vue";
import PortfolioMap from "../components/PortfolioMap.vue";
import { label } from "../format";

const plants = ref<PlantSummary[]>([]);
const error = ref<string | null>(null);
const loading = ref(true);
const view = ref<"tiles" | "map">("tiles");
const type = ref("");
const status = ref("");

const shown = computed(() =>
  plants.value.filter((p) => (!type.value || p.plant_type === type.value) && (!status.value || p.status === status.value)),
);
const summary = computed(() => ({
  online: plants.value.filter((p) => p.status === "online").length,
  alarms: plants.value.reduce((n, p) => n + (p.open_alarms ?? 0), 0),
}));

onMounted(async () => {
  try {
    let cursor: string | undefined;
    const all: PlantSummary[] = [];
    do {
      const page = unwrap(await api.GET("/v1/plants", { params: { query: { limit: 200, cursor } } }));
      all.push(...page.items);
      cursor = page.next_cursor ?? undefined;
    } while (cursor);
    plants.value = all;
  } catch (exc) {
    error.value = String(exc);
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <div class="stack">
    <div class="row spread">
      <div>
        <h1>Portfolio</h1>
        <p class="muted">{{ plants.length }} plants · {{ summary.online }} online · {{ summary.alarms }} open alarms</p>
      </div>
      <div class="row" role="group" aria-label="View">
        <button type="button" :aria-pressed="view === 'tiles'" @click="view = 'tiles'">Tiles</button>
        <button type="button" :aria-pressed="view === 'map'" @click="view = 'map'">Map</button>
      </div>
    </div>
    <div class="row">
      <label>Type
        <select v-model="type">
          <option value="">All types</option>
          <option v-for="t in ['gas_boiler', 'heat_pump', 'district_heating']" :key="t" :value="t">{{ label(t) }}</option>
        </select>
      </label>
      <label>Status
        <select v-model="status">
          <option value="">All</option>
          <option value="online">Online</option>
          <option value="offline">Offline</option>
          <option value="never">No data yet</option>
        </select>
      </label>
    </div>
    <p v-if="loading" class="muted">Loading…</p>
    <p v-else-if="error" class="notice error" role="alert">{{ error }}</p>
    <template v-else>
      <div v-if="view === 'tiles'" class="grid tiles">
        <PlantTile v-for="plant in shown" :key="plant.key" :plant="plant" />
      </div>
      <PortfolioMap v-else :plants="shown" />
    </template>
  </div>
</template>

<style scoped>
.tiles { grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }
</style>
