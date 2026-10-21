// Component tests: render like a user sees them, query by role and label (not CSS classes),
// and run the axe accessibility checker on each.
import { fireEvent, render, screen } from "@testing-library/vue";
import { describe, expect, it } from "vitest";
import { createMemoryHistory, createRouter } from "vue-router";
import type { Alarm, Parameter, PlantSummary } from "../../src/api/client";
import AlarmList from "../../src/components/AlarmList.vue";
import ParameterForm from "../../src/components/ParameterForm.vue";
import PlantTile from "../../src/components/PlantTile.vue";
import StatusBadge from "../../src/components/StatusBadge.vue";
import { accessibilityViolations } from "./a11y";

const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:p(.*)*", component: { template: "<div/>" } }] });
const global = { plugins: [router] };

const level: Parameter = { datapoint: "heating_curve_level", unit: "degC", value: 26, min_value: 15, max_value: 50, version: 3 };

describe("StatusBadge", () => {
  it("shows the state as text, not only as colour", () => {
    render(StatusBadge, { props: { status: "offline" } });
    expect(screen.getByText("Offline")).toBeTruthy();
  });
});

describe("PlantTile", () => {
  const plant: PlantSummary = {
    key: "p-0004", name: "Offenbach gas boiler 4", plant_type: "gas_boiler", gateway: "gw-0004", status: "online",
    created_at: "2026-10-21T08:00:00Z", last_seen_at: new Date().toISOString(), site: null, open_alarms: 2,
  };

  it("links to the plant and shows open alarms", async () => {
    const { container } = render(PlantTile, { props: { plant }, global });
    expect(screen.getByRole("link", { name: "Plant Offenbach gas boiler 4" }).getAttribute("href")).toBe("/plants/p-0004");
    expect(screen.getByText(/2 alarms/)).toBeTruthy();
    expect(await accessibilityViolations(container)).toEqual([]);
  });
});

describe("ParameterForm", () => {
  it("refuses values outside the allowed range before sending", async () => {
    const { emitted } = render(ParameterForm, { props: { parameter: level } });
    await fireEvent.update(screen.getByLabelText(/Heating curve level/), "80");
    expect(screen.getByText("Allowed: 15 to 50")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Request change" }) as HTMLButtonElement).disabled).toBe(true);
    expect(emitted().submit).toBeUndefined();
  });

  it("sends the value together with the version the user saw (S16)", async () => {
    const { emitted } = render(ParameterForm, { props: { parameter: level } });
    await fireEvent.update(screen.getByLabelText(/Heating curve level/), "24");
    await fireEvent.click(screen.getByRole("button", { name: "Request change" }));
    expect(emitted().submit).toEqual([[24, 3]]);
  });

  it("is disabled for viewers and passes the accessibility check", async () => {
    const { container } = render(ParameterForm, { props: { parameter: level, disabled: true } });
    expect((screen.getByLabelText(/Heating curve level/) as HTMLInputElement).disabled).toBe(true);
    expect(await accessibilityViolations(container)).toEqual([]);
  });
});

describe("AlarmList", () => {
  const alarm: Alarm = {
    id: 7, plant_key: "p-0004", plant_name: null, rule_id: 1, rule_name: "Short cycling", kind: "rate", urgency: "medium",
    state: "firing", since: "2026-10-21T09:00:00Z", opened_at: "2026-10-21T09:15:00Z", last_value: 16, threshold: 13,
    acknowledged_by: null, acknowledged_at: null,
  };

  it("lets operators acknowledge a firing alarm", async () => {
    const { emitted, container } = render(AlarmList, { props: { alarms: [alarm], canAcknowledge: true }, global });
    await fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    expect(emitted().acknowledge).toEqual([[7]]);
    expect(await accessibilityViolations(container)).toEqual([]);
  });

  it("hides the button for viewers", () => {
    render(AlarmList, { props: { alarms: [alarm], canAcknowledge: false }, global });
    expect(screen.queryByRole("button", { name: "Acknowledge" })).toBeNull();
  });
});
