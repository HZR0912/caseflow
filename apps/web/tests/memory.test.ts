import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, expect, it, vi } from "vitest";
import MemoryPanel from "../src/MemoryPanel.vue";

afterEach(() => vi.unstubAllGlobals());
const button = (wrapper: any, name: string) =>
  wrapper.findAll("button").find((b: any) => b.text() === name)!;
function setup(handler: (url: string, init: any) => unknown, role = "admin") {
  const fetch = vi.fn(async (url: string, init: any) => ({
    ok: true,
    json: async () => handler(url, init),
  }));
  vi.stubGlobal("fetch", fetch);
  return {
    wrapper: mount(MemoryPanel, { props: { token: "secret", role } }),
    fetch,
  };
}
it("requires explicit confirmation before a long-term write and sends current version", async () => {
  const writes: any[] = [];
  const { wrapper, fetch } = setup((url, init) => {
    if (init.method === "PUT") {
      writes.push(JSON.parse(init.body));
      return { status: "saved" };
    }
    return {
      items: [
        {
          key: "permission",
          value: "read",
          version: 7,
          expires_at: "2099-01-01",
        },
      ],
    };
  });
  expect(fetch).not.toHaveBeenCalled();
  expect(button(wrapper, "保存长期偏好").attributes("disabled")).toBeDefined();
  await button(wrapper, "读取我的偏好").trigger("click");
  await flushPromises();
  await wrapper.findAll('input[type="checkbox"]')[3].setValue(true);
  await button(wrapper, "保存长期偏好").trigger("click");
  await flushPromises();
  expect(writes[0]).toMatchObject({
    confirmed: true,
    expected_version: 7,
    key: "permission",
  });
  expect(fetch.mock.calls[0][1].headers.Authorization).toBe("Bearer secret");
});
it("emits explicit memory selection, pinned to restored session version", async () => {
  const { wrapper } = setup(() => ({ id: "sid", version: 4, facts: {} }));
  await button(wrapper, "创建会话").trigger("click");
  await flushPromises();
  await wrapper.findAll('input[type="checkbox"]')[2].setValue(true);
  expect(wrapper.emitted("selection")?.at(-1)?.[0]).toEqual({
    session_id: "sid",
    session_version: 4,
    use_preferences: false,
    use_organization: false,
  });
  await button(wrapper, "遗忘当前会话").trigger("click");
  await flushPromises();
  expect(wrapper.emitted("selection")?.at(-1)?.[0]).toBeUndefined();
});
it("retries an uncertain turn with the same id and expected version", async () => {
  const turns: any[] = [];
  const { wrapper } = setup((url, init) => {
    if (url.endsWith("/turns")) {
      turns.push(JSON.parse(init.body));
      if (turns.length === 1) throw new Error("response lost");
      return { id: "sid", version: 1, facts: {} };
    }
    return { id: "sid", version: 0, facts: {} };
  });
  await button(wrapper, "创建会话").trigger("click");
  await flushPromises();
  await button(wrapper, "确认本轮信息").trigger("click");
  await flushPromises();
  expect(wrapper.get('[role="alert"]').text()).toContain("response lost");
  await button(wrapper, "确认本轮信息").trigger("click");
  await flushPromises();
  expect(turns[1]).toEqual(turns[0]);
});
it("does not offer organization editing to employees", async () => {
  const { wrapper } = setup(
    (url) =>
      url.endsWith("/context")
        ? { manager_chain: [] }
        : { version: 1, departments: [], members: [], systems: [] },
    "employee",
  );
  await button(wrapper, "读取组织资料").trigger("click");
  await flushPromises();
  expect(wrapper.text()).not.toContain("保存组织资料");
});
