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
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(process.env.W9_BROWSER_URL + "/approvals");
  await page.locator("#credential").fill(process.env.W9_BROWSER_TOKEN);
  await page.getByRole("button", { name: "连接 / 切换身份" }).click();
  await page.getByRole("heading", { name: "记忆与组织" }).waitFor();
  await page
    .getByText("个人长期记忆 · 明确保存与遗忘", { exact: true })
    .click();
  await page.getByRole("button", { name: "读取我的偏好" }).click();
  await expect(page.locator(".memory-panel")).toContainText("仓库权限：write");
  await page.getByLabel("偏好值", { exact: true }).fill("write");
  await expect(
    page.getByRole("button", { name: "保存长期偏好" }),
  ).toBeDisabled();
  await page.getByLabel("确认保存为长期偏好（180 天）").check();
  await page.getByRole("button", { name: "保存长期偏好" }).click();
  await expect(page.locator(".memory-panel [role=status]")).toContainText(
    "偏好已保存",
  );
  await page.getByText("会话记忆 · 中断后接着办", { exact: true }).click();
  await page
    .getByLabel("会话 ID", { exact: true })
    .fill(process.env.W9_BROWSER_SESSION);
  await page.getByRole("button", { name: "恢复会话" }).click();
  await expect(page.locator(".memory-panel")).toContainText("版本 1");
  await page.getByLabel(/使用当前会话/).check();
  await page.getByLabel("使用我的长期偏好").check();
  await page.getByLabel("使用组织默认资料").check();
  await page
    .getByText("组织模型 · 部门、导师与系统归属", { exact: true })
    .click();
  await page.getByRole("button", { name: "读取组织资料" }).click();
  await expect(page.locator(".memory-panel")).toContainText(
    "建议导师 reviewer",
  );
  await page
    .locator(".request-item")
    .filter({ hasText: process.env.W9_BROWSER_REQUEST.slice(0, 8) })
    .click();
  await expect(page.locator("article .eyebrow")).toContainText(
    process.env.W9_BROWSER_REQUEST.slice(0, 8),
  );
  await page
    .getByText("本次使用的记忆来源（提交时冻结）", { exact: true })
    .click();
  await expect(page.locator(".memory-provenance")).toContainText(
    "permission ← 长期偏好",
  );
  await expect(page.locator(".memory-provenance")).toContainText("组织版本 1");
  await page.screenshot({
    path: path.resolve("tmp/w9-memory-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  if (
    await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
  )
    throw new Error("Mobile page overflows");
  await page.screenshot({
    path: path.resolve("tmp/w9-memory-mobile.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "退出", exact: true }).click();
  await expect(page.locator(".memory-panel")).toHaveCount(0);
  if (await page.evaluate(() => localStorage.length || sessionStorage.length))
    throw new Error("Credential or memory persisted in browser storage");
  if (errors.length) throw new Error(errors.join("\n"));
  console.log(
    "W9 browser passed: preferences, session restore, opt-in, organization, frozen provenance, mobile, logout.",
  );
} finally {
  await browser.close();
}
