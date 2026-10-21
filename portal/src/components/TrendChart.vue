<!-- What this file does: a time-series chart for several datapoints (each with its unit on
     the axis), with zoom. Optional second period is drawn dashed for comparison. A data
     table below the chart gives the same numbers for screen readers. -->
<script setup lang="ts">
import { LineChart } from "echarts/charts";
import { DataZoomComponent, GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { Series } from "../api/client";
import { formatValue, label, unitLabel } from "../format";

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, CanvasRenderer]);

const props = defineProps<{ series: Series[]; previous?: Series[]; offsetMs?: number }>();
const el = ref<HTMLDivElement | null>(null);
let chart: echarts.ECharts | null = null;
const PALETTE = ["#b4441f", "#2f6db5", "#1d7a4d", "#8a4fb3", "#9a6200"];

function render(): void {
  if (!chart) return;
  const found = [...new Set(props.series.map((s) => s.unit || "value"))];
  const units = found.length ? found : ["value"];  // an axis must exist even before data arrives
  const lines = props.series.flatMap((s, i) => {
    const color = PALETTE[i % PALETTE.length];
    const axis = units.indexOf(s.unit || "value");
    const now = {
      name: label(s.datapoint), type: "line" as const, showSymbol: false, yAxisIndex: Math.min(axis, 1),
      itemStyle: { color }, data: s.points.map((p) => [p.ts, Number(p.avg.toFixed(2))]),
    };
    const before = props.previous?.find((p) => p.datapoint === s.datapoint);
    if (!before || !props.offsetMs) return [now];
    const shift = props.offsetMs;
    return [now, {
      name: `${label(s.datapoint)} (previous period)`, type: "line" as const, showSymbol: false,
      yAxisIndex: Math.min(axis, 1), itemStyle: { color }, lineStyle: { type: "dashed" as const, opacity: 0.6 },
      data: before.points.map((p) => [new Date(new Date(p.ts).getTime() + shift).toISOString(), Number(p.avg.toFixed(2))]),
    }];
  });
  chart.setOption(
    {
      animation: false,
      tooltip: { trigger: "axis" },
      legend: { top: 0, type: "scroll" },
      grid: { left: 56, right: units.length > 1 ? 56 : 20, top: 36, bottom: 56 },
      xAxis: { type: "time" },
      yAxis: units.slice(0, 2).map((u, i) => ({ type: "value", name: unitLabel(u), position: i ? "right" : "left", scale: true })),
      dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 8 }],
      series: lines,
    },
    true,
  );
}

let observer: ResizeObserver | null = null;
onMounted(() => {
  if (!el.value) return;
  chart = echarts.init(el.value);
  render();
  observer = new ResizeObserver(() => chart?.resize());
  observer.observe(el.value);
});
onBeforeUnmount(() => {
  observer?.disconnect();
  chart?.dispose();
});
watch(() => [props.series, props.previous], render, { deep: true });
</script>

<template>
  <div ref="el" class="chart" role="img" :aria-label="`Chart of ${series.map((s) => label(s.datapoint)).join(', ')}`" />
  <details>
    <summary class="muted">Show as table</summary>
    <table>
      <thead><tr><th scope="col">Datapoint</th><th scope="col">Time</th><th scope="col" class="num">Mean</th></tr></thead>
      <tbody>
        <template v-for="s in series" :key="s.datapoint">
          <tr v-for="p in s.points.slice(-24)" :key="s.datapoint + p.ts">
            <td>{{ label(s.datapoint) }}</td><td>{{ new Date(p.ts).toLocaleString("en-GB") }}</td>
            <td class="num">{{ formatValue(p.avg, s.unit) }}</td>
          </tr>
        </template>
      </tbody>
    </table>
  </details>
</template>

<style scoped>
.chart { width: 100%; height: 340px; }
details { margin-top: .5rem; }
</style>
