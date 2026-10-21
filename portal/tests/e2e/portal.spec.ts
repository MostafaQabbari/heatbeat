// Browser tests: the main user journeys, against the fake API.
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { expect, test } from "@playwright/test";
import { login, mockApi } from "./mock-api";

const axeSource = readFileSync(createRequire(import.meta.url).resolve("axe-core/axe.min.js"), "utf8");

test("pages need a login first", async ({ page }) => {
  await mockApi(page);
  await page.goto("/plants/p-0004");
  await expect(page).toHaveURL(/\/login\?redirect=/);
});

test("wrong password shows a clear message", async ({ page }) => {
  await mockApi(page);
  await page.goto("/login");
  await page.getByLabel("E-mail").fill("x@a.example");
  await page.getByLabel("Password").fill("wrong");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toHaveText("Wrong e-mail or password");
});

test("portfolio shows tiles and a map", async ({ page }) => {
  await mockApi(page);
  await login(page);
  await expect(page.getByRole("link", { name: /Plant Offenbach gas boiler 4/ })).toContainText("1 alarm");
  await page.getByRole("button", { name: "Map" }).click();
  await expect(page.getByRole("region", { name: "Map of plants" })).toBeVisible();
});

test("operator requests a parameter change", async ({ page }) => {
  const api = await mockApi(page, { role: "operator" });
  await login(page);
  await page.getByRole("link", { name: /Plant Offenbach gas boiler 4/ }).click();
  await page.getByLabel(/Heating curve level/).fill("24");
  await page.getByRole("button", { name: "Request change" }).click();
  await expect(page.getByRole("status")).toContainText("Waiting for the gateway to confirm");
  expect(api.commands).toEqual([{ datapoint: "heating_curve_level", value: 24, expected_version: 0 }]);
});

test("a conflicting change shows why it was refused (S16)", async ({ page }) => {
  await mockApi(page, { commandStatus: 409 });
  await login(page);
  await page.goto("/plants/p-0004");
  await page.getByLabel(/Heating curve level/).fill("24");
  await page.getByRole("button", { name: "Request change" }).click();
  await expect(page.getByRole("status")).toContainText("Parameter changed in the meantime");
});

test("viewers cannot change parameters", async ({ page }) => {
  await mockApi(page, { role: "viewer" });
  await login(page);
  await page.goto("/plants/p-0004");
  await expect(page.getByLabel(/Heating curve level/)).toBeDisabled();
  await expect(page.getByText("Viewers can see parameters but not change them.")).toBeVisible();
});

test("main pages have no automatically detectable accessibility problems", async ({ page }) => {
  await mockApi(page);
  await login(page);
  for (const path of ["/", "/plants/p-0004", "/alarms"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    await page.addScriptTag({ content: axeSource });
    const violations = await page.evaluate(async () => {
      // @ts-expect-error axe is injected above
      const result = await window.axe.run(document, { rules: { region: { enabled: false } } });
      return result.violations.map((v: { id: string; nodes: unknown[] }) => `${v.id} (${v.nodes.length})`);
    });
    expect(violations, path).toEqual([]);
  }
});
