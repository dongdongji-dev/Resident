
import { reactive } from "vue";

// ── 单端口：前端由 console 提供，/api/* 由 console 同源代理到 kernel ──
export const CORE = "";                     // 同源（console 单端口路径路由）
const TOKEN_KEY = "resident_console_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}
export function setToken(t) {
  localStorage.setItem(TOKEN_KEY, t);
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

function authHeaders() {
  const t = getToken();
  return t ? { "X-Api-Token": t } : {};
}

// ── console（同源）API：控制/日志/代理到 kernel 的数据端点 ──
export async function consoleApi(path, opts = {}) {
  opts.headers = Object.assign(authHeaders(), opts.headers || {});
  const r = await fetch(path, opts);
  if (r.status === 401) throw new AuthError("unauthorized");
  return r;
}
export class AuthError extends Error {}

// core API 走 console 代理（同源）；保留签名兼容原聊天逻辑
export async function coreApi(path, opts = {}) {
  opts.headers = Object.assign(authHeaders(), opts.headers || {});
  const r = await fetch(CORE + path, opts);
  if (r.status === 401) throw new AuthError("unauthorized");
  return r;
}

// 登录：校验 console token 并持久化
export async function login(token) {
  const r = await fetch("/api/status", { headers: { "X-Api-Token": token } });
  if (r.status === 401) throw new AuthError("invalid token");
  setToken(token);
  return r;
}

// ── 聊天状态（agent 队列特性：排队 → 上下文 → 流式）─────────────
export const chat = reactive({
  // timeline：已写入 context 的消息（context_appended 权威顺序）
  messages: [],
  // queue：已发送、等待模型取出的 user 消息（右侧消息队列面板显示）
  queue: [],
  // 流式：当前轮的 assistant 消息（token 追加 / tool 卡片）
  stream: null,
  // 状态
  state: "idle",
  round: 0,
  connected: false,
  coreRunning: false,
  corePid: null,
  logFile: "",
});

export function resetChat() {
  chat.messages = [];
  chat.queue = [];
  chat.stream = null;
  chat.state = "idle";
  chat.round = 0;
}

