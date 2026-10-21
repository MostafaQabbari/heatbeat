<!-- What this file does: plants on a map (OpenStreetMap tiles via Leaflet). Markers use a
     shape per status, not only a colour. Clicking a marker opens the plant.
     Note: the public OSM tile servers are for light use; production would self-host tiles. -->
<script setup lang="ts">
import L from "leaflet";
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import type { PlantSummary } from "../api/client";

const props = defineProps<{ plants: PlantSummary[] }>();
const router = useRouter();
const el = ref<HTMLDivElement | null>(null);
let map: L.Map | null = null;
let layer: L.LayerGroup | null = null;

const SHAPE: Record<string, { color: string; symbol: string }> = {
  online: { color: "#1d7a4d", symbol: "●" },
  offline: { color: "#b3261e", symbol: "▲" },
  never: { color: "#6b7280", symbol: "○" },
};

function draw(): void {
  if (!map || !layer) return;
  layer.clearLayers();
  const located = props.plants.filter((p) => p.site?.latitude != null && p.site?.longitude != null);
  for (const plant of located) {
    const look = plant.open_alarms ? { color: "#b3261e", symbol: "!" } : (SHAPE[plant.status] ?? SHAPE.never!);
    const icon = L.divIcon({
      className: "hb-marker",
      html: `<span style="background:${look.color}">${look.symbol}</span>`,
      iconSize: [26, 26],
    });
    L.marker([plant.site!.latitude!, plant.site!.longitude!], { icon, title: plant.name ?? plant.key, keyboard: true })
      .bindTooltip(`${plant.name ?? plant.key} · ${plant.status}${plant.open_alarms ? ` · ${plant.open_alarms} alarm(s)` : ""}`)
      .on("click", () => void router.push(`/plants/${plant.key}`))
      .addTo(layer);
  }
  if (located.length) {
    map.fitBounds(L.latLngBounds(located.map((p) => [p.site!.latitude!, p.site!.longitude!] as [number, number])), {
      padding: [30, 30],
      maxZoom: 12,
    });
  }
}

onMounted(() => {
  if (!el.value) return;
  map = L.map(el.value, { scrollWheelZoom: false }).setView([50.1, 9.0], 9);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  layer = L.layerGroup().addTo(map);
  draw();
});
onBeforeUnmount(() => map?.remove());
watch(() => props.plants, draw, { deep: true });
</script>

<template>
  <div ref="el" class="map card" role="region" aria-label="Map of plants" />
</template>

<style>
.map { height: 520px; padding: 0; overflow: hidden; }
.hb-marker span {
  display: grid; place-items: center; width: 26px; height: 26px; border-radius: 50%;
  color: #fff; font-weight: 700; border: 2px solid #fff; box-shadow: 0 1px 4px rgb(0 0 0 / 0.4);
}
</style>
