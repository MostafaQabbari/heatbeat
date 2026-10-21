// What this file does: build and dev-server settings. In development, requests to /v1
// are forwarded to the API on port 8000, so the browser sees one origin (no CORS needed).
import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [vue()],
  server: {
    // 127.0.0.1, not "localhost": on Windows "localhost" can mean IPv6 (::1) only, and then
    // http://127.0.0.1:5173 (used by the browser tests) never answers.
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/v1": "http://127.0.0.1:8000" },
  },
  build: { chunkSizeWarningLimit: 600 },  // the chart library (ECharts) loads only on the plant page
  test: {
    environment: "jsdom",
    include: ["tests/unit/**/*.spec.ts"],
    setupFiles: ["tests/unit/setup.ts"],
  },
});