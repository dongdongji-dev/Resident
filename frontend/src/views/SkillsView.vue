<template>
  <div class="skills">
    <div class="header">
      <div class="title">技能管理</div>
      <div class="actions">
        <n-button size="small" :loading="loading" @click="load">刷新</n-button>
        <n-button size="small" type="primary" :loading="uploading" @click="pick">
          {{ uploading ? "安装中…" : "安装技能（zip）" }}
        </n-button>
        <input ref="fileInput" type="file" accept=".zip" style="display:none" @change="onFile" />
      </div>
    </div>
    <div class="hint">
      技能 = agent 的应用包（zip 内需含 SKILL.md，名称优先取其中的 name 字段）。
      安装后立即出现在系统提示词的技能清单里，无需重启；覆盖安装自动备份旧版，
      删除移入回收目录（均在 data/ 下，agent 看不到）。
    </div>
    <n-data-table
      :columns="columns"
      :data="rows"
      :bordered="false"
      size="small"
      :loading="loading"
      :row-key="(r) => r.name"
    />
    <n-drawer v-model:show="drawer" :width="680" placement="right">
      <n-drawer-content :title="'SKILL.md · ' + current" closable>
        <div class="md" v-html="rendered"></div>
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<script setup>
import { h, onMounted, ref } from "vue";
import {
  NButton, NDataTable, NDrawer, NDrawerContent, NPopconfirm, useMessage,
} from "naive-ui";
import MarkdownIt from "markdown-it";
import { consoleApi } from "../api";

const md = new MarkdownIt({ html: false, linkify: true, breaks: true });
const message = useMessage();

const rows = ref([]);
const loading = ref(false);
const uploading = ref(false);
const drawer = ref(false);
const current = ref("");
const rendered = ref("");
const fileInput = ref(null);

function human(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return value + " B";
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
  return (value / 1024 / 1024).toFixed(1) + " MB";
}

async function load() {
  loading.value = true;
  try {
    const resp = await consoleApi("/api/skills");
    const data = await resp.json();
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || `加载失败 (${resp.status})`);
    }
    rows.value = data.skills || [];
  } catch (e) {
    message.error(String(e.message || e));
  } finally {
    loading.value = false;
  }
}

function pick() {
  fileInput.value?.click();
}

async function onFile(event) {
  const file = event.target.files && event.target.files[0];
  event.target.value = "";
  if (!file) return;
  uploading.value = true;
  try {
    const form = new FormData();
    form.append("file", file, file.name);
    const resp = await consoleApi("/api/skills/install", {
      method: "POST",
      body: form,
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      throw new Error(data.error || `安装失败 (${resp.status})`);
    }
    const extra = data.backup ? "，旧版已备份" : "";
    message.success(`已安装 ${data.name}（${data.files} 个文件${extra}）`);
    await load();
  } catch (e) {
    message.error(String(e.message || e));
  } finally {
    uploading.value = false;
  }
}

async function openDetail(name) {
  try {
    const resp = await consoleApi(
      "/api/skills/detail?name=" + encodeURIComponent(name));
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      throw new Error(data.error || `读取失败 (${resp.status})`);
    }
    current.value = name;
    rendered.value = md.render(data.content || "");
    drawer.value = true;
  } catch (e) {
    message.error(String(e.message || e));
  }
}

async function remove(name) {
  try {
    const resp = await consoleApi("/api/skills/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      throw new Error(data.error || `删除失败 (${resp.status})`);
    }
    message.success(`已移入回收目录：${data.trashed_to}`);
    await load();
  } catch (e) {
    message.error(String(e.message || e));
  }
}

const columns = [
  {
    title: "名称",
    key: "name",
    render: (row) => (row.title ? `${row.name}（${row.title}）` : row.name),
  },
  { title: "描述", key: "description", ellipsis: { tooltip: true } },
  { title: "文件", key: "files", width: 70 },
  { title: "大小", key: "bytes", width: 90, render: (row) => human(row.bytes) },
  { title: "更新时间", key: "updated_at", width: 170 },
  {
    title: "操作",
    key: "ops",
    width: 150,
    render: (row) => h("div", [
      h(NButton, { size: "tiny", onClick: () => openDetail(row.name) },
        { default: () => "详情" }),
      h(NPopconfirm, {
        onPositiveClick: () => remove(row.name),
        positiveText: "移入回收",
        negativeText: "取消",
      }, {
        trigger: () => h(NButton,
          { size: "tiny", type: "error", style: "margin-left:8px" },
          { default: () => "删除" }),
        default: () => "删除后移入回收目录（可手动恢复），确认？",
      }),
    ]),
  },
];

onMounted(load);
</script>

<style scoped>
.skills {
  padding: 20px 24px;
  height: 100%;
  overflow: auto;
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
.md {
  font-size: 13px;
  line-height: 1.7;
  word-break: break-word;
}
.md :deep(pre) {
  background: rgba(128, 128, 128, 0.12);
  padding: 10px;
  border-radius: 6px;
  overflow: auto;
}
.md :deep(code) {
  background: rgba(128, 128, 128, 0.15);
  padding: 1px 4px;
  border-radius: 4px;
}
</style>
