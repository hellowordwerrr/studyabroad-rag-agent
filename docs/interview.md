# 技术细节与调优实录

> 面向技术面试官与开发者：收录本项目实现过程中的架构设计决策、踩坑记录与三轮评测调优实录。产品功能速览见 [README](../README.md)。

## 目录

1. [架构设计决策](#1-架构设计决策)
2. [工具调用踩坑](#2-工具调用踩坑)
3. [对话记忆：受控记忆窗口](#3-对话记忆受控记忆窗口)
4. [评测体系与三轮调优实录](#4-评测体系与三轮调优实录)
5. [跨语言检索结论](#5-跨语言检索结论)
6. [部署与数据存放](#6-部署与数据存放)
7. [回归测试脚本](#7-回归测试脚本)
8. [二次开发方向](#8-二次开发方向)

## 1. 架构设计决策

### 1.1 为什么「主 Agent 调度 + doc_qa 子 Agent」，而不是给 DocChatAgent 直接挂工具

DocChatAgent 的 `llm_response` 走「检索→回答」专用流程，检索不到相关内容时
直接返回 `DO-NOT-KNOW`，根本不会进入 LLM 工具调用环节——这是实测踩出来的坑。
把文档问答封装成工具交给主 Agent 调度，路由才正确。

```
主 ChatAgent（ReAct 工具调度，判断问题类型并组织最终回答）
  ├── ranking_lookup 工具  —— 本地 QS 排名/院校信息查询（tools.py 自定义 ToolMessage）
  └── doc_qa 工具          —— 转交 DocChatAgent 子 Agent 检索文档并带引用回答
```

补充两条实测结论：

- **`agent.llm_response` 不执行工具**：它只做单次 LLM 调用，工具执行循环在
  `Task.run()`（交互式）或手动 ReAct 循环（`try_get_tool_messages` →
  `agent_response`）里；见 [test_tools.py](../test_tools.py) 的 `run_with_tools`。
- **`ingest_doc_paths` 不去重**：重复启动会重复入库污染检索 → [chat.py](../chat.py)
  里加了 `.qdrant/ingested.txt` 标记守卫；想强制重建知识库时删除 `.qdrant` 目录。

### 1.2 交互式 Task 不适合 Web：手动 ReAct 循环

`interactive=True` 在工具执行后会向用户索要输入，`interactive=False` 在最终回答后
无结束信号会 stall。解决方案：手动 ReAct 循环（`llm_response` →
`try_get_tool_messages` → `agent_response`）+ `cl.make_async` 把整段同步循环放入
线程池，配合官方 `lr.ChainlitAgentCallbacks` 渲染流式 Step。

### 1.3 anyio 工作线程事件循环坑

文档问答的「相关句批量抽取」（langroid 内部 `run_batch_tasks` → `asyncio.run`）
跑在 `cl.make_async` 的 anyio 工作线程里会报
`There is no current event loop in thread 'AnyIO worker thread'`——因为 chainlit
启动时 `nest_asyncio.apply()` 把 `asyncio.run` 补丁成了「要求当前线程已有事件
循环」，而工作线程没有；且只有检索命中的文档问题才走这条批量路径（排名类问题
不触发，CLI 主线程也不受影响），所以表现得很偶发。解决：[web_ui.py](../web_ui.py)
的 `run_react` 入口为当前线程 `asyncio.new_event_loop()` + `asyncio.set_event_loop()`
装一个专属循环，函数结束关闭（代码已内置）。

## 2. 工具调用踩坑

1. **DocChatAgent 挂工具无效**：其 `llm_response` 走 `answer_from_docs` 专用流程，
   无相关文档直接返回 `DO-NOT-KNOW`，不进工具循环 → 改用「主 Agent 调度 +
   doc_qa 工具封装子 Agent」架构。
2. **`agent.llm_response` 不执行工具**：见 1.1。
3. **交互模式下工具执行后需按回车**：langroid 的 Task 在每个非人类响应后等待用户
   输入（`interactive=True` 默认行为），工具结果返回后按回车，LLM 才继续给出最终回答。
4. **`ingest_doc_paths` 不去重**：见 1.1。
5. **LLM 转述可能引入事实错误**：工具结果本身正确，但 LLM 复述时偶发改动数字
   （实测 NYU 截止日期被改写过一次）。对策：工具结果直接落库/结构化输出，
   减少 LLM 二次转述。
6. **DeepSeek 偶发输出原生 DSML 工具调用格式**：实测约 1/5 轮不按 OpenAI
   tool_calls 协议输出，而是把原生 DSML（`<｜invoke name="...">`，注意分隔符
   是全角竖线 `｜` U+FF5C）写进 content。langroid 0.67.7 只识别 OpenAI
   tool_calls 与 JSON ToolMessage 格式 → 工具不执行、DSML 原文成为最终回答
   （用户看到 XML 垃圾）。修复：[tools.py](../tools.py) 的 `DeepSeekChatAgent`
   子类覆写 `get_tool_messages`（框架唯一工具提取入口），框架解析不到工具时
   回退解析 DSML，按 invoke 的 name 在 `llm_tools_map` 找工具类、用 parameter
   参数实例化。所有入口（chat.py / web_ui.py / eval.py / test_tools.py）统一
   换用该子类。这是针对 LLM 输出格式不稳定的容错设计（协议格式 + 原生格式
   双路解析）。

## 3. 对话记忆：受控记忆窗口

### 3.1 框架真相：隐式、无界

langroid 0.67.7 自带跨轮记忆，但隐式、无界。`ChatAgent.llm_response`
（chat_agent.py:1659，覆写了父类）会把 `self.message_history` 完整喂给 LLM——
每轮对话都会让历史消息线性膨胀（API 抓包实测：首请求消息条数随轮数
2→6→10→14→18→22，每轮 +4 条，包括工具调用 JSON 与工具结果），长会话最终
顶爆 64k 上下文且 token 成本失控。

### 3.2 改造：把隐式无界记忆改成显式受控窗口

- 每轮结束 `agent.clear_history(1, -1)` 清空 message_history（只留系统消息），
  切断框架隐式累积；
- 跨轮记忆由 `_compose_with_memory` 显式组装：只保留每会话最近
  `MAX_MEMORY_TURNS=6` 轮问答，历史回答截断到 500 字符；
- 效果（API 抓包前后对比）：改造前首请求消息条数 2→6→10→14→18→22 线性膨胀；
  改造后恒定 2 条，且第 6 轮指代题（「那这所大学的所在城市是哪里？」）仍能
  正确解析上一轮的学校。

设计取舍：不依赖框架隐式历史（无界、不可审计），改为应用层显式窗口——token
有界、窗口大小可调、喂给 LLM 的记忆内容可审计；超出窗口的最旧轮自然遗忘
（滑动窗口）。

### 3.3 无记忆 vs 有记忆对比实测

（headless 客户端抓事件流，同一 sessionId 双轮指代题）：

- 场景一「LSE 的雅思要求是多少？」→「那这个学校的学费呢？」：修复前 Q2 虽答对
  44,928 英镑，但那是歪打正着——doc_qa 工具的 few-shot 示例恰好泄漏了
  「LSE MSc Finance」指代；修复后 Q2 的工具调用为 `doc_qa query="LSE 的学费是
  多少？"`，指代被记忆显式解析，不再依赖运气。
- 场景二「牛津大学的 QS 排名是多少？」→「那这所大学的所在城市是哪里？」：
  修复前 Q2 答对牛津纯属 LLM 运气猜中（事件流显示零工具调用）；修复后通过
  记忆正确解析为牛津。
- 场景三「南洋理工大学的 QS 排名是多少？」→「那这所大学的申请截止日期是什么
  时候？」（决定性对比）：知识库未收录南洋理工，修复前 Q2 直接编造了 LSE 的
  截止日期（无中生有）；修复后记忆把「这所大学」解析为南洋理工，工具查无此文
  后诚实拒答（DO-NOT-KNOW）。

结论：对话记忆不只是「记得上文」，还通过显式指代消解防止了无记忆时 LLM 靠猜
回答的编造风险。

## 4. 评测体系与三轮调优实录

### 4.1 三层指标

自建问答评测集 [eval/questions.json](../eval/questions.json)（46 题 = 18 ranking
+ 16 doc + 7 mixed + 5 negative）：

1. **检索命中率**（doc/mixed 题）：`get_relevant_chunks` 返回块含 evidence 关键词
   即命中（只耗本地 embedding，不耗 LLM）；
2. **工具路由正确率**：捕获每题实际调用的工具名 vs 期望工具；
3. **答案正确率**：最终回答归一化后 expected 关键词全中；negative 题要求
   「含拒答词且不含 forbidden 关键词」（防编造：如问哈佛学费时不得出现任何
   货币金额）。

实测结果（2026-09-10，三轮迭代终态）：**检索命中率 19/19，工具路由正确率
46/46，答案正确率 46/46**。逐题明细见 [eval/report.txt](../eval/report.txt)。

### 4.2 判题集自身防错设计

- ranking 题期望值**运行时从 rankings.json 自动生成**（school 字段解析），题库
  只写校名与期望类型，不手抄排名数字——防评测集本身出错
- 每题新建主 Agent：框架把 message_history 喂给 LLM，复用同一 Agent 会跨题
  污染；negative 题专门验证「宁可拒答也不编造」
- 向量库用 `tmp/eval_qdrant` 全新副本：Qdrant 本地模式同目录只允许一个客户端，
  不能与运行中的 Web 服务抢 `.qdrant/data` 文件锁

### 4.3 三轮调优实录（33 → 38 → 满分）

- 首轮 33/46：两处根因。① DeepSeek 偶发原生 DSML 工具调用（见第 2 节第 6 条）
  → DeepSeekChatAgent 双路解析；② DocChatAgent 流水线三道随机 LLM 环节——
  followup_to_standalone 把中文问题随机翻译成英文再检索（英文检索命中不了中文
  块）、逐块相关句批量抽取会丢掉含答案的句子（→ 回答退化成 DO-NOT-KNOW）、
  默认跨问题累积 message_history 污染逐题判断。修复：`assistant_mode=True` +
  `relevance_extractor_config=None` + `conversation_mode=False`。
- 二轮 38/46：剩余 8 题全是跨语言检索 top-k 截断——中文块相似度分整体不高
  （0.5-0.7），真命中文块常排第 6-8 位，top-6 截断导致「检索命中但回答
  DO-NOT-KNOW」与「回答对但检索检查未命中」随机出现。更反直觉的是「查询改写」
  帮倒忙：改写把中文问题翻译成英文，英文问法把英文官网页顶进合并 top-k，把
  中文块挤出去。修复：候选块数直接取语料块数（小知识库全量召回，检索不耗
  LLM）+ 关闭改写问法 → 检索完全确定。
- 判题集自身两处修正（同样是评测体系的一部分）：doc-08 期望词加「三封/3封」
  any-of（LLM 数字写法不定）；doc-09 双事实题（sample 1000 字 vs 官网 A4 两页）
  any-of 组原写成 AND 结构，修正为任一命中。

### 4.4 运行方式

```powershell
.\.venv\Scripts\python.exe eval.py            # 全量 46 题（记得设 PYTHONUTF8=1）
.\.venv\Scripts\python.exe eval.py --limit 10 # 冒烟：只跑前 10 题
```

## 5. 跨语言检索结论

中文问题查英文文档时，langroid 默认的「稠密 + BM25 + 模糊匹配」混合检索
（RRF 融合）会把 "LSE" 这类英文关键词密度高的无关页面顶到最前，挤掉真正
相关的块。修复：[doc_qa.py](../doc_qa.py) 关闭 BM25/模糊匹配（纯稠密检索，
bge-small-zh 中英混查排序反而更准），候选块数直接取语料块数（跨语言相似度分
不高，任何 top-k 截断都会随机漏掉真命中块；详见第 4 节二轮实录）。

## 6. 部署与数据存放

数据存放：

- 向量索引：`.qdrant/data`（Qdrant 本地模式，无需 Docker）
- embedding 模型：HuggingFace 缓存目录（已下载，重装系统需重新下载）
- NLTK 语料：`%USERPROFILE%\nltk_data`（punkt/punkt_tab/wordnet/stopwords）

踩坑与解决方案（Windows 下实测）：

1. **HuggingFace 直连超时** → `.env` 中设置 `HF_ENDPOINT=https://hf-mirror.com`
2. **大文件 401（Xet 协议）** → `.env` 中设置 `HF_HUB_DISABLE_XET=1`
3. **qdrant-client 版本不兼容** → 固定 `qdrant-client==1.11.3`（langroid 0.67.7
   用了 1.12+ 已移除的旧 API）
4. **中文 TXT 报 GBK 解码错误** → 用 `run.ps1` 启动（设置 `PYTHONUTF8=1`）
5. **NLTK 语料下载失败（raw.githubusercontent.com 被墙）** → 手动下载到
   `%USERPROFILE%\nltk_data`；换机器需用 GitHub 代理（如 `https://gh-proxy.com/`）
   重新下载 punkt、punkt_tab、wordnet、stopwords 四个包

## 7. 回归测试脚本

[tmp/](../tmp/) 下的脚本是排查与回归工具（各有用途，可独立运行）：

- [test_tools.py](../test_tools.py)：非交互测试，验证两种工具路由（手动 ReAct 循环）
- [tmp/test_ui_client.py](../tmp/test_ui_client.py)：headless socket.io 客户端，
  模拟 Chainlit 前端协议驱动真实服务做问答回归（无需开浏览器）
- [tmp/test_memory_ui.py](../tmp/test_memory_ui.py)（及 2/3 变体）：双轮指代题
  回归，验证受控记忆窗口（对应 3.3 的三场景）
- [tmp/test_dsml_parse.py](../tmp/test_dsml_parse.py)：DSML 双路解析单元验证
  （对应第 2 节第 6 条）

## 8. 二次开发方向

- [x] 自定义 Agent 工具：院校排名查询（RankingTool，模糊匹配 + 本地数据源）
- [x] 多智能体雏形：主 Agent 工具调度 + 文档问答子 Agent（DocChatTool 封装）
- [x] 数据管道雏形：update_rankings.py 自动下载 QS 排名表 + 模糊匹配更新 +
      meta.json 时效元数据（RankingTool 回答附带「数据更新时间」）
- [x] 数据源升级（申请要求部分）：官网定向爬虫 crawler.py，LSE 试点 5 页入库。
      调研结论：申请要求/截止日期无现成公开数据集，爬官网是正路；官网数据
      权威可核查，且爬虫自带限速/重试/清洗/来源可溯，构成了完整的数据采集能力
- [ ] 数据源升级（排名部分）：GitHub 数据集换成 QS 官方实时数据，
      并支持自动更新 QS 2027（当前脚本的数据 URL 需随新数据集发布而更新）
- [ ] 更多工具：申请截止倒计时规划、汇率换算、文书润色
- [ ] 结构化抽取：从官网 PDF 抽取申请要求字段
- [x] Web 界面：Chainlit 2.x + langroid 官方 ChainlitAgentCallbacks（web_ui.py，
      web.ps1 启动；手动 ReAct 循环 + cl.make_async 线程池，规避交互式 Task 的
      等输入/stall 坑；支持上传文档入库）
- [x] 对话记忆：受控记忆窗口（每轮清空框架隐式历史 + 显式组装最近 6 轮问答，
      历史回答截断；API 抓包验证 token 不随轮数膨胀且指代仍正确）
- [x] 评测体系：eval.py 三层指标（检索命中/工具路由/答案正确）+ 46 题评测集
      （含 5 道防编造 negative 题），ranking 期望值运行时自动生成；
      三轮调优后终态 19/19 + 46/46 + 46/46（见第 4 节）
- [x] DeepSeek DSML 工具调用格式兼容：DeepSeekChatAgent 子类双路解析，
      修复约 1/5 轮工具不执行的偶发故障
