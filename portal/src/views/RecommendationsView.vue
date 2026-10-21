<!-- What this file does: what the optimiser suggests and why. Approving turns a suggestion
     into a normal parameter change (range check, gateway confirmation, audit trail). -->
<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ApiError, api, unwrap, type Recommendation } from "../api/client";
import { formatTime, formatValue, label } from "../format";
import { useAuth } from "../stores/auth";

const auth = useAuth();
const items = ref<Recommendation[]>([]);
const message = ref<string | null>(null);

async function load(): Promise<void> {
  items.value = unwrap(await api.GET("/v1/recommendations"));
}

async function decide(rec: Recommendation, action: "approve" | "dismiss"): Promise<void> {
  message.value = null;
  try {
    const path = { params: { path: { rec_id: rec.id } } };
    unwrap(action === "approve"
      ? await api.POST("/v1/recommendations/{rec_id}/approve", path)
      : await api.POST("/v1/recommendations/{rec_id}/dismiss", path));
  } catch (exc) {
    message.value = exc instanceof ApiError ? exc.message : String(exc);
  }
  await load();
}

onMounted(load);
</script>

<template>
  <div class="stack">
    <h1>Recommendations</h1>
    <p class="muted">Savings are measured after a change (weather-normalised), never promised in advance.</p>
    <p v-if="message" class="notice error" role="alert">{{ message }}</p>
    <p v-if="!items.length" class="muted">No recommendations yet. The optimiser runs every morning.</p>
    <article v-for="rec in items" :key="rec.id" class="card stack">
      <div class="row spread">
        <h2>{{ rec.title }}</h2>
        <span class="badge" :class="rec.state === 'suggested' ? 'warn' : rec.state === 'dismissed' ? 'idle' : 'ok'">{{ rec.state }}</span>
      </div>
      <p>{{ rec.rationale }}</p>
      <p class="muted">
        <RouterLink :to="`/plants/${rec.plant_key}`">{{ rec.plant_key }}</RouterLink> · {{ label(rec.datapoint) }}:
        {{ formatValue(rec.current_value) }} → {{ formatValue(rec.proposed_value) }} · {{ formatTime(rec.created_at) }}
        <template v-if="rec.decided_by"> · decided by {{ rec.decided_by }}</template>
      </p>
      <p v-if="rec.measured_effect" class="notice ok">Measured: {{ rec.measured_effect }}</p>
      <div v-if="rec.state === 'suggested'" class="row">
        <button class="primary" type="button" :disabled="!auth.can('admin')" @click="decide(rec, 'approve')">Approve and apply</button>
        <button type="button" :disabled="!auth.can('operator')" @click="decide(rec, 'dismiss')">Dismiss</button>
      </div>
    </article>
  </div>
</template>
