# 字体放置

表格渲染需要两个字体文件，放到本目录（`actions/send_flow/fonts/`）：

- **拉丁**：Times New Roman（如 `Times New Roman.ttf`）
- **中文**：仿宋_GB2312（如 `仿宋_GB2312.ttf`）

识别规则：扫描本目录下的 `.ttf` / `.otf` / `.ttc`，文件名包含 `times`（拉丁）或
`仿宋`（中文）关键词即被采用。

> 字体文件不随本仓库分发（按各自许可自行准备）。缺失时渲染会**直接报错**，
> 不做缺字体兜底——把字体放进来，或修改 `table_render.py` 的 `FONT_DIR` 指向你的字体目录。
