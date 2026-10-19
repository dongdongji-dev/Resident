<template>
  <div class="chat">
    <div class="chat-main">
    <div v-if="!chat.coreRunning" class="core-banner">
      Core 未运行（队列与交互不可用）
      <n-button size="small" type="primary" :loading="starting" @click="startCore">启动 Core</n-button>
    </div>
    <header class="chat-header">
      <span class="badge"><span class="dot" :class="chat.coreRunning ? 'ok' : ''"></span>
        {{ chat.coreRunning ? '在线 pid ' + chat.corePid : '未运行' }}</span>
      <n-tag size="small" :type="stateType" round>{{ chat.state }}</n-tag>
      <span class="badge">第 {{ chat.round }} 轮</span>
      <span v-if="chat.queue.length" class="badge warn">队列 {{ chat.queue.length }}</span>
      <span class="badge" :class="{ ok: chat.connected }">流 {{ chat.connected ? '已连接' : '重连中' }}</span>
      <n-tag v-if="focus.active" size="small" type="warning" round>{{ focusLabel }}</n-tag>
      <n-button v-if="focus.active" size="small" type="warning" :loading="focusBusy" @click="releaseFocus">解除专注</n-button>
    </header>

    <div class="messages" ref="scroller">
      <div class="msg-col">
      <!-- 上下文消息（权威顺序） -->
      <template v-for="(m, i) in chat.messages" :key="m.id">
        <!-- 压缩分界线 -->
        <div v-if="m.kind === 'compaction'" class="sys">── 上下文已压缩（历史已归档）──</div>
        <!-- 用户消息：靠右（content 可能是 str 或 text/image block 列表） -->
        <div v-else-if="m.role === 'user'" class="row user">
          <div class="bubble user">
            <template v-for="(part, pi) in contentParts(m.content)" :key="pi">
              <div v-if="part.type === 'text'" class="bubble-text">{{ part.text }}</div>
              <n-image v-else class="bubble-image" :src="part.url" :width="240"
                :img-props="{ style: 'border-radius:10px;display:block;max-width:100%' }" />
            </template>
          </div>
        </div>
        <!-- 助手消息：靠左，思考在前正文在后 -->
        <div v-else-if="m.role === 'assistant'" class="row assistant">
          <div class="card assistant">
            <details v-if="m.reasoning" class="thinking">
              <summary>思考过程</summary>
              <div class="thinking-body">{{ m.reasoning }}</div>
            </details>
            <div v-if="m.content" class="md" v-html="renderMd(m.content)"></div>
            <div v-else-if="!m.tools?.length && !m.streaming" class="md empty">（空回复）</div>
            <div v-if="m.streaming && chat.state === 'streaming'"
              class="cursor"></div>
            <!-- 工具卡片只在执行中显示（完成后由下方独立的 tool 消息卡片
                 接管展示，避免同一工具结果渲染两遍） -->
            <template v-for="(t, ti) in m.tools" :key="ti">
              <details v-if="t.running" class="toolcard" open>
                <summary>
                  <span class="tool-name">{{ t.name }}</span>
                  <span v-if="t.params" class="tool-params">{{ t.params }}</span>
                  <span class="preview">执行中…</span>
                </summary>
              </details>
            </template>
          </div>
        </div>
        <!-- 工具结果（context 权威顺序） -->
        <div v-else-if="m.role === 'tool'" class="row tool">
          <details class="toolcard" :open="m.open">
            <summary @click.prevent="m.open = !m.open">
              <span class="tool-name">{{ m.toolName }}</span>
              <span class="preview">{{ previewOf(m.content, m.toolName) }}</span>
              <span class="fold">{{ m.open ? '收起▾' : '展开▾' }}</span>
            </summary>
            <div class="tool-result" v-html="renderToolResult(m.content, m.toolName)"></div>
          </details>
        </div>
      </template>
      </div><!-- /msg-col -->

      <!-- 消息区右侧：队列（滚轮在队列右侧，即消息容器最右） -->
      <aside class="queue-panel">
        <div class="queue-head">
          <span>消息队列</span>
          <span class="qcount">{{ chat.queue.length }}</span>
        </div>
        <div class="queue-list">
          <div v-if="!chat.queue.length" class="qempty">队列为空</div>
          <div v-for="item in chat.queue" :key="item.id" class="qitem">
            <div class="qhead-line">
              <span class="qrole r-user">user</span>
              <span class="qkind">{{ item.uploading ? "上传中…" : "排队中" }}</span>
            </div>
            <div class="qcontent">
              <template v-for="(part, pi) in contentParts(item.content)" :key="pi">
                <div v-if="part.type === 'text'">{{ part.text }}</div>
                <img v-else :src="part.url" class="qimage" />
              </template>
            </div>
          </div>
        </div>
      </aside>
    </div>

    <footer class="composer-wrap">
      <div v-if="attachments.length" class="attach-strip">
        <div v-for="a in attachments" :key="a.id" class="attach-item">
          <img :src="a.previewUrl || a.dataUrl" alt="" />
          <button type="button" class="attach-remove" title="移除"
            @click="removeAttachment(a.id)">×</button>
        </div>
        <span class="attach-hint">
          {{ Math.round(attachBytes / 1024) }} KB
          <template v-if="!imgbedEnabled">/ {{ Math.round(ATTACH_TOTAL_BYTES / 1024) }} KB</template>
          <template v-else>（原图模式）</template>
        </span>
      </div>
      <div class="composer">
        <textarea v-model="input" rows="1"
          placeholder="输入消息，Enter 发送（Shift+Enter 换行，可粘贴图片）"
          @keydown="onKey" @input="autoGrow" @paste="onPaste"></textarea>
        <n-button type="primary" :disabled="!canSend" @click="send">发送</n-button>
      </div>
    </footer>
    </div><!-- /chat-main -->

    <!-- 登录模态 -->
    <n-modal v-model:show="loginShow" :mask-closable="false" preset="card"
      title="登录" style="width: 380px">
      <div class="login-form">
        <p class="login-hint">密钥见仓库根目录 .env 的 RESIDENT_CONSOLE_TOKEN（首次启动会打印在日志里）</p>
        <n-input v-model:value="loginToken" type="password" show-password-on="click"
          placeholder="请输入登录密钥" @keydown="(e) => { if (!e.isComposing && e.key === 'Enter') doLogin(); }" />
        <n-button type="primary" block style="margin-top: 14px"
          :loading="loginLoading" @click="doLogin">登录</n-button>
      </div>
    </n-modal>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted, onBeforeUnmount } from "vue";
import { NButton, NTag, NModal, NInput, NImage, useMessage } from "naive-ui";
import MarkdownIt from "markdown-it";
import hljs from "highlight.js";
import { chat, CORE, coreApi, consoleApi, getToken, setToken, clearToken, AuthError } from "../api";

const md = new MarkdownIt({
  linkify: true, breaks: true,
  highlight: (str, lang) => {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return '<pre class="hljs"><code class="language-' + lang + '">'
          + hljs.highlight(str, { language: lang, ignoreIllegals: true }).value
          + "</code></pre>";
      } catch { /* fallthrough */ }
    }
    return '<pre class="hljs"><code>' + md.utils.escapeHtml(str) + "</code></pre>";
  },
});

// context 里工具消息的 name 在顶层（metadata 被展开）；兼容两种结构
function toolNameOf(en) {
  return ((en.metadata || {}).name || en.name || "").trim();
}

// content 可能是 str（纯文本）或 OpenAI 风格 block 列表
// （{type:'text'} / {type:'image_url',image_url:{url}}）；拆成可渲染片段。
// 非法/空值安全忽略，绝不把块对象直接塞进模板。
function contentParts(content) {
  if (typeof content === "string") {
    return content ? [{ type: "text", text: content }] : [];
  }
  if (!Array.isArray(content)) {
    return content == null ? [] : [{ type: "text", text: String(content) }];
  }
  const parts = [];
  for (const block of content) {
    if (!block || typeof block !== "object") continue;
    if (block.type === "text" && block.text) {
      parts.push({ type: "text", text: String(block.text) });
    } else if (block.type === "image_url") {
      const raw = block.image_url;
      const url = typeof raw === "string" ? raw : (raw && raw.url) || "";
      if (url) parts.push({ type: "image", url });
    }
  }
  return parts;
}

// 稳定内容键：块列表必须序列化比较，否则 "…" + content 变成
// "[object Object]" 会让不同消息互相误判为重复。
function contentKey(content) {
  if (typeof content === "string") return content;
  try { return JSON.stringify(content); } catch { return String(content); }
}

// Responses API keeps private reasoning opaque but may return a visible
// reasoning summary inside provider_items. Support both the normalized field
// and historical raw Responses entries without rewriting context.jsonl.
function reasoningOf(en) {
  if (en.reasoning_content) return String(en.reasoning_content);
  const parts = [];
  for (const item of (en.provider_items || [])) {
    if (item?.type !== "reasoning") continue;
    for (const summary of (item.summary || [])) {
      if (summary?.text) parts.push(String(summary.text));
    }
  }
  return parts.join("\n\n");
}

// 工具结果 diff 行着色：仅 workspace_edit 的返回渲染（避免误伤
// 其他工具输出里正常的 markdown 列表 "-"）；name 匹配失败时用 diff
// 内容特征（@@ + +++/--- 文件头）兜底。+ 绿 / - 红整行实色背景；
// 无前缀空行直接过滤（不显示），保持 diff 紧凑连续。
function esc(s) {
  const d = document.createElement("div");
  d.textContent = String(s ?? "");
  return d.innerHTML;
}
function renderToolResult(text, toolName) {
  const content = String(text || "");
  const isDiffText = content.includes("@@")
    && /^\s*(\+\+\+|---)/m.test(content);
  if (toolName !== "workspace_edit" && !isDiffText) return esc(content);
  return content.split("\n")
    // 过滤所有空内容行：无前缀空行（context 空行）与 +/- 前缀的空行
    // （新增/删除空行），保持 diff 紧凑
    .filter((line) => {
      const t = line.trim();
      return t !== "" && t !== "+" && t !== "-";
    })
    .map((line) => {
      const t = line.trimStart();
      let cls = "diff-plain";
      if (t.startsWith("+++") || t.startsWith("---")) cls = "diff-file";
      else if (t.startsWith("+")) cls = "diff-add";
      else if (t.startsWith("-")) cls = "diff-del";
      else if (t.startsWith("@@")) cls = "diff-hunk";
      // 所有行都包 span（block）：join 不带换行符，避免 pre-wrap 把
      // block 之间的 \n 渲染成额外空行
      return `<span class="${cls}">${esc(line)}</span>`;
    }).join("");
}
const message = useMessage();
const input = ref("");
const scroller = ref(null);
const starting = ref(false);
const TOKEN = getToken();

// ── 粘贴图片：压缩到字节预算内，随 /api/event 以 block 列表发送 ──
const MAX_ATTACHMENTS = 4;
const ATTACH_TOTAL_BYTES = 700 * 1024;  // base64 模式：附件 data URL 总长上限
const ATTACH_MAX_BYTES = 384 * 1024;    // base64 模式：单图上限
const ATTACH_MAX_ORIGINAL_BYTES = 20 * 1024 * 1024;  // 图床模式：原图上限
const MAX_EDGE = 2048;
const attachments = ref([]);
const imgbedEnabled = ref(false);  // /api/state 下发；true 时附件走图床原图 URL
const objectUrls = [];  // 图床模式的本地预览 URL，页面卸载时统一释放
const attachBytes = computed(() =>
  attachments.value.reduce((n, a) =>
    n + (a.file ? a.file.size : (a.dataUrl || "").length), 0));

function loadBitmap(file) {
  return new Promise((resolve, reject) => {
    if (window.createImageBitmap) {
      createImageBitmap(file).then(resolve).catch(reject);
      return;
    }
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("图片解码失败")); };
    img.src = url;
  });
}

function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("读取失败"));
    reader.readAsDataURL(blob);
  });
}

// canvas 压缩：限长边 → JPEG 质量 0.85→0.4 → 边长 ×0.8，直到 ≤ maxBytes
async function compressImageToDataUrl(file, maxBytes) {
  let source;
  try { source = await loadBitmap(file); } catch { return null; }
  let width = source.width || 0, height = source.height || 0;
  if (!width || !height) return null;
  if (Math.max(width, height) > MAX_EDGE) {
    const scale = MAX_EDGE / Math.max(width, height);
    width = Math.max(1, Math.round(width * scale));
    height = Math.max(1, Math.round(height * scale));
  }
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  let quality = 0.85;
  try {
    for (let i = 0; i < 24; i++) {
      canvas.width = width;
      canvas.height = height;
      ctx.fillStyle = "#fff";  // JPEG 无透明：先铺白底
      ctx.fillRect(0, 0, width, height);
      ctx.drawImage(source, 0, 0, width, height);
      const blob = await new Promise((res) =>
        canvas.toBlob(res, "image/jpeg", quality));
      if (!blob) break;
      const dataUrl = await blobToDataUrl(blob);
      if (dataUrl.length <= maxBytes) return dataUrl;
      if (quality > 0.4) { quality = Math.max(0.4, quality - 0.1); continue; }
      if (Math.min(width, height) <= 64) break;
      width = Math.max(1, Math.round(width * 0.8));
      height = Math.max(1, Math.round(height * 0.8));
      quality = 0.7;
    }
  } finally {
    if (source.close) source.close();
  }
  return null;
}

async function addAttachment(file) {
  if (attachments.value.length >= MAX_ATTACHMENTS) {
    message.warning(`最多 ${MAX_ATTACHMENTS} 张图片`);
    return;
  }
  // 图床模式：不做压缩（请求体只是上传，不占上下文），直接存原文件
  if (imgbedEnabled.value) {
    if (file.size > ATTACH_MAX_ORIGINAL_BYTES) {
      message.warning(
        `原图 ${(file.size / 1048576).toFixed(1)}MB 超过上限 ` +
        `${ATTACH_MAX_ORIGINAL_BYTES / 1048576}MB`);
      return;
    }
    const previewUrl = URL.createObjectURL(file);
    objectUrls.push(previewUrl);
    attachments.value.push({
      id: "at" + Date.now() + Math.random().toString(36).slice(2, 7),
      name: file.name || "clipboard.png",
      file,
      previewUrl,
      dataUrl: "",
    });
    return;
  }
  const remaining = ATTACH_TOTAL_BYTES - attachBytes.value;
  const target = Math.min(ATTACH_MAX_BYTES, remaining);
  if (target < 16 * 1024) {
    message.warning("附件总量已达上限");
    return;
  }
  // 与后端 readimage 对齐：支持格式且已在目标字节内 → 原图直传
  // （不重编码，截图文字不损失）；超限或格式不支持才 canvas 压缩
  const passthrough = ["image/png", "image/jpeg", "image/webp", "image/gif"]
    .includes(String(file.type || "").toLowerCase());
  let dataUrl = null;
  if (passthrough) {
    const raw = await blobToDataUrl(file).catch(() => null);
    if (raw && raw.length <= target) dataUrl = raw;
  }
  if (!dataUrl) dataUrl = await compressImageToDataUrl(file, target);
  if (!dataUrl) {
    message.error("图片压缩失败或过大");
    return;
  }
  attachments.value.push({
    id: "at" + Date.now() + Math.random().toString(36).slice(2, 7),
    name: file.name || "clipboard.png",
    file,
    previewUrl: "",
    dataUrl,
  });
}

async function onPaste(e) {
  const items = Array.from((e.clipboardData && e.clipboardData.items) || []);
  const files = [];
  for (const item of items) {
    if (item.kind === "file" && String(item.type || "").startsWith("image/")) {
      const f = item.getAsFile();
      if (f) files.push(f);
    }
  }
  if (!files.length) return;  // 纯文本粘贴走浏览器默认
  e.preventDefault();
  for (const f of files) await addAttachment(f);
}

function removeAttachment(id) {
  attachments.value = attachments.value.filter((a) => a.id !== id);
}

async function serverError(r) {
  try {
    const data = await r.json();
    if (data && data.error) return data.error;
  } catch { /* ignore */ }
  return `发送失败（HTTP ${r.status}）`;
}

// 图床模式：经内核上传原图（key 不下发浏览器），返回公开 URL
async function uploadToImgbed(att) {
  let blob = att.file;
  if (!blob) blob = await (await fetch(att.dataUrl)).blob();
  const form = new FormData();
  form.append("file", blob, att.name || "image.jpg");
  const r = await coreApi("/api/image", { method: "POST", body: form });
  if (!r.ok) throw new Error(await serverError(r));
  const data = await r.json();
  if (!data || !data.url) throw new Error("图床未返回链接");
  return data.url;
}

// 登录模态
const loginShow = ref(!TOKEN);
const loginToken = ref("");
const loginLoading = ref(false);
async function doLogin() {
  const t = loginToken.value.trim();
  if (!t) return;
  loginLoading.value = true;
  try {
    const r = await coreApi("/api/context", { headers: { "X-Api-Token": t } });
    await r.json();
    setToken(t);
    loginShow.value = false;
    location.reload();
  } catch {
    message.error("密钥无效或 core 不可达");
  } finally { loginLoading.value = false; }
}

const canSend = computed(() => !!getToken() && chat.coreRunning
  && (!!input.value.trim() || attachments.value.length > 0));
const stateType = computed(() => ({
  idle: "default", streaming: "info", tool_use: "warning",
  compress: "warning",
}[chat.state] || "default"));

function renderMd(text) {
  return md.render(String(text || ""));
}
function scrollBottom() {
  // 硬吸底：发送/初始加载等主动定位场景
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
}

// ── 智能吸底：仅当贴近底部时跟随流式；检测到上翻后屏蔽一小段时间 ──
const SCROLL_GAP = 60;   // 距底部小于该值才继续跟随
const BLOCK_MS = 400;    // 用户上翻后屏蔽自动吸底的毫秒数
let suppressUntil = 0;
function bottomDist(el) { return el.scrollHeight - el.scrollTop - el.clientHeight; }
function shouldFollow(el) { return performance.now() > suppressUntil && bottomDist(el) <= SCROLL_GAP; }
function maybeFollow() {
  if (scroller.value && shouldFollow(scroller.value))
    scroller.value.scrollTop = scroller.value.scrollHeight;
}
function onChatWheel() {
  if (scroller.value && bottomDist(scroller.value) > SCROLL_GAP)
    suppressUntil = performance.now() + BLOCK_MS;
}
function onChatScroll() {
  if (scroller.value && bottomDist(scroller.value) <= SCROLL_GAP) suppressUntil = 0;
}

// 工具结果折叠预览：只显示前 3 行
function previewOf(text, name) {
  const full = String(text || "");
  if (!full) return "";
  const lines = full.split("\n");
  const preview = lines.slice(0, 3).join("\n");
  return lines.length > 3 ? preview + " …" : preview;
}
function autoGrow(e) {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 140) + "px";
}
function onKey(e) {
  // 中文输入法组词时按 Enter 是“确认候选词”，不是发送——
  // 这时 isComposing=true（或 keyCode 229），必须忽略，否则半截字被发出去
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
}

// ── 发送：乐观入队列（agent 队列机制，等待 context 写入）──
// 带图且启用图床时：立即入队并标"上传中"，并行上传后把 URL 换进消息，
// 交互不被上传阻塞；任一上传失败则移除队列项并恢复附件。
async function send() {
  const text = input.value.trim();
  const files = attachments.value.slice();
  if ((!text && !files.length) || !canSend.value) return;
  input.value = "";
  attachments.value = [];
  const id = "q" + Date.now() + Math.random().toString(36).slice(2, 7);
  const uploading = files.length > 0 && imgbedEnabled.value;
  const content = files.length
    ? [
        ...(text ? [{ type: "text", text }] : []),
        ...files.map((a) => ({ type: "image_url",
                               image_url: { url: a.dataUrl } })),
      ]
    : text;
  const item = { id, content, uploading };
  chat.queue.push(item);
  scrollBottom();
  try {
    if (uploading) {
      const urls = await Promise.all(files.map((a) => uploadToImgbed(a)));
      let index = 0;
      for (const part of item.content) {
        if (part.type === "image_url" && urls[index]) {
          part.image_url.url = urls[index];
        }
        if (part.type === "image_url") index += 1;
      }
    }
    const r = await coreApi("/api/event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: item.content }),
    });
    if (!r.ok) throw new Error(await serverError(r));
    item.uploading = false;
  } catch (e) {
    // 发送失败：从队列移除，避免消息卡在"排队中/上传中"；附件恢复可重试
    const idx = chat.queue.findIndex((q) => q.id === id);
    if (idx >= 0) chat.queue.splice(idx, 1);
    if (files.length) attachments.value = files;
    if (e instanceof AuthError) { loginShow.value = true; clearToken(); }
    else message.error(e && e.message ? e.message : "发送失败：core 不可达");
  }
}

async function startCore() {
  starting.value = true;
  await consoleApi("/api/control/start", { method: "POST" });
  setTimeout(() => { starting.value = false; location.reload(); }, 2500);
}

// ── SSE：context 是唯一真相源，stream 只是流式动画层 ──
// 所有消息都来自 context_appended（权威顺序）；token/tool_started 只
// 更新"最后一条正在流式的 assistant"（streaming 标记），它最终由
// context_appended 落定（不再有消息丢失/覆盖问题）。
let es = null;
function lastAssistant(streamingOnly = false) {
  for (let i = chat.messages.length - 1; i >= 0; i--) {
    const m = chat.messages[i];
    if (m.role === "assistant" && (!streamingOnly || m.streaming)) return m;
  }
  return null;
}
function connectStream() {
  es = new EventSource(CORE + "/api/stream?token=" + encodeURIComponent(TOKEN));
  es.onopen = () => {
    chat.connected = true;
    // 拉一次真实状态：state_changed 只在状态变化时发，首次进入/重连时
    // 若 agent 一直 idle，前端否则永远显示初始的假状态
    refreshState();
    if (!chat.messages.length) {
      // 进入页面时若历史还没拉成功（比如 kernel 刚重启），连上后自动补拉
      loadHistory({ silent: true });
    } else {
      // 断流重连：回放的历史带 _replay 会被忽略，增量补拉断流期间
      // 错过的 context_appended（按 content+role 去重，不会重复）
      syncFromContext();
    }
  };
  es.onerror = () => { chat.connected = false; };
  es.onmessage = (ev) => {
    let e; try { e = JSON.parse(ev.data); } catch { return; }
    // SSE 回放的历史事件（_replay）已由 /api/context 全量提供，仅保留
    // state_changed 更新当前状态，其余忽略——否则消息/工具整段重复渲染。
    if (e._replay && e.type !== "state_changed") return;
    switch (e.type) {
      case "context_appended": {
        const msg = e.message;
        if (msg.role === "user") {
          // 用户消息入上下文：从队列移除。agent 按 FIFO 处理，所以 shift
          // 最旧排队项即本次这条（内容匹配在合并/清洗下可能对不上）。
          chat.queue.shift();
          chat.messages.push({
            id: "u" + Date.now() + Math.random().toString(36).slice(2, 7),
            role: "user", content: msg.content,
          });
        } else if (msg.role === "assistant") {
          const last = lastAssistant(true);
          if (last) {
            // 流式消息落定：以 context 为准（工具轮正文可能为空，消息保留）
            last.content = msg.content || last.content;
            last.reasoning = reasoningOf(msg) || last.reasoning;
            last.streaming = false;
          } else {
            chat.messages.push({
              id: "a" + Date.now() + Math.random().toString(36).slice(2, 7),
              role: "assistant", content: msg.content || "",
              reasoning: reasoningOf(msg), streaming: false,
            });
          }
        } else if (msg.role === "tool") {
          // 工具结果卡片：context 驱动（权威顺序，不依赖流式状态）
          chat.messages.push({
            id: "t" + Date.now() + Math.random().toString(36).slice(2, 7),
            role: "tool", content: msg.content || "",
            toolName: toolNameOf(msg), open: false,
          });
        }
        // 兜底：凡内容已出现在上下文（用户消息）里的排队项，一律清掉，
        // 避免因合并/清洗导致 shift/匹配没清干净而残留
        chat.queue = chat.queue.filter(
          (q) => !chat.messages.some(
            (m) => m.role === "user"
              && contentKey(m.content) === contentKey(q.content)));
        maybeFollow();
        break;
      }
      case "state_changed":
        chat.state = e.state;
        break;
      case "round_started": {
        chat.round = e.round_id;
        // 新一轮：push 一条流式占位（后续 token/工具/context 都作用于此）
        chat.messages.push({
          id: "r" + chat.round, role: "assistant",
          content: "", reasoning: "", tools: [], streaming: true,
        });
        maybeFollow();
        break;
      }
      case "token": {
        // 只追加到正在流式的占位消息（round_started 推送的那条）。
        // 不 fallback 到已落定的历史消息——否则流结束后迟到的 token
        // 会追加到已落定内容，造成正文重复（如"继续"变"继续继续"）。
        const m = lastAssistant(true);
        if (!m) break;
        if (e.channel === "thinking") m.reasoning += e.delta;
        else m.content += e.delta;
        maybeFollow();
        break;
      }
      case "tool_started": {
        // 工具调用属于最后一条 assistant（此时可能已 context 落定，
        // 不限于 streaming 状态）
        const m = lastAssistant(false);
        if (m) {
          if (!m.tools) m.tools = [];
          m.tools.push({
            name: e.name, params: JSON.stringify(e.params),
            result: "", running: true, open: false,
          });
        }
        maybeFollow();
        break;
      }
      case "tool_finished": {
        // 更新最后一条 assistant 的 running 工具
        const m = lastAssistant(false);
        if (m && m.tools) {
          const tool = m.tools.find((t) => t.running);
          if (tool) { tool.result = e.content; tool.running = false; }
        }
        maybeFollow();
        break;
      }
      case "context_compressed":
        chat.messages.push({ id: "c" + Date.now(), kind: "compaction" });
        maybeFollow();
        break;
      case "agent_crashed":
        message.error("Agent 崩溃：" + (e.error || ""));
        break;
    }
  };
}

// 兼容 kernel 返回 {entries:[...]} 或裸数组；为容错而双写
function contextEntries(data) {
  return Array.isArray(data) ? data : (data && data.entries) || [];
}

async function loadHistory({ silent = false } = {}) {
  try {
    const r = await coreApi("/api/context");
    const entries = contextEntries(await r.json());
    chat.messages = entries.map((en, i) => ({
      id: "h" + i + "_" + en.role,
      role: en.role,
      kind: en.kind,
      content: en.content || "",
      reasoning: reasoningOf(en),
      tools: [],
      streaming: false,
      toolName: toolNameOf(en),
      open: false,
    }));
    scrollBottom();
  } catch (e) {
    if (e instanceof AuthError) {
      loginShow.value = true;
      clearToken();
    } else if (!silent) {
      // 进入页面时的瞬时失败只静默，等 SSE 连上后自动重试
      message.error("上下文加载失败：core 不可达");
    }
  }
}

// ── 专注模式：状态显示 + 一键解除（解除后积压立即回补）──
const focus = ref({ active: false, remaining_minutes: 0, mode: null });
const focusBusy = ref(false);
const focusLabel = computed(() => {
  const suffix = focus.value.mode === "whitelist" ? "（白名单）"
    : focus.value.mode === "blacklist" ? "（黑名单）" : "";
  return `专注中${suffix} · 剩余 ${focus.value.remaining_minutes} 分钟`;
});
async function pollFocus() {
  try {
    const data = await (await consoleApi("/api/focus")).json();
    if (!data.error) focus.value = data;
  } catch { focus.value.active = false; }
}
async function releaseFocus() {
  focusBusy.value = true;
  try {
    await consoleApi("/api/focus/release", { method: "POST" });
    await pollFocus();
  } finally { focusBusy.value = false; }
}

// ── SSE 断流兜底：HTTP 轮询同步（队列移除/新消息展示不依赖 SSE）──
let lastSynced = "";
async function refreshState() {
  try {
    const r = await coreApi("/api/state");
    const st = await r.json();
    chat.state = st.state;
    chat.round = st.round;
    imgbedEnabled.value = !!st.imgbed;
  } catch { /* kernel 不可达时静默，等下一轮 */ }
}

async function syncFromContext() {
  try {
    const [ctxResp, stateResp] = await Promise.all([
      coreApi("/api/context"),
      coreApi("/api/state"),
    ]);
    const st = await stateResp.json();
    const entries = contextEntries(await ctxResp.json());
    // 断流期间 state_changed 收不到：同步 agent 状态/轮次（否则前端
    // 状态停留在断流前的 idle，看起来"有消息却不处理"）
    chat.state = st.state;
    chat.round = st.round;
    imgbedEnabled.value = !!st.imgbed;
    const known = new Set(
      chat.messages.map((m) => contentKey(m.content) + "|" + m.role));
    const fresh = entries.filter(
      (en) => !known.has(contentKey(en.content) + "|" + en.role));
    if (fresh.length) {
      chat.messages.push(...fresh.map((en, i) => ({
        id: "s" + Date.now() + "_" + i + "_" + en.role,
        role: en.role,
        kind: en.kind,
        content: en.content || "",
        reasoning: reasoningOf(en),
        toolName: toolNameOf(en),
        open: false,
      })));
      // 入上下文的用户消息：清掉对应排队项
      fresh.forEach((en) => {
        if (en.role === "user") {
          const idx = chat.queue.findIndex(
            (q) => contentKey(q.content) === contentKey(en.content));
          if (idx >= 0) chat.queue.splice(idx, 1);
        }
      });
      scrollBottom();
    }
    lastSynced = Date.now();
  } catch { /* core 不可达时静默，等下一轮 */ }
}

onMounted(async () => {
  await loadHistory({ silent: true });
  connectStream();
  if (scroller.value) {
    scroller.value.addEventListener("wheel", onChatWheel);
    scroller.value.addEventListener("scroll", onChatScroll);
  }
  // SSE 断流兜底
  setInterval(() => {
    if (!chat.connected) syncFromContext();
  }, 3000);
  // 专注模式状态轮询（头部显示 + 解除按钮）
  pollFocus();
  setInterval(pollFocus, 3000);
});
onBeforeUnmount(() => {
  if (es) es.close();
  if (scroller.value) {
    scroller.value.removeEventListener("wheel", onChatWheel);
    scroller.value.removeEventListener("scroll", onChatScroll);
  }
});
</script>

<style scoped>
.chat { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.chat-main { flex: 1; min-width: 0; display: flex; flex-direction: column; height: 100%; min-height: 0; }
.core-banner { display: flex; align-items: center; justify-content: center;
  gap: 12px; background: var(--banner-bg); color: var(--banner-text);
  font-size: 13px; padding: 8px 12px; flex-shrink: 0; }
.chat-header { display: flex; align-items: center; gap: 12px;
  padding: 9px 16px; border-bottom: 1px solid var(--line); flex-wrap: wrap;
  flex-shrink: 0; }
.badge { font-size: 13px; color: var(--dim); }
.badge.ok { color: var(--info); }
.badge.warn { color: var(--warn); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--err);
  display: inline-block; margin-right: 6px; }
.dot.ok { background: var(--info); box-shadow: 0 0 6px var(--info); }

/* 滚动容器：消息区在左、队列在右，滚动条位于整体最右（消息区 → 队列 → 滚轮） */
.messages { flex: 1 1 auto; min-height: 0; overflow-y: auto;
  display: flex; align-items: flex-start; }
.msg-col { flex: 1 1 0; min-width: 0; padding: 18px 22px 26px; }
.row { display: flex; margin-bottom: 14px; }
/* 用户消息：容器与模型同宽（居中 820px），气泡靠右（右缘与模型右缘
   对齐），宽度上限为模型宽度的 70%（必然小于模型气泡） */
.row.user { width: min(820px, 100%); margin-left: auto; margin-right: auto;
  justify-content: flex-end; }
.bubble { max-width: 85%; padding: 11px 15px; border-radius: 16px;
  font-size: 15.5px; line-height: 1.65; white-space: pre-wrap; word-break: break-word; }
.bubble.user { background: var(--user-bg); color: var(--user-text);
  border-bottom-right-radius: 4px; }
/* 多模态气泡：文本块 + 图片块（图片点击可预览大图） */
.bubble-text + .bubble-text { margin-top: 6px; }
.bubble-image { display: block; margin-top: 6px; max-width: 100%; }
/* 模型输出：居中内容流（固定宽度、与背景同色、无描边——逻辑气泡
   但视觉上是正文，不显卡片感） */
.row.assistant { justify-content: center; }
.card.assistant { width: min(820px, 100%); background: transparent;
  border: none; padding: 4px 0; font-size: 15.5px; line-height: 1.7; }
/* 工具卡片：居中、带边框和底色 */
.row.tool { justify-content: center; }
.row.tool .toolcard { width: min(820px, 100%); margin-top: 0; }
.md { word-break: break-word; }
.md :deep(p) { margin: 4px 0; }
.md :deep(pre) { background: var(--code-bg); border: 1px solid var(--line);
  border-radius: 8px; padding: 10px 12px; overflow-x: auto; margin: 8px 0; }
.md :deep(code) { font-family: ui-monospace, Menlo, monospace; font-size: 13.5px; }
/* markdown 表格：边框 + 表头底色 */
.md :deep(table) { border-collapse: collapse; margin: 8px 0; display: block;
  overflow-x: auto; max-width: 100%; }
.md :deep(th), .md :deep(td) { border: 1px solid var(--line); padding: 6px 12px; }
.md :deep(th) { background: var(--tool-bg); font-weight: 600; }
.md :deep(pre code) { background: none; padding: 0; }
/* 代码高亮（vscode 风格 token 色，跟随主题） */
.md :deep(.hljs-keyword) { color: var(--hl-keyword); font-weight: 600; }
.md :deep(.hljs-string) { color: var(--hl-string); }
.md :deep(.hljs-number), .md :deep(.hljs-literal) { color: var(--hl-number); }
.md :deep(.hljs-comment) { color: var(--hl-comment); font-style: italic; }
.md :deep(.hljs-title), .md :deep(.hljs-title.function_),
.md :deep(.hljs-function) { color: var(--hl-fn); }
.md :deep(.hljs-attr), .md :deep(.hljs-attribute),
.md :deep(.hljs-params) { color: var(--hl-attr); }
.md :deep(.hljs-built_in), .md :deep(.hljs-type) { color: var(--hl-builtin); }
.md :deep(.hljs-variable), .md :deep(.hljs-name),
.md :deep(.hljs-tag) { color: var(--hl-variable); }
.thinking { margin-top: 6px; font-size: 13.5px; color: var(--dim); }
.thinking summary { cursor: pointer; user-select: none; }
.thinking-body { margin-top: 6px; padding: 8px 10px; background: var(--thinking-bg);
  border: 1px dashed var(--line); border-radius: 8px; white-space: pre-wrap;
  word-break: break-word; }
.cursor { display: inline-block; width: 8px; height: 15px; margin-left: 2px;
  background: var(--cursor); vertical-align: -2px; animation: blink 1s infinite; }
@keyframes blink { 50% { opacity: 0; } }
.toolcard { margin-top: 8px; font-size: 13.5px; background: var(--tool-bg);
  border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.toolcard > summary { display: flex; gap: 8px; align-items: baseline;
  padding: 9px 12px; cursor: pointer; user-select: none; list-style: none;
  background: var(--tool-bg); }
.toolcard > summary::-webkit-details-marker { display: none; }
.toolcard[open] > summary { border-bottom: 1px solid var(--line); }
.toolcard .preview { color: var(--dim); overflow: hidden; white-space: nowrap;
  text-overflow: ellipsis; flex: 1; min-width: 0; }
.toolcard .fold { color: var(--dim); font-size: 12px; white-space: nowrap; }
.toolcard .tool-result { padding: 6px 12px 10px; max-height: 60vh; overflow: auto; }
.toolcard .tool-result :deep(span) { display: block; }
.tool-name { color: var(--accent); font-weight: 600; margin-right: 8px; }
.tool-params { color: var(--dim); margin-right: 8px; word-break: break-all; }
.tool-result { color: var(--text); word-break: break-all; white-space: pre-wrap; }
.tool-result.running { color: var(--warn); }
/* diff 着色：整行背景色（+ 绿 / - 红），文字同色 */
.tool-result :deep(span) { display: block; }  /* 所有行 block 换行 */
.tool-result :deep(.diff-add) { color: var(--diff-add);
  background: color-mix(in srgb, var(--diff-add) 18%, transparent); }
.tool-result :deep(.diff-del) { color: var(--diff-del);
  background: color-mix(in srgb, var(--diff-del) 18%, transparent); }
.tool-result :deep(.diff-hunk) { color: var(--diff-hunk); font-weight: 600; }
.tool-result :deep(.diff-file) { color: var(--diff-file); font-weight: 600; }
.tool-result :deep(.diff-ctx) { display: block;
  background: color-mix(in srgb, var(--line) 15%, transparent); }
.sys { color: var(--dim); font-size: 13px; text-align: center; margin: 12px 0; }
.tool-sys { text-align: left; font-size: 12px; }
.empty { color: var(--dim); }

.composer-wrap { border-top: 1px solid var(--line); flex-shrink: 0; }
.composer { display: flex; gap: 10px; padding: 12px 16px; align-items: flex-end; }
/* 粘贴图片附件条：缩略图 + 单张移除 + 总量提示 */
.attach-strip { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 10px 16px 0; }
.attach-item { position: relative; width: 56px; height: 56px; border-radius: 8px;
  overflow: hidden; border: 1px solid var(--line); }
.attach-item img { width: 100%; height: 100%; object-fit: cover; display: block; }
.attach-remove { position: absolute; top: 0; right: 0; width: 18px; height: 18px;
  border: none; border-radius: 0 0 0 6px; background: rgba(0, 0, 0, .55);
  color: #fff; font-size: 12px; line-height: 18px; cursor: pointer; padding: 0; }
.attach-hint { font-size: 11px; color: var(--dim); }
.composer textarea { flex: 1; background: var(--card); color: var(--text);
  border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px;
  font: inherit; font-size: 14px; resize: none; outline: none; }
.composer textarea:focus { border-color: var(--accent); }
.login-form { display: flex; flex-direction: column; gap: 4px; }
.login-hint { font-size: 12px; color: var(--dim); margin-bottom: 8px; }

/* 消息区右侧队列卡片：随消息区滚动容器一起滚动、sticky 贴顶，
   因此滚动条位于队列右侧（整体最右）：消息区 → 队列 → 滚轮。 */
.queue-panel { width: 300px; flex-shrink: 0; position: sticky; top: 16px;
  align-self: flex-start; margin: 16px 16px 16px 8px;
  max-height: calc(100vh - 96px);
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  box-shadow: 0 6px 24px rgba(0,0,0,.14); overflow: hidden;
  display: flex; flex-direction: column; min-height: 0; }
.queue-head { display: flex; justify-content: space-between; align-items: center;
  padding: 12px 14px; border-bottom: 1px solid var(--line); font-size: 13px;
  color: var(--dim); flex-shrink: 0; }
.queue-head .qcount { display: inline-flex; align-items: center; justify-content: center;
  min-width: 22px; height: 22px; padding: 0 7px; border-radius: 999px;
  background: var(--nav-active); color: var(--accent); font-weight: 600; font-size: 12px; }
.queue-list { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 8px; }
.queue-list::-webkit-scrollbar { width: 8px; }
.queue-list::-webkit-scrollbar-thumb { background: var(--line); border-radius: 4px; }
.qempty { color: var(--dim); text-align: center; padding: 24px 8px; font-size: 12px; }
.qitem { border: 1px solid var(--line); border-radius: 8px; padding: 7px 9px;
  margin-bottom: 7px; font-size: 12px; background: var(--panel); }
.qitem:last-child { margin-bottom: 0; }
.qhead-line { display: flex; align-items: center; gap: 6px; margin-bottom: 3px; }
.qrole { font-weight: 600; font-size: 11px; }
.qrole.r-user { color: var(--accent); }
.qrole.r-tool { color: var(--warn); }
.qrole.r-system, .qrole.r-runtime { color: var(--dim); }
.qkind { color: var(--dim); font-size: 10px; }
.qcontent { color: var(--text); white-space: pre-wrap; word-break: break-word;
  max-height: 130px; overflow-y: auto; font-size: 12px; }
.qimage { display: block; max-width: 100%; border-radius: 6px; margin-top: 4px; }

/* ── 移动端适配：窄屏隐藏队列面板，内容自适应全宽 ── */
@media (max-width: 768px) {
  .chat-header { padding: 8px 12px; gap: 8px; }
  .badge { font-size: 12px; }
  .msg-col { padding: 12px 12px 20px; }
  .row.user, .card.assistant { width: 100%; }
  .row.user { margin-left: 0; margin-right: 0; }
  .bubble { max-width: 90%; font-size: 15px; line-height: 1.6; }
  .card.assistant { font-size: 15px; line-height: 1.6; }
  .thinking-body { font-size: 13px; }
  .composer { padding: 10px 12px; gap: 8px; }
  .composer textarea { font-size: 15px; }
  .toolcard .tool-result { max-height: 45vh; }
  .queue-panel { display: none; }
}
</style>