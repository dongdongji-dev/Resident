<template>
  <n-config-provider :theme="isDark ? darkTheme : null">
    <n-message-provider>
      <n-dialog-provider>
        <div class="app" :data-theme="isDark ? 'dark' : 'light'">
          <aside class="sidebar">
            <div class="brand">Resident</div>
            <button class="nav" :class="{ active: route.path === '/' }" @click="go('/')">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
              聊天
            </button>
            <button class="nav" :class="{ active: route.path === '/console' }" @click="go('/console')">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
              控制台
            </button>
            <button class="nav" :class="{ active: route.path === '/skills' }" @click="go('/skills')">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
              技能
            </button>
            <button class="nav" :class="{ active: route.path === '/config' }" @click="go('/config')">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
              配置
            </button>
            <button class="nav theme-toggle" @click="toggleTheme">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
              {{ isDark ? '白天模式' : '夜间模式' }}
            </button>
          </aside>
          <main class="content">
            <router-view />
          </main>
          <!-- 运行时错误显示（任何 JS 崩溃都会出现在这里） -->
          <div v-if="fatal" class="fatal">
            <b>前端运行时错误</b>
            <pre>{{ fatal }}</pre>
          </div>
        </div>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<script setup>
import { NConfigProvider, NMessageProvider, NDialogProvider, darkTheme } from "naive-ui";
import { useRoute, useRouter } from "vue-router";
import { onMounted, ref } from "vue";
import { chat, consoleApi } from "./api";

const route = useRoute();
const router = useRouter();
const fatal = ref("");
function go(p) { router.push(p); }

// ── 主题（dark/light），持久化 ──
const THEME_KEY = "agent_runtime_theme";
const isDark = ref(localStorage.getItem(THEME_KEY) !== "light");
function applyTheme() {
  document.documentElement.setAttribute("data-theme", isDark.value ? "dark" : "light");
}
function toggleTheme() {
  isDark.value = !isDark.value;
  localStorage.setItem(THEME_KEY, isDark.value ? "dark" : "light");
  applyTheme();
}
applyTheme();

// 全局错误捕获：任何 JS 错误显示在页面上（避免静默崩溃）
window.addEventListener("error", (e) => {
  fatal.value = e.message + "\n" + (e.filename || "") + ":" + (e.lineno || "");
});
window.addEventListener("unhandledrejection", (e) => {
  fatal.value = "Unhandled: " + (e.reason && e.reason.message || e.reason);
});

onMounted(async () => {
  // core 状态轮询（console supervisor 权威）
  const poll = async () => {
    try {
      const s = await (await consoleApi("/api/status")).json();
      chat.coreRunning = s.running;
      chat.corePid = s.pid;
      chat.logFile = s.log_file;
    } catch { chat.coreRunning = false; chat.corePid = null; }
  };
  poll();
  setInterval(poll, 3000);
});
</script>

<style>
* { box-sizing: border-box; margin: 0; }
html, body, #app { height: 100%; }
body { background: var(--bg); color: var(--text);
  font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif; }

/* ── 明暗主题变量 ── */
:root {
  --bg: #f6f7f9; --panel: #ffffff; --line: #d8dee4;
  --text: #1f2328; --dim: #656d76;
  --accent: #0969da; --accent-strong: #ffffff;
  --nav-hover: #eef1f4; --nav-active: #dbe9fb;
  --user-bg: #d3e5ff; --user-text: #0b2c5a;
  --card: #ffffff; --code-bg: #f6f8fa;
  --thinking-bg: #f6f8fa; --tool-bg: #f6f8fa;
  --err: #cf222e; --warn: #9a6700; --info: #1a7f37; --debug: #6e7781;
  --logger: #8250df;
  --banner-bg: #ffebe9; --banner-text: #cf222e;
  --cursor: #0969da;
  --diff-add: #1a7f37; --diff-del: #cf222e;
  --diff-hunk: #0969da; --diff-file: #8250df;
  --hl-keyword: #cf222e; --hl-string: #0a3069; --hl-number: #0550ae;
  --hl-comment: #6e7781; --hl-fn: #8250df; --hl-attr: #0550ae;
  --hl-builtin: #953800; --hl-variable: #24292f;
}
[data-theme="dark"] {
  --bg: #101014; --panel: #17171c; --line: #2a2a31;
  --text: #e6edf3; --dim: #9da3ad;
  --accent: #7eb0ff; --accent-strong: #0b1f3a;
  --nav-hover: #212127; --nav-active: #2a3f63;
  --user-bg: #2b4a75; --user-text: #eaf2ff;
  --card: #1b1b21; --code-bg: #101015;
  --thinking-bg: #121218; --tool-bg: #121218;
  --err: #f85149; --warn: #d29922; --info: #3fb950; --debug: #8b8b96;
  --logger: #bc8cff;
  --banner-bg: #3a1d1d; --banner-text: #ff9c9c;
  --cursor: #7eb0ff;
  --diff-add: #3fb950; --diff-del: #f85149;
  --diff-hunk: #58a6ff; --diff-file: #bc8cff;
  --hl-keyword: #ff7b72; --hl-string: #a5d6ff; --hl-number: #79c0ff;
  --hl-comment: #8b949e; --hl-fn: #d2a8ff; --hl-attr: #79c0ff;
  --hl-builtin: #ffa657; --hl-variable: #e6edf3;
}

.app { display: flex; height: 100vh; overflow: hidden; }
.sidebar { width: 150px; flex-shrink: 0; background: var(--panel);
  border-right: 1px solid var(--line); padding: 14px 8px; display: flex;
  flex-direction: column; gap: 4px; }
.brand { font-size: 15px; font-weight: 700; padding: 6px 10px 16px;
  color: var(--text); }
.nav { display: flex; align-items: center; gap: 9px; padding: 9px 10px;
  border-radius: 8px; color: var(--dim); cursor: pointer; font-size: 14px;
  border: none; background: transparent; text-align: left; }
.nav:hover { background: var(--nav-hover); color: var(--text); }
.nav.active { background: var(--nav-active); color: var(--accent); }
.nav.theme-toggle { margin-top: auto; }
.content { flex: 1; min-width: 0; height: 100%; min-height: 0;
  overflow: hidden; }
.fatal { position: fixed; left: 160px; right: 0; bottom: 0; z-index: 9999;
  background: var(--banner-bg); color: var(--banner-text); font-size: 12px;
  padding: 8px 14px; border-top: 1px solid var(--line); }
.fatal pre { white-space: pre-wrap; margin-top: 4px; }
</style>
