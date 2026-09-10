# 留学申请文档问答知识库

基于 [Langroid](https://github.com/langroid/langroid)（MIT 协议，多智能体 + RAG 框架）构建的独立应用——框架仅作为 pip 依赖使用，本项目未复制任何框架源码；多智能体架构、自定义工具、评测体系、对话记忆、数据管道等业务代码均为原创（详见文末「技术基础与致谢」）。

## 架构（二次开发核心设计）

```
主 ChatAgent（ReAct 工具调度，判断问题类型并组织最终回答）
  ├── ranking_lookup 工具  —— 本地 QS 排名/院校信息查询（tools.py 自定义 ToolMessage）
  └── doc_qa 工具          —— 转交 DocChatAgent 子 Agent 检索文档并带引用回答
```

即：**主 Agent 工具调度 + 文档问答子 Agent** 的多智能体模式。为什么不用「给
DocChatAgent 直接挂工具」的简单方案：DocChatAgent 的 `llm_response` 走「检索→回答」
专用流程，检索不到相关内容时直接返回 `DO-NOT-KNOW`，根本不会进入 LLM 工具调用
环节——这是实测踩出来的坑。把文档问答封装成工具交给主 Agent 调度，路由才正确。

- `chat.py`：入口（主 Agent + 建库去重守卫）
- `tools.py`：自定义工具（RankingTool 模糊匹配本地院校数据；DocChatTool 封装子 Agent）
- `doc_qa.py`：文档问答子 Agent 工厂
- `data/rankings.json`：本地院校 QS 排名数据（15 所，可自行扩充），配套
  `data/meta.json` 时效元数据（版本/更新时间/来源），RankingTool 回答时会展示
  「数据更新时间」。注意：**QS 排名每年 6 月更新**，QS 2027 已于 2026-06 发布
  （NTU 仍为 12，NUS 8→10），本数据当前停留在 QS 2026 版。
- `update_rankings.py`：**数据管道**——一键刷新排名数据，解决「静态 JSON 会过期」
  问题。见下方「数据更新」一节。
- `crawler.py`：**数据采集管道**——定向抓取院校官网申请要求页面，清洗后写入
  `docs/` 自动入库。见下方「官网申请要求爬取」一节。
- `test_tools.py`：非交互测试脚本，验证两种工具路由（手动 ReAct 循环）
- `web_ui.py`：Chainlit Web 聊天界面（同一套多智能体架构 + 上传文档入库），
  配套 `web.ps1` 一键启动脚本，见下方「Web 界面」一节

## 环境

- Windows + Python 3.12（虚拟环境 `.venv`）
- 依赖见 `requirements.txt`（langroid 0.67.7 + fastembed + python-docx + qdrant-client 1.11.3）

## 快速开始

1. 编辑 `.env`，填入 DeepSeek 密钥（[platform.deepseek.com](https://platform.deepseek.com) 获取）：

   ```
   DEEPSEEK_API_KEY=sk-xxxxx
   ```

2. 运行（`run.ps1` 已自动设置 UTF-8，避免中文 Windows GBK 编码报错）：

   ```powershell
   .\run.ps1 docs                 # 用示例文档测试
   .\run.ps1 "<你的文档目录>"        # 你自己的 PDF/TXT/DOCX 文件或文件夹
   .\run.ps1 docs -m deepseek/deepseek-reasoner   # 换 R1 推理模型
   ```

   问答中输入 `x` 或 `q` 退出。可以这样试：
   - 「剑桥大学的 QS 排名是多少？」→ 触发 ranking_lookup 工具
   - 「LSE 的雅思要求是多少？」→ 触发 doc_qa 工具（带 [^1] 引用）
   - 「NYU 的 QS 排名和申请截止日期分别是什么？」→ 一次调用两个工具后整合回答

   非交互验证：`.\.venv\Scripts\python.exe test_tools.py`

## Web 界面（Chainlit）

同一套多智能体架构的网页版：流式回答、工具调用过程可视化（LLM 思考
Step / 工具调用与结果 Step），还支持在对话框直接上传 PDF/TXT/DOCX 文档，
上传后自动入库即可提问（按内容哈希去重，重复上传同一文件会跳过）。

```powershell
# 安装（清华镜像；已装 langroid 不会重装，只补装 chainlit 与其依赖）
.\.venv\Scripts\python.exe -m pip install "langroid[chainlit]==0.67.7" -i https://pypi.tuna.tsinghua.edu.cn/simple

# 启动，然后浏览器打开 http://localhost:8000
.\web.ps1
```

- 默认模型 `deepseek/deepseek-chat`，可用环境变量 `WEB_MODEL` 覆盖
- 默认知识库为 `docs/` 目录，启动时自动入库（与 CLI 共用去重守卫）
- 已知限制：文档子 Agent 是进程级单例（Qdrant 本地模式同一目录只允许一个
  客户端，多标签页/刷新时会触发文件锁冲突，故全会话共享一份，doc_qa 调用
  由锁串行化）；定位单用户本地使用。重建知识库删除 `.qdrant` 目录即可

实现踩坑：交互式 Task 不适合 Web——`interactive=True` 在工具
执行后会向用户索要输入，`interactive=False` 在最终回答后无结束信号会 stall。
解决方案：手动 ReAct 循环（`llm_response` → `try_get_tool_messages` →
`agent_response`）+ `cl.make_async` 把整段同步循环放入线程池，配合官方
`lr.ChainlitAgentCallbacks` 渲染流式 Step。

另一个排查很久的坑：文档问答的「相关句批量抽取」（langroid 内部
`run_batch_tasks` → `asyncio.run`）跑在 `cl.make_async` 的 anyio 工作线程里
会报 `There is no current event loop in thread 'AnyIO worker thread'`——因为
chainlit 启动时 `nest_asyncio.apply()` 把 `asyncio.run` 补丁成了「要求当前
线程已有事件循环」，而工作线程没有；且只有检索命中的文档问题才走这条批量
路径（排名类问题不触发，CLI 主线程也不受影响），所以表现得很偶发。解决：
`web_ui.py` 的 `run_react` 入口为当前线程 `asyncio.new_event_loop()` +
`asyncio.set_event_loop()` 装一个专属循环，函数结束关闭（代码已内置）。
另有 `tmp/test_ui_client.py`：headless socket.io 客户端，模拟 Chainlit 前端
协议驱动真实服务做问答回归（无需开浏览器）。

## 对话记忆（受控记忆窗口）

先说清框架真相：langroid 0.67.7 **自带跨轮记忆，但隐式、无界**。
`ChatAgent.llm_response`（chat_agent.py:1659，覆写了父类）会把
`self.message_history` 完整喂给 LLM——每轮对话都会让历史消息线性膨胀
（API 抓包实测：首请求消息条数随轮数 2→6→10→14→18→22，每轮 +4 条，
包括工具调用 JSON 与工具结果），长会话最终顶爆 64k 上下文且 token
成本失控。

本项目的改造（web_ui.py）：把「框架隐式无界记忆」改成「显式受控记忆
窗口」：

- 每轮结束 `agent.clear_history(1, -1)` 清空 message_history（只留系统
  消息），切断框架隐式累积；
- 跨轮记忆由 `_compose_with_memory` 显式组装：只保留每会话最近
  `MAX_MEMORY_TURNS=6` 轮问答，历史回答截断到 500 字符；
- 效果（API 抓包前后对比）：改造前首请求消息条数 2→6→10→14→18→22
  线性膨胀；改造后**恒定 2 条**，且第 6 轮指代题（「那这所大学的所在
  城市是哪里？」）仍能正确解析上一轮的学校。

设计取舍：不依赖框架隐式历史（无界、不可审计），改为
应用层显式窗口——token 有界、窗口大小可调、喂给 LLM 的记忆内容可审计；
超出窗口的最旧轮自然遗忘（滑动窗口）。

无记忆 vs 有记忆的对比实测（headless 客户端抓事件流，同一 sessionId 双轮
指代题）：

- 场景一「LSE 的雅思要求是多少？」→「那这个学校的学费呢？」：
  **修复前** Q2 虽答对 44,928 英镑，但那是歪打正着——doc_qa 工具的
  few-shot 示例恰好泄漏了「LSE MSc Finance」指代；**修复后** Q2 的工具
  调用为 `doc_qa query="LSE 的学费是多少？"`，指代被记忆**显式解析**，
  不再依赖运气。
- 场景二「牛津大学的 QS 排名是多少？」→「那这所大学的所在城市是哪里？」：
  修复前 Q2 答对牛津纯属 LLM 运气猜中（事件流显示**零工具调用**）；
  修复后通过记忆正确解析为牛津。
- 场景三「南洋理工大学的 QS 排名是多少？」→「那这所大学的申请截止日期
  是什么时候？」（决定性对比）：知识库未收录南洋理工，**修复前** Q2 直接
  编造了 LSE 的截止日期（无中生有）；**修复后** 记忆把「这所大学」解析为
  南洋理工，工具查无此文后**诚实拒答**（DO-NOT-KNOW）。

结论：对话记忆不只是「记得上文」，还通过显式指代消解防止了无记忆时
LLM 靠猜回答的编造风险。

## 工具调用踩坑记录（实测结论）

1. **DocChatAgent 挂工具无效**：其 `llm_response` 走 `answer_from_docs` 专用流程，
   无相关文档直接返回 `DO-NOT-KNOW`，不进工具循环 → 改用「主 Agent 调度 +
   doc_qa 工具封装子 Agent」架构。
2. **`agent.llm_response` 不执行工具**：它只做单次 LLM 调用，工具执行循环在
   `Task.run()`（交互式）或手动 ReAct 循环（`try_get_tool_messages` → `agent_response`）
   里；见 `test_tools.py` 的 `run_with_tools`。
3. **交互模式下工具执行后需按回车**：langroid 的 Task 在每个非人类响应后等待用户
   输入（`interactive=True` 默认行为），工具结果返回后按回车，LLM 才继续给出最终回答。
4. **`ingest_doc_paths` 不去重**：重复启动会重复入库污染检索 → `chat.py` 里加了
   `.qdrant/ingested.txt` 标记守卫；想强制重建知识库时删除 `.qdrant` 目录。
5. **LLM 转述可能引入事实错误**：工具结果本身正确，但 LLM 复述时偶发改动数字
   （实测 NYU 截止日期被改写过一次）。对策：工具结果直接落库/结构化输出，
   减少 LLM 二次转述。
6. **DeepSeek 偶发输出原生 DSML 工具调用格式**：实测约 1/5 轮不按 OpenAI
   tool_calls 协议输出，而是把原生 DSML（`<｜invoke name="...">`，注意分隔符
   是全角竖线 `｜` U+FF5C）写进 content。langroid 0.67.7 只识别 OpenAI
   tool_calls 与 JSON ToolMessage 格式 → 工具不执行、DSML 原文成为最终回答
   （用户看到 XML 垃圾）。修复：`tools.py` 的 `DeepSeekChatAgent` 子类覆写
   `get_tool_messages`（框架唯一工具提取入口），框架解析不到工具时回退解析
   DSML，按 invoke 的 name 在 `llm_tools_map` 找工具类、用 parameter 参数
   实例化。所有入口（chat.py / web_ui.py / eval.py / test_tools.py）统一换用
   该子类。这是针对 LLM 输出格式不稳定的容错设计（协议格式 + 原生格式双路
   解析）。

## 可选参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `-m / --model` | LLM 模型名 | `deepseek/deepseek-chat` |
| `-e / --embed-model` | fastembed embedding 模型 | `BAAI/bge-small-zh-v1.5` |

## 数据更新（update_rankings.py）

排名数据不再靠人手动改，一条命令自动刷新：

```powershell
# 预览（推荐先跑）：只显示哪些学校会变，不写文件
.\.venv\Scripts\python.exe update_rankings.py --auto --dry-run

# 自动模式：从 GitHub 公开数据集下载 QS 排名表并更新
# （raw.githubusercontent.com 被墙时自动走 gh-proxy.com 镜像）
.\.venv\Scripts\python.exe update_rankings.py --auto

# 半自动模式：导入本地表格（QS 官网导出的 xlsx/csv 均可，自动识别列）
.\.venv\Scripts\python.exe update_rankings.py --file "<你的表格文件>"
```

流程：下载/导入表格 → 按校名模糊匹配（thefuzz）→ 更新排名 → 写回
`rankings.json` + `meta.json`（数据版本/更新时间/来源）。

### 实战故事：自动校验抓出了人工没发现的错

2026-09-08 首次跑 `--dry-run`，脚本立刻发现 4 所学校的排名与官方表不符：
牛津 3→4、哈佛 4→5、剑桥 5→6、斯坦福 6→3（斯坦福 QS 2026 升至第 3，前三校
各退一位）。此前这些数据经人工「核实」都没发现错。教训：**手工维护的数据
必须有自动校验**；也注意表格里排名单元格可能是公式（如 `=17`），解析时需
去掉前导 `=`（脚本已处理）。

## 官网申请要求爬取（crawler.py）

「大多数问题回答不出来」的根因是文档覆盖不足，而申请要求/截止日期没有
现成的公开数据集（调研结论：GitHub 上只有录取预测数据），所以做了一条
**定向爬官网**的数据采集管道。试点：LSE 研究生申请 5 个官方页面
（入学要求、英语要求、学费资助、如何申请、MSc 金融项目规章）。

```powershell
# 预览抓取清单（不请求网络）
.\.venv\Scripts\python.exe crawler.py --dry-run

# 正式抓取：写入 docs/crawled-<校>-<页面>.txt，重启 Web 后自动入库
.\.venv\Scripts\python.exe crawler.py
```

设计要点：

- 只抓官方 `.edu`/`.ac.uk` 页面：数据权威、可核查
- 礼貌爬取：浏览器 UA + 单线程 + 页间 sleep 2s 限速 + 每页最多重试 2 次
- 正文提取：trafilatura 优先（专门做正文提取的库），失败回退 BeautifulSoup；
  去空行、单文件截断 2 万字符
- 来源可溯：每个文件头部写入「# 来源: URL」+「# 抓取时间」，回答引用自带出处
- 更新语义：重跑覆盖文件 → 内容哈希变化 → 重启 Web 自动重新入库
  （web_ui.py 的 `_startup_ingest` 按「文件名+内容 md5」去重）。
  已知限制：更新时旧向量仍留在集合中，彻底干净需删除 `.qdrant` 目录重建
- 扩展：`CRAWL_SOURCES` 加一所学校即可（先 `--dry-run` 验证 URL 可达；
  实测 UCL/NYU/帝国理工官网有 WAF 反爬，LSE/剑桥直连可抓）

跨语言检索实测结论（踩坑记录）：中文问题查英文文档时，langroid 默认的
「稠密 + BM25 + 模糊匹配」混合检索（RRF 融合）会把 "LSE" 这类英文关键词
密度高的无关页面顶到最前，挤掉真正相关的块。修复：`doc_qa.py` 关闭
BM25/模糊匹配（纯稠密检索，bge-small-zh 中英混查排序反而更准），候选块
数直接取语料块数（跨语言相似度分不高，任何 top-k 截断都会随机漏掉真命
中文块；详见「评测」节二轮实录）。

## 数据存放

- 向量索引：`.qdrant/data`（Qdrant 本地模式，无需 Docker）
- embedding 模型：HuggingFace 缓存目录（本机已下载，重装系统需重新下载）
- NLTK 语料：`%USERPROFILE%\nltk_data`（本机已下载 punkt/punkt_tab/wordnet/stopwords）

## 部署踩坑记录（本机已验证的解决方案）

1. **HuggingFace 直连超时** → `.env` 中设置 `HF_ENDPOINT=https://hf-mirror.com`
2. **大文件 401（Xet 协议）** → `.env` 中设置 `HF_HUB_DISABLE_XET=1`
3. **qdrant-client 版本不兼容** → 固定 `qdrant-client==1.11.3`（langroid 0.67.7 用了 1.12+ 已移除的旧 API）
4. **中文 TXT 报 GBK 解码错误** → 用 `run.ps1` 启动（设置 `PYTHONUTF8=1`）
5. **NLTK 语料下载失败（raw.githubusercontent.com 被墙）** → 已手动下载到 `%USERPROFILE%\nltk_data`；换机器需用 GitHub 代理（如 `https://gh-proxy.com/`）重新下载 punkt、punkt_tab、wordnet、stopwords 四个包

## 评测（eval.py）

自建问答评测集 `eval/questions.json`（46 题 = 18 ranking + 16 doc +
7 mixed + 5 negative），三层指标：

1. **检索命中率**（doc/mixed 题）：`get_relevant_chunks` 返回块含
   evidence 关键词即命中（只耗本地 embedding，不耗 LLM）；
2. **工具路由正确率**：捕获每题实际调用的工具名 vs 期望工具；
3. **答案正确率**：最终回答归一化后 expected 关键词全中；negative 题
   要求「含拒答词且不含 forbidden 关键词」（防编造：如问哈佛学费时
   不得出现任何货币金额）。

实测结果（2026-09-10，三轮迭代终态）：**检索命中率 19/19，工具路由
正确率 46/46，答案正确率 46/46**。失败题明细见 `eval/report.txt`。

评测驱动调优实录（三轮）：

- 首轮 33/46：两处根因。① DeepSeek 偶发原生 DSML 工具调用（见「工具调用
  踩坑记录」第 6 条）→ DeepSeekChatAgent 双路解析；② DocChatAgent
  流水线三道随机 LLM 环节——followup_to_standalone 把中文问题随机翻译
  成英文再检索（英文检索命中不了中文块）、逐块相关句批量抽取会丢掉含
  答案的句子（→ 回答退化成 DO-NOT-KNOW）、默认跨问题累积
  message_history 污染逐题判断。修复：`assistant_mode=True` +
  `relevance_extractor_config=None` + `conversation_mode=False`。
- 二轮 38/46：剩余 8 题全是**跨语言检索 top-k 截断**——中文块相似度分
  整体不高（0.5-0.7），真命中文块常排第 6-8 位，top-6 截断导致「检索
  命中但回答 DO-NOT-KNOW」与「回答对但检索检查未命中」随机出现。更反
  直觉的是「查询改写」帮倒忙：改写把中文问题翻译成英文，英文问法把英
  文官网页顶进合并 top-k，把中文块挤出去。修复：候选块数直接取语料块
  数（小知识库全量召回，检索不耗 LLM）+ 关闭改写问法 → 检索完全确定。
- 判题集自身两处修正（同样是评测体系的一部分）：doc-08 期望词加
  「三封/3封」any-of（LLM 数字写法不定）；doc-09 双事实题（sample
  1000 字 vs 官网 A4 两页）any-of 组原写成 AND 结构，修正为任一命中。

设计要点：

- ranking 题期望值**运行时从 rankings.json 自动生成**（school 字段
  解析），题库只写校名与期望类型，不手抄排名数字——防评测集本身出错
- 每题新建主 Agent：框架把 message_history 喂给 LLM，复用同一 Agent
  会跨题污染；negative 题专门验证「宁可拒答也不编造」
- 向量库用 `tmp/eval_qdrant` 全新副本：Qdrant 本地模式同目录只允许
  一个客户端，不能与运行中的 Web 服务抢 `.qdrant/data` 文件锁

```powershell
.\.venv\Scripts\python.exe eval.py            # 全量 46 题（记得设 PYTHONUTF8=1）
.\.venv\Scripts\python.exe eval.py --limit 10 # 冒烟：只跑前 10 题
```

## 二次开发方向

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
      三轮调优后终态 19/19 + 46/46 + 46/46（见「评测」节）
- [x] DeepSeek DSML 工具调用格式兼容：DeepSeekChatAgent 子类双路解析，
      修复约 1/5 轮工具不执行的偶发故障

## 技术基础与致谢

- 本项目构建于 [Langroid](https://github.com/langroid/langroid) v0.67.7
  （MIT License，Copyright (c) 2023 langroid）之上：框架通过 pip 依赖引入，
  定制方式为**子类化**（`tools.py` 的 `DeepSeekChatAgent` 继承 `ChatAgent`
  并覆写 `get_tool_messages`），未复制任何框架源码
- 其余依赖（Chainlit、Qdrant、fastembed、python-docx、trafilatura 等）
  各自遵循其自身开源协议
- 本项目自身代码以 MIT License 发布（见 [LICENSE](LICENSE)）
