# skill 分发包（qq_adapter / subagent / resident_dev）

三个独立可用的 skill 目录，各自拷贝到目标环境的 `.skills/` 下即可。

```
qq_adapter/     QQ（OneBot11 / napcat）常驻适配器：收发消息、群操作、白名单、注意力
subagent/       常驻 HTTP hub 驱动的多 agent 框架：spawn / prompt / 审批 / 压缩 / 回滚
resident_dev/   Resident 开发参考文档：skill 开发规范、内核 HTTP 接口、行为清单、记忆组织方式
```

## 分发口径

- **配置**：只带 `config/default.yaml`（通用默认，零身份零密钥零本机路径）。
  使用者自己写 `config/local.yaml` 覆盖连接地址、白名单、端口等本机差异。
- **密钥**：不随包分发；运行期从 `.secrets/`（或环境变量）读取，
  见各 skill 的 `requirements.txt` / `start.sh` / `SKILL.md`。
- **数据**：包内不含任何运行时数据（pid / 日志 / 状态文件 / 缓存 / 数据集）；
  运行时数据写在 `skill_data/<name>/`。
- **可替换项**：端口、路径、LLM 后端地址与模型名、账号与白名单，一律走配置或环境变量，
  代码里不写死。
- **示例值**：文档与测试里的 QQ 号 / 群号均为占位示例，替换成自己的即可。

## 快速开始

每个目录的 `SKILL.md` 是给 agent 的触发说明，`README.md` / `ARCHITECTURE.md` 是给人看的
结构与用法。通用步骤：

```bash
cd <skill 目录>
pip install -r requirements.txt          # 有依赖的才需要
vim config/local.yaml                    # 填自己的连接地址 / 白名单 / 端口
bash start.sh start                      # 常驻启动（幂等）；stop / status / --clean 同理
```

> 常驻服务的配置只在启动时读一次，改完要重启。
