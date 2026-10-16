# 科研脚本代码规范（Researcher-Style Coding）

> 一句话：让 AI 把代码写成**实验记录**，而不是**软件产品**——线性、命令式、一步一行、看得懂优先。
>
> 来源：参考 B站 silencedream《AI辅助科研》第八期《让AI写出研究者风格的代码》（BV1bWbn6EEkz）的思路，
> 补上适用边界、示例对照与跨平台接入方式。这份文档**不绑定任何具体工具或项目**，可以直接搬到
> OpenCode / Codex / Claude Code / Cursor / 任何聊天窗口使用。

---

## 1. 什么时候用，什么时候别用（先看这条）

| | 用研究风格 | 用工程风格 |
|---|---|---|
| 典型场景 | 数据清洗、统计建模、画图、跑实验的一次性脚本 | 会被反复运行的工具、CLI、服务、库 |
| 谁读 | 自己读，读一次 | 自己反复读 / 别人读 / 多人协作 |
| 改动频率 | 跑完基本不改 | 会持续演化 |
| 输入 | 已知的、自己的数据 | 会变的输入、别人的数据、管道上游 |
| 失败代价 | 低，报错本身就是信息 | 高，需要容错、重试、日志 |

**一句话判据：这段代码是"读一次就跑一次"，还是"会被反复调用"？** 前者用研究风格，后者老实写工程代码。

> 知道自己违反规范也是选择：研究风格的代价是**复用性差、改动要改多处、没有错误处理**。
> 一旦某个脚本开始被反复使用，就应该主动把它"升级"成工程写法（见第 6 节）。

---

## 2. 七条规则

1. **线性 + 命令式**：按执行顺序从上往下写——读入 → 清洗 → 变换 → 建模 → 保存。
   不要先来一堆类/函数定义、把真实流程藏到最后。
2. **少封装**：不要为"以后可能复用"去抽函数。一个脚本就是一个流程；
   同样的逻辑真的重复三次以上，再考虑抽出来。
3. **少循环**：能直接表达就直说。为了"优雅"套一层循环/推导式，换来的是理解成本；
   研究场景里"一眼看清每一步在干什么"比"写法漂亮"值钱。
4. **不写防御性代码**：不要默认加 `try/except`、不要对已知输入反复校验、不要层层兜底。
   数据有问题的时候会报错，报错信息比静默兜底更有用。
5. **不为格式拆行**：一行能表达的（读入 → 去缺失 → 筛选）就写一行；只有确实过长才换行。
   目标是紧凑可读，而不是满足所有格式规范的换行建议。
6. **变量名说人话**：用领域词（`age` / `group` / `score` / `treatment`）而不是 `df2`、`tmp`、`data1`；
   中间结果保留成可见的变量，方便随时停下来 `print` 看。
7. **打印关键中间结果**：每一步留一个可打印的中间输出（行数、形状、前几行、分组计数）。
   让"跑一次"就能自证对不对，而不是回头去 debug 一遍。

---

## 3. 一个例子：同一个任务，两种风格

**任务**：读 `data.csv` → 删除缺失值 → 筛选 `age > 30` → 按 `group` 分组做 OLS 回归 → 结果存 `out.csv`。

### ❌ 工程风（AI 默认容易长成这样，示意）

```python
import pandas as pd
import statsmodels.formula.api as smf


class DataPipeline:
    """Pipeline for cleaning and modeling."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.df: pd.DataFrame | None = None

    def load(self) -> "DataPipeline":
        try:
            self.df = pd.read_csv(self.path)
        except FileNotFoundError as exc:
            raise RuntimeError(f"input missing: {self.path}") from exc
        return self

    def clean(self, min_age: int = 30) -> "DataPipeline":
        self.df = self.df.dropna()
        self.df = self.df[self.df["age"] > min_age]
        return self

    def fit_by_group(self) -> pd.DataFrame:
        rows = []
        for group, sub in self.df.groupby("group"):
            model = smf.ols(self._formula(sub), data=sub).fit()
            ...
        # ……（后面还有 _formula / save / run 等方法，此处省略）
```

> 问题不在"写得差"，而在于：读这段代码要先建立一整套心智模型（它有什么状态、怎么串起来），
> 才能回答"这份数据到底被做了什么处理"。研究场景里这是纯开销。

### ✅ 研究风（实测生成的形态，可直接当模板）

```python
import pandas as pd
import statsmodels.formula.api as smf

df = pd.read_csv("data.csv")
df = df.dropna()
df = df[df["age"] > 30]
print("清洗后行数:", len(df), "| 分组:", df["group"].value_counts().to_dict())

predictors = [c for c in df.columns if c not in ("y", "group")]
formula = "y ~ " + " + ".join(predictors)
print("回归式:", formula)

rows = []
for name, sub in df.groupby("group"):
    fit = smf.ols(formula, data=sub).fit()
    row = {"group": name}
    row.update(fit.params)
    row["rsquared"] = fit.rsquared
    rows.append(row)

pd.DataFrame(rows).to_csv("out.csv", index=False)
```

> 从上往下读就是执行顺序，没有需要记忆的状态，`print` 让每一步都能核验。

---

## 4. 怎么接入不同平台

| 平台 | 放哪里 | 说明 |
|---|---|---|
| OpenCode | 项目根 `AGENTS.md`（或全局规则文件） | 把第 5 节规则粘进去；项目级只对该项目生效 |
| Codex CLI | 项目根 `AGENTS.md` | 同上 |
| Claude Code | `CLAUDE.md` | 同上 |
| Cursor | `.cursorrules` / Rules | 同上 |
| 任意聊天窗口 | 直接粘贴第 5 节提示词 | 最省事，随时可用 |
| 自己的 agent / 脚本 | system prompt 里加一段规则 | 直接复用第 5 节文本 |

如果要**全局生效**（所有项目都这样写），放在平台的全局配置里；
如果只是某个论文/实验仓库，放项目根的规则文件即可。两种内容完全一样，不需要改。

---

## 5. 可直接粘贴的提示词

### 中文版

```text
写代码时请使用「线性、命令式、研究脚本式」风格（linear imperative research scripting）：

- 按执行顺序从上往下写，一步一行，像流水线：读入 → 清洗 → 变换 → 建模 → 保存
- 不要定义类/函数来封装，不要抽象层；重复三次以上才考虑抽函数
- 尽量不用循环和防御性代码（不要默认加 try/except，不要重复校验已知输入）
- 不要为了满足代码格式规范把简单语句拆成多行；一行能说清就一行
- 变量名用领域术语，保留中间结果，方便我停下来检查
- 关键中间步骤打印出来（行数 / 形状 / 前几行 / 分组计数）
- 目标是：我扫一眼就知道每行在做什么、要改哪里

不要写成工程项目的风格（不要 class、不要分层、不要 docstring 堆砌）。
如果我要求的东西会反复运行、给别人用，或者要接进数据管道，再改回工程写法。
```

### English

```text
Write code in a linear, imperative "research scripting" style:

- Top-to-bottom in execution order, one step per line: read -> clean -> transform -> model -> save
- No classes/functions for encapsulation, no abstraction layers (extract only after 3+ real repetitions)
- Avoid loops and defensive code (no default try/except, no re-validating known inputs)
- Don't split simple statements across lines just to satisfy a style guide; one line if it fits
- Use domain-meaningful variable names; keep intermediate results visible for inspection
- Print key intermediate results (row counts, shapes, group counts)
- Goal: I can scan it and see exactly what each line does and what to change

Do not write it like an engineering project (no classes, no layering, no docstring padding).
Switch back to engineering style only when the code will be run repeatedly, shared, or piped.
```

---

## 6. 什么时候切回工程风格

当脚本要变成**资产**时，明确切换（可以直接对 AI 说"这段现在按工程风格重写"）：

- 会被定时/反复运行 → 加参数解析、日志、失败重试
- 别人会用 → 加输入校验、错误提示、README、函数化接口
- 接进数据管道 → 加数据契约（列名/类型校验）、异常处理、单元测试
- 成为库 → 明确的接口与封装

一个实用习惯：**在每个文件开头声明它属于哪种模式**（例如 `# mode: research-script`），
让 AI 和你自己都能立刻知道该按哪套规矩来。

---

*参考：B站 silencedream《AI辅助科研》第八期（BV1bWbn6EEkz）。本文档为在此基础上补充边界与工程化切换路径的通用版本，可自由复制到任何平台使用。*
