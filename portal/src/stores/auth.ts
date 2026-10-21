// What this file does: who is logged in. The token lives in sessionStorage (gone when the
// tab closes). Known trade-off: a script injected into the page could read it; an
// httpOnly cookie would be safer and is listed in docs/security.md as a next step.
import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { api, requestToken, unwrap, type Me } from "../api/client";

const KEY = "heatbeat.token";
const RANK: Record<string, number> = { viewer: 1, operator: 2, admin: 3 };

function readToken(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function writeToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(KEY, token);
    else sessionStorage.removeItem(KEY);
  } catch {
    /* storage blocked: stay logged in for this page only */
  }
}

export const useAuth = defineStore("auth", () => {
  const token = ref<string | null>(readToken());
  const me = ref<Me | null>(null);
  const role = computed(() => me.value?.role ?? "viewer");

  function can(required: "viewer" | "operator" | "admin"): boolean {
    return (RANK[role.value] ?? 0) >= (RANK[required] ?? 99);
  }

  async function loadMe(): Promise<void> {
    if (token.value && !me.value) me.value = unwrap(await api.GET("/v1/auth/me"));
  }

  async function signIn(email: string, password: string): Promise<void> {
    token.value = await requestToken(email, password);
    writeToken(token.value);
    me.value = null;
    await loadMe();
  }

  function signOut(): void {
    token.value = null;
    me.value = null;
    writeToken(null);
  }

  return { token, me, role, can, loadMe, signIn, signOut };
});
