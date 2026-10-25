<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
type Frame = {
  span_id: string;
  parent_span_id: string;
  name: string;
  status: string;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  input: unknown;
  output: unknown;
  attributes: unknown;
};
type Trace = {
  trace_id: string;
  request_id: string | null;
  created_at: string;
  span_count: number;
};
type Detail = Trace & {
  frames: Frame[];
  truncated: boolean;
  metrics: {
    active_ms: number;
    wall_ms: number | null;
    error_count: number;
    unfinished_count: number;
    model_calls: number;
    input_tokens: number | null;
    output_tokens: number | null;
    cost_usd: number | null;
    cost_basis: string;
    partial: boolean;
  };
};
const credential = ref(""),
  token = ref(""),
  error = ref(""),
  busy = ref(false);
const items = ref<Trace[]>([]),
  detail = ref<Detail | null>(null),
  index = ref(0),
  playing = ref(false);
const selected = computed(() => detail.value?.frames[index.value]);
let timer: ReturnType<typeof setInterval> | undefined;
let generation = 0;
function pause() {
  clearInterval(timer);
  playing.value = false;
}
function reset() {
  generation++;
  pause();
  detail.value = null;
  items.value = [];
  index.value = 0;
  error.value = "";
}
function logout() {
  reset();
  token.value = "";
  credential.value = "";
  busy.value = false;
}
async function read(path: string) {
  const response = await fetch("/v1/traces" + path, {
    headers: { Authorization: "Bearer " + token.value },
    cache: "no-store",
  });
  if (!response.ok)
    throw new Error(
      response.status === 401
        ? "凭据无效，请重新连接"
        : response.status === 403
          ? "当前身份没有查看权限"
          : response.status === 404
            ? "记录不存在或不属于当前身份"
            : "记录暂时不可用，请稍后重试",
    );
  return response.json();
}
async function connect() {
  reset();
  token.value = credential.value.trim();
  credential.value = "";
  const version = generation;
  busy.value = true;
  try {
    const result = await read("");
    if (version === generation) {
      items.value = result.items;
      if (!result.enabled) error.value = "追踪采集未开启；已有记录仍可查看";
    }
  } catch (e) {
    if (version === generation) error.value = (e as Error).message;
  } finally {
    if (version === generation) busy.value = false;
  }
}
async function open(id: string) {
  pause();
  const version = ++generation;
  busy.value = true;
  error.value = "";
  detail.value = null;
  try {
    const result = await read("/" + id);
    if (version === generation) {
      detail.value = result;
      index.value = 0;
    }
  } catch (e) {
    if (version === generation) error.value = (e as Error).message;
  } finally {
    if (version === generation) busy.value = false;
  }
}
function play() {
  if (playing.value) return pause();
  if (!detail.value?.frames.length) return;
  if (index.value >= detail.value.frames.length - 1) index.value = 0;
  playing.value = true;
  timer = setInterval(() => {
    if (index.value < detail.value!.frames.length - 1) index.value++;
    else pause();
  }, 900);
}
function move(next: number) {
  pause();
  index.value = next;
}
function status(value: string) {
  return (
    (
      {
        ok: "完成",
        error: "失败 / 拒绝",
        waiting: "等待审批",
        running: "未完成 · 结果未知",
      } as Record<string, string>
    )[value] || value
  );
}
onBeforeUnmount(logout);
</script>

<template>
  <header>
    <div class="brand">◈ <span>CaseFlow</span></div>
    <span class="header-note">办事过程 · 可观测性</span
    ><a class="stage" href="/approvals">返回审批台</a>
  </header>
  <main class="trace-page">
    <div class="intro">
      <div>
        <p class="eyebrow">TRACE REPLAY / W10</p>
        <h1>把每一步，看清楚</h1>
        <p>从规划到审批、执行与补偿，按时间查看同一次办事的记录。</p>
      </div>
      <span class="shield">只读回放 · 不重复办事</span>
    </div>
    <form class="connection" @submit.prevent="connect">
      <label for="trace-token">访问凭据</label
      ><input
        id="trace-token"
        v-model="credential"
        type="password"
        autocomplete="off"
        placeholder="输入当前身份的访问凭据"
      /><button :disabled="busy || !credential.trim()">连接 / 切换身份</button
      ><button v-if="token" type="button" @click="logout">退出</button
      ><small>凭据仅保存在本页内存，退出即清除。</small>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="busy" role="status">正在读取记录…</p>
    <div v-if="token" class="trace-layout">
      <aside class="trace-card trace-list">
        <h2>最近的办事记录</h2>
        <p class="muted">最多显示 100 条可访问记录</p>
        <p v-if="!items.length && !busy">暂无记录</p>
        <button
          v-for="item in items"
          :key="item.trace_id"
          class="trace-item"
          :aria-pressed="detail?.trace_id === item.trace_id"
          @click="open(item.trace_id)"
        >
          <strong>{{
            item.request_id ? "申请 " + item.request_id.slice(0, 8) : "规划预览"
          }}</strong
          ><small
            >{{ new Date(item.created_at).toLocaleString() }} ·
            {{ item.span_count }} 步</small
          ><code>{{ item.trace_id }}</code>
        </button>
      </aside>
      <section class="trace-card trace-detail" aria-label="回放详情">
        <p v-if="!detail" class="muted">选择一条记录，查看执行经过。</p>
        <template v-else>
          <p class="eyebrow">TRACE / {{ detail.trace_id }}</p>
          <h2>执行回放</h2>
          <p class="muted">
            输入和输出仅展示安全摘要，不包含员工资料、原始提示词或工具参数。
          </p>
          <div class="trace-metrics">
            <div>
              <small>实际处理耗时</small
              ><strong>{{ detail.metrics.active_ms.toFixed(0) }} ms</strong>
            </div>
            <div>
              <small>全程跨度（含等待）</small
              ><strong
                >{{ detail.metrics.wall_ms?.toFixed(0) ?? "未知" }} ms</strong
              >
            </div>
            <div>
              <small>失败 / 拒绝记录</small
              ><strong>{{ detail.metrics.error_count }}</strong>
            </div>
            <div>
              <small>模型输入 / 输出 tokens</small
              ><strong
                >{{ detail.metrics.input_tokens ?? "未知" }} /
                {{ detail.metrics.output_tokens ?? "未知" }}</strong
              >
            </div>
            <div>
              <small>模型费用估算 · USD</small
              ><strong>{{
                detail.metrics.cost_usd === null
                  ? "未知"
                  : "$" + detail.metrics.cost_usd.toFixed(6)
              }}</strong
              ><small>{{
                detail.metrics.model_calls === 0
                  ? "本次未调用模型"
                  : "按配置单价估算，非账单"
              }}</small>
            </div>
          </div>
          <p v-if="detail.metrics.unfinished_count" class="trace-warning">
            存在
            {{
              detail.metrics.unfinished_count
            }}
            条未结束记录：可能仍在执行，也可能发生过进程中断；请结合业务结果判断。
          </p>
          <p v-if="detail.truncated" class="trace-warning">
            超过 2000 条，仅展示前 2000 条；统计为部分数据。
          </p>
          <div v-if="detail.frames.length" class="trace-controls">
            <button @click="play">{{ playing ? "暂停" : "播放" }}</button
            ><button :disabled="index === 0" @click="move(index - 1)">
              上一步</button
            ><button
              :disabled="index >= detail.frames.length - 1"
              @click="move(index + 1)"
            >
              下一步</button
            ><span>{{ index + 1 }} / {{ detail.frames.length }}</span
            ><input
              aria-label="回放进度"
              type="range"
              min="0"
              :max="detail.frames.length - 1"
              :value="index"
              @input="move(Number(($event.target as HTMLInputElement).value))"
            />
          </div>
          <div class="trace-inspector">
            <ol class="trace-timeline">
              <li v-for="(frame, i) in detail.frames" :key="frame.span_id">
                <button :class="{ current: index === i }" @click="move(i)">
                  <span>{{ i + 1 }} · {{ frame.name }}</span
                  ><small :class="'trace-' + frame.status"
                    >{{ status(frame.status) }} ·
                    {{ frame.duration_ms?.toFixed(1) ?? "?" }} ms</small
                  >
                </button>
              </li>
            </ol>
            <div v-if="selected" class="trace-frame">
              <h3>{{ selected.name }}</h3>
              <p>{{ status(selected.status) }} · {{ selected.started_at }}</p>
              <p class="muted">
                Span {{ selected.span_id }}<br />父级
                {{ selected.parent_span_id }}
              </p>
              <h4>输入摘要</h4>
              <pre>{{ JSON.stringify(selected.input, null, 2) }}</pre>
              <h4>输出摘要</h4>
              <pre>{{ JSON.stringify(selected.output, null, 2) }}</pre>
              <details>
                <summary>用量与附加属性</summary>
                <pre>{{ JSON.stringify(selected.attributes, null, 2) }}</pre>
              </details>
            </div>
          </div>
        </template>
      </section>
    </div>
  </main>
</template>

<style scoped>
.trace-layout {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 22px;
  margin-top: 24px;
}
.trace-card {
  background: white;
  border: 1px solid #dce4e8;
  border-radius: 12px;
  padding: 22px;
  min-width: 0;
}
.trace-card h2 {
  font-size: 19px;
  margin: 0 0 14px;
}
.trace-item {
  display: block;
  text-align: left;
  width: 100%;
  margin: 12px 0;
  background: #f6f9f9;
  color: #173042;
  border: 1px solid #dce4e8;
}
.trace-item[aria-pressed="true"] {
  border-color: #16816b;
  background: #ecf7f2;
}
.trace-item small,
.trace-item code {
  display: block;
  margin-top: 7px;
  font-size: 11px;
  overflow-wrap: anywhere;
}
.trace-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 12px;
  margin: 22px 0;
}
.trace-metrics div {
  background: #f3f7f6;
  border-radius: 8px;
  padding: 14px;
}
.trace-metrics small {
  display: block;
  color: #657785;
  font-size: 11px;
}
.trace-metrics strong {
  display: block;
  margin-top: 8px;
  font-size: 19px;
}
.trace-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 14px 0;
  border-top: 1px solid #dce4e8;
}
.trace-controls input {
  width: 100%;
  padding: 0;
}
.trace-inspector {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) minmax(0, 1.4fr);
  gap: 20px;
  margin-top: 14px;
}
.trace-timeline {
  list-style: none;
  padding: 0;
  margin: 0;
  max-height: 580px;
  overflow: auto;
}
.trace-timeline button {
  display: block;
  text-align: left;
  width: 100%;
  background: #fff;
  color: #173042;
  border: 1px solid #e1e7ea;
  margin-bottom: 7px;
  overflow-wrap: anywhere;
}
.trace-timeline .current {
  border-color: #16816b;
  background: #ecf7f2;
}
.trace-timeline small {
  display: block;
  margin-top: 5px;
  color: #657785;
}
.trace-timeline .trace-error {
  color: #ad4335;
}
.trace-warning {
  background: #fff5de;
  padding: 12px;
  border-radius: 6px;
}
.trace-frame {
  min-width: 0;
  overflow-wrap: anywhere;
}
.trace-frame pre {
  font-size: 12px;
  background: #f4f7f8;
  padding: 14px;
  border-radius: 8px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.eyebrow {
  overflow-wrap: anywhere;
}
.trace-page [role="alert"] {
  color: #ad4335;
}
@media (max-width: 850px) {
  .trace-layout {
    grid-template-columns: 1fr;
  }
  .trace-list {
    max-height: 300px;
    overflow: auto;
  }
  .trace-inspector {
    grid-template-columns: 1fr;
  }
  .trace-timeline {
    max-height: 240px;
  }
  .trace-card {
    padding: 16px;
  }
  .trace-page .intro {
    display: block;
  }
  .trace-page .shield {
    display: inline-block;
  }
  .header-note {
    display: none;
  }
}
</style>
