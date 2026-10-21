<!-- What this file does: shows a plant's or alarm's state as text with a shape, so it is
     readable for colour-blind users and in black-and-white print. -->
<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ status: string }>();

const LOOK: Record<string, { cls: string; mark: string; text: string }> = {
  online: { cls: "ok", mark: "●", text: "Online" },
  offline: { cls: "bad", mark: "▲", text: "Offline" },
  never: { cls: "idle", mark: "○", text: "No data yet" },
  firing: { cls: "bad", mark: "▲", text: "Firing" },
  pending: { cls: "warn", mark: "◆", text: "Pending" },
  normal: { cls: "ok", mark: "●", text: "Normal" },
};
const look = computed(() => LOOK[props.status] ?? { cls: "idle", mark: "○", text: props.status });
</script>

<template>
  <span class="badge" :class="look.cls">
    <span aria-hidden="true">{{ look.mark }}</span>{{ look.text }}
  </span>
</template>
