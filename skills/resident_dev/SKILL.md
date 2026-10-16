---
name: resident_dev
description: "Resident 内部开发参考（四份文档）：skill 开发规范、resident HTTP 接口、枚举的行为清单、memory的推荐组织方式。触发方式：要新建/改造一个 skill、要写常驻服务或渠道适配器、自建程序要与内核对接、要查自己有哪些工具与行为、或要搭/改记忆库结构时，先 skills_load 本 skill——四份文档在 `.skills/resident_dev/references/`。"
---

# resident_dev

四份参考文档，都在 `.skills/resident_dev/references/`：

| 文件 | 是什么 |
|---|---|
| `skill-spec.md` | **skill 开发规范**——目录结构；配置、数据、密钥分别放哪；输入层与动作层怎么定；开发顺序；测试要求 |
| `http-api.md` | **resident HTTP 接口**——端口与认证；core 的 messages / notice / events 端点；实测坑；组件与数据落点 |
| `behavior-list.md` | **行为清单**——我现在有哪些工具、装了哪些技能、有哪些项目线、探索方向在哪、边界在哪 |
| `memory-organization.md` | **记忆的推荐组织方式**——记忆库分层（raw / wiki / 归档 / 追写缓冲）、wiki 分类与三页分工、单一真相源、检索与维护节奏、与 skill 的接法 |

写 skill、接适配器、让自建程序跟内核对接、或搭/改记忆库，先读对应那份。
文档按「任何一份 resident 都适用」的口径写；改环境＝改配置，不写死在代码里。
