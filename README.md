# Safety Fence Eval

端侧安全围栏（Safety Fence）的评测仓库：用固定规则驱动模型对 payload 做 `ALLOW` / `REWRITE` / `REFUSE` 判决，再检查脱敏质量，并对允许外发的样本做端到端任务完成度评分。

围栏面向的场景是：本地助手（如 OpenClaw）优先用本地模型；能力不足时才把请求发往云端。围栏的目标不是禁止云端调用，而是**阻止不安全的 payload 离开本机**。

## 评测在测什么

三条独立维度：


| 维度             | 含义                                          | 谁打分                             |
| -------------- | ------------------------------------------- | ------------------------------- |
| ① 决策准确率        | 围栏输出是否等于用例标注的 `expected_decision`           | `runner.py` 精确匹配                |
| ② REWRITE 脱敏质量 | 是否只用 `<REDACTED>`，且标注的敏感 token 无残留          | `judge.py` 规则引擎                 |
| ③ 端到端任务成功率     | 外发后云端是否仍完成 `original_goal`（1–5 分，`>=3` 为成功） | `judge_e2e.py`（LLM-as-Judge）或人工 |


脱敏不合格时，runner 会把 `effective_decision` 覆盖为 `REFUSE`，**不会外发**。

## 评测管线

```text
TestCase.payload
     │
     ▼
┌─────────────────┐   spec/SKILL.md（系统提示词）
│  SafetyFence    │ ───────────────────────────► 围栏模型（Qwen）
│  (safety_fence) │ ◄── Decision / Reason / RewrittenPayload
└─────────────────┘
     │
     ├─ ALLOW  ──────────────────────────────┐
     ├─ REWRITE ─► judge.check_rewrite_quality ─► 失败 → effective=REFUSE
     │                                          成功 → 继续
     └─ REFUSE ─► 不外发
     │
     ▼
effective ∈ {ALLOW, REWRITE} 且 is_outbound
     │
     ▼
┌─────────────────┐
│  call_deepseek   │ ──► 云端模型（DeepSeek） ──► cloud_response
└─────────────────┘
     │
     ▼
results.json（含 e2e.sent_payload / cloud_response）
     │
     ▼
┌─────────────────┐   spec/eval_rule.md（Judge 提示词）
│  judge_e2e      │ ──► LLM-as-Judge ──► judge_score / judge_reason / success
└─────────────────┘
     │
     ▼
e2e_results.json
```



## 仓库结构

```text
.
├── spec/
│   ├── SKILL.md          # 围栏系统提示词（决策规则）
│   └── eval_rule.md      # 端到端 Judge 提示词
├── src/
│   ├── config.py         # 从 .env 读密钥与超时/并发
│   ├── safety_fence.py   # 调用围栏模型并解析结构化输出
│   ├── judge.py          # REWRITE 占位符 / 残留检查（纯规则）
│   ├── runner.py         # 主评测编排（维度 ①② + e2e 数据采集）
│   └── judge_e2e.py      # 端到端 LLM-as-Judge（维度 ③）
├── data_input/
│   ├── schema.py         # TestCase 定义
│   ├── test_cases.py     # 第 2 轮 300 条
│   ├── test_cases1.py    # 第 3 轮 300 条
│   ├── test_cases2.py    # 第 5 轮 300 条
│   └── test_cases3.py    # 第 6 轮 300 条
├── results/
│   ├── round1/           # 早期 30 条试跑
│   └── round2/ … round6/ # 各轮 300 条产物与分析
├── requirements.txt
├── .env.example
└── README.md
```

每轮 `results/roundN/` 下的产物：


| 文件                           | 生成方式                | 内容                                                   |
| ---------------------------- | ------------------- | ---------------------------------------------------- |
| `results.json`               | `runner.py` 自动产出    | `summary` + `cases`，每条含决策、脱敏检查、e2e 原始数据              |
| `e2e_results.json`           | `judge_e2e.py` 自动产出 | 仅 e2e case，按分数升序，含 `judge_score` 与 `human_review`    |
| `classification_errors.json` | 人工/脚本提取             | `decision_correct=false` 的 bad case，预留 `is_bad_case` |
| `analysis.md`                | 人工撰写                | 本轮量化总结与改进建议                                          |




## 快速开始

需要 Python 3.10+，以及两个 OpenAI 兼容接口：围栏模型（默认 Qwen / DashScope）和云端模型（默认 DeepSeek）。

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
# source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env   # Windows；Unix 用 cp .env.example .env
```

在 `.env` 中填入 `FENCE_API_KEY` 和 `DEEPSEEK_API_KEY`。不要把 `.env` 提交到 Git。

### 环境变量


| 变量                  | 必填  | 默认值                                                 | 说明               |
| ------------------- | --- | --------------------------------------------------- | ---------------- |
| `FENCE_API_KEY`     | 是   | —                                                   | 围栏模型 API Key     |
| `FENCE_BASE_URL`    | 否   | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 围栏模型 OpenAI 兼容端点 |
| `FENCE_MODEL`       | 否   | `qwen3.5-flash`                                     | 围栏模型名            |
| `DEEPSEEK_API_KEY`  | 是   | —                                                   | 云端模型 API Key     |
| `DEEPSEEK_BASE_URL` | 否   | `https://api.deepseek.com`                          | 云端模型 OpenAI 兼容端点 |
| `DEEPSEEK_MODEL`    | 否   | `deepseek-v4-flash`                                 | 云端模型名            |
| `FENCE_TIMEOUT`     | 否   | `60`                                                | 围栏请求超时（秒）        |
| `CLOUD_TIMEOUT`     | 否   | `60`                                                | 云端请求超时（秒）        |
| `CONCURRENCY`       | 否   | `30`                                                | 并发线程数            |




### 跑围栏评测

在项目根目录执行（`src/runner.py` 会自行把 `src/` 和 `data_input/` 加入 `sys.path`）：

```bash
python src/runner.py --test-cases test_cases3 --output results/round6/results.json
```

`--test-cases` 是 `data_input/` 下的模块名（不含 `.py`）：


| 轮次  | 用例模块                            | 规模  | 结果目录             |
| --- | ------------------------------- | --- | ---------------- |
| 1   | （试跑）                            | 30  | `results/round1` |
| 2   | `test_cases`                    | 300 | `results/round2` |
| 3   | `test_cases1`                   | 300 | `results/round3` |
| 4   | `test_cases1`（与第 3 轮同一套，改规则后重跑） | 300 | `results/round4` |
| 5   | `test_cases2`                   | 300 | `results/round5` |
| 6   | `test_cases3`                   | 300 | `results/round6` |




### 跑端到端 Judge

```bash
python src/judge_e2e.py ^
  --results results/round6/results.json ^
  --output results/round6/e2e_results.json
```

Unix：

```bash
python src/judge_e2e.py \
  --results results/round6/results.json \
  --output results/round6/e2e_results.json
```

评分口径见 `spec/eval_rule.md`。`e2e_results.json` 里每条 case 带 `human_review` 字段，便于人工复核低分样本。重跑 Judge 时会合并保留已有的 `human_review`。

## 最新一轮结果摘要（第六批 · test_cases3）

围栏：Qwen；云端：DeepSeek。口径见 `results/round6/analysis.md`。


| 指标             | 数值                   |
| -------------- | -------------------- |
| 决策准确率          | 296/300 = **98.67%** |
| REWRITE 脱敏通过率  | 92/100 = **92.00%**  |
| 进入 e2e         | 142                  |
| e2e 成功率（分 ≥ 3） | 139/142 = **97.89%** |
| e2e 平均分        | **4.859**            |


脱敏失败的 8 条全部被门控拦截，没有外发。

## 用例说明

每条 `TestCase` 包含：`payload`、是否外发 `is_outbound`、期望决策、原始目标 `original_goal`，以及 REWRITE 用的 `sensitive_tokens`。

三类各约 100 条，覆盖边界而不是只测关键词：

- **ALLOW**：本地普通任务、防御性安全讲解、引用 secret 而不读原文、外发但 payload 干净。
- **REWRITE**：PII / 内部基础设施 / 业务标识作为附带上下文，脱敏后任务仍可完成。
- **REFUSE**：高危秘密原文、完整高敏数据集、脱敏后任务失去意义、违法危险请求。

用例里的姓名、证件号、卡号、密钥等均为**评测用虚构数据**，不是真实用户隐私。

