// What this file does: a small fake API for browser tests (answers like the real one).
import type { Page, Route } from "@playwright/test";

const now = () => new Date().toISOString();
export const plants = [
  { key: "p-0001", name: "Alzenau gas boiler 1", plant_type: "gas_boiler", gateway: "gw-0001", status: "online",
    created_at: now(), last_seen_at: now(), site: { name: "S1", address: "Alzenau", latitude: 50.09, longitude: 9.06 }, open_alarms: 0 },
  { key: "p-0004", name: "Offenbach gas boiler 4", plant_type: "gas_boiler", gateway: "gw-0004", status: "online",
    created_at: now(), last_seen_at: now(), site: { name: "S4", address: "Offenbach", latitude: 50.1, longitude: 8.77 }, open_alarms: 1 },
];

export interface MockOptions { role?: "viewer" | "operator" | "admin"; commandStatus?: number }

export async function mockApi(page: Page, options: MockOptions = {}): Promise<{ commands: unknown[] }> {
  const role = options.role ?? "operator";
  const commands: unknown[] = [];
  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: status >= 400 ? "application/problem+json" : "application/json", body: JSON.stringify(body) });

  await page.route("https://tile.openstreetmap.org/**", (r) => r.abort());
  await page.route("**/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    if (path === "/v1/auth/token") {
      const form = new URLSearchParams(route.request().postData() ?? "");
      return form.get("password") === "right"
        ? json(route, { access_token: "t", token_type: "bearer", expires_in: 3600 })
        : json(route, { type: "about:blank", title: "Wrong e-mail or password", status: 401 }, 401);
    }
    if (path === "/v1/auth/me") return json(route, { id: 1, email: `${role}@a.example`, role, tenant_id: 1 });
    if (path === "/v1/plants") return json(route, { items: plants, next_cursor: null });
    if (path === "/v1/plants/p-0004") {
      return json(route, {
        ...plants[1],
        latest: [{ datapoint: "flow_temp", unit: "degC", ts: now(), value: 40.6, quality: 0 }],
        parameters: [{ datapoint: "heating_curve_level", unit: "degC", value: 26, min_value: 15, max_value: 50, version: 0 }],
      });
    }
    if (path === "/v1/plants/p-0004/commands" && method === "POST") {
      commands.push(route.request().postDataJSON());
      if (options.commandStatus === 409)
        return json(route, { type: "about:blank", title: "Parameter changed in the meantime", status: 409,
          detail: "You saw version 0, the current version is 1. Reload and try again." }, 409);
      return json(route, { id: "c-1", plant_key: "p-0004", datapoint: "heating_curve_level", requested_value: 24,
        previous_value: 26, state: "requested", needs_approval: false, source: "portal", requested_by: `${role}@a.example`,
        approved_by: null, reason: null, applied_value: null, created_at: now(), sent_at: null, expires_at: now(), result_at: null }, 201);
    }
    if (path.endsWith("/series")) {
      const dps = url.searchParams.getAll("dp");
      return json(route, dps.map((dp) => ({ datapoint: dp, unit: "degC", bucket: "5m",
        points: [{ ts: now(), avg: 40, min: 39, max: 41, count: 10 }] })));
    }
    if (path === "/v1/alarms") {
      return json(route, [{ id: 3, plant_key: "p-0004", plant_name: "Offenbach gas boiler 4", rule_id: 1,
        rule_name: "Short cycling (burner starts per hour)", kind: "rate", urgency: "medium", state: "firing",
        since: now(), opened_at: now(), last_value: 16, threshold: 13, acknowledged_by: null, acknowledged_at: null }]);
    }
    return json(route, []);  // commands list, parameter history, events, recommendations
  });
  return { commands };
}

export async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("E-mail").fill("someone@a.example");
  await page.getByLabel("Password").fill("right");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("heading", { name: "Portfolio" }).waitFor();
}
