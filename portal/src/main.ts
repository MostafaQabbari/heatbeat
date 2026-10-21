// What this file does: starts the Vue app with router and store, and connects the API client
// to the login state (a 401 from the API sends the user back to the login page).
import { createPinia } from "pinia";
import { createApp } from "vue";
import App from "./App.vue";
import { configureAuth } from "./api/client";
import { router } from "./router";
import { useAuth } from "./stores/auth";
import "leaflet/dist/leaflet.css";
import "./style.css";

const app = createApp(App).use(createPinia()).use(router);
const auth = useAuth();
configureAuth(
  () => auth.token,
  () => {
    auth.signOut();
    void router.push({ name: "login" });
  },
);
app.mount("#app");
