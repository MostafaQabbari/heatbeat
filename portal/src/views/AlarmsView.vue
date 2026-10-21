<!-- What this file does: all firing alarms of the customer, and the alarm history of the last 7 days. -->
<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api, unwrap, type Alarm, type AlarmEvent } from "../api/client";
import AlarmList from "../components/AlarmList.vue";
import { formatTime, formatValue } from "../format";
import { useAuth } from "../stores/auth";

const auth = useAuth();
const alarms = ref<Alarm[]>([]);
const events = ref<AlarmEvent[]>([]);

async function load(): Promise<void> {
  alarms.value = unwrap(await api.GET("/v1/alarms", { params: { query: { state: "firing" } } }));
  const to = new Date();
  const from = new Date(to.getTime() - 7 * 86400_000);
  events.value = unwrap(await api.GET("/v1/alarm-events", { params: { query: { from: from.toISOString(), to: to.toISOString() } } }));
}

async function acknowledge(id: number): Promise<void> {
  unwrap(await api.POST("/v1/alarms/{alarm_id}/acknowledge", { params: { path: { alarm_id: id } } }));
  await load();
}

onMounted(load);
</script>

<template>
  <div class="stack">
    <h1>Alarms</h1>
    <section class="card" aria-labelledby="firing-h">
      <h2 id="firing-h">Firing now</h2>
      <AlarmList :alarms="alarms" :can-acknowledge="auth.can('operator')" show-plant @acknowledge="acknowledge" />
    </section>
    <section class="card" aria-labelledby="events-h">
      <h2 id="events-h">Last 7 days</h2>
      <p v-if="!events.length" class="muted">Nothing happened.</p>
      <table v-else>
        <thead><tr><th scope="col">When (device time)</th><th scope="col">Plant</th><th scope="col">Rule</th>
          <th scope="col">Event</th><th scope="col" class="num">Value</th></tr></thead>
        <tbody>
          <tr v-for="e in events" :key="e.id">
            <td>{{ formatTime(e.at) }}</td>
            <td><RouterLink :to="`/plants/${e.plant_key}`">{{ e.plant_key }}</RouterLink></td>
            <td>{{ e.rule_name }}</td>
            <td><span class="badge" :class="e.kind === 'fired' ? 'bad' : 'ok'">{{ e.kind === "fired" ? "▲ fired" : "● cleared" }}</span></td>
            <td class="num">{{ formatValue(e.value) }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>
