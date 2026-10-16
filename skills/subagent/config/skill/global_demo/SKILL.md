---
name: global_demo
description: "全局技能示例：所有 subagent 实例启动时可见。用途：演示全局 skill 扫描与 load_skill 加载。触发方式：需要验证全局技能系统时使用。"
---

# 全局演示技能

这是放在 `subagent/config/skill/global_demo/` 的全局技能。

所有实例启动时系统提示词都会扫描到它的 description。
运行时可用 load_skill 加载本文件的完整内容。