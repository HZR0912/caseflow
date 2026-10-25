import { chromium } from "@playwright/test";
import path from "node:path";

const browser = await chromium.launch({
  headless: true,
  channel: process.platform === "win32" ? "msedge" : undefined,
});
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  page.setDefaultTimeout(90000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const connect = async () => {
    await page.goto(process.env.W7_BROWSER_URL + "/approvals");
    await page.locator("#credential").fill(process.env.W7_BROWSER_TOKEN);
    await page.getByRole("button", { name: "连接 / 切换身份" }).click();
    await page.locator(".request-item").filter({ hasText: process.env.W7_BROWSER_REQUEST.slice(0, 8) }).click();
  };
  await connect();
  await page.getByRole("button", { name: "执行已批准计划", exact: true }).click();
  await page.locator(".decision [role=status]").filter({ hasText: "执行成功" }).waitFor();
  await page.getByText("重试与执行记录", { exact: true }).click();
  if (!(await page.locator(".decision pre").innerText()).includes("retryable"))
    throw new Error("Retry events were not displayed");
  await page.getByText("重试与执行记录", { exact: true }).click();
  await page.screenshot({ path: path.resolve("tmp/w7-execution-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth))
    throw new Error("Mobile page overflows");
  await page.screenshot({ path: path.resolve("tmp/w7-execution-mobile.png"), fullPage: true });
  await connect();
  await page.locator(".decision [role=status]").filter({ hasText: "执行成功" }).waitFor();
  if (await page.getByRole("button", { name: "执行已批准计划", exact: true }).count())
    throw new Error("Completed execution must not offer a new execution");
  if (errors.length) throw new Error(errors.join("\n"));
  console.log("W7 browser passed: execute, retries, persisted results, desktop/mobile, no JS errors.");
} finally {
  await browser.close();
}
