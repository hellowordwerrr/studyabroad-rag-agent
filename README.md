<!-- BEAUTIFIED -->
<h1 align="center">留学智库</h1>
<p align="center">
  <strong>把「查一所学校的申请信息」从翻 5 个官网页面变成一句话提问</strong>
  <br />
  <em>留学申请文档问答知识库 · RAG 多智能体 · 回答带来源引用 · 查不到就明确拒答</em>
</p>

<p align="center">
  <a href="#快速开始"><img src="https://img.shields.io/badge/快速开始-4CAF50?style=for-the-badge" alt="Quick Start" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/许可证-MIT-yellow?style=for-the-badge" alt="License: MIT" /></a>
</p>

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12-blue" alt="Python" /></a>
  <a href="https://github.com/langroid/langroid"><img src="https://img.shields.io/badge/Langroid-0.67.7-green" alt="Langroid" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow" alt="License" /></a>
  <a href="https://platform.deepseek.com"><img src="https://img.shields.io/badge/LLM-DeepSeek-purple" alt="LLM" /></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Chainlit-4B5563?style=flat" alt="Chainlit" />
  <img src="https://img.shields.io/badge/Qdrant-DC244C?style=flat&logo=qdrant&logoColor=white" alt="Qdrant" />
  <img src="https://img.shields.io/badge/SQLite-003B57?style=flat&logo=sqlite&logoColor=white" alt="SQLite" />
  <img src="https://img.shields.io/badge/fastembed-3776AB?style=flat" alt="fastembed" />
</p>

## 这个系统解决什么问题

准备留学申请时，一所学校的申请要求散落在官网的五六个页面上——语言成绩、截止日期、学费、文书要求……再加上 QS 排名、所在城市，把一所学校查齐往往要半小时，信息还容易记错抄错。这个系统把 LSE、NYU 等院校官网的申请要求文档建成可提问的知识库，配合本地 QS 排名数据，用一句话提问替代逐页翻找。**有依据的回答带 [^n] 引用来源，没依据的宁可明确拒答，也不编造。**

## 我遇到的最大问题 + 怎么修的

### ① 模型偶发不按协议输出 → 双路解析容错

**问题**：DeepSeek 约 1/5 轮不按 OpenAI 的 tool_calls 协议输出工具调用，而是把原生 DSML（`<｜invoke name="...">`，分隔符是全角竖线）写进正文。框架只认协议格式 → 工具不执行，用户直接看到 XML 垃圾。

**判断**：模型不会永远守协议，框架是 pip 依赖改不动——容错只能做在自己的代码层。

**修复**：子类化 ChatAgent 并覆写 `get_tool_messages`（框架唯一的工具提取入口）：框架解析不到时，回退解析 DSML 原生格式、按 name 实例化对应工具。协议格式 + 原生格式双路解析，所有入口统一换用该子类。

### ② 对话记忆无界膨胀 + 指代靠猜 → 显式受控窗口

**问题**：框架自带的跨轮记忆是隐式、无界的——API 抓包实测每轮消息条数 2→6→10→14→18→22 线性膨胀，长会话顶爆上下文、token 失控；而关掉记忆后，指代追问（「那这所学校的学费呢？」）LLM 靠猜，实测连知识库没有的学校都能编出「LSE 的截止日期」。

**判断**：框架记忆是黑盒——喂给模型什么不可审计；而申请信息（学费、截止日期）错了比没有更糟，猜就是风险。

**修复**：每轮结束后清空框架隐式历史，改为应用层显式组装最近 6 轮问答（历史回答截断到 500 字）。改造后抓包恒定 2 条消息、token 不再膨胀；指代被记忆显式解析；查不到的学校诚实拒答。

### ③ 数据靠手工维护不可靠 → 自动校验 + 官网定向采集

**问题**：QS 排名表手工更新会漏错；申请要求（语言要求、截止日期）没有现成的公开数据集。

**判断**：官网数据权威且可核查——爬官网是正路；数据更新必须能被程序自动校验，而不是靠人眼。

**修复**：排名更新一条命令自动下载 + 自动校验（上线前就揪出了人工漏掉的错）；官网定向爬虫（限速/重试/清洗、来源可溯）采集 LSE 申请要求 5 页试点入库。

## 我怎么定义「答得好」

「能回答」不等于「答得好」。项目自建了 46 题评测集（18 排名 + 16 文档 + 7 混合 + 5 防编造），用三层指标打分，而不是凭感觉：

1. **检索命中率**：doc/mixed 题先验证检索出的块里有没有答案的证据关键词——只耗本地 embedding、不耗 LLM，先确认「找得到」，再谈「答得对」；
2. **工具路由正确率**：捕获每题实际调用的工具，与期望工具比对——「该查文档还是该查排名」这个判断本身对不对；
3. **答案正确率**：回答归一化后核对期望关键词；其中 5 道「防编造」题专门问知识库没有的内容（比如哈佛的学费）——**必须拒答，且不得出现任何货币金额**。

判题集自身也防错：ranking 题的期望值运行时从数据源自动生成（题库不手抄排名数字）；每题新建独立 Agent（防止框架历史跨题污染）。

## 我做了什么产品判断

1. **「答不出」是功能，不是故障**：知识库没覆盖就明确拒答，宁可少答不可错答——申请截止日期、学费这种答案，错了比没有更糟；
2. **记忆窗口显式化**：不依赖框架的隐式无界历史，应用层显式窗口——token 有界、喂给模型的记忆可审计、指代照样正确；
3. **小语料用确定性换效率**：跨语言检索相似度分整体偏低，任何 top-k 截断都会随机漏掉真命中块；全量召回不花 LLM token，换来检索 100% 确定；
4. **按上游最坏情况设计**：模型约 1/5 轮不按协议输出，就做双路解析——容错写进架构，而不是祈祷模型守规矩；
5. **本地优先、能力可开关**：SQLite 持久化零外部依赖；登录做成 .env 开关——单机自用无门槛，需要历史会话列表时再开。

## 结果

评测集 46 题三轮迭代，从 33 分走到满分：

- **首轮 33/46**：两处根因——DSML 偶发原生格式（双路解析修复）；文档问答流水线里三道随机 LLM 环节（中文问题被随机翻译成英文再检索、逐块抽取会丢掉含答案的句子、历史消息跨题累积污染）→ 关掉随机翻译/抽取/累积三项后大幅回升；
- **二轮 38/46**：剩下的 8 题全是跨语言检索 top-k 截断——中文块的相似度分整体不高（0.5-0.7），真命块常排第 6-8 位被截断；更反直觉的是「查询改写」帮倒忙：改写把中文问题翻译成英文，英文问法把英文官网页顶进合并 top-k，把中文块挤出去 → 候选块数直接取语料块数（全量召回）+ 关闭改写，检索完全确定；
- **终态**：检索命中 19/19，工具路由 46/46，答案正确 46/46——含 5 道防编造题全部正确拒答。逐题明细见 [eval/report.txt](eval/report.txt)。

## ✨ 功能亮点

- 🤖 **多智能体架构** —— 主 Agent 调度两个工具：`ranking_lookup`（QS 排名/城市查询）+ `doc_qa`（文档问答，回答带来源引用）
- 🎯 **自建评测体系** —— 46 题三层指标（检索命中 / 工具路由 / 答案正确）全部满分（19/19 + 46/46 + 46/46），含 5 道「防编造」题
- 🧠 **受控对话记忆** —— 显式记忆窗口 + 指代消解，多轮追问「那这所学校呢？」正确解析；查不到的宁可拒答也不编造
- 💬 **Web 会话中心** —— 侧栏历史会话/新建会话，断线或刷新后完整恢复（消息 + 对话记忆）；密码登录可开关；快捷问题按钮、模型切换、一键导出 Markdown、回答附「📚 引用来源」卡片
- 📡 **数据管道** —— QS 排名一条命令自动更新（自动校验揪出人工漏掉的错）；官网爬虫定向采集申请要求，来源可溯
- 🛡️ **模型输出容错** —— DeepSeek 偶发原生 DSML 工具调用（约 1/5 轮），双路解析修复，工具不再偶发失效

### 界面一览

![登录页：品牌化卡片 + 世界名校校徽墙](docs/screenshot_login.png)

![首屏：欢迎横幅 + 快捷问题按钮 + 历史会话侧栏](docs/screenshot.png)

![问答画面：带 [^n] 来源引用的回答 + 引用来源卡片](docs/screenshot_qa.png)

## 🛠 技术栈

### LLM 与智能体

| 技术 | 用途 |
|---|---|
| DeepSeek（deepseek-chat / deepseek-reasoner） | 主模型与推理模型，Web 端可切换 |
| Langroid 0.67.7 | 多智能体 + RAG 框架（子类化定制） |

### 检索与存储

| 技术 | 用途 |
|---|---|
| Qdrant（本地模式） | 向量库 |
| fastembed（BAAI/bge-small-zh-v1.5） | 中文向量 embedding |
| SQLite（aiosqlite） | Web 会话持久化（历史会话/恢复） |

### Web 与数据管道

| 技术 | 用途 |
|---|---|
| Chainlit 2.x | Web 界面（登录/会话中心/导出） |
| Python 3.12 | 开发语言（CLI/评测/爬虫全栈） |

## 项目结构

```
chat.py               # CLI 入口（交互式问答）
web_ui.py             # Chainlit Web（会话中心/登录/恢复/四件套）
tools.py              # 自定义工具 + DSML 双路解析（DeepSeekChatAgent）
doc_qa.py             # 文档问答子 Agent（检索配置）
eval.py               # 评测（三层指标 46 题）
update_rankings.py    # QS 排名自动更新 + 自动校验
crawler.py            # 官网定向爬虫（限速/重试/来源可溯）
eval/                 # 评测题库 questions.json 与报告 report.txt
docs/                 # 技术实录与界面截图
public/               # Web 主题与品牌资源（theme.css/logo）
.chainlit/            # Web 配置（品牌名/中文文案/会话库）
```

## 🏗 架构

```mermaid
graph TD
    U[用户提问] --> M[主 ChatAgent<br/>ReAct 工具调度]
    M -->|ranking_lookup 工具| R[本地 QS 排名数据<br/>校名模糊匹配]
    M -->|doc_qa 工具| D[DocChatAgent 子 Agent]
    D --> V[向量检索<br/>Qdrant + 中文 embedding]
    V --> A[带 [^n] 来源引用回答]
    style U fill:#E8A5BE,color:#2B462B
    style M fill:#2B462B,color:#E8A5BE
    style R fill:#FDF6EC,color:#2B462B,stroke:#2B462B
    style D fill:#FDF6EC,color:#2B462B,stroke:#2B462B
    style V fill:#FDF6EC,color:#2B462B,stroke:#2B462B
    style A fill:#FDF6EC,color:#2B462B,stroke:#2B462B
```

核心文件：[chat.py](chat.py)（CLI 入口）· [web_ui.py](web_ui.py)（Chainlit Web）· [tools.py](tools.py)（自定义工具）· [doc_qa.py](doc_qa.py)（文档问答子 Agent）· [eval.py](eval.py)（评测）· [update_rankings.py](update_rankings.py) + [crawler.py](crawler.py)（数据管道）

## 🚀 快速开始

### 前置要求

- Python 3.12

### 安装

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

（国内网络建议用清华镜像。）

### 配置

编辑 `.env`，填入 DeepSeek 密钥（[platform.deepseek.com](https://platform.deepseek.com) 获取）：

```
DEEPSEEK_API_KEY=sk-xxxxx
```

其余环境变量见下方[配置](#配置)节。

### 运行

`run.ps1` 自动设置 UTF-8；`-m deepseek/deepseek-reasoner` 可换 R1 模型：

```powershell
.\run.ps1 docs   # CLI 问答：用示例文档测试，输入 x 退出
.\web.ps1        # Web 界面：浏览器打开 http://localhost:8001
```

## 使用示例

### CLI 问答

```powershell
.\run.ps1 docs
# 输入问题回车即答；输入 x 退出
# 例：LSE 的雅思要求是多少？
# 例：剑桥大学的 QS 排名是多少？
```

### Web 界面

```powershell
.\web.ps1   # 浏览器打开 http://localhost:8001
```

Web 端支持历史会话恢复、模型切换（V3/R1）、一键导出对话，界面见上方[界面一览](#界面一览)。

## 配置

环境变量全部写在项目根目录的 `.env`（参考 [.env.example](.env.example)）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥（必填） | — |
| `CHAINLIT_WEB_PASSWORD` | Web 登录口令；设置后启用登录页与历史会话列表（不设置则无登录） | 空（无登录） |
| `CHAINLIT_AUTH_SECRET` | 登录 JWT 签名密钥；不设置则每次启动随机生成（重启后需重新登录）；登录时用户名可任意填写 | 随机 |
| `HF_ENDPOINT` | HuggingFace 镜像地址（国内直连超时） | `https://hf-mirror.com` |
| `HF_HUB_DISABLE_XET` | 禁用 Xet 下载协议（镜像不支持，会导致大文件 401） | `1` |

## 📄 技术基础与致谢

- 本项目构建于 [Langroid](https://github.com/langroid/langroid) v0.67.7（MIT License，Copyright (c) 2023 langroid）之上：框架以 pip 依赖引入，定制方式为**子类化**（`tools.py` 的 `DeepSeekChatAgent` 继承 `ChatAgent` 并覆写 `get_tool_messages`），未复制任何框架源码
- 其余依赖（Chainlit / Qdrant / fastembed 等）各自遵循其开源协议
- 本项目自身代码以 MIT License 发布（见 [LICENSE](LICENSE)）

## 许可证

[MIT](LICENSE)

📚 技术细节与调优实录（架构决策、故障排查、评测三轮调优）见 **[docs/interview.md](docs/interview.md)**
