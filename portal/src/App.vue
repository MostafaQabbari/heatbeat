<!-- What this file does: the page frame: top navigation (when logged in) and the current page. -->
<script setup lang="ts">
import { useRoute, useRouter } from "vue-router";
import { useAuth } from "./stores/auth";

const auth = useAuth();
const route = useRoute();
const router = useRouter();

function logout(): void {
  auth.signOut();
  void router.push({ name: "login" });
}
</script>

<template>
  <header v-if="!route.meta.public" class="topbar">
    <RouterLink to="/" class="brand" aria-label="Heatbeat home">
      <svg viewBox="0 0 32 32" width="26" height="26" aria-hidden="true">
        <rect width="32" height="32" rx="7" fill="var(--accent)" />
        <path d="M5 17h6l3-7 4 13 3-6h6" fill="none" stroke="var(--accent-ink)" stroke-width="2.6"
          stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      <span>Heatbeat</span>
    </RouterLink>
    <nav aria-label="Main">
      <RouterLink to="/">Portfolio</RouterLink>
      <RouterLink to="/alarms">Alarms</RouterLink>
      <RouterLink to="/faults">Faults</RouterLink>
      <RouterLink to="/recommendations">Recommendations</RouterLink>
    </nav>
    <div class="who">
      <span v-if="auth.me" class="muted">{{ auth.me.email }} · {{ auth.me.role }}</span>
      <button type="button" @click="logout">Log out</button>
    </div>
  </header>
  <main>
    <RouterView />
  </main>
</template>

<style scoped>
.topbar {
  display: flex; align-items: center; gap: 1.5rem; padding: .6rem 1.25rem; flex-wrap: wrap;
  background: var(--surface); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 1000;
}
.brand { display: flex; align-items: center; gap: .5rem; font-weight: 700; text-decoration: none; }
nav { display: flex; gap: .25rem; flex: 1; flex-wrap: wrap; }
nav a { text-decoration: none; padding: .35rem .65rem; border-radius: 8px; color: var(--muted); }
nav a.router-link-exact-active { background: var(--surface-2); color: var(--text); font-weight: 600; }
.who { display: flex; gap: .75rem; align-items: center; font-size: .9rem; }
</style>
