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
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(process.env.W8_BROWSER_URL + "/approvals");
  await page.locator("#credential").fill(process.env.W8_BROWSER_TOKEN);
  await page.getByRole("button", { name: "连接 / 切换身份" }).click();
  const select = async (id) => {
    await page
      .locator(".request-item")
      .filter({ hasText: id.slice(0, 8) })
      .click();
    await expect(page.locator("article .eyebrow")).toContainText(
      id.slice(0, 8),
    );
    await page.getByRole("heading", { name: "最终影响范围" }).waitFor();
  };
  await select(process.env.W8_BROWSER_FUNDS);
  if (
    !(await page.locator("article").innerText())
      .replace(/\s+/g, "")
      .includes("已补偿：p1_invite")
  )
    throw new Error("Precise membership compensation missing");
  if (!(await page.locator("article").innerText()).includes("p1_identity"))
    throw new Error("Approved compensation groups missing");
  await page.screenshot({
    path: path.resolve("tmp/w8-scope-desktop.png"),
    fullPage: true,
  });
  await select(process.env.W8_BROWSER_BATCH);
  await page
    .getByRole("heading", { name: "批次逐项结果 · assign_tickets" })
    .waitFor();
  if (
    (await page
      .getByRole("cell", { name: "分配成功", exact: true })
      .count()) !== 2 ||
    (await page
      .getByRole("cell", { name: "分配失败", exact: true })
      .count()) !== 2
  )
    throw new Error("Batch item counts incorrect");
  await page.screenshot({
    path: path.resolve("tmp/w8-batch-desktop.png"),
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
    path: path.resolve("tmp/w8-batch-mobile.png"),
    fullPage: true,
  });
  await select(process.env.W8_BROWSER_MANUAL);
  if (
    !(await page.locator("article").innerText())
      .replace(/\s+/g, "")
      .includes("补偿结果未知：p1_invite")
  )
    throw new Error("Unknown undo outcome not visible");
  if (
    await page
      .getByRole("button", { name: /执行已批准计划|恢复执行 \/ 重试/ })
      .count()
  )
    throw new Error("Terminal manual state must not offer automatic replay");
  await page.screenshot({
    path: path.resolve("tmp/w8-manual-mobile.png"),
    fullPage: true,
  });
  await page.reload();
  await page.locator("#credential").fill(process.env.W8_BROWSER_TOKEN);
  await page.getByRole("button", { name: "连接 / 切换身份" }).click();
  await select(process.env.W8_BROWSER_MANUAL);
  if (
    !(await page.locator("article").innerText())
      .replace(/\s+/g, "")
      .includes("需人工处理：p1_invite")
  )
    throw new Error("Manual result did not survive refresh");
  if (errors.length) throw new Error(errors.join("\n"));
  console.log(
    "W8 browser passed: precise scope, item results, manual/unknown state, persistence, desktop/mobile.",
  );
} finally {
  await browser.close();
}
