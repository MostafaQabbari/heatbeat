<!-- What this file does: fault analysis: how often each fault happened, how long it lasted,
     which plants are affected most. -->
<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import { api, unwrap, type FaultStats } from "../api/client";

const days = ref(30);
const stats = ref<FaultStats | null>(null);

function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "–";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} h`;
  return `${(seconds / 86400).toFixed(1)} days`;
}

async function load(): Promise<void> {
  const to = new Date();
  const from = new Date(to.getTime() - days.value * 86400_000);
  stats.value = unwrap(await api.GET("/v1/faults/stats", { params: { query: { from: from.toISOString(), to: to.toISOString() } } }));
}

onMounted(load);
watch(days, load);
</script>

<template>
  <div class="stack">
    <div class="row spread">
      <h1>Fault analysis</h1>
      <div class="row" role="group" aria-label="Period">
        <button v-for="d in [7, 30, 90]" :key="d" type="button" :aria-pressed="days === d" @click="days = d">{{ d }} days</button>
      </div>
    </div>
    <template v-if="stats">
      <p class="muted">
        {{ stats.total_occurrences }} fault occurrences<template v-if="stats.most_frequent">; most frequent:
        <strong>{{ stats.most_frequent }}</strong></template>
      </p>
      <div class="grid cols-2">
        <section class="card" aria-labelledby="rule-h">
          <h2 id="rule-h">By fault</h2>
          <table>
            <thead><tr><th scope="col">Fault</th><th scope="col" class="num">Count</th><th scope="col" class="num">Open</th>
              <th scope="col" class="num">Mean duration</th><th scope="col" class="num">Total</th></tr></thead>
            <tbody>
              <tr v-for="r in stats.by_rule" :key="r.rule_name">
                <td>{{ r.rule_name }} <span class="muted">({{ r.urgency }})</span></td>
                <td class="num">{{ r.occurrences }}</td><td class="num">{{ r.open_now }}</td>
                <td class="num">{{ duration(r.mean_duration_s) }}</td><td class="num">{{ duration(r.total_duration_s) }}</td>
              </tr>
            </tbody>
          </table>
        </section>
        <section class="card" aria-labelledby="plant-h">
          <h2 id="plant-h">By plant</h2>
          <table>
            <thead><tr><th scope="col">Plant</th><th scope="col" class="num">Count</th><th scope="col">Share of time in fault</th></tr></thead>
            <tbody>
              <tr v-for="p in stats.by_plant" :key="p.plant_key">
                <td><RouterLink :to="`/plants/${p.plant_key}`">{{ p.plant_key }}</RouterLink></td>
                <td class="num">{{ p.occurrences }}</td>
                <td>
                  <meter :value="Math.min(1, p.total_duration_s / (days * 86400))" min="0" max="1"
                    :aria-label="`${p.plant_key}: ${duration(p.total_duration_s)} in fault`" />
                  <span class="muted"> {{ duration(p.total_duration_s) }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>
    </template>
  </div>
</template>
