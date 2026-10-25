<script setup lang="ts">
import { computed, ref } from "vue";
import MemoryPanel from "./MemoryPanel.vue";
import BackgroundPanel from "./BackgroundPanel.vue";
import ReconciliationPanel from "./ReconciliationPanel.vue";
const memorySelection = ref<Record<string, unknown> | undefined>();
const backgroundState = ref("");
const memorySources: Record<string, string> = {
  explicit: "本次输入",
  session: "会话记忆",
  preference: "长期偏好",
  organization: "组织资料",
};

type Approval = {
  id: string;
  status: string;
  requester: string;
  plan_hash: string;
  expires_at: string;
  expired: boolean;
  decision?: {
    actor: string;
    decision: string;
    reason: string;
    decided_at: string;
  };
  snapshot?: any;
  trace?: string[];
  next?: string[];
  interrupted?: boolean;
  recovery_needed?: boolean;
  report?: string;
  executable?: boolean;
  execution?: {
    status: string;
    steps: any[];
    compensation_executed?: boolean;
    summary?: Record<string, string[]>;
    saga?: { status: string; group?: string; steps: any[] };
  };
};
const tokenInput = ref(""),
  token = ref("");
const identity = ref<{
  id: string;
  role: string;
  tenant: string;
  can_review: boolean;
  can_execute: boolean;
  can_reconcile: boolean;
  can_resolve: boolean;
} | null>(null);
const items = ref<Approval[]>([]),
  selected = ref<Approval | null>(null);
const error = ref(""),
  notice = ref(""),
  busy = ref(false),
  reason = ref("");
const text = ref("下周一来3个实习生，做前端的，权限给到12月底，请准备入职环境");
const today = ref("2026-10-24"),
  facts = ref("{}"),
  showForm = ref(false);
let creationAttempt: { body: string; key: string } | null = null;
let decisionAttempt: { body: string; key: string } | null = null;
const labels: Record<string, string> = {
  submitted: "待恢复",
  awaiting_approval: "等待审批",
  approved_ready: "审批通过 · 待执行",
  rejected: "已拒绝",
  clarify: "需要补充信息",
  denied: "无权申请",
  blocked: "输入已拦截",
  running: "执行中 · 可恢复",
  retry_waiting: "等待重试",
  succeeded: "执行成功",
  human_required: "需要人工处理",
  compensation_required: "业务拒绝 · 等待补偿",
  retry_exhausted: "重试次数已用尽",
  not_started: "尚未执行",
  partial_success: "部分成功 · 保留组外成果",
  compensated: "已补偿 · 原任务未完成",
  compensating: "正在补偿 · 可恢复",
  compensation_waiting: "补偿等待重试",
  compensation_failed: "补偿未完成 · 需人工处理",
  failed: "执行失败 · 无已知成功步骤",
};
const risks: Record<string, string> = { low: "低", medium: "中", high: "高" };
const displayStatus = (item: Approval) =>
  labels[item.execution?.status ?? item.status] ?? item.status;
const canDecide = computed(
  () =>
    identity.value?.can_review &&
    selected.value?.requester !== identity.value?.id &&
    selected.value?.status === "awaiting_approval" &&
    !selected.value?.expired &&
    !selected.value?.decision,
);
const steps = computed(() => selected.value?.snapshot?.plan?.steps ?? []);
const diffRows = computed(() => selected.value?.snapshot?.preview?.steps ?? []);
const stats = computed(() => ({
  pending: items.value.filter((x) => x.status === "awaiting_approval").length,
  approved: items.value.filter((x) => x.status === "approved_ready").length,
}));
const messages: Record<string, string> = {
  invalid_or_missing_credentials: "凭据无效或已失效，请重新连接。",
  self_approval_forbidden: "申请人不能审批自己的申请，请切换审批人凭据。",
  approval_expired: "审批已过期，请重新预演并提交新申请。",
  approval_already_decided: "该审批已有决定，请刷新查看。",
  approval_busy_retry: "另一个请求正在处理此审批，请稍后重试。",
  approval_policy_denied: "当前身份无此操作权限，或策略服务不可用。",
  approval_plan_changed: "计划版本不一致，请刷新后核对。",
  approval_storage_unavailable: "审批存储暂不可用，请稍后重试。",
  reconciliation_already_closed: "此任务已人工结案，不能再次执行。",
  execution_policy_unavailable: "权限服务暂不可用，已保存的后台任务会按计划重试。",
};
async function api(path: string, init: RequestInit = {}) {
  const response = await fetch("/v1/approval-requests" + path, {
    ...init,
    headers: {
      Authorization: "Bearer " + token.value,
      "Content-Type": "application/json",
      ...init.headers,
    },
  });
  const body = await response.json();
  if (!response.ok)
    throw new Error(
      messages[body.detail] ??
        (typeof body.detail === "string"
          ? body.detail
          : JSON.stringify(body.detail)),
    );
  return body;
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
  items.value = (await api("")).items;
  if (selected.value) selected.value = await api("/" + selected.value.id);
}
function connect() {
  run(async () => {
    token.value = tokenInput.value.trim();
    tokenInput.value = "";
    identity.value = null;
    memorySelection.value = undefined;
    selected.value = null;
    items.value = [];
    identity.value = await api("/identity");
    await refresh();
  });
}
function disconnect() {
  token.value = "";
  tokenInput.value = "";
  identity.value = null;
  memorySelection.value = undefined;
  selected.value = null;
  items.value = [];
  reason.value = "";
  creationAttempt = null;
  decisionAttempt = null;
}
function select(item: Approval) {
  run(async () => {
    backgroundState.value = "";
    selected.value = await api("/" + item.id);
    reason.value = "";
    decisionAttempt = null;
  });
}
function submit() {
  run(async () => {
    const body = JSON.stringify({
      text: text.value,
      today: today.value,
      facts: JSON.parse(facts.value),
      memory: memorySelection.value,
    });
    if (creationAttempt?.body !== body)
      creationAttempt = { body, key: crypto.randomUUID() };
    const result = await api("", {
      method: "POST",
      body,
      headers: { "Idempotency-Key": creationAttempt!.key },
    });
    if (!result.id) {
      notice.value =
        (labels[result.status] ?? result.status) +
        "：" +
        (result.snapshot.questions?.join("；") ??
          result.snapshot.reason ??
          "请检查请求");
      return;
    }
    creationAttempt = null;
    selected.value = result;
    showForm.value = false;
    await refresh();
  });
}
function decide(decision: "approve" | "reject") {
  run(async () => {
    if (!selected.value || !canDecide.value) return;
    if (decision === "reject" && !reason.value.trim())
      throw new Error("拒绝时请填写理由。");
    const base = {
      decision,
      reason: reason.value.trim(),
      plan_hash: selected.value.plan_hash,
    };
    const body = JSON.stringify(base);
    if (decisionAttempt?.body !== body)
      decisionAttempt = { body, key: crypto.randomUUID() };
    selected.value = await api("/" + selected.value.id + "/decision", {
      method: "POST",
      body: JSON.stringify({ ...base, operation_id: decisionAttempt!.key }),
    });
    decisionAttempt = null;
    await refresh();
    notice.value = selected.value?.report ?? "决定已保存";
  });
}
function recover() {
  run(async () => {
    if (selected.value) {
      selected.value = await api("/" + selected.value.id + "/recover", {
        method: "POST",
      });
      await refresh();
    }
  });
}
function execute() {
  run(async () => {
    if (!selected.value) return;
    const result = await api("/" + selected.value.id + "/execute", {
      method: "POST",
    });
    await refresh();
    notice.value = labels[result.status] ?? result.status;
  });
}
function refreshProgress() {
  if (!busy.value && identity.value) void run(refresh);
}
const fmt = (value: string) => new Date(value).toLocaleString("zh-CN");
</script>

<template>
  <header>
    <div class="brand">C<span>CaseFlow</span></div>
    <span class="header-note">企业办事 · 人工审批</span
    ><a class="stage" href="/traces">Trace 回放 · W10</a>
  </header>
  <main>
    <section class="intro">
      <div>
        <p class="eyebrow">HUMAN IN THE LOOP</p>
        <h1>先看清影响，再做决定。</h1>
        <p>核对计划、预演结果与回滚限制。审批通过后进入待执行状态。</p>
      </div>
      <span class="shield">审批通过后，可单独发起正式执行</span>
    </section>
    <section class="connection">
      <label for="credential">访问凭据</label
      ><input
        id="credential"
        v-model="tokenInput"
        type="password"
        autocomplete="off"
        placeholder="粘贴申请人或审批人的 Bearer token"
        @keyup.enter="connect"
      /><button :disabled="busy || !tokenInput.trim()" @click="connect">
        连接 / 切换身份</button
      ><button
        v-if="identity"
        class="quiet"
        :disabled="busy"
        @click="disconnect"
      >
        退出</button
      ><small v-if="identity"
        >{{ identity.id }} · {{ identity.role }} · {{ identity.tenant }}</small
      ><small v-else>凭据仅保留在本页内存中，刷新后需重新输入。</small>
    </section>
    <p v-if="error" class="message error" role="alert">{{ error }}</p>
    <p v-if="notice" class="message" role="status">{{ notice }}</p>
    <template v-if="identity">
      <MemoryPanel
        :key="token"
        :token="token"
        :role="identity.role"
        @selection="memorySelection = $event"
      />
      <section class="metrics">
        <div>
          <strong>{{ items.length }}</strong
          ><span>最近申请 · 最多 100 条</span>
        </div>
        <div>
          <strong>{{ stats.pending }}</strong
          ><span>等待审批</span>
        </div>
        <div>
          <strong>{{ stats.approved }}</strong
          ><span>已批准</span>
        </div>
      </section>
      <div class="toolbar">
        <h2>审批工作台</h2>
        <button class="quiet" :disabled="busy" @click="run(refresh)">
          刷新</button
        ><button :disabled="busy" @click="showForm = !showForm">
          新建申请
        </button>
      </div>
      <section v-if="showForm" class="panel new-request">
        <h3>预演并提交审批</h3>
        <label>需求<textarea v-model="text" rows="3"></textarea></label
        ><label>参照日期<input v-model="today" type="date" /></label
        ><label
          >已确认信息（facts JSON，含员工名单、邮箱域、仓库与项目 ID）<textarea
            v-model="facts"
            rows="8"
            spellcheck="false"
            placeholder='{"employees": [...], "repo_id": "..."}'
          ></textarea></label
        ><button :disabled="busy" @click="submit">
          生成计划 · 预演 · 提交
        </button>
      </section>
      <div class="workspace">
        <aside class="panel">
          <p class="list-title">申请列表</p>
          <p v-if="!items.length" class="empty">
            还没有审批申请。可新建申请，或运行演示准备数据。
          </p>
          <button
            v-for="item in items"
            :key="item.id"
            class="request-item"
            :class="{ active: selected?.id === item.id }"
            :disabled="busy"
            @click="select(item)"
          >
            <span class="badge" :class="item.status">{{
              displayStatus(item)
            }}</span
            ><strong>{{ item.requester }} 的申请</strong
            ><code>{{ item.id.slice(0, 8) }}</code
            ><small>{{
              item.expired && !item.decision
                ? "已过期"
                : "截止 " + fmt(item.expires_at)
            }}</small>
          </button>
        </aside>
        <article class="panel detail">
          <div v-if="!selected" class="empty large">
            选择一份申请，查看完整影响范围与预演结果。
          </div>
          <template v-else>
            <div class="detail-heading">
              <div>
                <p class="eyebrow">REQUEST {{ selected.id.slice(0, 8) }}</p>
                <h2>入职准备审批</h2>
              </div>
              <span class="badge" :class="selected.status">{{
                displayStatus(selected)
              }}</span>
            </div>
            <p class="muted">
              申请人 {{ selected.requester }} · 截止
              {{ fmt(selected.expires_at) }}<br />审批快照版本
              <code>{{ selected.plan_hash.slice(0, 16) }}</code>
            </p>
            <p class="flow">
              {{ selected.trace?.join(" → ")
              }}<span v-if="selected.interrupted"> · 已保存，等待人工决定</span>
            </p>
            <div class="impact">
              <strong>{{ steps.length }}</strong> 个操作 <span>·</span>
              {{ selected.snapshot?.facts?.employees?.length ?? 0 }} 位员工
              <span>·</span>
              {{ steps.filter((s: any) => s.risk === "high").length }} 项高风险
            </div>
            <ul class="warnings">
              <li v-for="w in selected.snapshot?.plan?.warnings" :key="w">
                {{ w }}
              </li>
            </ul>
            <details
              v-if="selected.snapshot?.memory_context"
              class="memory-provenance"
            >
              <summary>本次使用的记忆来源（提交时冻结）</summary>
              <p>{{ selected.snapshot.memory_context.notice }}</p>
              <ul>
                <li
                  v-for="source in selected.snapshot.memory_context.sources"
                  :key="source.field"
                >
                  {{ source.field }} ← {{ memorySources[source.source] }}
                </li>
              </ul>
              <p v-if="selected.snapshot.memory_context.organization">
                组织版本
                {{ selected.snapshot.memory_context.organization.version }} ·
                建议导师
                {{
                  selected.snapshot.memory_context.organization.mentor_id ??
                  "未设置"
                }}
              </p>
            </details>
            <h3>计划与回滚限制</h3>
            <div class="table-wrap">
              <table class="plan-table">
                <thead>
                  <tr>
                    <th>步骤 / 操作</th>
                    <th>依赖</th>
                    <th>风险</th>
                    <th>回滚限制</th>
                    <th>补偿分组</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="step in steps" :key="step.id">
                    <td>
                      <strong>{{ step.id }}</strong
                      ><small>{{ step.operation }}</small>
                    </td>
                    <td>{{ step.depends_on.join(", ") || "起点" }}</td>
                    <td>
                      <span class="risk" :class="step.risk">{{
                        risks[step.risk]
                      }}</span>
                    </td>
                    <td>{{ step.rollback }}</td>
                    <td>{{ step.saga_group || "未启用自动补偿" }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <h3>预演影响</h3>
            <details v-for="row in diffRows" :key="row.step_id">
              <summary>{{ row.step_id }} · {{ row.status }}</summary>
              <pre>{{ JSON.stringify(row.diff ?? row, null, 2) }}</pre>
            </details>
            <details>
              <summary>制度依据与授权记录</summary>
              <div v-for="e in selected.snapshot?.evidence" :key="e.chunk_id">
                <h4>{{ e.citation }}</h4>
                <blockquote>{{ e.quote }}</blockquote>
              </div>
              <pre>{{
                JSON.stringify(selected.snapshot?.decisions, null, 2)
              }}</pre>
            </details>
            <section class="decision">
              <h3>审批决定</h3>
              <template v-if="selected.decision"
                ><p>
                  <strong>{{ selected.decision.actor }}</strong> 于
                  {{ fmt(selected.decision.decided_at) }}
                  {{
                    selected.decision.decision === "approve" ? "批准" : "拒绝"
                  }}
                </p>
                <p>{{ selected.decision.reason || "未填写附加说明" }}</p>
                <p>{{ selected.report }}</p></template
              ><template v-else-if="canDecide"
                ><label
                  >审批说明（拒绝时必填）<textarea
                    v-model="reason"
                    maxlength="1000"
                    rows="3"
                    placeholder="请说明审核结论，不要填写员工敏感信息。"
                  ></textarea>
                </label>
                <div class="actions">
                  <button :disabled="busy" @click="decide('approve')">
                    批准计划</button
                  ><button
                    class="danger"
                    :disabled="busy || !reason.trim()"
                    @click="decide('reject')"
                  >
                    拒绝并记录理由
                  </button>
                </div></template
              >
              <p v-else class="muted">
                {{
                  selected.expired
                    ? "申请已过期，请重新预演并提交。"
                    : selected.requester === identity.id
                      ? "你是申请人，需要另一位有权限的审批人作出决定。"
                      : "当前身份无审批权限。"
                }}
              </p>
              <button
                v-if="selected.recovery_needed"
                class="quiet"
                :disabled="busy"
                @click="recover"
              >
                从已保存进度恢复
              </button>
            </section>
            <section
              v-if="selected.status === 'approved_ready'"
              class="decision"
            >
              <h3>正式执行</h3>
              <p>
                执行会修改业务数据。失败后按已批准的补偿分组撤销本组已完成操作，组外工作保留。结果未知或数据已被修改时转人工核对。
              </p>
              <p v-if="!selected.executable">
                此历史审批没有可恢复的执行参数，请重新预演并提交。
              </p>
              <BackgroundPanel
                :key="'background-' + selected.id"
                :request-id="selected.id"
                :execution-status="selected.execution?.status"
                :api="api"
                @changed="refreshProgress"
                @state="backgroundState = $event"
              />
              <button
                v-if="
                  selected.executable &&
                  identity.can_execute &&
                  !['queued', 'running', 'waiting', 'completed'].includes(
                    backgroundState,
                  ) &&
                  (!selected.execution ||
                    [
                      'running',
                      'retry_waiting',
                      'compensating',
                      'compensation_waiting',
                    ].includes(selected.execution.status))
                "
                :disabled="busy"
                @click="execute"
              >
                {{
                  backgroundState === "blocked"
                    ? "重新授权并继续"
                    : selected.execution
                      ? "恢复执行 / 重试"
                      : "执行已批准计划"
                }}
              </button>
              <template v-if="selected.execution">
                <p role="status">
                  {{
                    labels[selected.execution.status] ??
                    selected.execution.status
                  }}
                </p>
                <div class="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>步骤</th>
                        <th>结果</th>
                        <th>调用次数</th>
                        <th>失败原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr
                        v-for="step in selected.execution.steps"
                        :key="step.step_id"
                      >
                        <td>{{ step.step_id }}</td>
                        <td>
                          {{
                            step.status === "partial_success"
                              ? "批次部分成功"
                              : (labels[step.status] ?? step.status)
                          }}
                        </td>
                        <td>{{ step.attempts }}</td>
                        <td>{{ step.code ?? "—" }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <details>
                  <summary>重试与执行记录</summary>
                  <pre>{{ JSON.stringify(selected.execution, null, 2) }}</pre>
                </details>
                <template v-if="selected.execution.summary">
                  <h3>最终影响范围</h3>
                  <p>
                    原始执行未全部成功：{{
                      selected.execution.summary.failed?.join("、") || "无"
                    }}
                  </p>
                  <p>
                    尚未确认撤销的成功步骤：{{
                      selected.execution.summary.retained.join("、") || "无"
                    }}
                  </p>
                  <p>
                    已补偿：{{
                      selected.execution.summary.compensated.join("、") || "无"
                    }}
                  </p>
                  <p>
                    未执行：{{
                      selected.execution.summary.not_started.join("、") || "无"
                    }}
                  </p>
                  <p>
                    结果未知：{{
                      selected.execution.summary.unknown.join("、") || "无"
                    }}
                  </p>
                  <p>
                    补偿结果未知：{{
                      selected.execution.summary.compensation_unknown?.join(
                        "、",
                      ) || "无"
                    }}
                  </p>
                  <p>
                    需人工处理：{{
                      selected.execution.summary.manual.join("、") || "无"
                    }}
                  </p>
                </template>
                <template
                  v-for="step in selected.execution.steps"
                  :key="'items-' + step.step_id"
                >
                  <div v-if="step.items" class="table-wrap">
                    <h3>批次逐项结果 · {{ step.step_id }}</h3>
                    <p>这里展示原始执行结果，补偿后的状态见“最终影响范围”。</p>
                    <table>
                      <thead>
                        <tr>
                          <th>工单 ID</th>
                          <th>原始结果</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr v-for="item in step.items" :key="item.id">
                          <td>{{ item.id }}</td>
                          <td>
                            {{
                              item.status === "assigned"
                                ? "分配成功"
                                : "分配失败"
                            }}
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </template>
                <template v-if="selected.execution.saga">
                  <h3>补偿记录</h3>
                  <p>
                    分组：{{ selected.execution.saga.group || "无" }} ·
                    {{ selected.execution.saga.status }}
                  </p>
                  <div class="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>原步骤</th>
                          <th>补偿结果</th>
                          <th>次数</th>
                          <th>原因</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr
                          v-for="undo in selected.execution.saga.steps"
                          :key="undo.step_id"
                        >
                          <td>{{ undo.step_id }}</td>
                          <td>
                            {{
                              undo.status === "succeeded"
                                ? "已补偿"
                                : undo.status
                            }}
                          </td>
                          <td>{{ undo.attempts }}</td>
                          <td>{{ undo.code || "—" }}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                  <p
                    v-if="
                      selected.execution.status === 'compensation_failed' ||
                      selected.execution.saga.status ===
                        'blocked_unknown_outcome'
                    "
                  >
                    请核对供应方操作记录和实际资源状态。不要创建新申请重复执行；自动重试预算不会因刷新而重置。
                  </p>
                </template>
              </template>
              <ReconciliationPanel
                v-if="selected.execution"
                :key="'reconciliation-' + selected.id"
                :request-id="selected.id"
                :execution-status="selected.execution.status"
                :can-reconcile="identity.can_reconcile"
                :can-resolve="identity.can_resolve"
                :api="api"
                @closed="refreshProgress"
              />
            </section>
          </template>
        </article>
      </div>
    </template>
    <footer>
      审批记录与进度保存在服务端。关闭页面或服务重启后，可重新连接查看。
    </footer>
  </main>
</template>
