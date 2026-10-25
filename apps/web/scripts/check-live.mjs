import { chromium } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";
const channel =
  process.env.W6_BROWSER_CHANNEL ||
  (process.platform === "win32" ? "msedge" : undefined);
const browser = await chromium.launch({ headless: true, channel });
try {
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1100 },
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.W6_BROWSER_URL + "/approvals");
  await page.locator("#credential").fill(process.env.W6_BROWSER_TOKEN);
  await page.getByRole("button", { name: "连接 / 切换身份" }).click();
  await page
    .locator(".request-item")
    .filter({ hasText: process.env.W6_BROWSER_REQUEST.slice(0, 8) })
    .click();
  await page.getByRole("button", { name: "批准计划", exact: true }).waitFor();
  if ((await page.locator("tbody tr").count()) !== 12)
    throw new Error("Expected twelve planned operations");
  if (
    !(await page.getByRole("button", { name: "拒绝并记录理由" }).isDisabled())
  )
    throw new Error("Rejection must require reason");
  await page.getByText("p1_user · ok", { exact: true }).click();
  await page.getByText("制度依据与授权记录", { exact: true }).click();
  if ((await page.locator(".detail blockquote").count()) === 0)
    throw new Error("Evidence not rendered");
  await page.getByText("制度依据与授权记录", { exact: true }).click();
  await page.getByText("p1_user · ok", { exact: true }).click();
  const out = path.resolve("tmp");
  await mkdir(out, { recursive: true });
  await page.screenshot({
    path: path.join(out, "w6-approval-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  if (
    await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    )
  )
    throw new Error("Mobile page overflows");
  await page.screenshot({
    path: path.join(out, "w6-approval-mobile.png"),
    fullPage: true,
  });
  if (errors.length) throw new Error(errors.join("\n"));
  console.log(
    "Live browser passed: authenticated console, 12 rows, evidence/diff, reason guard, desktop/mobile; no JS errors.",
  );
} finally {
  await browser.close();
}
