// What this file does: the pages of the portal and the rule "log in first".
import { createRouter, createWebHistory } from "vue-router";
import { useAuth } from "./stores/auth";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: () => import("./views/LoginView.vue"), meta: { public: true } },
    { path: "/", name: "portfolio", component: () => import("./views/PortfolioView.vue") },
    { path: "/plants/:plantKey", name: "plant", component: () => import("./views/PlantView.vue"), props: true },
    { path: "/alarms", name: "alarms", component: () => import("./views/AlarmsView.vue") },
    { path: "/faults", name: "faults", component: () => import("./views/FaultsView.vue") },
    { path: "/recommendations", name: "recommendations", component: () => import("./views/RecommendationsView.vue") },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

router.beforeEach(async (to) => {
  const auth = useAuth();
  if (to.meta.public) return true;
  if (!auth.token) return { name: "login", query: { redirect: to.fullPath } };
  try {
    await auth.loadMe();
  } catch {
    auth.signOut();
    return { name: "login" };
  }
  return true;
});
