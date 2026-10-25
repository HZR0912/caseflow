import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../src/App.vue";

const row = {
  id: "request-1",
  requester: "owner",
  status: "awaiting_approval",
  plan_hash: "a".repeat(64),
  expires_at: "2099-01-01",
  expired: false,
  interrupted: true,
  trace: ["review"],
  snapshot: {
    facts: { employees: [{}] },
    plan: {
      steps: [
        {
          id: "p1_user",
          operation: "identity.create_user",
          depends_on: [],
          risk: "medium",
          rollback: "可禁用账号",
        },
      ],
      warnings: ["尚未执行"],
    },
    preview: { steps: [] },
  },
};
function setup(actor = "reviewer", failOnce = false, initial: any = row) {
  let current = structuredClone(initial);
  const decisions: any[] = [];
  const fetch = vi.fn(async (url: string, init: any) => {
    let body: any;
    if (url.endsWith("/identity"))
      body = {
        id: actor,
        role: "admin",
        tenant: "test",
        can_review: true,
        can_execute: true,
        can_reconcile: false,
        can_resolve: false,
      };
    else if (url.endsWith("/background")) body = { job: null };
    else if (url.endsWith("/reconciliation"))
      body = {
        status: "not_required",
        latest_evidence: null,
        resolution: null,
        events: [],
      };
    else if (url.endsWith("/decision")) {
      decisions.push(JSON.parse(init.body));
      if (failOnce && decisions.length === 1)
        throw new Error("connection lost");
      body = {
        ...row,
        status: "rejected",
        decision: {
          actor,
          decision: "reject",
          reason: "需更正",
          decided_at: "2026-10-24",
        },
        report: "未执行",
      };
    } else if (url.endsWith("/execute")) {
      current.execution = {
        status: "succeeded",
        steps: [
          {
            step_id: "p1_user",
            status: "succeeded",
            attempts: 3,
            events: [{ status: "retryable" }],
          },
        ],
      };
      body = current.execution;
    } else if (url.endsWith("/request-1")) body = current;
    else body = { items: [current] };
    return { ok: true, json: async () => body };
  });
  vi.stubGlobal("fetch", fetch);
  return { wrapper: mount(App), decisions, fetch };
}
async function login(wrapper: any) {
  await wrapper.get("#credential").setValue("test-token");
  await wrapper
    .findAll("button")
    .find((b: any) => b.text() === "连接 / 切换身份")
    .trigger("click");
  await flushPromises();
  await wrapper.get(".request-item").trigger("click");
  await flushPromises();
}
afterEach(() => vi.unstubAllGlobals());
describe("审批台", () => {
  it.each(["partial_success", "compensation_failed", "compensation_waiting"])(
    "展示补偿范围与逐项结果：%s",
    async (status) => {
      const { wrapper } = setup("owner", false, {
        ...row,
        status: "approved_ready",
        executable: true,
        execution: {
          status,
          steps: [
            {
              step_id: "batch",
              status: "partial_success",
              attempts: 1,
              items: [
                { id: "ticket-a", status: "assigned" },
                { id: "ticket-b", status: "failed" },
              ],
            },
          ],
          summary: {
            retained: ["p1_user"],
            compensated: ["batch"],
            not_started: [],
            unknown: [],
            compensation_unknown: ["p1_invite"],
            manual: ["p1_invite"],
          },
          saga: {
            group: "access",
            status,
            steps: [{ step_id: "batch", status: "succeeded", attempts: 2 }],
          },
        },
      });
      await login(wrapper);
      expect(wrapper.text()).toContain("已补偿：batch");
      expect(wrapper.text()).toContain("补偿结果未知：p1_invite");
      expect(wrapper.text()).toContain("需人工处理：p1_invite");
      expect(wrapper.text()).toContain("分配成功");
      expect(wrapper.text()).toContain("分配失败");
      expect(
        wrapper.findAll("button").some((b) => b.text() === "恢复执行 / 重试"),
      ).toBe(status === "compensation_waiting");
      wrapper.unmount();
    },
  );
  it("已批准计划可以执行并展示持久化结果，完成后不再显示执行按钮", async () => {
    const { wrapper, fetch } = setup("owner", false, {
      ...row,
      status: "approved_ready",
      executable: true,
    });
    await login(wrapper);
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "执行已批准计划")!
      .trigger("click");
    await flushPromises();
    expect(
      fetch.mock.calls.filter(([url]) => url.endsWith("/execute")),
    ).toHaveLength(1);
    expect(wrapper.text()).toContain("执行成功");
    expect(wrapper.text()).toContain("retryable");
    expect(
      wrapper.findAll("button").some((b) => b.text() === "执行已批准计划"),
    ).toBe(false);
    wrapper.unmount();
  });
  it("历史审批没有执行参数时提示重新提交", async () => {
    const { wrapper } = setup("owner", false, {
      ...row,
      status: "approved_ready",
      executable: false,
    });
    await login(wrapper);
    expect(wrapper.text()).toContain("请重新预演并提交");
    expect(
      wrapper.findAll("button").some((b) => b.text() === "执行已批准计划"),
    ).toBe(false);
    wrapper.unmount();
  });
  it("显示影响、依赖和回滚限制，拒绝前必须有理由", async () => {
    const { wrapper, decisions } = setup();
    await login(wrapper);
    expect(wrapper.text()).toContain("可禁用账号");
    const reject = wrapper
      .findAll("button")
      .find((b) => b.text() === "拒绝并记录理由")!;
    expect(reject.attributes("disabled")).toBeDefined();
    await wrapper.get(".decision textarea").setValue("需更正");
    await reject.trigger("click");
    await flushPromises();
    expect(decisions[0]).toMatchObject({
      decision: "reject",
      reason: "需更正",
      plan_hash: row.plan_hash,
    });
    expect(decisions[0].actor).toBeUndefined();
    wrapper.unmount();
  });
  it("申请人不能看到审批按钮", async () => {
    const { wrapper } = setup("owner");
    await login(wrapper);
    expect(wrapper.text()).toContain("需要另一位有权限的审批人");
    expect(wrapper.findAll("button").some((b) => b.text() === "批准计划")).toBe(
      false,
    );
    wrapper.unmount();
  });
  it("网络失败后重试复用同一个 operation_id", async () => {
    const { wrapper, decisions } = setup("reviewer", true);
    await login(wrapper);
    await wrapper.get(".decision textarea").setValue("需更正");
    const reject = wrapper
      .findAll("button")
      .find((b) => b.text() === "拒绝并记录理由")!;
    await reject.trigger("click");
    await flushPromises();
    expect(wrapper.get("[role=alert]").text()).toContain("connection lost");
    await reject.trigger("click");
    await flushPromises();
    expect(decisions).toHaveLength(2);
    expect(decisions[0].operation_id).toBe(decisions[1].operation_id);
    wrapper.unmount();
  });
  it("退出清除审批数据，不把凭据放入 localStorage", async () => {
    const { wrapper } = setup();
    await login(wrapper);
    expect(localStorage.length).toBe(0);
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "退出")!
      .trigger("click");
    expect(wrapper.find(".workspace").exists()).toBe(false);
    expect((wrapper.get("#credential").element as HTMLInputElement).value).toBe(
      "",
    );
    wrapper.unmount();
  });
});
