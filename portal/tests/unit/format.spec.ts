import { describe, expect, it } from "vitest";
import { ago, formatValue, label, unitLabel } from "../../src/format";

describe("format", () => {
  it("shows values with sensible precision and units", () => {
    expect(formatValue(62.456, "degC")).toBe("62.5 °C");
    expect(formatValue(3.14159, "")).toBe("3.14");
    expect(formatValue(null)).toBe("–");
  });

  it("turns keys into labels", () => {
    expect(label("flow_temp")).toBe("Flow temp");
    expect(label("burner_power_kw")).toBe("Burner power (kW)");
    expect(label("cop")).toBe("COP");
    expect(unitLabel("degC")).toBe("°C");
  });

  it("says how long ago", () => {
    const now = new Date("2026-10-21T12:00:00Z");
    expect(ago("2026-10-21T11:59:30Z", now)).toBe("30 s ago");
    expect(ago("2026-10-21T11:00:00Z", now)).toBe("60 min ago");
    expect(ago(null, now)).toBe("never");
  });
});
