<script setup lang="ts">
import { ref, watch } from "vue";
const props = defineProps<{ token: string; role: string }>();
const emit = defineEmits<{
  selection: [value: Record<string, unknown> | undefined];
}>();
const preferences = ref<any[]>([]),
  error = ref(""),
  notice = ref(""),
  busy = ref(false);
const key = ref("permission"),
  value = ref("read"),
  confirmed = ref(false);
const sessionId = ref(""),
  session = ref<any>(null),
  slots = ref("{}"),
  clear = ref("");
const useSession = ref(false),
  usePreferences = ref(false),
  useOrganization = ref(false);
const directory = ref<any>(null),
  organizationJson = ref(""),
  context = ref<any>(null);
const names: Record<string, string> = {
  department_id: "常用部门",
  email_domain: "邮箱域",
  repo_id: "代码仓库",
  project_id: "工单项目",
  permission: "仓库权限",
};
let turnAttempt: { body: string; id: string } | null = null;
watch([useSession, usePreferences, useOrganization, session], () => {
  emit(
    "selection",
    useSession.value || usePreferences.value || useOrganization.value
      ? {
          ...(useSession.value && session.value
            ? {
                session_id: session.value.id,
                session_version: session.value.version,
              }
            : {}),
          use_preferences: usePreferences.value,
          use_organization: useOrganization.value,
        }
      : undefined,
  );
});
async function api(path: string, method = "GET", body?: unknown) {
  const response = await fetch("/v1/memory" + path, {
    method,
    headers: {
      Authorization: "Bearer " + props.token,
      "Content-Type": "application/json",
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const result = await response.json();
  if (!response.ok)
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : JSON.stringify(result.detail),
    );
  return result;
}
async function run(action: () => Promise<void>) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    await action();
  } catch (e) {
    error.value = e instanceof Error ? e.message : "请求失败";
  } finally {
    busy.value = false;
  }
}
async function refresh() {
  preferences.value = (await api("/preferences")).items;
}
function save() {
  run(async () => {
    const current = preferences.value.find((p) => p.key === key.value);
    const result = await api("/preferences", "PUT", {
      key: key.value,
      value: value.value,
      confirmed: confirmed.value,
      expected_version: current?.version ?? 0,
    });
    notice.value =
      result.status === "not_saved"
        ? "请先确认这是长期偏好。"
        : "偏好已保存；本次明确输入仍然优先。";
    confirmed.value = false;
    await refresh();
  });
}
function forget(p: any) {
  run(async () => {
    await api(`/preferences/${p.key}?expected_version=${p.version}`, "DELETE");
    await refresh();
  });
}
function createSession() {
  run(async () => {
    session.value = await api("/sessions", "POST");
    sessionId.value = session.value.id;
    turnAttempt = null;
  });
}
function restore() {
  run(async () => {
    session.value = await api("/sessions/" + sessionId.value.trim());
    turnAttempt = null;
  });
}
function saveTurn() {
  run(async () => {
    const payload = {
      expected_version: session.value.version,
      request: {
        text: "请准备入职环境",
        today: new Date().toLocaleDateString("en-CA"),
        facts: JSON.parse(slots.value),
      },
      clear_fields: clear.value
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    };
    const body = JSON.stringify(payload);
    if (turnAttempt?.body !== body)
      turnAttempt = { body, id: crypto.randomUUID() };
    session.value = await api(`/sessions/${session.value.id}/turns`, "POST", {
      ...payload,
      turn_id: turnAttempt.id,
    });
    turnAttempt = null;
    slots.value = "{}";
    clear.value = "";
    notice.value = "本轮已确认的信息已保存。";
  });
}
function forgetSession() {
  run(async () => {
    await api("/sessions/" + session.value.id, "DELETE");
    session.value = null;
    sessionId.value = "";
    useSession.value = false;
    slots.value = "{}";
    turnAttempt = null;
  });
}
function loadOrg() {
  run(async () => {
    directory.value = await api("/organization");
    const { version, ...data } = directory.value;
    organizationJson.value = JSON.stringify(
      { expected_version: version, ...data },
      null,
      2,
    );
    context.value = await api("/organization/context");
  });
}
function saveOrg() {
  run(async () => {
    const result = await api(
      "/organization",
      "PUT",
      JSON.parse(organizationJson.value),
    );
    directory.value = await api("/organization");
    const { version, ...data } = directory.value;
    organizationJson.value = JSON.stringify(
      { expected_version: version, ...data },
      null,
      2,
    );
    notice.value = `组织版本 ${result.version} 已保存。`;
  });
}
</script>

<template>
  <section class="panel memory-panel">
    <h2>记忆与组织</h2>
    <p>
      本次填写优先，其次是会话、长期偏好、组织默认值。记忆不会授予权限，仍需预演和审批。
    </p>
    <p v-if="error" role="alert" class="message error">{{ error }}</p>
    <p v-if="notice" role="status" class="message">{{ notice }}</p>
    <fieldset :disabled="busy">
      <legend>新申请使用哪些记忆</legend>
      <label
        ><input v-model="usePreferences" type="checkbox" />
        使用我的长期偏好</label
      >
      <label
        ><input v-model="useOrganization" type="checkbox" />
        使用组织默认资料</label
      >
      <label
        ><input v-model="useSession" type="checkbox" :disabled="!session" />
        使用当前会话（版本 {{ session?.version ?? "未选择" }}）</label
      >
    </fieldset>
    <details>
      <summary>个人长期记忆 · 明确保存与遗忘</summary>
      <button class="quiet" :disabled="busy" @click="run(refresh)">
        读取我的偏好
      </button>
      <ul>
        <li v-for="p in preferences" :key="p.key">
          {{ names[p.key] }}：{{ p.value }} · 版本 {{ p.version }} ·
          {{ p.expired ? "已过期" : "有效" }} · 到期
          {{ new Date(p.expires_at).toLocaleDateString() }}
          <button class="quiet" :disabled="busy" @click="forget(p)">
            遗忘 {{ names[p.key] }}
          </button>
        </li>
      </ul>
      <label
        >偏好类型<select v-model="key">
          <option v-for="(label, k) in names" :key="k" :value="k">
            {{ label }}
          </option>
        </select></label
      >
      <label>偏好值<input v-model="value" maxlength="100" /></label>
      <label
        ><input v-model="confirmed" type="checkbox" /> 确认保存为长期偏好（180
        天）</label
      >
      <button :disabled="busy || !confirmed || !value.trim()" @click="save">
        保存长期偏好
      </button>
      <button
        class="quiet"
        :disabled="busy"
        @click="
          run(async () => {
            const result = await api('/purge-expired', 'POST');
            await refresh();
            notice = `已清理 ${result.preferences} 条偏好、${result.sessions} 个会话`;
          })
        "
      >
        清理我的过期记忆
      </button>
    </details>
    <details>
      <summary>会话记忆 · 中断后接着办</summary>
      <p>
        保留已确认信息 30 天；员工信息加密保存，下面只显示脱敏内容。保管会话 ID
        可在重启后恢复。
      </p>
      <button :disabled="busy" @click="createSession">创建会话</button>
      <label>会话 ID<input v-model="sessionId" /></label
      ><button
        class="quiet"
        :disabled="busy || !sessionId.trim()"
        @click="restore"
      >
        恢复会话
      </button>
      <template v-if="session">
        <p>
          当前会话：<code>{{ session.id }}</code> · 版本 {{ session.version }}
        </p>
        <pre>{{ JSON.stringify(session.facts ?? {}, null, 2) }}</pre>
        <p>{{ session.questions?.join("；") }}</p>
        <label
          >本轮确认信息（facts JSON）<textarea v-model="slots" rows="5" />
        </label>
        <label
          >清除字段（逗号分隔，如 permission）<input v-model="clear"
        /></label>
        <button :disabled="busy" @click="saveTurn">确认本轮信息</button>
        <button class="danger" :disabled="busy" @click="forgetSession">
          遗忘当前会话
        </button>
      </template>
    </details>
    <details>
      <summary>组织模型 · 部门、导师与系统归属</summary>
      <button class="quiet" :disabled="busy" @click="loadOrg">
        读取组织资料
      </button>
      <p v-if="context">
        当前部门 {{ context.department_id ?? "未关联" }} · 建议导师
        {{ context.mentor_id ?? "未设置" }} · 汇报链
        {{ context.manager_chain.join(" → ") || "未设置" }}
      </p>
      <template v-if="directory">
        <p>
          组织版本 {{ directory.version }} ·
          {{ directory.departments.length }} 个部门
        </p>
        <ul>
          <li v-for="dept in directory.departments" :key="dept.id">
            {{ dept.name }}（{{ dept.id }}）· 上级
            {{ dept.parent_id ?? "无" }} · {{ dept.active ? "启用" : "停用" }}
          </li>
        </ul>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>成员</th>
                <th>部门</th>
                <th>主管 / 导师</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="member in directory.members" :key="member.id">
                <td>
                  {{ member.id }}{{ member.is_lead ? " · 部门负责人" : ""
                  }}{{ member.active ? "" : " · 停用" }}
                </td>
                <td>{{ member.department_id }}</td>
                <td>
                  {{ member.manager_id ?? "未设置" }} /
                  {{ member.mentor_id ?? "未设置" }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>部门 / 系统</th>
                <th>资源</th>
                <th>负责人</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="system in directory.systems"
                :key="system.department_id + system.kind"
              >
                <td>{{ system.department_id }} / {{ names[system.kind] }}</td>
                <td>{{ system.resource_id }}</td>
                <td>{{ system.owner_id }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      <details v-if="role === 'admin' && directory">
        <summary>编辑组织资料</summary>
        <label
          >组织完整快照（修改后保存，包含版本）<textarea
            v-model="organizationJson"
            rows="10"
          />
        </label>
        <button :disabled="busy" @click="saveOrg">保存组织资料</button>
      </details>
    </details>
  </section>
</template>

<style scoped>
.memory-panel {
  margin-bottom: 24px;
  padding: 24px;
  min-width: 0;
}
label {
  display: block;
  margin: 12px 0;
}
pre {
  max-height: 300px;
  overflow: auto;
}
.table-wrap {
  margin: 16px 0;
}
fieldset {
  border: 1px solid #dbe3e8;
  border-radius: 8px;
  padding: 12px;
}
fieldset label {
  display: inline-flex;
  align-items: center;
  margin: 8px 20px 8px 0;
}
input[type="checkbox"] {
  width: auto;
  margin-right: 8px;
}
select {
  padding: 10px;
  border: 1px solid #cbd5df;
  border-radius: 6px;
}
pre,
code {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
button {
  margin: 6px 8px 6px 0;
}
</style>
