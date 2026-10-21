<!-- What this file does: request a change of one writable parameter. Checks the allowed
     range before sending, sends the version the user saw (conflict check on the server),
     and explains what happens next (approval, gateway confirmation). -->
<script setup lang="ts">
import { computed, ref } from "vue";
import type { Parameter } from "../api/client";
import { formatValue, label } from "../format";

const props = defineProps<{ parameter: Parameter; disabled?: boolean }>();
const emit = defineEmits<{ submit: [value: number, version: number] }>();

const isSwitch = computed(() => props.parameter.datapoint === "heating_enabled");
const draft = ref<string>(isSwitch.value ? String(1 - (props.parameter.value ?? 1)) : "");
const error = computed(() => {
  const value = Number(draft.value);
  if (draft.value.trim() === "" || Number.isNaN(value)) return "Enter a number";
  if (value < props.parameter.min_value || value > props.parameter.max_value)
    return `Allowed: ${props.parameter.min_value} to ${props.parameter.max_value}`;
  if (value === props.parameter.value) return "That is already the current value";
  return null;
});
const showError = computed(() => draft.value !== "" && error.value !== null);
const inputId = computed(() => `param-${props.parameter.datapoint}`);

function send(): void {
  if (!error.value) emit("submit", Number(draft.value), props.parameter.version);
}
</script>

<template>
  <form class="param" @submit.prevent="send">
    <label :for="inputId">
      {{ label(parameter.datapoint) }}
      <span class="muted">now {{ formatValue(parameter.value, parameter.unit) }} · version {{ parameter.version }}</span>
    </label>
    <div class="row">
      <select v-if="isSwitch" :id="inputId" v-model="draft" :disabled="disabled">
        <option value="1">On</option>
        <option value="0">Off (summer mode)</option>
      </select>
      <input v-else :id="inputId" v-model="draft" type="text" inputmode="decimal" :disabled="disabled"
        :placeholder="`${parameter.min_value} to ${parameter.max_value}`" :aria-invalid="showError"
        :aria-describedby="`${inputId}-help`" />
      <button class="primary" type="submit" :disabled="disabled || !!error">Request change</button>
    </div>
    <small :id="`${inputId}-help`" :class="showError ? 'error' : 'muted'">
      {{ showError ? error : "Goes to the gateway with an expiry; big or risky changes wait for a second person." }}
    </small>
  </form>
</template>

<style scoped>
.param { display: grid; gap: .35rem; padding: .75rem 0; border-bottom: 1px solid var(--border); }
.param:last-child { border-bottom: 0; }
input { width: 9rem; }
.error { color: var(--bad); }
</style>
