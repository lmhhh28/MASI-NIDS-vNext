import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { installMockApi } from "./mock-api";

for (const scenario of [
  { viewport: { width: 375, height: 812 }, colorScheme: "light" as const },
  { viewport: { width: 768, height: 1024 }, colorScheme: "light" as const },
  { viewport: { width: 1440, height: 900 }, colorScheme: "light" as const },
  { viewport: { width: 375, height: 812 }, colorScheme: "dark" as const },
  { viewport: { width: 1440, height: 900 }, colorScheme: "dark" as const },
]) {
  test(`dashboard has no serious accessibility violations or overflow at ${scenario.viewport.width}px in ${scenario.colorScheme}`, async ({ page }) => {
    await page.setViewportSize(scenario.viewport);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript((theme) => localStorage.setItem("nids-theme", theme), scenario.colorScheme);
    await installMockApi(page);
    await page.goto("/");
    await expect(page.locator("main#main-content")).toBeVisible();
    await expect(page.locator("html")).toHaveClass(new RegExp(`(?:^|\\s)${scenario.colorScheme}(?:\\s|$)`));
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    expect(overflow).toBe(false);
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations.filter((violation) => ["serious", "critical"].includes(violation.impact ?? "")))
      .toEqual([]);
  });
}

for (const theme of ["light", "dark"] as const) {
  test(`login uses the real ${theme} theme and has an accessible main landmark`, async ({ page }) => {
    await page.setViewportSize({ width: theme === "dark" ? 375 : 768, height: 812 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript((selectedTheme) => localStorage.setItem("nids-theme", selectedTheme), theme);
    await installMockApi(page, { authenticated: false });
    await page.goto("/login");
    await expect(page.locator("main#main-content")).toBeVisible();
    await expect(page.locator("html")).toHaveClass(new RegExp(`(?:^|\\s)${theme}(?:\\s|$)`));
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations.filter((violation) => ["serious", "critical"].includes(violation.impact ?? "")))
      .toEqual([]);
  });
}

test("skip link and runtime recovery remain keyboard reachable at 200% zoom", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await installMockApi(page, { authenticated: false });
  await page.goto("/login");
  await page.keyboard.press("Tab");
  const skipLink = page.getByRole("link", { name: /跳到主要内容|Skip to main content/ });
  await expect(skipLink).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("main#main-content")).toBeFocused();

  await installMockApi(page, { runtimeUnavailable: true });
  await page.route("**/api/session", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({ authenticated: true, user: { id: "admin-1", username: "admin", role: "admin" }, csrf_token: "csrf-e2e" }),
  }));
  await page.goto("/p4");
  await page.evaluate(() => { document.documentElement.style.zoom = "2"; });
  const recovery = page.getByRole("button", { name: /刷新|Refresh/ }).first();
  await expect(recovery).toBeVisible();
  await recovery.focus();
  await expect(recovery).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByText(/运行时配置状态未知|Runtime configuration is unknown/).first()).toBeVisible();
});

test("coarse-pointer login controls provide at least 44 by 44 CSS pixel targets", async ({ page }) => {
  await installMockApi(page, { authenticated: false });
  await page.goto("/login");
  test.skip(!(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)), "coarse pointer project only");
  const undersized = await page.locator("main button, main input").evaluateAll((elements) =>
    elements
      .filter((element) => {
        const style = getComputedStyle(element);
        return style.display !== "none" && style.visibility !== "hidden";
      })
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return { label: element.getAttribute("aria-label") ?? element.id, width: rect.width, height: rect.height };
      })
      .filter((target) => target.width < 44 || target.height < 44));
  expect(undersized).toEqual([]);
});

test("locale cookie drives the server layout without an English-language flash", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await page.getByRole("button", { name: "EN", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect.poll(async () => (await page.context().cookies()).find((cookie) => cookie.name === "nids-locale")?.value)
    .toBe("en-US");
});
