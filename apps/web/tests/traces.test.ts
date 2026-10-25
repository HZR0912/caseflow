import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, expect, it, vi } from "vitest";
import TraceApp from "../src/TraceApp.vue";
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
const button = (w: any, name: string) =>
  w.findAll("button").find((b: any) => b.text() === name)!;
const trace = {
  trace_id: "a".repeat(32),
  request_id: "req",
  created_at: "2026-10-25",
  span_count: 2,
};
const detail = {
  ...trace,
  frames: [0, 1].map((i) => ({
    span_id: String(i),
    name: "node" + i,
    status: i ? "running" : "ok",
    started_at: "2026-10-25",
    ended_at: i ? null : "2026-10-25",
    duration_ms: i ? null : 2,
    input: {},
    output: {},
    attributes: {},
  })),
  metrics: {
    active_ms: 2,
    wall_ms: 3,
    error_count: 0,
    unfinished_count: 1,
    model_calls: 1,
    input_tokens: null,
    output_tokens: null,
    cost_usd: null,
    partial: true,
  },
};
async function connect(w: any) {
  await w.find("#trace-token").setValue("secret");
  await w.find("form").trigger("submit");
  await flushPromises();
}
it("replays GET records, shows unknown cost and unfinished status, clears on logout", async () => {
  const fetch = vi.fn(async (url: string) => ({
    ok: true,
    json: async () =>
      url === "/v1/traces" ? { enabled: true, items: [trace] } : detail,
  }));
  vi.stubGlobal("fetch", fetch);
  const w = mount(TraceApp);
  await connect(w);
  await w.find(".trace-item").trigger("click");
  await flushPromises();
  expect(w.find(".trace-metrics").text()).toContain("未知");
  expect(w.text()).toContain("未结束记录");
  await button(w, "下一步").trigger("click");
  expect(w.find(".trace-frame").text()).toContain("node1");
  expect(
    fetch.mock.calls.every((call) => call[0].startsWith("/v1/traces")),
  ).toBe(true);
  await button(w, "退出").trigger("click");
  expect(w.find(".trace-layout").exists()).toBe(false);
  expect((w.find("#trace-token").element as HTMLInputElement).value).toBe("");
  w.unmount();
});
it("does not restore data when a pending response arrives after logout", async () => {
  let resolve: (v: unknown) => void = () => {};
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    ),
  );
  const w = mount(TraceApp);
  await connect(w);
  await button(w, "退出").trigger("click");
  resolve({ ok: true, json: async () => ({ items: [trace], enabled: true }) });
  await flushPromises();
  expect(w.find(".trace-layout").exists()).toBe(false);
  expect(w.text()).not.toContain(trace.trace_id);
  w.unmount();
});
it("reports authorization failure without exposing server response bodies", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: false, status: 403 })),
  );
  const w = mount(TraceApp);
  await connect(w);
  expect(w.find("[role=alert]").text()).toContain("没有查看权限");
  w.unmount();
});
