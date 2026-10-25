<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";

const props = defineProps<{
  requestId: string;
  executionStatus?: string;
  api: (path: string, init?: RequestInit) => Promise<any>;
}>();
const emit = defineEmits<{ changed: []; state: [value: string] }>();
type Job = {
  status: string;
  due_at: string;
  expires_at: string;
  last_error?: string;
};
const job = ref<Job | null>(null);
const error = ref("");
let timer: ReturnType<typeof setTimeout> | undefined;
let generation = 0;
let signature = "";
const labels: Record<string, string> = {
  queued: "已安排后台接续",
  running: "后台处理中",
  waiting: "等待到期后自动继续",
  completed: "后台接续已结束",
  blocked: "后台接续已暂停",
};
const reasons: Record<string, string> = {
  background_authorization_expired: "后台授权已过期，请核对任务后重新授权。",
  background_credentials_revoked: "执行凭据已失效，需要有权限的人重新授权。",
  background_identity_changed: "执行身份或权限已变化，需要重新核对。",
  approval_policy_denied: "当前权限不允许继续，请有权限的处理人核对。",
  execution_policy_unavailable: "权限服务持续不可用，自动重试已暂停，请确认恢复后重新授权。",
  approval_storage_unavailable: "数据库暂不可用，后台会按计划重试。",
};
const fmt = (value: string) => new Date(value).toLocaleString("zh-CN");

async function load(version: number) {
  try {
    const body = await props.api(`/${props.requestId}/background`);
    if (version !== generation) return;
    job.value = body.job;
    emit("state", body.job?.status ?? "");
    error.value = "";
    const next = JSON.stringify(body.job);
    if (signature && signature !== next) emit("changed");
    signature = next;
  } catch {
    if (version !== generation) return;
    error.value = "暂时无法查询后台进度，请稍后刷新。";
  }
  if (
    version === generation &&
    (error.value ||
      !job.value ||
      ["queued", "running", "waiting"].includes(job.value?.status ?? ""))
  ) {
    timer = setTimeout(() => load(version), 3000);
  }
}
watch(
  () => [props.requestId, props.executionStatus],
  () => {
    clearTimeout(timer);
    const version = ++generation;
    void load(version);
  },
  { immediate: true },
);
onUnmounted(() => {
  ++generation;
  clearTimeout(timer);
});
</script>

<template>
  <section
    v-if="job || error"
    class="background-status"
    aria-label="后台恢复状态"
  >
    <h4>后台接续</h4>
    <p v-if="error" role="status">{{ error }}</p>
    <template v-if="job">
      <p role="status">{{ labels[job.status] ?? job.status }}</p>
      <p v-if="['queued', 'running', 'waiting'].includes(job.status)">
        关闭页面后仍会继续。等待中的任务会在到期后由后台接续，无需反复点击执行。
      </p>
      <p v-if="job.status === 'waiting' && job.due_at">
        下次处理时间：{{ fmt(job.due_at) }}
      </p>
      <p v-if="job.status === 'blocked'">
        {{
          reasons[job.last_error ?? ""] ??
          "后台接续已暂停，请核对执行结果与权限后处理。"
        }}
      </p>
      <p v-if="job.status === 'completed'">
        请以执行结果和补偿记录判断最终影响。
      </p>
    </template>
  </section>
</template>
