#!/usr/bin/env python3
"""subagent 框架自测（读真配置 → 真 LLM 命中 → 打印 context）。

用法: python3 selftest_hello.py [profile]
- 读 .skills/subagent/config/config.yaml 的 llm_profiles.<profile> + .env
  （profile 缺省 default；也可用环境变量 SUBAGENT_TEST_PROFILE 指定）
- 建 Agent(临时 context 文件) → push "你好" → 跑到 idle → 打印 context.jsonl
- 退出码：0=拿到 assistant 回复，1=失败（无回复/超时/profile 不存在）

2026-09-15 重写：旧版读 cfg["llm"]（09-05 的单 llm 段，已改为 llm_profiles），
早就跑不起来；同时把"睡 8 秒"改成轮询到 idle，避免慢模型下假失败。
"""
import asyncio
import os
import re
import sys
import tempfile
import time

CONFIG_YAML = "/home/sandbox/.skills/subagent/config/config.yaml"
ENV_FILE = "/home/sandbox/.skills/subagent/config/.env"

sys.path.insert(0, "/home/sandbox/.skills/subagent/src")


def load_env(path: str) -> dict:
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def load_config() -> dict:
    import yaml
    env = load_env(ENV_FILE)
    with open(CONFIG_YAML, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    def expand(value):
        if isinstance(value, str):
            return re.sub(r"\$\{(\w+)\}",
                          lambda m: env.get(m.group(1), m.group(0)), value)
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        return value

    return expand(raw)


async def main() -> int:
    profile = (sys.argv[1] if len(sys.argv) > 1
               else os.environ.get("SUBAGENT_TEST_PROFILE", "default"))
    cfg = load_config()
    profiles = cfg.get("llm_profiles") or {}
    if profile not in profiles:
        print(f"FAIL: llm_profiles 无 profile '{profile}'，"
              f"现有 {list(profiles)}")
        return 1
    llm_cfg = profiles[profile]
    print(f"llm profile = {profile}:",
          {k: (v[:5] + "***" if k == "api_key" and v else v)
           for k, v in llm_cfg.items()})

    from kernel.agent_framework.llm.factory import create_llm_client
    from kernel.agent_framework.agent import Agent
    from kernel.agent_framework.context import ContextStore
    from kernel.agent_framework.tools import ToolRegistry

    llm = create_llm_client(llm_cfg)
    tmpdir = tempfile.mkdtemp(prefix="subagent_ctx_")
    ctx_path = os.path.join(tmpdir, "context.jsonl")
    print("临时 context 文件:", ctx_path)

    ctx = ContextStore(ctx_path)
    agent = Agent(
        llm=llm, registry=ToolRegistry(), context=ctx,
        system_prompt="你是一个 subagent,请简短回答。",
        tool_timeout_seconds=60.0,
        event_batch_wait_seconds=0.1,
        repair_latest_n=5,
        trailing_empty_user=False,
        user_merge_enabled=False,
        user_merge_separator="\n",
        user_merge_notice="",
    )
    run_task = asyncio.ensure_future(agent.run())
    await asyncio.sleep(0.3)
    t0 = time.time()
    agent.push_event({"role": "user", "content": "你好"})

    # 轮询到本轮回完（round 增加且回到 idle），上限 120s
    ok = False
    while time.time() - t0 < 120:
        await asyncio.sleep(0.5)
        if agent.round_number >= 1 and agent.state.value == "idle":
            ok = True
            break
    print(f"耗时 {time.time() - t0:.1f}s，state={agent.state.value} "
          f"round={agent.round_number}")
    agent.stop()
    await run_task

    print("\n===== context.jsonl 内容 =====")
    with open(ctx_path, encoding="utf-8") as f:
        text = f.read()
    print(text)

    if not ok:
        print("FAIL: 未在 120s 内完成一轮（LLM 无响应？）")
        return 1
    if '"assistant"' not in text or '"好"' not in text.replace("你好", ""):
        pass  # 内容不作硬判据，只报有无 assistant
    if '"assistant"' not in text:
        print("FAIL: context 里没有 assistant 消息")
        return 1
    print(f"PASS: profile={profile} 真 LLM 命中，assistant 已落 context")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
