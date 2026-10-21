// What this file does: one way to show numbers, units, times and labels everywhere.
const time = new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short" });

export function formatTime(iso: string | null | undefined): string {
  return iso ? time.format(new Date(iso)) : "never";
}

export function ago(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "never";
  const seconds = Math.max(0, Math.round((now.getTime() - new Date(iso).getTime()) / 1000));
  if (seconds < 90) return `${seconds} s ago`;
  if (seconds < 90 * 60) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 36 * 3600) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

const UNITS: Record<string, string> = { degC: "°C", "%": "%", kW: "kW" };

export function unitLabel(unit: string): string {
  return UNITS[unit] ?? unit;
}

export function formatValue(value: number | null | undefined, unit = ""): string {
  if (value === null || value === undefined) return "–";
  const digits = Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 10 ? 1 : 2;
  const shown = value.toLocaleString("en-GB", { maximumFractionDigits: digits });
  const suffix = UNITS[unit] ?? unit;
  return suffix ? `${shown} ${suffix}` : shown;
}

export function label(key: string): string {
  const text = key.replace(/_kw$/, " (kW)").replace(/^cop$/, "COP").replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}
