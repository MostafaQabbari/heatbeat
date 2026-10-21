<!-- What this file does: a table of alarms with an "Acknowledge" button for operators. -->
<script setup lang="ts">
import type { Alarm } from "../api/client";
import { formatTime, formatValue } from "../format";
import StatusBadge from "./StatusBadge.vue";

defineProps<{ alarms: Alarm[]; canAcknowledge: boolean; showPlant?: boolean }>();
const emit = defineEmits<{ acknowledge: [id: number] }>();
</script>

<template>
  <p v-if="!alarms.length" class="muted">No alarms.</p>
  <table v-else>
    <thead>
      <tr>
        <th scope="col">State</th>
        <th v-if="showPlant" scope="col">Plant</th>
        <th scope="col">Rule</th>
        <th scope="col">Urgency</th>
        <th scope="col">Since</th>
        <th scope="col" class="num">Last value</th>
        <th scope="col">Acknowledged</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="alarm in alarms" :key="alarm.id">
        <td><StatusBadge :status="alarm.state" /></td>
        <td v-if="showPlant"><RouterLink :to="`/plants/${alarm.plant_key}`">{{ alarm.plant_name ?? alarm.plant_key }}</RouterLink></td>
        <td>{{ alarm.rule_name }}</td>
        <td>{{ alarm.urgency }}</td>
        <td>{{ formatTime(alarm.opened_at ?? alarm.since) }}</td>
        <td class="num">{{ formatValue(alarm.last_value) }}</td>
        <td>
          <span v-if="alarm.acknowledged_by">{{ alarm.acknowledged_by }}</span>
          <button v-else-if="canAcknowledge && alarm.state === 'firing'" type="button"
            @click="emit('acknowledge', alarm.id)">Acknowledge</button>
          <span v-else class="muted">–</span>
        </td>
      </tr>
    </tbody>
  </table>
</template>
