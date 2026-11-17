# 项目记忆：mathcoach（数学辅导 Agent）

> 本文件由 ChatGPT Codex 会话整理，供 Claude Code 续接。
> 最后更新：2026-11-16。
> 具体实现以仓库代码和 `README.md` 为准。

---

## 1. 项目定位

- 面向数学竞赛 / 考研数学的解题与诊断 Agent
- 目标：可验证、可评测、可演示的个人项目
- 技术栈：Python、FastAPI、Web 前端、OCR、RAG

---

## 2. 当前仓库状态

- 20 个提交，HEAD 为 `2f5f7db feat: 会话持久化与刷新恢复，停止后立即追问`
- 存在未提交改动：
  - 已修改：`.gitignore`、`README.md`、`rag/build.py`、`rag/hybrid.py`、`rag/index.py`、`test_rag.py`、`web/app.py`、`web/static/app.js`、`web/static/index.html`、`web/static/styles.css`、`web/test_app.py`
  - 新增未跟踪：`rag/seventeenth.py`
- 继续工作前先看 `README.md` 的“评测结果”“已知限制”“下一步”。

---

## 3. 已完成能力（按提交与目录）

- Agent 工具调用循环、评测跑批
- 本地 Web 解题界面、多轮追问
- CMC 真题 RAG：399 道题，BGE 向量 + BM25 + RRF 融合检索
- OCR 识题（本地 GLM-OCR）、公式渲染、可编辑题目卡
- 会话持久化与刷新恢复、停止生成
- 评测：`eval/cases.jsonl`、`eval/judge.py`、`eval/run_eval.py`、`eval/ocr_eval.py`

---

## 4. 关键目录

| 路径 | 用途 |
|---|---|
| `agent.py`、`llm.py`、`tools.py`、`schemas.py` | Agent 主循环、模型、工具、数据结构 |
| `ocr.py`、`mathparse.py`、`solution.py` | OCR、公式解析、解题流程 |
| `rag/` | build、index、hybrid、search、evaluate |
| `web/` | app.py、sessions.py、static 前端 |
| `eval/` | 评测集、判分、跑批、OCR 评测 |
| `data/`、`papers/`、`traces/`、`docs/` | 数据、真题、追踪、文档 |

---

## 5. 约束

- `.env` 已被 `.gitignore` 忽略，绝不能提交。
- `data/private` 与 `papers/` 可能包含大文件或版权材料，提交前谨慎检查。
- 不要重写已经验证过的评测链路；改动前先跑测试。
- 不确定的指标不要编造，以 `README.md` 与 `traces/` 的实际记录为准。

---

## 6. 建议的下一步

1. 先审阅未提交改动，确认无误后按主题分组提交。
2. 按 `README.md` 运行 `pytest` 与评测脚本，确认没有回归。
3. 检查 RAG 检索评测与 OCR 评测的最新结果。
4. 若要继续功能，优先看 `README.md` 的“下一步”与“已知限制”。
