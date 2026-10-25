import { chromium, expect } from "@playwright/test";
import path from "node:path";
const browser = await chromium.launch({
  headless: true,
  channel: process.platform === "win32" ? "msedge" : undefined,
});
try {
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1100 },
  });
  const errors = [],
    writes = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("request", (r) => {
    if (r.url().includes("/v1/") && r.method() !== "GET")
      writes.push(r.method());
  });
  await page.goto(process.env.W10_BROWSER_URL + "/traces");
  await page
    .getByLabel("访问凭据", { exact: true })
    .fill(process.env.W10_BROWSER_TOKEN);
  await page.getByRole("button", { name: "连接 / 切换身份" }).click();
  await page
    .locator(".trace-item")
    .filter({ hasText: process.env.W10_TRACE_ID })
    .click();
  await expect(page.getByRole("heading", { name: "执行回放" })).toBeVisible();
  await expect(page.locator(".trace-metrics")).toContainText("100 / 20");
  await page.getByRole("button", { name: "下一步", exact: true }).click();
  await expect(page.locator(".trace-controls")).toContainText("2 /");
  await page.getByRole("button", { name: "播放", exact: true }).click();
  await page.waitForTimeout(1100);
  await page.getByRole("button", { name: "暂停", exact: true }).click();
  await expect(page.locator(".trace-controls")).not.toContainText("2 /");
  await page
    .locator(".trace-timeline button")
    .filter({ hasText: "model.call" })
    .click();
  await expect(page.locator(".trace-frame")).toContainText("0.00028");
  await page.screenshot({
    path: path.resolve("tmp/w10-trace-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  if (
    await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
  )
    throw new Error("Mobile overflow");
  await page.screenshot({
    path: path.resolve("tmp/w10-trace-mobile.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "退出", exact: true }).click();
  await expect(page.locator(".trace-layout")).toHaveCount(0);
  if (await page.evaluate(() => localStorage.length || sessionStorage.length))
    throw new Error("Persistent credentials");
  if (errors.length || writes.length)
    throw new Error(JSON.stringify({ errors, writes }));
  console.log(
    "W10 browser passed: playback, navigation, usage, mobile, logout, GET-only.",
  );
} finally {
  await browser.close();
}
