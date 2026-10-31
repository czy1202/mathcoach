# mathcoach

面向 CMC（中国大学生数学竞赛）/ 考研数学的解题 Agent。

目标不是"把题目转发给大模型"：模型负责讲解和推导，关键计算交给 sympy
工具验证；题干信息不全时明确要求补充；每次改动都用固定评测集跑分对比，
让"变好了还是变差了"有数据可依。

## 项目规格

- 输入：文字题目（自然语言或 LaTeX）
- 输出：
  1. 涉及的知识点
  2. 解题思路
  3. 分步解题过程
  4. 最后一行固定为 `最终答案：<答案>`
- 工具：sympy（求导 / 积分 / 极限 / 化简 / 解方程）；CMC 非数学类历年真题检索
- 真题推荐：回答后附“相关真题”卡片（出处 + 题干 + 知识点），检索结果不作为
  本题的条件；检索用 BM25 + BGE 向量 + RRF
- 拒答：题干信息不全，或题目本身是未解决猜想时，说明原因并请用户补充，
  不引入假设硬答
- 成功标准：24 道评测题（20 道求解 + 4 道应拒答），总通过率 ≥ 70%
- 暂不实现：拍照识题、cross-encoder rerank

## 快速开始

```bash
cd mathcoach
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 配置模型：.env 已被 .gitignore 忽略，不要提交
copy .env.example .env
# 填入 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL

python ask.py "计算 lim(x→0) sin x / x"   # 真实模型一问一答
python ask.py                              # 连续提问模式，exit 退出
python ask.py --stub "1+1"                 # 离线假模型，不联网
python -m web.app                          # 本地 Web 版，打开 http://127.0.0.1:8000
python -m rag.build                        # 从本地真题 PDF 构建题库（需要先有 PDF）
python -m rag.build_embeddings             # 构建 BGE 向量缓存（首次会下载模型）
python -m rag.search "二重积分" --top 5     # 命令行检索真题（默认混合检索）
python -m rag.search "二重积分" --retriever bm25   # 只跑 BM25，便于对照
python -m rag.evaluate --misses             # 检索评测：BM25 / BGE / RRF
python agent.py                     # StubClient 离线演示，不联网
python -m eval.run_eval --stub      # 离线跑通评测流程
python -m eval.run_eval --real      # 真实模型跑评测并输出通过率
python -m eval.trace_report traces/eval-xxxx.jsonl   # 查看耗时 / token / 工具调用
pytest                              # 229 个测试
```

`eval.run_eval` 的常用开关：

| 开关 | 作用 |
| --- | --- |
| `--real` / `--stub` | 真实模型 / 离线假模型（默认 stub） |
| `--limit N` | 只跑前 N 题，用于冒烟测试 |
| `--trace-dir DIR` | 轨迹输出目录，默认 `traces/` |
| `--no-trace` | 不写轨迹文件 |

## Web 版（本地）

```bash
python -m web.app
# 浏览器打开 http://127.0.0.1:8000
```

只监听 `127.0.0.1`，不需要登录；API Key 留在服务端 `.env`，浏览器拿不到。
同一时间最多处理 2 道题，超过会直接返回 429，避免本地演示时并发消耗额度。

页面能力：

- 首页居中提供「问题目 / 查看真题」两种模式，互不干扰；点击左上角 ∑ 随时回到模式选择
- 解题模式：Markdown + KaTeX 渲染公式，`\(...\)` / `\[...\]` 会自动转成美元符号分隔符
- 解题模式：回答末尾单独提取"最终答案"卡片，正文里重复的那行标记会被去掉
- 同一页面内支持追问：服务端保存"第一轮原题 + 最近 3 轮"，自动做上下文裁剪；
  追问时不会强制重复完整解题格式
- 顶栏"新对话"会清空当前会话和页面消息，并回到解题模式
- 回答下方展示“相关真题”卡片（出处 + 题干 + 知识点），题库缺失时自动隐藏
- 真题模式：左侧列出第 1–16 届的所有试卷（初赛 / 决赛 / A、B 类 / 补赛），
  点击后右侧显示该卷全部题干；题干直接来自题库，不经过模型改写
- 显示耗时、轮次、模型调用次数、工具调用次数、token 用量
- 可展开的解题轨迹：每一步模型调用的耗时，以及每次工具调用的参数和结果
- 每次提问写一条 `traces/web-*.jsonl`，和评测轨迹使用同一套格式

![Web 欢迎页](docs/web-welcome.png)

![Web 问答与公式渲染](docs/web-preview.png)

![多轮追问](docs/web-followup.png)

![相关真题检索](docs/web-rag.png)

后端接口：

- `POST /api/ask`：请求体 `{question, session_id?}`；首次提问不传 `session_id`，
  响应会返回服务端生成的 id，后续追问带同一个 id。如果模型调用了
  `list_past_problems`，响应会额外带上完整试卷 `paper`
- `DELETE /api/session/{session_id}`：结束会话，"新对话"按钮会调用
- `GET /api/papers`：第 1–16 届的试卷目录（每份卷子的阶段、类别、补赛、题数）
- `GET /api/paper?edition=16&stage=决赛&category=B&supplement=&section=`：
  读取一份试卷的完整题干；`edition` 与 `year` 二选一，
  `supplement` 省略表示不限补赛、传空串表示只要正卷
- `GET /api/health`：除模型状态外，还返回 `retriever`（auto/bm25/vector/hybrid）、
  `vector_available` 和 `embedding_model`，方便确认混合检索是否真的启用

解题逻辑全部复用 `agent.Agent`，Web 层只做 HTTP 包装、会话管理和展示字段整理。
会话只存在进程内存里，服务重启后旧 id 自动失效并开始新会话；页面刷新也会开始
新对话（前端不持久化 session_id）。

## 真题库 RAG（本地）

题库来自本机的 CMC 非数学类历年真题 PDF，默认读取
`~/Desktop/数学竞赛分享`：

```bash
python -m rag.build
# 抽取题目：428 道 -> 去重后 417 道（初赛 240 / 决赛 177）
# 知识点覆盖 359/417（86%）

python -m rag.build_embeddings
# 417 道 × 512 维，写入 data/processed/embeddings.npz

python -m rag.search "计算二重积分，其中D由x+y=1与两坐标轴围成" --top 3
python -m rag.search "求幂级数的收敛域" --stage 初赛 --json
python -m rag.search "圆环区域上带指数衰减因子的二重积分" --retriever hybrid
```

也可以指定目录和输出：

```bash
python -m rag.build --papers-dir "D:/papers" --out data/processed/problems.jsonl
# 环境变量：CMC_PAPERS_DIR 控制 PDF 目录，CMC_INDEX_PATH 控制 JSONL 位置
```

处理链路：

```text
34 份试卷 PDF
   │ PyMuPDF 抽文本
   ▼
按版式切题（“题目N” / 2024-2025 的填空-选择-解答）
   │
   ▼
归一化：PDF 符号伪影（Z→∫、X→∑）、Unicode 数学符号、LaTeX 命令
   │
   ▼
中文二元组分词 + 25 个受控知识点标签
   │
   ▼
data/processed/problems.jsonl（题干 / 出处 / 届数 / 类别 / 知识点）
   ├─ BM25 索引（中文二元组 + 知识点加权）
   └─ BGE 编码 → data/processed/embeddings.npz（417 × 512）
          │
用户提问 ─┼─ BM25 Top-20 ─┐
          └─ BGE Top-20 ──┴─ RRF 融合 → Top-K
   │
   ├─ Agent 工具 search_past_problems(query, k) → 题干 + 出处 + 知识点
   └─ Web 返回 related_problems → 前端“相关真题”卡片
```

设计取舍：

- 题库没构建时，Web 端不开放检索工具，避免每轮都拿到“题库不存在”的工具错误。
- 工具只返回题干、出处、知识点，不返回解答：工具结果会进模型上下文，需要控制
  体积；完整解答留给后续的 `get_problem_detail`。
- 提示词明确要求检索结果是“其他题目”，禁止把其中的数字和结论搬进本题解答；
  正文最多用一句话点出最相关的一道，不重复题干（前端已有卡片）。
- 检索默认是 BM25 + BGE 向量 + RRF：BM25 负责公式和术语的精确词面命中，
  BGE 负责同义表述和语义查询，RRF 只看名次、不看两路分数的量纲。中文仍按
  二元组切分，不依赖词典；向量模型是 `BAAI/bge-small-zh-v1.5`（512 维，CPU）。
- 向量缓存与题库、模型绑定：题号或模型变化后，工具层检测到不一致会自动回退
  BM25，不会拿旧向量硬搜新题库。
- 追问（“这一步为什么”）不强制检索，也不做检索补位，避免拿追问去搜题库。

### BGE 向量与 RRF 混合检索

BM25 对公式、符号和术语很强，但用户只给语义描述时会漏召回。向量检索补这一块，
两路结果再用 RRF 合并：

```text
查询 ── BM25 Top-20 ─────────────┐
  │                              ├─ RRF：Σ weight / (60 + rank) ── Top-K
  └─ BGE 编码 → 余弦 Top-20 ─────┘
```

实现位置：

- `rag/vector.py`：BGE 懒加载、文档/查询编码、L2 归一化、余弦 Top-K、
  `.npz` 缓存和“题库/模型是否变化”的校验；
- `rag/hybrid.py`：RRF 融合、纯向量检索、BM25+向量混合检索；
- `rag/build_embeddings.py`：为整库生成向量缓存；
- `tools.search_past_problems`：按 `CMC_RETRIEVER` 选择 `auto`（默认）/
  `bm25` / `vector` / `hybrid`；模型缺失、依赖未安装、缓存过期时自动回退 BM25。

本机配置（`.env`，已被 gitignore）：

```bash
CMC_EMBEDDING_MODEL=D:/mathcoach-ai/models/bge-small-zh-v1.5
CMC_MODEL_CACHE=D:/mathcoach-ai/models
HF_HOME=D:/mathcoach-ai/hf-cache
CMC_EMBEDDINGS_PATH=data/processed/embeddings.npz
CMC_RETRIEVER=auto
# 默认不加查询指令；设成官方指令可做消融
CMC_QUERY_PREFIX=
```

几个取舍：

- **编码文本 = 题干 + 知识点标签**。出处、届数不进编码文本，避免“第 16 届”
  这类元数据抢语义权重；知识点是题目的压缩摘要，能帮助短题干和公式题聚类。
  在 47 条评测查询上（同一组融合参数），加标签让 RRF 的 Recall@1
  从 76.6% 提到 83.0%。
- **查询侧默认不加 BGE 指令**。官方建议短查询加
  `为这个句子生成表示以用于检索相关文章：`，但在本项目的评测集上不加指令
  的召回更好；保留 `CMC_QUERY_PREFIX` 便于复现官方设置或做消融。
- **BM25 权重 1.5 : 向量 1.0**。数学题里公式和术语是强信号，词面精确命中的
  精度高；向量负责补同义表述。RRF 的 `k=60`，候选各取 20 条。
- **首次检索会加载模型**（本机 CPU 约 7 秒），之后同一进程内复用；
  向量缓存重新构建后需要重启 Web 进程，或者调用 `tools.clear_vector_cache()`。
- `sentence-transformers` 会安装 CPU 版 torch（数百 MB），模型文件约 100 MB；
  下载路径由 `CMC_MODEL_CACHE` / `HF_HOME` 控制，本项目放在 D 盘。

### 按届浏览完整真题

检索解决“找相似题”，按届浏览解决“把某一年的卷子完整拿出来”，两者走不同路径：

```text
查看真题页 ── GET /api/papers ── paper_index()：第 1-16 届目录
     │
     └─ 点击试卷 ── GET /api/paper?edition=…&stage=… ── list_problems()
                                                    │
                                                    └─ group_problems()
                                                       按阶段 / 类别 / 补赛 / 题型分组，
                                                       并计算缺号
```

实现要点：

- **不经过模型**：题干直接从 `problems.jsonl` 读取，避免模型漏抄、改写，也避免
  把整卷塞进上下文。
- **去重别名展开**：建库时同一道题可能跨届重复，去重后只保留一条主记录，旧 id
  放进 `aliases`。按届浏览时会把 `[id, *aliases]` 逐个解析回原试卷位置，否则
  会出现“第 13 届决赛缺第 9 题”的假象。现在第 1–16 届的题号都是连续的。
- **Agent 也能列卷**：聊天里说“我要第一届真题”时，模型调用 `list_past_problems`；
  工具只返回题号、出处和数量，Web 层再按同一组参数重新读题库补全题干。
- **区分语义检索和元数据过滤**：届数、阶段、类别都是确定字段，过滤即可 100%
  命中；向量检索只解决“题目意思相近”，不能替代按届浏览。

当前题库：417 道题，第 1 届 23 道、第 13 届 31 道、第 15 届 31 道、第 16 届 42 道；
所有试卷分组均为题号连续。

数据说明：

- 真题 PDF 有版权风险，`data/` 已加入 `.gitignore`；仓库里只有代码，不提交题库。
- 2024/2025 两份试卷使用 Symbol 字体，抽出的公式会散架（如 `ZZ` 代表二重积分号），
  题干适合检索但可读性一般；展示前需要模型重建 LaTeX。
- 2018 年以前的试卷不分 A/B 类，统一标为“未分类”；检索 A 类时会把它们一并算入。
- 2024/2025 两份试卷按卷面结构切分，其余按“题目N”切分。

### 检索评测

固定查询集 `rag/eval_queries.jsonl`，共 47 条人工标注查询：

- `easy` 37 条：原题改写、公式查询、知识点关键词；
- `hard` 10 条：只给语义描述，尽量避开题干原词（例如“圆环区域上带指数衰减因子的二重积分”）。

```bash
python -m rag.evaluate            # 对比 BM25 / 标签加权 / BGE / RRF
python -m rag.evaluate --misses   # 列出未命中的查询
python -m rag.evaluate --no-vector  # 只跑 BM25 两组，不加载模型
```

当前基线（k=5）：

| 方案 | Recall@1 | Recall@5 | MRR |
| --- | --- | --- | --- |
| BM25（不加权） | 74.5% | 97.9% | 0.835 |
| BM25 + 标签加权 | 74.5% | 97.9% | 0.835 |
| BGE 向量 | 68.1% | 91.5% | 0.763 |
| **BM25×1.5 + BGE×1 + RRF** | **83.0%** | **97.9%** | **0.885** |

分难度（RRF）：

| 难度 | 条数 | Recall@1 | Recall@5 | MRR |
| --- | --- | --- | --- | --- |
| easy | 37 | 83.8% | 100.0% | 0.899 |
| hard | 10 | 80.0% | 90.0% | 0.833 |

四个结论：

- **RRF 明显改善排序质量**：Recall@1 74.5% → 83.0%，MRR 0.835 → 0.885，
  Recall@5 保持 97.9%。单看 BGE 反而更差（68.1%），说明它不是替代 BM25，
  而是提供另一种排序信号。
- **标签加权仍然无效**：Top-5 候选通常共享同一个知识点，乘性系数对所有候选
  相同，等于乘了一个常数。它目前只是保留作消融对照。
- **h01 仍未被 Top-5 召回**：`计算圆环区域上带指数衰减因子的二重积分`
  的正确答案在 BGE 里排第 1，但 BM25 的前几名也被 BGE 排进前 5，RRF 的
  “共识票”把它挤了下去。这是 RRF 的典型失败模式，下一步应尝试
  cross-encoder rerank 或 query-adaptive 融合，而不是继续手调权重。
- **BGE 的 4 条未命中集中在纯公式题**（q01–q03 的极限、q08 的二重积分）：
  符号和上下标在语义模型里信息量有限，这类查询更适合 BM25。

> 47 条查询仍是初版标注，而且上面的权重/指令设置是在同一组查询上比较后选的，
> 存在过拟合风险。应先复核并冻结标签，再用留出集确认收益；只看单个例子的
> “感觉更准了”不算数。

## 评测结果

评测集 24 题：20 道求解（symbolic / numeric / exact 三种判分方式）+ 4 道
应拒答。求解题先提取 `最终答案：` 行，再做符号等价 / 数值容差 / 精确匹配；
拒答题检查回答是否明确表示无法作答。

| 版本 | 主要改动 | solve | refuse | 总计 |
| --- | --- | --- | --- | --- |
| v1 | 初始提示词，整段回答直接判分 | 17/20 | 3/4 | 20/24（83.3%） |
| v2 | 最终答案契约 + 提取器 + LaTeX/Unicode 归一化 | 19/20 | 3/4 | 22/24（91.7%） |
| v3 | 修复 `2x\sin x` 的函数边界；扩充拒答标志 | 20/20 | 3/4 | 23/24（95.8%） |
| v4 | 补充"无法证明 / 未解决"类拒答标志 | 19/20 | 4/4 | 23/24（95.8%） |
| v5 | 修复 `\operatorname{...}` 的答案提取 | 20/20 | 4/4 | **24/24（100%）** |

v4 的 1 道 solve 记为 UNCERTAIN，原因是提取器不认识
`\operatorname{Var}(X)=1/12`；修复后回放同一份模型回答判为 PASS，v5 是
修复后的完整真实运行。

v5 轨迹指标：

- 耗时：总计 97.99 秒，平均 4.08 秒/题，p95 9.17 秒
- token：50,237（输入 37,646 / 输出 12,591），平均 2,093/题
- 调用：模型 53 次，工具 48 次，工具错误 0 次（0.0%）
- 护栏触发：0 次（无超时、无超步数、无模型异常）

> 注意：24 道题、单次运行的样本量都很小，100% 不代表模型已经稳定可靠。
> 后续扩题、多次运行取均值后，这组数字才适合做更严格的结论。

## 工作原理

```text
用户题目
   │
   ▼
Agent 循环（最多 10 步，单步 30 秒超时）
   ├─ 调用模型
   ├─ 模型请求工具 → run_tool → 结果回填 → 回到"调用模型"
   └─ 模型不再请求工具 → 返回最终回答
   │
   ▼
最终回答（知识点 / 思路 / 步骤 / 最终答案）
```

评测链路：

```text
cases.jsonl
   │
   ▼
Agent（注入 TraceRecorder）
   ├─ 正常回答 → extract_final_answer → judge_answer → PASS/FAIL/UNCERTAIN
   └─ 应拒答   → is_refusal → 正确拒答/未拒答
   │
   ├─ traces/*.jsonl（逐题事件、token、耗时、工具调用）
   └─ 控制台报告 + trace_report 汇总
```

## 目录结构

```text
mathcoach/
├── ask.py              # 命令行一问一答入口（最小产品壳）
├── agent.py            # Agent 循环、超时/步数护栏、轨迹事件
├── llm.py              # OpenAI 兼容客户端 + StubClient（同一接口）
├── tools.py            # sympy 验证 + 真题检索/列卷工具、注册表、run_tool 分派
├── mathparse.py        # LaTeX / Unicode / 省略乘号的统一归一化
├── tracing.py          # TraceRecorder：逐题写 JSONL
├── schemas.py          # Verdict 等数据结构
├── config.py           # 读取 .env
├── rag/
│   ├── build.py        # PDF -> JSONL 构建入口
│   ├── papers.py       # 试卷版式解析、去重
│   ├── textnorm.py     # 检索归一化、二元分词、知识点标签
│   ├── bm25.py         # 纯 Python Okapi BM25
│   ├── index.py        # 索引加载、BM25 打分、按届浏览与分组
│   ├── vector.py       # BGE 编码、向量缓存、余弦 Top-K
│   ├── hybrid.py       # RRF 融合、BM25 + BGE 混合检索
│   ├── build_embeddings.py # 为整库构建向量缓存
│   ├── evaluate.py     # 检索评测：Recall@1 / Recall@5 / MRR
│   ├── eval_queries.jsonl  # 47 条标注查询
│   └── search.py       # 命令行检索
├── data/               # 题库产物 problems.jsonl（已 gitignore）
├── web/
│   ├── app.py          # FastAPI 后端：/api/ask、/api/papers、/api/paper
│   ├── sessions.py     # 内存会话、TTL、上下文裁剪
│   ├── test_app.py
│   ├── test_sessions.py
│   └── static/         # 单页前端；KaTeX / markdown-it / DOMPurify 本地化
├── eval/
│   ├── cases.jsonl     # 24 道评测题
│   ├── judge.py        # symbolic / numeric / exact 判定
│   ├── extract.py      # 从回答里提取"最终答案："
│   ├── run_eval.py     # 跑批、自检、报告
│   └── trace_report.py # 轨迹汇总
├── test_agent.py
├── test_ask.py
├── test_rag.py
├── test_vector.py      # 向量索引、RRF、工具回退
├── test_tools.py
└── traces/             # 评测与问答轨迹（已 gitignore）
```

## 已知限制

- 只支持文字输入；拍照 / 相册识题尚未实现。
- `sympy.parse_expr` 最终会执行字符串表达式。目前只处理本地评测集和模型
  输出；接入真实用户输入前，需要换成受限命名空间或独立的沙箱解析。
- 拒答识别仍是关键词匹配，措辞变化可能误判。后续计划换成 LLM-as-judge，
  并用混淆矩阵评估。
- 评测集只有 24 题，且每版只跑一次，无法反映模型波动。
- Web 版没有登录、限流和公网部署方案，只适合本机演示。
- 多轮会话只存在服务端内存：服务重启丢失；刷新页面会开始新对话；没有跨会话的
  长期记忆。
- 检索是 BM25 + BGE + RRF，但仍没有 cross-encoder rerank；h01 这类
  “向量排第 1、BM25 也有一批高共识候选”的查询仍会被挤出 Top-5。
- 向量模型在 CPU 上首次加载约 7 秒；换题库或换模型后必须重建向量缓存。
- 47 条检索评测仍是初版标注，权重和查询指令是在同一组数据上选的，
  存在过拟合风险。
- 2024/2025 真题的公式抽取质量差，只适合检索，不适合直接展示。
- 无流式输出。

## 下一步

1. 扩充解题评测集，覆盖证明题、选择题和更多拒答措辞；引入多次运行与方差统计。
2. 复核并冻结 47 条检索评测（重点看 hard 集），补更多同义改写和短查询；
   把当前 BM25 / BGE / RRF 数字固定为正式基线，避免继续在同一组查询上调参。
3. 在冻结的留出集上验证 RRF 收益；针对 h01 这类失败样本尝试 cross-encoder
   rerank 或 query-adaptive 融合，并重新设计或移除基本无效的标签加权。
4. Planner → Solver → Verifier 多 Agent 协作，与当前单 Agent 做消融对比。
5. 完善 Web 版：流式输出、图片输入，以及带登录和限流后的公网部署。
