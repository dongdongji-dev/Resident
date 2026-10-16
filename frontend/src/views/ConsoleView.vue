<template>
  <div class="console">
    <header class="console-header">
      <h2>Core 控制台</h2>
      <n-tag size="small" :type="status.running ? 'success' : 'error'" round>
        {{ status.running ? '运行中' : '已停止' }}</n-tag>
      <span class="meta">{{ meta }}</span>
      <div class="actions">
        <n-button size="small" type="success" :disabled="status.running || busy" :loading="busyAction === 'start'" @click="control('start')">启动</n-button>
        <n-button size="small" type="error" :disabled="!status.running || busy" :loading="busyAction === 'stop'" @click="control('stop')">停止</n-button>
        <n-button size="small" type="info" :disabled="!status.running || busy" :loading="busyAction === 'restart'" @click="control('restart')">重启</n-button>
      </div>
    </header>

    <div class="filters">
      <n-select v-model:value="fLevel" size="small" :options="levelOptions"
        style="width: 120px" @update:value="resetLogs" />
      <n-input v-model:value="fLogger" size="small" placeholder="类别（agent_runtime.*）"
        clearable style="width: 190px" @input="resetLogs" />
      <n-input v-model:value="fQuery" size="small" placeholder="关键词…"
        clearable style="width: 160px" @input="resetLogs" />
      <n-checkbox v-model:checked="autoScroll">自动滚动</n-checkbox>
    </div>

    <div class="log-area" ref="logBox">
      <div v-for="(l, i) in lines" :key="i" class="line" :class="l.level">
        <span class="t-time">{{ l.time }}</span>
        <span class="t-thread">{{ l.thread }}</span>
        <span class="t-level" :class="'lv-' + l.level">{{ l.level }}</span>
        <span class="t-logger">{{ l.logger }}</span>
        <span class="t-msg">{{ l.message }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from "vue";
import { NButton, NTag, NSelect, NInput, NCheckbox } from "naive-ui";
import { consoleApi } from "../api";

const status = ref({ running: false, pid: null, started_at: null, uptime: null, log_file: "", core_port: 18086 });
const busy = ref(false);
const busyAction = ref("");
const fLevel = ref("INFO");
const fQuery = ref("");
const fLogger = ref("");
const autoScroll = ref(true);
const lines = ref([]);
const logBox = ref(null);
let offset = 0;

const levelOptions = [
  { label: "全部级别", value: "" },
  { label: "DEBUG", value: "DEBUG" },
  { label: "INFO", value: "INFO" },
  { label: "WARNING", value: "WARNING" },
  { label: "ERROR", value: "ERROR" },
];
const meta = computed(() => status.value.running
  ? `pid ${status.value.pid} · 启动 ${status.value.started_at} · 运行 ${status.value.uptime}s · core 端口 ${status.value.core_port}`
  : `日志 ${status.value.log_file} · core 端口 ${status.value.core_port}`);

async function pollStatus() {
  try {
    status.value = await (await consoleApi("/api/status")).json();
  } catch { status.value.running = false; }
}
async function fetchLogs() {
  const q = `after=${offset}&level=${encodeURIComponent(fLevel.value)}` +
    `&logger=${encodeURIComponent(fLogger.value)}&q=${encodeURIComponent(fQuery.value)}`;
  try {
    const data = await (await consoleApi("/api/logs?" + q)).json();
    if (data.error) return;
    offset = data.offset;
    if (!data.lines.length) return;
    const box = logBox.value;
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
    lines.value.push(...data.lines);
    if (autoScroll.value || nearBottom) {
      nextTick(() => { box.scrollTop = box.scrollHeight; });
    }
  } catch { /* console 不可达 */ }
}
function resetLogs() { offset = 0; lines.value = []; fetchLogs(); }

async function control(action) {
  busy.value = true; busyAction.value = action;
  try {
    await consoleApi("/api/control/" + action, { method: "POST" });
    setTimeout(pollStatus, 300);
    if (action !== "stop") resetLogs();
  } finally { busy.value = false; busyAction.value = ""; }
}

onMounted(() => {
  pollStatus();
  fetchLogs();
  setInterval(pollStatus, 3000);
  setInterval(fetchLogs, 1000);
});
onBeforeUnmount(() => { /* 定时器随组件销毁 */ });
</script>

<style scoped>
.console { display: flex; flex-direction: column; height: 100%; }
.console-header { display: flex; align-items: center; gap: 12px;
  padding: 10px 16px; border-bottom: 1px solid #2a2a31; flex-wrap: wrap; }
h2 { font-size: 15px; }
.meta { font-size: 12px; color: #9da3ad; }
.actions { margin-left: auto; display: flex; gap: 8px; }
.filters { display: flex; gap: 8px; padding: 8px 16px;
  border-bottom: 1px solid #2a2a31; align-items: center; flex-wrap: wrap; }
.log-area { flex: 1; overflow-y: auto; padding: 8px 14px;
  font-family: ui-monospace, Menlo, monospace; font-size: 12px; line-height: 1.6; }
.line { display: flex; gap: 10px; padding: 1px 4px; border-radius: 4px;
  white-space: pre-wrap; word-break: break-all; }
.line:hover { background: #1b1b21; }
.t-time { color: #8b8b96; flex-shrink: 0; }
.t-thread { color: #6e7681; flex-shrink: 0; }
.t-level { flex-shrink: 0; font-weight: 700; width: 66px; }
.lv-ERROR { color: #f85149; } .lv-WARNING { color: #d29922; }
.lv-INFO { color: #3fb950; } .lv-DEBUG { color: #8b8b96; }
.t-logger { color: #bc8cff; flex-shrink: 0; }
.line.ERROR .t-msg { color: #f85149; }
.line.WARNING .t-msg { color: #d29922; }
</style>
