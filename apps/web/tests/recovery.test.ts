import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";
import BackgroundPanel from "../src/BackgroundPanel.vue";
import ReconciliationPanel from "../src/ReconciliationPanel.vue";

afterEach(() => vi.useRealTimers());
const evidence = {
  id: "evidence-1",
  journal_hash: "a".repeat(64),
  observed_at: "2026-10-25T08:00:00Z",
  expires_at: "2026-10-25T08:10:00Z",
  items: [
    {
      step_id: "order",
      operation: "money.create_order",
      forward: "committed",
      compensation: "missing",
      state: "unconfirmed",
      needs_attention: true,
    },
  ],
  can_confirm_compensated: false,
  can_accept_observed_state: false,
};
const open = {
  status: "open",
  latest_evidence: evidence,
  resolution: null,
  events: [],
};
const props = {
  requestId: "request-1",
  executionStatus: "compensation_failed",
  canReconcile: true,
  canResolve: true,
};

describe("后台接续状态", () => {
  it("等待时自动刷新，完成后通知主界面，卸载后停止查询", async () => {
    vi.useFakeTimers();
    const api = vi
      .fn()
      .mockResolvedValueOnce({
        job: { status: "waiting", due_at: "2026-10-25T08:10:00Z" },
      })
      .mockResolvedValue({ job: { status: "completed" } });
    const wrapper = mount(BackgroundPanel, { props: { requestId: "r1", api } });
    await flushPromises();
    expect(wrapper.text()).toContain("关闭页面后仍会继续");
    await vi.advanceTimersByTimeAsync(3000);
    expect(wrapper.emitted("changed")).toHaveLength(1);
    expect(wrapper.text()).toContain("请以执行结果和补偿记录判断最终影响");
    wrapper.unmount();
    await vi.advanceTimersByTimeAsync(6000);
    expect(api).toHaveBeenCalledTimes(2);
  });
  it("查询失败时保留明确提示，不把失败显示成后台完成", async () => {
    const wrapper = mount(BackgroundPanel, {
      props: {
        requestId: "r1",
        api: vi.fn().mockRejectedValue(new Error("offline")),
      },
    });
    await flushPromises();
    expect(wrapper.text()).toContain("暂时无法查询后台进度");
    expect(wrapper.text()).not.toContain("后台接续已结束");
    wrapper.unmount();
  });
  it("已保存授权但响应中断时，仍会继续查询新出现的任务", async () => {
    vi.useFakeTimers();
    const api = vi
      .fn()
      .mockResolvedValueOnce({ job: null })
      .mockResolvedValue({ job: { status: "running" } });
    const wrapper = mount(BackgroundPanel, { props: { requestId: "r1", api } });
    await flushPromises();
    await vi.advanceTimersByTimeAsync(3000);
    expect(wrapper.text()).toContain("后台处理中");
    expect(wrapper.emitted("changed")).toHaveLength(1);
    wrapper.unmount();
  });
});

describe("人工对账与结案", () => {
  it("显示接口审计事件中的处理决定、时间和理由", async () => {
    const api = vi.fn().mockResolvedValue({
      ...open,
      events: [{
        id: 1,
        actor: "operator",
        event: "kept_open",
        created_at: "2026-10-25T08:05:00Z",
        payload: { decision: "keep_open", reason: "等待供应方补充收据" },
      }],
    });
    const wrapper = mount(ReconciliationPanel, { props: { ...props, api } });
    await flushPromises();
    const history = wrapper.get("details").text();
    expect(history).toContain("保留待处理并记录原因");
    expect(history).toContain("等待供应方补充收据");
    expect(history).toContain("operator");
    expect(history).toContain("2026");
    wrapper.unmount();
  });
  it("未知结果禁止两个结案选项，必须说明后才能保留待处理", async () => {
    const api = vi.fn().mockResolvedValue(open);
    const wrapper = mount(ReconciliationPanel, { props: { ...props, api } });
    await flushPromises();
    expect(wrapper.text()).toContain("结果尚无法确认");
    expect(
      wrapper
        .findAll("option")
        .slice(1)
        .every((o) => o.attributes("disabled") !== undefined),
    ).toBe(true);
    const button = wrapper
      .findAll("button")
      .find((b) => b.text() === "保留待处理并记录原因")!;
    expect(button.attributes("disabled")).toBeDefined();
    await wrapper.get("textarea").setValue("需要供应方进一步核实");
    await button.trigger("click");
    await flushPromises();
    const sent = JSON.parse(api.mock.calls.at(-1)![1].body);
    expect(sent).toEqual({
      evidence_id: "evidence-1",
      journal_hash: evidence.journal_hash,
      decision: "keep_open",
      reason: "需要供应方进一步核实",
    });
    wrapper.unmount();
  });
  it("接受当前影响必须明确选择，保留原执行状态说明", async () => {
    const api = vi
      .fn()
      .mockResolvedValueOnce({
        ...open,
        latest_evidence: { ...evidence, can_accept_observed_state: true },
      })
      .mockResolvedValue({
        ...open,
        status: "closed",
        resolution: {
          decision: "accept_observed_state",
          reason: "确认保留账号",
          actor: "operator",
          resolved_at: "2026-10-25T08:05:00Z",
        },
      });
    const wrapper = mount(ReconciliationPanel, { props: { ...props, api } });
    await flushPromises();
    expect((wrapper.get("select").element as HTMLSelectElement).value).toBe(
      "keep_open",
    );
    await wrapper.get("select").setValue("accept_observed_state");
    expect(wrapper.text()).toContain("不会撤销这些操作");
    await wrapper.get("textarea").setValue("确认保留账号");
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "接受当前影响并结案")!
      .trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("结案不代表原任务已成功完成");
    expect(wrapper.emitted("closed")).toHaveLength(1);
    expect(wrapper.find("textarea").exists()).toBe(false);
    wrapper.unmount();
  });
  it("证据过期错误不会显示结案成功", async () => {
    const api = vi
      .fn()
      .mockResolvedValueOnce(open)
      .mockRejectedValueOnce(new Error("reconciliation_evidence_expired"));
    const wrapper = mount(ReconciliationPanel, { props: { ...props, api } });
    await flushPromises();
    await wrapper.get("textarea").setValue("继续核对");
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "保留待处理并记录原因")!
      .trigger("click");
    await flushPromises();
    expect(wrapper.get("[role=alert]").text()).toContain("证据已过期");
    expect(wrapper.emitted("closed")).toBeUndefined();
    wrapper.unmount();
  });
  it("普通查看者没有核对和结案操作", async () => {
    const api = vi.fn().mockResolvedValue(open);
    const wrapper = mount(ReconciliationPanel, {
      props: { ...props, canReconcile: false, canResolve: false, api },
    });
    await flushPromises();
    expect(wrapper.findAll("button")).toHaveLength(0);
    expect(wrapper.find("textarea").exists()).toBe(false);
    expect(api).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });
});
