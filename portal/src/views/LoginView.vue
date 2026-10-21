<!-- What this file does: the login page. -->
<script setup lang="ts">
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError } from "../api/client";
import { useAuth } from "../stores/auth";

const auth = useAuth();
const router = useRouter();
const route = useRoute();
const email = ref("");
const password = ref("");
const error = ref<string | null>(null);
const busy = ref(false);

async function submit(): Promise<void> {
  error.value = null;
  busy.value = true;
  try {
    await auth.signIn(email.value, password.value);
    const target = typeof route.query.redirect === "string" ? route.query.redirect : "/";
    await router.push(target);
  } catch (exc) {
    error.value = exc instanceof ApiError ? exc.title : "The server is not reachable";
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section class="login card">
    <h1>Heatbeat</h1>
    <p class="muted">Heating plants of every make, in one place.</p>
    <form class="stack" @submit.prevent="submit">
      <label>E-mail <input v-model="email" type="email" autocomplete="username" required /></label>
      <label>Password <input v-model="password" type="password" autocomplete="current-password" required /></label>
      <p v-if="error" class="notice error" role="alert">{{ error }}</p>
      <button class="primary" type="submit" :disabled="busy">{{ busy ? "Signing in…" : "Sign in" }}</button>
    </form>
  </section>
</template>

<style scoped>
.login { max-width: 380px; margin: 12vh auto 0; padding: 2rem; }
input { width: 100%; }
button { width: 100%; padding: .6rem; }
</style>
