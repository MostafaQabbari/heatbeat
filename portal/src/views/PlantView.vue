<!-- What this file does: one plant: latest values, alarms, trends (with period comparison),
     parameters with change requests, open/past commands, and the audit history. -->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import {
  ApiError, api, unwrap,
  type Alarm, type AuditEntry, type Command, type PlantDetail, type Series,
} from "../api/client";
import AlarmList from "../components/AlarmList.vue";
import ParameterForm from "../components/ParameterForm.vue";
import StatusBadge from "../components/StatusBadge.vue";
import TrendChart from "../components/TrendChart.vue";
import { ago, formatTime, formatValue, label } from "../format";
import { useAuth } from "../stores/auth";

const props = defineProps<{ plantKey: string }>();  // from the route /plants/:plantKey
const auth = useAuth();
const plantKey = computed(() => props.plantKey);

const plant = ref<PlantDetail | null>(null);
const alarms = ref<Alarm[]>([]);
const commands = ref<Command[]>([]);
const history = ref<AuditEntry[]>([]);
const series = ref<Series[]>([]);
const previous = ref<Series[]>([]);
const error = ref<string | null>(null);
const message = ref<{ kind: "ok" | "error"; text: string } | null>(null);

const RANGES = { "6 h": 6, "24 h": 24, "7 days": 168, "30 days": 720 } as const;
const range = ref<keyof typeof RANGES>("24 h");
const compare = ref(false);
const chosen = ref<string[]>(["flow_temp", "return_temp", "outdoor_temp"]);
const available = computed(() => plant.value?.latest.map((v) => v.datapoint) ?? []);
const offsetMs = computed(() => RANGES[range.value] * 3600_000);

function bucketFor(hours: number): "1m" | "5m" | "15m" | "1h" {
  return hours <= 6 ? "1m" : hours <= 24 ? "5m" : hours <= 168 ? "15m" : "1h";
}

async function loadSeries(): Promise<void> {
  const hours = RANGES[range.value];
  const to = new Date();
  const from = new Date(to.getTime() - hours * 3600_000);
  const dp = chosen.value.filter((d) => available.value.includes(d));
  if (!dp.length) { series.value = []; return; }
  const query = (start: Date, end: Date) => ({
    params: { path: { plant_key: plantKey.value }, query: { dp, from: start.toISOString(), to: end.toISOString(), bucket: bucketFor(hours) } },
  });
  series.value = unwrap(await api.GET("/v1/plants/{plant_key}/series", query(from, to)));
  previous.value = compare.value
    ? unwrap(await api.GET("/v1/plants/{plant_key}/series", query(new Date(from.getTime() - offsetMs.value), from)))
    : [];
}

async function load(): Promise<void> {
  try {
    const path = { params: { path: { plant_key: plantKey.value } } };
    plant.value = unwrap(await api.GET("/v1/plants/{plant_key}", path));
    alarms.value = unwrap(await api.GET("/v1/alarms", { params: { query: { plant: plantKey.value } } }));  // firing
    commands.value = unwrap(await api.GET("/v1/plants/{plant_key}/commands", path));
    history.value = unwrap(await api.GET("/v1/plants/{plant_key}/parameter-history", path));
    await loadSeries();
  } catch (exc) {
    error.value = exc instanceof ApiError && exc.status === 404 ? "Plant not found" : String(exc);
  }
}

async function requestChange(datapoint: string, value: number, version: number): Promise<void> {
  message.value = null;
  try {
    const created = unwrap(await api.POST("/v1/plants/{plant_key}/commands", {
      params: { path: { plant_key: plantKey.value } },
      body: { datapoint, value, expected_version: version },
    }));
    message.value = {
      kind: "ok",
      text: created.state === "pending_approval"
        ? "Requested. This change needs approval by a second person (admin)."
        : "Requested. Waiting for the gateway to confirm…",
    };
  } catch (exc) {
    message.value = { kind: "error", text: exc instanceof ApiError ? exc.message : String(exc) };
  }
  await load();
}

async function act(command: Command, action: "approve" | "cancel"): Promise<void> {
  message.value = null;
  try {
    const path = { params: { path: { command_id: command.id } } };
    unwrap(action === "approve"
      ? await api.POST("/v1/commands/{command_id}/approve", path)
      : await api.POST("/v1/commands/{command_id}/cancel", path));
  } catch (exc) {
    message.value = { kind: "error", text: exc instanceof ApiError ? exc.message : String(exc) };
  }
  await load();
}

async function acknowledge(id: number): Promise<void> {
  unwrap(await api.POST("/v1/alarms/{alarm_id}/acknowledge", { params: { path: { alarm_id: id } } }));
  await load();
}

onMounted(load);
watch([range, compare, chosen], () => void loadSeries().catch((e) => (error.value = String(e))), { deep: true });
</script>

<template>
  <p v-if="error" class="notice error" role="alert">{{ error }}</p>
  <div v-else-if="plant" class="stack">
    <div>
      <RouterLink to="/" class="muted">← Portfolio</RouterLink>
      <div class="row spread">
        <h1>{{ plant.name ?? plant.key }}</h1>
        <StatusBadge :status="plant.status" />
      </div>
      <p class="muted">
        {{ label(plant.plant_type) }} · <span class="mono">{{ plant.key }}</span> via <span class="mono">{{ plant.gateway }}</span>
        <template v-if="plant.site"> · {{ plant.site.address }}</template> · last data {{ ago(plant.last_seen_at) }}
      </p>
    </div>

    <section v-if="alarms.length" class="card" aria-labelledby="alarms-h">
      <h2 id="alarms-h">Alarms</h2>
      <AlarmList :alarms="alarms" :can-acknowledge="auth.can('operator')" @acknowledge="acknowledge" />
    </section>

    <section class="card" aria-labelledby="trend-h">
      <div class="row spread">
        <h2 id="trend-h">Trends</h2>
        <div class="row">
          <div class="row" role="group" aria-label="Period">
            <button v-for="(_, name) in RANGES" :key="name" type="button" :aria-pressed="range === name"
              @click="range = name">{{ name }}</button>
          </div>
          <label class="inline"><input v-model="compare" type="checkbox" /> compare with previous period</label>
        </div>
      </div>
      <fieldset class="row dps">
        <legend class="muted">Datapoints</legend>
        <label v-for="dp in available" :key="dp" class="inline">
          <input v-model="chosen" type="checkbox" :value="dp" /> {{ label(dp) }}
        </label>
      </fieldset>
      <TrendChart :series="series" :previous="previous" :offset-ms="compare ? offsetMs : 0" />
    </section>

    <div class="grid cols-2">
      <section class="card" aria-labelledby="latest-h">
        <h2 id="latest-h">Latest values</h2>
        <table>
          <tbody>
            <tr v-for="v in plant.latest" :key="v.datapoint">
              <th scope="row">{{ label(v.datapoint) }}</th>
              <td class="num">{{ formatValue(v.value, v.unit) }}</td>
              <td class="muted">{{ ago(v.ts) }}<span v-if="v.quality" title="implausible value"> · flagged</span></td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="card" aria-labelledby="params-h">
        <h2 id="params-h">Parameters</h2>
        <p v-if="message" class="notice" :class="message.kind" role="status">{{ message.text }}</p>
        <ParameterForm v-for="p in plant.parameters" :key="p.datapoint + p.version" :parameter="p"
          :disabled="!auth.can('operator')" @submit="(value, version) => requestChange(p.datapoint, value, version)" />
        <p v-if="!auth.can('operator')" class="muted">Viewers can see parameters but not change them.</p>
      </section>
    </div>

    <section class="card" aria-labelledby="commands-h">
      <h2 id="commands-h">Parameter changes</h2>
      <p v-if="!commands.length" class="muted">No changes requested yet.</p>
      <table v-else>
        <thead><tr><th scope="col">Requested</th><th scope="col">Parameter</th><th scope="col" class="num">From → to</th>
          <th scope="col">State</th><th scope="col">By</th><th scope="col"></th></tr></thead>
        <tbody>
          <tr v-for="c in commands" :key="c.id">
            <td>{{ formatTime(c.created_at) }}</td>
            <td>{{ label(c.datapoint) }}</td>
            <td class="num">{{ formatValue(c.previous_value) }} → {{ formatValue(c.requested_value) }}</td>
            <td>{{ c.state.replace("_", " ") }}<span v-if="c.reason" class="muted"> · {{ c.reason }}</span></td>
            <td>{{ c.requested_by ?? c.source }}<span v-if="c.approved_by" class="muted"> · approved by {{ c.approved_by }}</span></td>
            <td class="row">
              <button v-if="c.state === 'pending_approval' && auth.can('admin') && c.requested_by !== auth.me?.email"
                type="button" @click="act(c, 'approve')">Approve</button>
              <button v-if="['pending_approval', 'requested'].includes(c.state) && auth.can('operator')"
                type="button" @click="act(c, 'cancel')">Cancel</button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="card" aria-labelledby="audit-h">
      <h2 id="audit-h">History (audit trail)</h2>
      <p v-if="!history.length" class="muted">No confirmed changes yet.</p>
      <table v-else>
        <thead><tr><th scope="col">When</th><th scope="col">Parameter</th><th scope="col" class="num">Old → new</th>
          <th scope="col">Source</th><th scope="col">Who</th></tr></thead>
        <tbody>
          <tr v-for="h in history" :key="h.id">
            <td>{{ formatTime(h.at) }}</td><td>{{ label(h.datapoint) }}</td>
            <td class="num">{{ formatValue(h.old_value) }} → {{ formatValue(h.new_value) }}</td>
            <td>{{ h.source }}</td>
            <td>{{ h.actor ?? "–" }}<span v-if="h.approved_by" class="muted"> · approved by {{ h.approved_by }}</span></td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
  <p v-else class="muted">Loading…</p>
</template>

<style scoped>
.inline { display: inline-flex; gap: .35rem; align-items: center; }
.dps { border: 0; padding: 0; margin: .5rem 0; }
</style>
