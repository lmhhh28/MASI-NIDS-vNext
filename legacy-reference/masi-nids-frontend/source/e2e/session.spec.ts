import { expect, test } from "@playwright/test";

import { adminUser, installMockApi } from "./mock-api";

test("login, refresh and logout keep bearer tokens out of browser storage", async ({ page }) => {
  let loggedIn = false;
  let refreshCalls = 0;
  let logoutCalls = 0;
  await installMockApi(page, { authenticated: false });
  await page.route("**/api/session", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        authenticated: loggedIn,
        user: loggedIn ? adminUser : null,
        csrf_token: "csrf-e2e",
      }),
    }),
  );
  await page.route("**/api/session/login", async (route) => {
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-e2e");
    loggedIn = true;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ authenticated: true, user: adminUser, csrf_token: "csrf-e2e" }),
    });
  });
  await page.route("**/api/session/refresh", async (route) => {
    refreshCalls += 1;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ authenticated: true, user: adminUser, csrf_token: "csrf-e2e" }),
    });
  });
  await page.route("**/api/session/logout", async (route) => {
    logoutCalls += 1;
    loggedIn = false;
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ status: "ok" }) });
  });

  await page.goto("/login");
  await page.getByLabel("用户名").fill("admin");
  await page.locator("#password").fill("password123");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).toHaveURL(/\/$/);

  const storage = await page.evaluate(() => ({
    local: { ...window.localStorage },
    session: { ...window.sessionStorage },
  }));
  expect(JSON.stringify(storage)).not.toMatch(/access_token|refresh_token|bearer/i);

  await page.evaluate(async () => {
    await fetch("/api/session/refresh", {
      method: "POST",
      headers: { "x-csrf-token": "csrf-e2e" },
    });
  });
  expect(refreshCalls).toBe(1);

  const signOut = page.getByRole("button", { name: "退出登录" });
  if (!(await signOut.isVisible())) {
    await page.getByRole("button", { name: "Toggle Sidebar" }).click();
  }
  await signOut.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/login$/);
  expect(logoutCalls).toBe(1);
});
