// What this file does: runs the axe accessibility checker on a rendered component.
import axe from "axe-core";

export async function accessibilityViolations(container: Element): Promise<string[]> {
  const result = await axe.run(container, { rules: { region: { enabled: false } } });
  return result.violations.map((v) => `${v.id}: ${v.help}`);
}
