<template>
  <div class="config">
    <div class="header">
      <div class="title">配置</div>
      <div class="actions">
        <n-button size="small" :loading="loadingFiles" @click="loadFiles">
          刷新列表
        </n-button>
        <n-button v-if="restartNeeded" size="small" type="warning"
                  @click="restartKernel">
          重启 Kernel 生效
        </n-button>
        <n-button v-if="isYaml && dirtyCount" size="small" @click="discard">
          丢弃修改
        </n-button>
        <n-button size="small" type="primary" :disabled="!current"
                  :loading="saving" @click="save">
          {{ isYaml ? (dirtyCount ? `保存（${dirtyCount} 项）` : "保存") : "保存" }}
        </n-button>
      </div>
    </div>
    <div class="hint">
      YAML 按「分区 → 选项」表单编辑（只改值、注释与其余内容不动，保存前
      自动备份到 data/config_backups/）；md/txt 为文本编辑。
      <template v-if="currentHint">当前文件：{{ currentHint }}</template>
    </div>
    <div class="body">
      <aside class="filelist">
        <n-input v-model:value="filter" size="small" placeholder="过滤文件名…"
                 clearable />
        <div class="files">
          <div v-for="f in shownFiles" :key="f.path" class="file"
               :class="{ active: f.path === current }" @click="open(f.path)">
            <div class="fpath">{{ f.path }}</div>
            <div class="fmeta">{{ human(f.size) }} · {{ f.mtime }}</div>
          </div>
          <div v-if="!shownFiles.length" class="empty">（无可编辑文件）</div>
        </div>
      </aside>

      <!-- YAML：结构化选项表单 -->
      <section v-if="isYaml" class="form">
        <div class="form-head">
          <span class="epath">{{ current }}</span>
          <n-input v-model:value="search" size="small" style="width: 220px"
                   placeholder="搜索选项…" clearable />
        </div>
        <div class="form-body">
          <div v-for="group in shownGroups" :key="group.path" class="card">
            <div class="card-title">
              <span class="gkey">{{ group.key }}</span>
              <span v-if="group.comment" class="gcomment">{{ short(group.comment) }}</span>
            </div>
            <GroupItems :group="group" :dirty="dirty" :search="search"
                        :get-value="displayValue" :set-value="setValue" />
          </div>
          <div v-if="!shownGroups.length" class="empty">（无匹配选项）</div>
        </div>
      </section>

      <!-- 其他：原始文本编辑 -->
      <section v-else class="editor">
        <div class="editor-head">
          <span class="epath">{{ current || "未选择文件" }}</span>
        </div>
        <textarea v-model="content" class="textarea" spellcheck="false"
                  :disabled="!current" @keydown.tab.prevent="insertTab" />
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, h, onMounted, reactive, ref } from "vue";
import {
  NButton, NInput, NInputNumber, NSelect, NSwitch, NTag, useMessage,
} from "naive-ui";
import { consoleApi } from "../api";

const message = useMessage();
const files = ref([]);
const filter = ref("");
const search = ref("");
const current = ref("");
const content = ref("");
const schema = ref(null);
const originals = reactive({});
const dirty = reactive({});
const loadingFiles = ref(false);
const saving = ref(false);
const restartNeeded = ref(false);

const isYaml = computed(() => /\.(ya?ml)$/i.test(current.value));
const dirtyCount = computed(() => Object.keys(dirty).length);

const shownFiles = computed(() => {
  const key = filter.value.trim().toLowerCase();
  if (!key) return files.value;
  return files.value.filter((f) => f.path.toLowerCase().includes(key));
});

const currentHint = computed(() => {
  const path = current.value;
  if (path === "agent.yaml") return "保存后需重启 Kernel 生效（右上角按钮）";
  if (path === "console.yaml") {
    return "保存后需重启 console 生效（scripts/stop.sh && scripts/start.sh）";
  }
  if (path.startsWith("prompt/") || path.startsWith("schedule/")) {
    return "下一轮/下次唤醒即生效";
  }
  return "";
});

function human(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return value + " B";
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
  return (value / 1024 / 1024).toFixed(1) + " MB";
}

// 统一的响应解析：非 JSON（如代理 404 纯文本）给出可读错误，
// 避免“Unexpected non-whitespace character after JSON”这类难懂报错
async function readJson(resp) {
  const text = await resp.text();
  try {
    return JSON.parse(text);
  } catch {
    const snippet = text.trim().slice(0, 200);
    throw new Error(
      `HTTP ${resp.status} 响应不是 JSON：${snippet || "（空响应）"}`);
  }
}

function short(text) {
  return String(text).replace(/\n/g, " · ").slice(0, 90);
}

function displayValue(path) {
  return path in dirty ? dirty[path] : originals[path];
}

function setValue(path, value) {
  dirty[path] = value;
}

function matches(group, query) {
  if (!query) return true;
  const key = query.toLowerCase();
  const textOf = (node) => [
    node.key, node.path, node.comment || "",
    ...(node.items || []).flatMap((i) => [i.key, i.path, i.comment || ""]),
  ].join(" ").toLowerCase();
  return textOf(group).includes(key);
}

const shownGroups = computed(() => {
  const groups = schema.value?.groups || [];
  const key = search.value.trim().toLowerCase();
  if (!key) return groups;
  return groups.filter((g) => matches(g, key));
});

// ── 选项控件（按类型渲染） ─────────────────────────────────────────────
const GroupItems = {
  name: "GroupItems",
  props: ["group", "dirty", "search", "getValue", "setValue"],
  setup(props) {
    const row = (item) => {
      const query = props.search.trim().toLowerCase();
      if (query) {
        const text = [item.key, item.path, item.comment || ""].join(" ").toLowerCase();
        if (!text.includes(query)) return null;
      }
      const changed = item.path in props.dirty;
      const value = props.getValue(item.path);
      let control;
      if (!item.editable) {
        control = h("div", { class: "ro" }, [
          h("code", null, JSON.stringify(item.value)),
          h("span", { class: "ro-hint" }, "手工编辑"),
        ]);
      } else if (item.type === "bool") {
        control = h(NSwitch, {
          value: Boolean(value),
          "onUpdate:value": (v) => props.setValue(item.path, v),
        });
      } else if (item.type === "int" || item.type === "float") {
        const step = item.type === "int" ? 1 : 0.001;
        control = h(NInputNumber, {
          value: typeof value === "number" ? value : null,
          step,
          style: "width: 200px",
          "onUpdate:value": (v) => props.setValue(item.path, v),
        });
      } else if (item.type === "str_list") {
        control = h(NSelect, {
          value: Array.isArray(value) ? value : [],
          multiple: true,
          filterable: true,
          tag: true,
          size: "small",
          placeholder: "回车添加…",
          style: "min-width: 320px",
          "onUpdate:value": (v) => props.setValue(item.path, v),
        });
      } else if (item.type === "dict" || item.type === "list") {
        control = h(NInput, {
          value: typeof value === "string" ? value : JSON.stringify(value),
          type: "textarea",
          autosize: { minRows: 2, maxRows: 6 },
          placeholder: "YAML 文本",
          "onUpdate:value": (v) => props.setValue(item.path, v),
        });
      } else {
        const multiline = typeof value === "string" && value.includes("\n");
        control = h(NInput, {
          value: value == null ? "" : String(value),
          type: multiline ? "textarea" : "text",
          autosize: multiline ? { minRows: 2, maxRows: 6 } : undefined,
          "onUpdate:value": (v) => props.setValue(item.path, v),
        });
      }
      return h("div", { class: ["row", changed && "changed"], key: item.path }, [
        h("div", { class: "label" }, [
          h("span", { class: "ikey" }, item.key),
          changed ? h(NTag, { size: "tiny", type: "warning", bordered: false },
                      { default: () => "已改" }) : null,
          item.comment
            ? h("div", { class: "icomment" }, item.comment)
            : null,
        ]),
        h("div", { class: "control" }, control),
      ]);
    };
    return () => h("div", { class: "items" }, [
      ...(props.group.items || []).map(row).filter(Boolean),
      ...(props.group.children || []).map((child) =>
        h("div", { class: "subgroup", key: child.path }, [
          h("div", { class: "sub-title" }, [
            h("span", { class: "gkey" }, child.key),
            child.comment
              ? h("span", { class: "gcomment" }, child.comment.split("\n")[0].slice(0, 80))
              : null,
          ]),
          h(GroupItems, {
            group: child, dirty: props.dirty, search: props.search,
            getValue: props.getValue, setValue: props.setValue,
          }),
        ])),
    ]);
  },
};

// ── 载入/保存 ──────────────────────────────────────────────────────────
async function loadFiles() {
  loadingFiles.value = true;
  try {
    const resp = await consoleApi("/api/config/files");
    const data = await readJson(resp);
    if (!resp.ok || !data.ok) {
      throw new Error(data.error || `加载失败 (${resp.status})`);
    }
    files.value = data.files || [];
  } catch (e) {
    message.error(String(e.message || e));
  } finally {
    loadingFiles.value = false;
  }
}

async function loadYaml(path) {
  const resp = await consoleApi(
    "/api/config/schema?path=" + encodeURIComponent(path));
  const data = await readJson(resp);
  if (!resp.ok || !data.ok) {
    throw new Error(data.error || `读取失败 (${resp.status})`);
  }
  schema.value = data;
  for (const key of Object.keys(originals)) delete originals[key];
  for (const node of walkGroups(data.groups)) {
    originals[node.path] = node.value;
  }
  for (const key of Object.keys(dirty)) delete dirty[key];
}

function* walkGroups(groups) {
  for (const group of groups || []) {
    for (const item of group.items || []) yield item;
    yield* walkGroups(group.children);
  }
}

async function open(path) {
  if (dirtyCount.value) {
    if (!window.confirm("有未保存的修改，放弃并切换文件？")) return;
  }
  try {
    if (/\.(ya?ml)$/i.test(path)) {
      await loadYaml(path);
      content.value = "";
    } else {
      const resp = await consoleApi(
        "/api/config/file?path=" + encodeURIComponent(path));
      const data = await readJson(resp);
      if (!resp.ok || !data.ok) {
        throw new Error(data.error || `读取失败 (${resp.status})`);
      }
      schema.value = null;
      content.value = data.content || "";
    }
    current.value = path;
  } catch (e) {
    message.error(String(e.message || e));
  }
}

function discard() {
  for (const key of Object.keys(dirty)) delete dirty[key];
  if (current.value && isYaml.value) open(current.value);
}

async function save() {
  if (!current.value) return;
  saving.value = true;
  try {
    if (isYaml.value) {
      if (!dirtyCount.value) {
        message.info("没有修改");
        return;
      }
      const resp = await consoleApi("/api/config/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: current.value, updates: { ...dirty } }),
      });
      const data = await readJson(resp);
      if (!resp.ok || !data.ok) {
        throw new Error(data.error || `保存失败 (${resp.status})`);
      }
      message.success(`已保存 ${data.updated.length} 项` +
        (data.backup ? `（旧版备份：${data.backup}）` : ""));
      restartNeeded.value = restartNeeded.value || current.value === "agent.yaml";
      await loadYaml(current.value);
    } else {
      const resp = await consoleApi("/api/config/file", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: current.value, content: content.value }),
      });
      const data = await readJson(resp);
      if (!resp.ok || !data.ok) {
        throw new Error(data.error || `保存失败 (${resp.status})`);
      }
      message.success(`已保存 ${data.path}（${data.bytes} 字节` +
        (data.backup ? `，旧版备份：${data.backup}` : "") + "）");
      await loadFiles();
    }
  } catch (e) {
    message.error(String(e.message || e));
  } finally {
    saving.value = false;
  }
}

async function restartKernel() {
  try {
    const resp = await consoleApi("/api/control/restart", { method: "POST" });
    const data = await readJson(resp);
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || "重启失败");
    }
    message.success(`Kernel 已重启（pid=${data.pid}）`);
    restartNeeded.value = false;
  } catch (e) {
    message.error(String(e.message || e));
  }
}

function insertTab(event) {
  const el = event.target;
  const start = el.selectionStart;
  const end = el.selectionEnd;
  content.value = content.value.slice(0, start) + "  " + content.value.slice(end);
  requestAnimationFrame(() => {
    el.selectionStart = el.selectionEnd = start + 2;
  });
}

onMounted(loadFiles);
</script>

<style scoped>
.config {
  padding: 20px 24px;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.title {
  font-size: 16px;
  font-weight: 600;
}
.actions {
  display: flex;
  gap: 8px;
}
.hint {
  margin: 10px 0 14px;
  font-size: 12px;
  opacity: 0.65;
  line-height: 1.6;
}
.body {
  flex: 1;
  min-height: 0;
  display: flex;
  gap: 14px;
}
.filelist {
  width: 240px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
}
.files {
  flex: 1;
  overflow: auto;
  border: 1px solid rgba(128, 128, 128, 0.25);
  border-radius: 8px;
}
.file {
  padding: 8px 10px;
  cursor: pointer;
  border-bottom: 1px solid rgba(128, 128, 128, 0.12);
}
.file:hover {
  background: rgba(128, 128, 128, 0.08);
}
.file.active {
  background: rgba(64, 128, 255, 0.15);
}
.fpath {
  font-size: 13px;
  word-break: break-all;
}
.fmeta {
  font-size: 11px;
  opacity: 0.55;
  margin-top: 2px;
}
.empty {
  padding: 12px;
  font-size: 12px;
  opacity: 0.5;
}
.form {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.form-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  gap: 12px;
}
.form-body {
  flex: 1;
  overflow: auto;
  padding-right: 4px;
}
.card {
  border: 1px solid rgba(128, 128, 128, 0.25);
  border-radius: 10px;
  padding: 12px 14px;
  margin-bottom: 12px;
}
.card-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 8px;
}
.gkey {
  font-family: ui-monospace, Menlo, monospace;
  font-size: 14px;
  font-weight: 600;
}
.gcomment {
  font-size: 12px;
  opacity: 0.6;
}
.items {
  display: flex;
  flex-direction: column;
}
.subgroup {
  margin: 6px 0 2px 12px;
  padding-left: 10px;
  border-left: 2px solid rgba(128, 128, 128, 0.25);
}
.sub-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 4px 0;
}
.epath {
  font-family: ui-monospace, Menlo, monospace;
  font-size: 13px;
}
.editor {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.editor-head {
  margin-bottom: 8px;
}
.textarea {
  flex: 1;
  width: 100%;
  resize: none;
  padding: 12px;
  border-radius: 8px;
  border: 1px solid rgba(128, 128, 128, 0.3);
  background: transparent;
  color: inherit;
  font-family: ui-monospace, Menlo, monospace;
  font-size: 13px;
  line-height: 1.6;
  outline: none;
}
</style>

<style>
.form .row {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: 7px 8px;
  border-radius: 8px;
}
.form .row:hover {
  background: rgba(128, 128, 128, 0.06);
}
.form .row.changed {
  background: rgba(240, 160, 32, 0.10);
}
.form .label {
  width: 42%;
  min-width: 200px;
}
.form .ikey {
  font-family: ui-monospace, Menlo, monospace;
  font-size: 13px;
  margin-right: 6px;
}
.form .icomment {
  font-size: 11px;
  opacity: 0.55;
  line-height: 1.5;
  margin-top: 2px;
  white-space: pre-line;
}
.form .control {
  flex: 1;
  min-width: 0;
  display: flex;
  justify-content: flex-end;
}
.form .ro {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  opacity: 0.7;
}
.form .ro-hint {
  font-size: 11px;
  opacity: 0.7;
}
</style>
