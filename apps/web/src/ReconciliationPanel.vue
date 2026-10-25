<script setup lang="ts">
import { computed, ref, watch } from "vue";

const props = defineProps<{
  requestId: string;
  executionStatus: string;
  canReconcile: boolean;
  canResolve: boolean;
  api: (path: string, init?: RequestInit) => Promise<any>;
}>();
const emit = defineEmits<{ closed: [] }>();
type Evidence = {
  id: string;
  journal_hash: string;
  observed_at: string;
  expires_at: string;
  items: Array<{
    step_id: string;
    operation: string;
    forward: string;
    compensation: string;
    state: string;
    needs_attention: boolean;
  }>;
  can_confirm_compensated: boolean;
  can_accept_observed_state: boolean;
};
type View = {
  status: string;
  latest_evidence: Evidence | null;
  resolution: null | {
    decision: string;
    reason: string;
    actor: string;
    resolved_at: string;
  };
  events: Array<{
    id: number;
    event: string;
    payload: { decision?: string; reason?: string };
    actor: string;
    created_at: string;
  }>;
};
const view = ref<View | null>(null);
const error = ref("");
const busy = ref(false);
const reason = ref("");
const decision = ref("keep_open");
let generation = 0;
const evidence = computed(() => view.value?.latest_evidence);
const decisions: Record<string, string> = {
  confirm_compensated: "确认补偿后结案",
  accept_observed_state: "接受当前影响并结案",
  keep_open: "保留待处理并记录原因",
};
const eventLabels: Record<string, string> = {
  inspected: "已核对供应方",
  kept_open: "保留待处理",
  closed: "已人工结案",
};
const states: Record<string, string> = {
  compensated: "已确认撤销",
  retained: "仍有实际影响",
  changed: "状态已发生变化",
  unconfirmed: "结果尚无法确认",
  not_started: "尚未执行",
  not_committed: "已确认未提交",
};
const receipts: Record<string, string> = {
  committed: "有成功收据",
  missing: "未查到收据",
  invalid: "收据无效",
};
const errors: Record<string, string> = {
  reconciliation_not_required: "此任务当前不需要人工对账。",
  approval_busy_retry: "任务正在处理，请稍后再核对或结案。",
  reconciliation_execution_active: "任务仍在处理中，请等待执行结束后再核对。",
  reconciliation_already_closed: "任务已结案，请刷新查看记录。",
  reconciliation_stale_evidence: "执行进度已变化，请重新核对供应方。",
  reconciliation_evidence_expired: "核对证据已过期，请重新核对供应方。",
  reconciliation_evidence_changed: "供应方状态已变化，请重新核对后决定。",
  reconciliation_unconfirmed_outcome: "仍有无法确认的结果，暂时不能结案。",
  reconciliation_compensation_not_confirmed:
    "尚未确认所需补偿全部完成，不能按已补偿结案。",
  reconciliation_supplier_unavailable: "供应方暂不可用，无法确认实际结果。",
  reconciliation_invalid_supplier_evidence:
    "供应方返回的证据无法验证，请稍后再核对。",
};
const fmt = (value: string) => new Date(value).toLocaleString("zh-CN");
const canSubmit = computed(
  () =>
    props.canResolve &&
    !!evidence.value &&
    !!reason.value.trim() &&
    !busy.value &&
    (decision.value === "keep_open" ||
      (decision.value === "confirm_compensated" &&
        evidence.value.can_confirm_compensated) ||
      (decision.value === "accept_observed_state" &&
        evidence.value.can_accept_observed_state)),
);

async function perform(action: string, body?: object) {
  if (busy.value) return;
  const version = generation;
  busy.value = true;
  error.value = "";
  try {
    const result = await props.api(`/${props.requestId}/${action}`, {
      method: "POST",
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    if (version !== generation) return;
    view.value = result;
    if (result.status === "closed") emit("closed");
    reason.value = "";
    decision.value = "keep_open";
  } catch (e) {
    if (version !== generation) return;
    const message = e instanceof Error ? e.message : "请求失败";
    error.value = errors[message] ?? message;
  } finally {
    if (version === generation) busy.value = false;
  }
}
function resolve() {
  if (!canSubmit.value || !evidence.value) return;
  void perform("resolve", {
    evidence_id: evidence.value.id,
    journal_hash: evidence.value.journal_hash,
    decision: decision.value,
    reason: reason.value.trim(),
  });
}
watch(
  () => [props.requestId, props.executionStatus],
  async () => {
    const version = ++generation;
    view.value = null;
    error.value = "";
    busy.value = false;
    reason.value = "";
    decision.value = "keep_open";
    try {
      const result = await props.api(`/${props.requestId}/reconciliation`);
      if (version === generation) view.value = result;
    } catch (e) {
      if (version === generation)
        error.value = e instanceof Error ? e.message : "无法读取对账记录";
    }
  },
  { immediate: true },
);
</script>

<template>
  <section
    v-if="error || (view && view.status !== 'not_required')"
    class="reconciliation"
    aria-label="异常对账与结案"
  >
    <h3>异常对账与结案</h3>
    <p>
      核对供应方收据和实际状态后记录处理决定。核对与结案本身不会重新执行操作或再次退款。
    </p>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="view?.status === 'closed' && view.resolution">
      <p role="status">
        已人工结案 ·
        {{ decisions[view.resolution.decision] ?? view.resolution.decision }}
      </p>
      <p>
        {{ view.resolution.actor }} · {{ fmt(view.resolution.resolved_at) }}
      </p>
      <p>处理说明：{{ view.resolution.reason }}</p>
      <p>原执行结果保留在上方；结案不代表原任务已成功完成。</p>
    </template>
    <template v-else-if="view">
      <button
        v-if="canReconcile"
        :disabled="busy"
        @click="perform('reconcile')"
      >
        {{ busy ? "正在处理…" : "核对供应方实际结果" }}
      </button>
      <p v-else>当前身份可查看处理记录；核对和结案需要有权限的处理人。</p>
      <template v-if="evidence">
        <p>
          核对时间：{{ fmt(evidence.observed_at) }} · 证据有效至
          {{ fmt(evidence.expires_at) }}
        </p>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>步骤</th>
                <th>执行收据</th>
                <th>补偿收据</th>
                <th>当前实际影响</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in evidence.items" :key="item.step_id">
                <td>{{ item.step_id }}</td>
                <td>{{ receipts[item.forward] ?? item.forward }}</td>
                <td>{{ receipts[item.compensation] ?? item.compensation }}</td>
                <td>
                  {{ states[item.state] ?? item.state
                  }}{{ item.needs_attention ? " · 需关注" : "" }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <template v-if="canResolve">
          <label :for="'resolution-decision-' + requestId">处理决定</label>
          <select
            :id="'resolution-decision-' + requestId"
            v-model="decision"
            :disabled="busy"
          >
            <option value="keep_open">保留待处理并记录原因</option>
            <option
              value="confirm_compensated"
              :disabled="!evidence.can_confirm_compensated"
            >
              确认补偿后结案
            </option>
            <option
              value="accept_observed_state"
              :disabled="!evidence.can_accept_observed_state"
            >
              接受当前影响并结案
            </option>
          </select>
          <p v-if="decision === 'accept_observed_state'">
            此决定表示接受核对时观察到的实际状态，包括已发生的变化，不会撤销这些操作。
          </p>
          <label :for="'resolution-reason-' + requestId"
            >处理说明（必填）</label
          >
          <textarea
            :id="'resolution-reason-' + requestId"
            v-model="reason"
            maxlength="1000"
            :disabled="busy"
            placeholder="说明核对结论、保留影响或继续待处理的原因"
          />
          <button :disabled="!canSubmit" @click="resolve">
            {{ decisions[decision] }}
          </button>
        </template>
      </template>
    </template>
    <details v-if="view?.events?.length">
      <summary>核对与处理记录（{{ view.events.length }}）</summary>
      <ul>
        <li v-for="event in view.events" :key="event.id">
          {{ event.actor }} · {{ fmt(event.created_at) }} ·
          {{ decisions[event.payload.decision ?? ""] ?? eventLabels[event.event] ?? "核对记录" }}
          <span v-if="event.payload.reason"> · {{ event.payload.reason }}</span>
        </li>
      </ul>
    </details>
  </section>
</template>
