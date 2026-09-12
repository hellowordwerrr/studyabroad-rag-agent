"""
Chainlit Web 聊天界面（基于 langroid 官方 ChainlitAgentCallbacks）

与 chat.py 架构一致：主 ChatAgent（ReAct 工具调度）+ doc_qa 工具封装
DocChatAgent 子 Agent。区别：外层从 CLI 换成 Web，并做四处适配：
1. 工具循环用「手动 ReAct 循环」（同 test_tools.py）取代交互式 Task——
   规避 Task(interactive=True) 工具执行后等用户输入、Task(interactive=False)
   5 步后 stall 返回 None 两个实测坑。
2. 整个同步循环包进 cl.make_async 跑线程池（langroid 同步路径无嵌套
   asyncio，线程池执行安全；Chainlit 回调层内部用 run_sync 渲染回主循环）。
3. 支持在对话框直接上传 PDF/TXT/DOCX，按「文件名 + 内容 md5」去重入库
   （chainlit 上传文件临时路径每次不同，不能按路径记）。
4. 受控对话记忆：langroid 0.67.7 的 ChatAgent.llm_response
   （chat_agent.py:1659）会把 self.message_history 完整喂给 LLM——隐式跨轮
   记忆、无界增长（每轮 +4 条消息，直到顶爆 64k 上下文）。本实现每轮结束
   清空 message_history（只留系统消息），改为显式维护每会话最近 N 轮问答
   窗口（MAX_MEMORY_TURNS + 历史回答截断）：token 有界、窗口可调、
   行为可审计（API 抓包实测：默认历史随轮数线性膨胀，改造后每轮恒定）。
5. 会话功能（DeepSeek 式侧栏）：SQLite 数据层落盘（历史会话列表 +
   断线恢复）+ 可开关口令登录 + 会话标题自动命名 + 快捷问题按钮 +
   引用来源卡片 + 对话头部模型切换下拉（Chat Profile）+ 对话导出 Markdown。

用法: .\\web.ps1  （或 .venv\\Scripts\\python.exe -m chainlit run web_ui.py -w）
      浏览器打开 http://localhost:8001
"""

import asyncio
import hashlib
import math
import os
import re
import secrets
import tempfile
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import chainlit as cl
from chainlit import run_sync
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.types import ThreadDict
from chainlit.user import User
from dotenv import load_dotenv

import langroid as lr
import langroid.language_models as lm
from langroid.agent.callbacks.chainlit import get_text_files
from langroid.agent.special.doc_chat_agent import DocChatAgent

from doc_qa import DEFAULT_EMBED_MODEL, build_doc_agent, ingest_if_needed
from tools import (
    DeepSeekChatAgent,
    DocChatTool,
    RankingTool,
    get_last_doc_sources,
    set_doc_agent,
)

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

MODEL = os.getenv("WEB_MODEL", "deepseek/deepseek-chat")
EMBED_MODEL = os.getenv("WEB_EMBED_MODEL", DEFAULT_EMBED_MODEL)
# 默认隐藏工具调用的 JSON Step（界面更简洁）；想看工具调用过程时，
# 设置环境变量 WEB_SHOW_TOOL_STEPS=1 再启动即可
SHOW_TOOL_STEPS = os.getenv("WEB_SHOW_TOOL_STEPS", "") == "1"
DOCS_DIR = Path(__file__).parent / "docs"
MAX_TOOL_ROUNDS = 8  # 手动 ReAct 循环的保险上限，防止工具调度死循环
# 对话记忆：每会话保留最近 N 轮（问答对）拼进上下文，超出窗口的最旧轮丢弃
MAX_MEMORY_TURNS = 6
# 历史回答截断长度（字符）：控制上下文 token 成本，保留关键结论即可
MAX_MEMORY_ANSWER_CHARS = 500
# 会话持久化：SQLite 数据层（历史会话列表 + 断线恢复的基础）。
# DB 放 .chainlit/（已被 .gitignore 忽略），重启不清空历史。
SESSIONS_DB = Path(__file__).parent / ".chainlit" / "sessions.db"
# 可开关的访问密码：.env 设置 CHAINLIT_WEB_PASSWORD 即启用登录页与
# 「Past Chats」历史会话列表（Chainlit 要求 dataPersistence && requireLogin
# 两者同时成立才渲染历史列表）。不设置则无登录（回归测试/截图管道用）。
WEB_PASSWORD = os.getenv("CHAINLIT_WEB_PASSWORD")
# 登录 JWT 签名密钥（chainlit auth/jwt.py 读取）。只在启用登录时需要；
# 未配置时随机生成并提示（重启后需重新登录，本地使用可接受）。
_AUTH_SECRET = os.getenv("CHAINLIT_AUTH_SECRET")
if WEB_PASSWORD and not _AUTH_SECRET:
    os.environ["CHAINLIT_AUTH_SECRET"] = secrets.token_hex(32)
    print("[web_ui] 未设置 CHAINLIT_AUTH_SECRET，已随机生成（重启后需重新登录）。")
# 对话头部可见的模型切换器（Chat Profile 下拉）：替换原来藏在设置齿轮里的
# 下拉框（齿轮入口太隐蔽）。Chainlit 会在聊天窗口顶部渲染一个显示当前模型
# 名的下拉框（WorkBuddy 式交互），点开可见全部候选。name 是内部标识
# （on_chat_start 用它选模型），display_name 是下拉框里显示的名字。
# icon 必须是图片 URL：前端把 icon 当 <img src> 渲染（emoji 字符串会裂图），
# 含 /public 前缀的路径由前端 buildEndpoint 解析成本机静态资源地址。
# 主 Agent 每会话独立；文档子 Agent 保持进程级单例（默认模型，不随切换变化）。
MODEL_PROFILES = [
    {
        "name": "deepseek-chat",
        "display_name": "DeepSeek-V3",
        "description": "通用问答模型：响应快、工具调用稳，日常问答首选（默认）",
        "icon": "/public/model-v3.svg",
    },
    {
        "name": "deepseek-reasoner",
        "display_name": "DeepSeek-R1",
        "description": "深度推理模型：复杂问题想得更深，工具调用稳定性略低",
        "icon": "/public/model-r1.svg",
    },
]
# profile 内部标识 → langroid 模型全名（deepseek/<name>）
_MODEL_BY_PROFILE = {p["name"]: f"deepseek/{p['name']}" for p in MODEL_PROFILES}
DEFAULT_MODEL_KEY = "model"


@cl.set_chat_profiles
async def _chat_profiles(_user, _language):
    """头部下拉框的候选列表（Chainlit 在 /project/settings 拉取后渲染）。"""
    return [
        cl.ChatProfile(
            name=p["name"],
            display_name=p["display_name"],
            markdown_description=p["description"],
            icon=p["icon"],
            default=(f"deepseek/{p['name']}" == MODEL),
        )
        for p in MODEL_PROFILES
    ]


# Chainlit 2.12 的 SQLAlchemyDataLayer 不负责建表（官方要求用户自建，
# 见官方 backend/tests/data/test_sql_alchemy.py）。schema 与官方测试同款；
# SQLite 对 UUID/JSONB/TEXT[] 类型名宽容（官方测试即用 sqlite 跑此 DDL），
# 且数据层对 metadata 等 JSON 字段自行 json.dumps 后按 TEXT 存取。
_SESSIONS_SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS users (
        "id" UUID PRIMARY KEY,
        "identifier" TEXT NOT NULL UNIQUE,
        "metadata" JSONB NOT NULL,
        "createdAt" TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS threads (
        "id" UUID PRIMARY KEY,
        "createdAt" TEXT,
        "name" TEXT,
        "userId" UUID,
        "userIdentifier" TEXT,
        "tags" TEXT[],
        "metadata" JSONB NOT NULL DEFAULT '{}',
        FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE
    )''',
    # 注意：官方 main 分支的测试 schema 缺 2.12.0 的 defaultOpen/autoCollapse
    # 等列（Step.to_dict 实际输出），且 disableFeedback 加了 NOT NULL 而
    # 2.12.0 的 create_step 动态拼列不带它——两处都是实测踩坑修正。
    '''CREATE TABLE IF NOT EXISTS steps (
        "id" UUID PRIMARY KEY,
        "name" TEXT NOT NULL,
        "type" TEXT NOT NULL,
        "threadId" UUID NOT NULL,
        "parentId" UUID,
        "disableFeedback" BOOLEAN,
        "streaming" BOOLEAN,
        "waitForAnswer" BOOLEAN,
        "isError" BOOLEAN,
        "metadata" JSONB,
        "tags" TEXT[],
        "input" TEXT,
        "output" TEXT,
        "createdAt" TEXT,
        "start" TEXT,
        "end" TEXT,
        "generation" JSONB,
        "showInput" TEXT,
        "language" TEXT,
        "indent" INT,
        "defaultOpen" BOOLEAN,
        "autoCollapse" BOOLEAN,
        "command" TEXT,
        "modes" JSONB,
        "icon" TEXT,
        "feedback" JSONB
    )''',
    '''CREATE TABLE IF NOT EXISTS elements (
        "id" UUID PRIMARY KEY,
        "threadId" UUID,
        "type" TEXT,
        "url" TEXT,
        "chainlitKey" TEXT,
        "name" TEXT NOT NULL,
        "display" TEXT,
        "objectKey" TEXT,
        "size" TEXT,
        "page" INT,
        "language" TEXT,
        "forId" UUID,
        "mime" TEXT,
        "props" TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS feedbacks (
        "id" UUID PRIMARY KEY,
        "forId" UUID NOT NULL,
        "threadId" UUID NOT NULL,
        "value" INT NOT NULL,
        "comment" TEXT
    )''',
]


def _init_sessions_db() -> None:
    """幂等建表：同步 sqlite3 建 schema（数据层首次使用前调用）。"""
    import sqlite3

    SESSIONS_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(SESSIONS_DB) as conn:
        for ddl in _SESSIONS_SCHEMA:
            conn.execute(ddl)


class TimestampedDataLayer(SQLAlchemyDataLayer):
    """SQLAlchemyDataLayer 子类：给缺失 createdAt 的 step 补当前时间。

    坑（chainlit 2.12.0 实测）：流式回答的 step 由 stream_start 首次落库，
    create_step 会把 None 字段过滤掉 → createdAt 列为 NULL。回放线程时
    get_thread 的步骤查询 ORDER BY createdAt ASC，SQLite 把 NULL 排最前，
    流式回答（子消息）被排在它的父 run 之前回放，前端找不到父消息直接
    丢弃——表现就是「刷新/恢复会话后，流式生成的回答整条消失」。
    修复：create_step 入口补时间戳（最终 update_step 时 createdAt 仍为
    None 也会被过滤，不会覆盖这个初始值）。
    """

    async def create_step(self, step_dict):
        if not step_dict.get("createdAt"):
            step_dict = {
                **step_dict,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
        return await super().create_step(step_dict)


@cl.data_layer
def get_data_layer():
    """会话持久化数据层：把会话消息落到 .chainlit/sessions.db（SQLite）。

    没有数据层时 Chainlit 2.x 不落盘：刷新后历史消失、侧栏也没有
    「Past Chats」列表（前端要求 dataPersistence && requireLogin 同时成立）。
    """
    _init_sessions_db()
    return TimestampedDataLayer(conninfo=f"sqlite+aiosqlite:///{SESSIONS_DB}")


if WEB_PASSWORD:  # 密码开关：未设置 CHAINLIT_WEB_PASSWORD 则不启用登录
    @cl.password_auth_callback
    async def password_auth_callback(
        username: str, password: str
    ) -> Optional[User]:
        """简单口令登录：密码匹配即放行（本机单用户使用场景）。"""
        if password == WEB_PASSWORD:
            return User(
                identifier=username.strip() or "本地用户",
                metadata={"role": "user"},
            )
        return None

SYSTEM_MESSAGE = (  # 与 chat.py 完全一致
    "你是一个留学申请助手。回答规则：\n"
    "1. 文档内容类问题（申请要求、语言成绩、截止日期、材料、费用等）："
    "调用 doc_qa 工具，并把返回答案中的引用标注一并保留。\n"
    "2. 院校排名/所在城市/基本信息类问题：调用 ranking_lookup 工具。\n"
    "3. 用户问题可能同时涉及多个信息点（如既问排名又问截止日期），"
    "此时必须逐个调用所有相关工具，全部查完后再统一组织回答，"
    "不要因为涉及多个方面就反问用户。\n"
    "4. 拿到工具结果后，用中文自然语言组织回答；工具查不到的如实告知。\n"
    "5. 其他闲聊问题直接回答，不要调用工具。"
)


def _looks_like_dsml(content: str) -> bool:
    """DeepSeek 原生 DSML 工具调用文本识别。

    DSML 不被 langroid 识别为工具调用时（oai_tool_calls 为空），会按普通
    LLM 文本渲染成用户可见消息。实际上 DeepSeekChatAgent 会解析并执行它，
    最终回答在其后正常渲染——所以界面层把 DSML 原文隐藏，与工具调用 JSON
    的 Step 同等对待。
    """
    return bool(content) and "invoke" in content and (
        "｜" in content or "DSML" in content
    )


class QuietAgentCallbacks(lr.ChainlitAgentCallbacks):
    """隐藏工具调用/工具结果/模型思考过程的 Step，界面只保留最终回答。

    官方回调把 Agent 的「思考过程」可视化：LLM 输出的工具调用参数
    （如 {"request": "ranking_lookup", ...}）渲染成一个 Step，工具返回的
    结果 JSON 再渲染成一个 Step。调试很有用，但日常使用显得杂乱。
    本子类把这两类 Step 都去掉：工具结果会由主 Agent 转述进最终回答，
    不影响内容完整性。

    另：DeepSeek-R1（deepseek-reasoner）的 reasoning_content 会被
    langroid 传给回调的 reasoning 参数，官方回调把它渲染成一条
    「💭 Reasoning」消息（实测用户会看到 "The tool returned. Now
    organize answer." 这类思考碎片）。这里直接丢弃，界面只保留
    最终回答，与 DeepSeek 官方界面折叠思考过程的做法一致。
    """

    def start_llm_stream(self):
        return super().start_llm_stream()

    def finish_llm_stream(
        self, content="", tools_content="", is_tool=False, reasoning=""
    ):
        # 工具调用 JSON 或 DeepSeek DSML 原文：移除流式 Step，不渲染
        if is_tool or _looks_like_dsml(content):
            if self.stream is not None:
                try:
                    run_sync(self.stream.remove())
                except Exception:
                    pass
            self.stream = None
            return
        super().finish_llm_stream(
            content=content,
            tools_content=tools_content,
            is_tool=is_tool,
            reasoning="",  # R1 思考过程不渲染（见类 docstring）
        )

    def show_llm_response(
        self,
        content="",
        tools_content="",
        is_tool=False,
        cached=False,
        language=None,
        reasoning="",
    ):
        # 非流式路径下的工具调用 JSON 与 DeepSeek DSML 原文同样不渲染
        if is_tool or _looks_like_dsml(content):
            return
        super().show_llm_response(
            content=content,
            tools_content=tools_content,
            is_tool=is_tool,
            cached=cached,
            language=language,
            reasoning="",  # R1 思考过程不渲染（见类 docstring）
        )

    def show_agent_response(self, content="", language="text", is_tool=False):
        # 工具返回结果 Step 不渲染（本项目该回调只会被工具结果触发）
        return


# 进程级单例的文档子 Agent。Qdrant 本地模式同一存储目录只允许一个客户端
# 实例，而每个 Chainlit 会话（每个标签页）都会触发 on_chat_start——若每次
# 都新建 DocChatAgent，多标签页/刷新页面时会同时打开 .qdrant/data，
# 触发「Storage folder is already accessed」文件锁冲突。
# 文档子 Agent 每次问答都是独立的单次检索（无跨问记忆），多会话共享安全。
_doc_agent: Optional[DocChatAgent] = None
_doc_lock = threading.Lock()


def _ensure_doc_agent() -> DocChatAgent:
    """文档子 Agent 进程级单例：首个会话创建并建库，后续会话直接复用。

    必须在 cl.make_async 的线程池里调用（建库是同步阻塞操作）。
    """
    global _doc_agent
    with _doc_lock:
        if _doc_agent is None:
            _doc_agent = build_doc_agent(model=MODEL, embed_model=EMBED_MODEL)
            set_doc_agent(_doc_agent)  # 注入给 DocChatTool（tools.py 模块级全局）
            _startup_ingest(_doc_agent)
    return _doc_agent


def build_main_agent(model: str = MODEL) -> lr.ChatAgent:
    """主 Agent：每会话一个（各自独立的消息历史），挂载两个工具。

    用 DeepSeekChatAgent 子类：兼容 DeepSeek 偶发输出的原生 DSML 工具
    调用格式（langroid 0.67.7 不识别，不处理则用户随机看到 XML 原文）。
    model 支持会话级切换（对话头部 Chat Profile 下拉框），默认 WEB_MODEL。
    """
    agent = DeepSeekChatAgent(
        lr.ChatAgentConfig(
            llm=lm.OpenAIGPTConfig(chat_model=model),
            system_message=SYSTEM_MESSAGE,
        )
    )
    agent.enable_message(RankingTool)
    agent.enable_message(DocChatTool)
    return agent


def _session_model() -> str:
    """当前会话选中的模型（对话头部下拉框可改，未改时用默认）。"""
    return cl.user_session.get(DEFAULT_MODEL_KEY) or MODEL


def _normalize_memory(history) -> List[tuple]:
    """会话恢复后记忆条目是 JSON 反序列化的 list（原为 tuple），归一化回来。

    任何坏条目直接丢弃，不因一条脏数据阻断整段记忆。
    """
    if not history:
        return []
    out = []
    for pair in history:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            out.append((str(pair[0]), str(pair[1])))
    return out


# 爬虫写在每篇文档头部的来源元数据行（crawler.py build_document）：入库后
# 成为检索块的前缀。卡片只负责「核对答案出处」，这两行与答案无关，
# 展示前剥掉；文档文件与检索内容原样保留，来源可溯不变。
_META_LINE_RE = re.compile(r"^#\s*(?:来源|抓取时间)[:：]")
# 中英句界：中文句号/问号/叹号/分号后，或英文句点+空格后跟大写（避免
# 小数点与 URL 误拆）。re.split 用零宽断言，句末标点留在前一句。
_SENT_BOUNDARY_RE = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+(?=[A-Z])")
# 行首 Markdown 触发符：标题/引用/列表/代码围栏/强调。摘录是普通文本，
# 不能被打成标题（问题①：「# 来源:」曾被渲染成超大标题）。
_MD_TRIGGER_RE = re.compile(
    r"^(?:#{1,6}\s|>|\s*[-+*]\s|\s*\d+[.)]\s|`{1,3}|\\|[*_])"
)
# 选句后进卡片的最大总字符数（1~2 句的展示预算）
_PICK_MAX_CHARS = 180
# 第 2 句与第 1 句的相似度分差阈值：分差不超过它视为「几乎同样相关」
_PICK_SCORE_GAP = 0.03


def _strip_metadata(excerpt: str) -> str:
    """剥掉爬虫头部元数据行（# 来源: / # 抓取时间:），返回正文行。"""
    kept = [ln for ln in excerpt.splitlines() if not _META_LINE_RE.match(ln.strip())]
    return "\n".join(kept).strip()


def _split_sentences(text: str) -> List[str]:
    """把摘录拆成候选句：先按行拆（检索块保留换行），长行再按中英句界拆。"""
    out: List[str] = []
    for line in text.splitlines():
        for piece in _SENT_BOUNDARY_RE.split(line.strip()):
            piece = piece.strip()
            if len(piece) >= 4:  # 过滤列表项/标点残留等太短的碎片
                out.append(piece)
    return out


def _md_escape(line: str) -> str:
    """行首 Markdown 触发符前加反斜杠，让摘录按普通文本渲染。"""
    return "\\" + line if _MD_TRIGGER_RE.match(line) else line


def _cosine(a: List[float], b: List[float]) -> float:
    """两个向量的余弦相似度（手写点积归一，不引入 numpy）。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _select_supporting(
    sentences: List[str], answer: str, embed_fn
) -> List[str]:
    """本地相似度选句：候选句与最终回答算余弦相似度，取最像的 1~2 句。

    复用知识库已加载的 embedding 模型（doc_agent.vecdb.embedding_fn，
    与检索同一套 bge 向量）：检索链路里的「LLM 抽取相关句」已随评测
    定稿关闭（relevance_extractor_config=None），这里只做展示层选句，
    零 API 成本、零额外延迟、不影响回答与评测。
    """
    if len(sentences) == 1:
        return sentences[:1]
    vecs = embed_fn(sentences + [answer])
    scores = [_cosine(v, vecs[-1]) for v in vecs[:-1]]
    ranked = sorted(range(len(sentences)), key=lambda i: -scores[i])
    picked = [sentences[ranked[0]]]
    # 第 2 句几乎同样相关且放得下时一并展示，按原文顺序输出
    if (
        len(ranked) > 1
        and scores[ranked[0]] - scores[ranked[1]] <= _PICK_SCORE_GAP
        and len(picked[0]) + len(sentences[ranked[1]]) <= _PICK_MAX_CHARS
    ):
        picked.append(sentences[ranked[1]])
    return [p for p in sorted(picked, key=sentences.index)]


async def _send_citations(agent: lr.ChatAgent, answer: str = "") -> None:
    """把本轮 doc_qa 的引用来源渲染成「📚 引用来源」消息。

    来源由 DocChatTool.handle 按主 Agent 实例暂存（tools.py），这里取后
    即清。曾用 cl.Text 元素做点击展开卡片：本环境（sqlite 数据层、无云
    存储）下元素走 element/send_step 通道会被静默丢弃（服务端无异常、
    客户端收不到任何事件），故降级为普通消息——与回答走同一条已验证
    可靠的消息通道；只显示文件名，不显示本地绝对路径。

    卡片只展示「命中答案的那句话」：剥掉爬虫元数据行 → 拆句 → 与最终
    回答做本地 embedding 相似度选句（取最像的 1~2 句）→ Markdown 转义
    防「# 来源:」被渲染成标题。选句依赖不可用时兜底显示清洗后的开头
    1~2 句；单条来源处理失败只降级该条，不阻断卡片其余部分。
    """
    sources = get_last_doc_sources(agent)
    if not sources:
        return
    # 文档子 Agent 进程级单例在 user_session（on_chat_start 写入）；
    # vecdb.embedding_fn 即知识库已加载的 bge embedding（直接复用不重载）
    doc_agent = cl.user_session.get("doc_agent")
    embed_fn = getattr(getattr(doc_agent, "vecdb", None), "embedding_fn", None)
    lines = ["📚 引用来源：", ""]
    for num, name, excerpt in sources:
        lines.append(f"**[^{num}] {name}**")
        try:
            text = _strip_metadata(excerpt)
            sentences = _split_sentences(text) if text else []
            if sentences:
                if answer and embed_fn is not None:
                    picked = _select_supporting(sentences, answer, embed_fn)
                else:  # 兜底：无回答文本或无 embedding 函数时取开头 1~2 句
                    picked = sentences[:2]
            else:
                picked = [text[:120]] if text else []
            for sentence in picked:
                lines.append(f"> {_md_escape(sentence)}")
        except Exception:
            pass  # 展示层出错绝不抛到问答主流程，这条引用静默降级
        lines.append("")
    msg = cl.Message(content="\n".join(lines).rstrip())
    await msg.send()


def _compose_with_memory(
    history: list[tuple[str, str]], question: str
) -> str:
    """把「最近 N 轮对话 + 当前问题」组装成一次 LLM 输入。

    langroid 0.67.7 的 ChatAgent.llm_response 默认把 message_history 完整
    喂给 LLM（隐式、无界）。本实现由 run_react 每轮结束清空
    message_history（只留系统消息），跨轮记忆改为在这里显式组装：
    仅保留最近 MAX_MEMORY_TURNS 轮、历史回答截断到
    MAX_MEMORY_ANSWER_CHARS——记忆窗口有界、可审计、可调，
    不再依赖框架隐式累积。
    """
    if not history:
        return question
    lines = [
        "（以下是本会话之前的对话记录，仅作背景参考，不要重复回答历史问题。）"
    ]
    for q, a in history:
        lines.append(f"用户: {q}")
        lines.append(f"助手: {a[:MAX_MEMORY_ANSWER_CHARS]}")
    lines.append(f"\n当前用户问题: {question}")
    return "\n".join(lines)


def run_react(agent: lr.ChatAgent, question: str):
    """手动 ReAct 循环（同步函数，整体跑在 cl.make_async 的线程池线程里）。

    每个 LLM 回合与工具结果 Step 由 ChainlitAgentCallbacks 实时渲染；
    返回值仅用于「无响应」兜底，正常情况不要再发送一遍。

    注意：langroid 文档问答的「批量相关句抽取」走 nest_asyncio 补丁过的
    asyncio.run()，它要求当前线程已有事件循环；而 make_async 的 anyio 工作
    线程没有。这里为当前线程装一个专属循环，用完关闭。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        response = agent.llm_response(question)
        for _ in range(MAX_TOOL_ROUNDS):
            if response is None or not agent.try_get_tool_messages(response):
                break
            response = agent.agent_response(response)  # 执行工具 handle()
            if response is None:
                break
            response = agent.llm_response(response)  # 工具结果交还 LLM
        return response.content if response else None
    finally:
        # 受控记忆：每轮结束清空本轮全部消息（只留 index 0 的系统消息）。
        # langroid 默认把 message_history 完整喂给 LLM（隐式无界记忆），
        # 这里显式切断，跨轮记忆统一由 _compose_with_memory 的窗口提供，
        # token 成本不随轮数线性膨胀（API 抓包验证：默认每轮 +4 条消息）。
        agent.clear_history(1, -1)
        loop.close()
        asyncio.set_event_loop(None)  # 清除线程局部循环，供线程池复用


def _startup_ingest(doc_agent) -> None:
    """启动入库（同步）：docs/ 目录下 PDF/TXT/DOCX/DOC，按「文件名+内容哈希」去重。

    爬虫重跑覆盖 docs/crawled-*.txt 后重启 Web，内容哈希变化即自动重新入库。
    """
    if not DOCS_DIR.is_dir():
        return
    for path in DOCS_DIR.iterdir():
        if path.suffix.lower() in (".txt", ".pdf", ".docx", ".doc"):
            key = _content_key("doc", path.name, str(path))
            ingest_if_needed(doc_agent, str(path), key)


def _content_key(prefix: str, name: str, path: str) -> str:
    """按「名称 + 内容 md5」生成入库去重 key：内容变了哈希变，自动重新入库。"""
    digest = hashlib.md5(Path(path).read_bytes()).hexdigest()
    return f"{prefix}:{name}:{digest}"


def _upload_key(name: str, path: str) -> str:
    """上传文件按内容哈希去重：chainlit 临时路径每次不同，不能按路径记。"""
    return _content_key("upload", name, path)


@cl.on_chat_start
async def on_chat_start():
    # 文档子 Agent：进程级单例 + 首次建库（走线程池，不阻塞事件循环）。
    # Qdrant 本地模式同一目录限单实例，多标签页共享这一份，避免文件锁冲突。
    doc_agent: Optional[DocChatAgent] = None
    ingest_error: Optional[str] = None
    try:
        doc_agent = await cl.make_async(_ensure_doc_agent)()
    except Exception as e:
        ingest_error = str(e)

    # 头部下拉框选中的 profile name（未选择时为 None），据此选模型建 Agent
    profile = cl.user_session.get("chat_profile")
    model = _MODEL_BY_PROFILE.get(profile, MODEL)
    agent = build_main_agent(model)
    # 注入 Chainlit 回调（流式 LLM Step / 工具 Step / 错误 Step 渲染）。
    # 建库在注入之前完成，不会把 ingest 渲染成 Step；回调只绑主 Agent。
    # 默认用 QuietAgentCallbacks（隐藏工具 JSON Step）；WEB_SHOW_TOOL_STEPS=1
    # 时恢复官方默认行为（演示工具调用过程用）。
    if SHOW_TOOL_STEPS:
        lr.ChainlitAgentCallbacks(agent)
    else:
        QuietAgentCallbacks(agent)
    cl.user_session.set("agent", agent)
    cl.user_session.set("doc_agent", doc_agent)
    cl.user_session.set("busy", False)
    cl.user_session.set("memory", [])  # 跨轮对话记忆：[(用户问题, 最终回答), ...]
    cl.user_session.set(DEFAULT_MODEL_KEY, model)

    welcome = (
        "知识库已就绪。点击下方快捷按钮试问，"
        "也可以直接输入问题，或上传 PDF/TXT/DOCX 文档入库后提问。"
    )
    if ingest_error:  # 初始化失败不阻断会话，排名类问题仍可用
        welcome = (
            f"知识库初始化遇到问题（{ingest_error}），文档问答暂不可用。\n\n" + welcome
        )
    await cl.Message(
        content=welcome,
        actions=[
            cl.Action(
                name="quick_question",
                payload={"question": "剑桥大学的 QS 排名是多少？"},
                label="🎓 剑桥 QS 排名",
            ),
            cl.Action(
                name="quick_question",
                payload={"question": "LSE 的雅思要求是多少？"},
                label="📋 LSE 雅思要求",
            ),
            cl.Action(
                name="quick_question",
                payload={"question": "NYU 的 QS 排名和申请截止日期分别是什么？"},
                label="🗽 NYU 排名+截止日期",
            ),
            cl.Action(
                name="export_chat",
                payload={},
                label="📥 导出对话",
            ),
        ],
    ).send()


@cl.on_chat_resume
async def on_chat_resume(_thread: ThreadDict):
    """历史会话恢复：Chainlit 在 resume 分支只触发本回调（不触发
    on_chat_start），所以这里重建主 Agent 与回调。对话记忆 memory 由框架
    自动持久化到线程 metadata 并在恢复前写回 user_session（断线→重连
    即「完整恢复」），无需手动处理；历史消息由框架自动渲染。
    """
    agent = build_main_agent(_session_model())
    if SHOW_TOOL_STEPS:
        lr.ChainlitAgentCallbacks(agent)
    else:
        QuietAgentCallbacks(agent)
    cl.user_session.set("agent", agent)
    cl.user_session.set("busy", False)
    cl.user_session.set("memory", _normalize_memory(cl.user_session.get("memory")))
    try:
        doc_agent = await cl.make_async(_ensure_doc_agent)()
    except Exception:
        doc_agent = _doc_agent  # 建库早已完成；异常时用现有单例兜底
    cl.user_session.set("doc_agent", doc_agent)


@cl.on_message
async def on_message(message: cl.Message):
    doc_agent = cl.user_session.get("doc_agent")
    # 上传的文档先入库（内容哈希去重），再处理提问
    files = await get_text_files(message)  # 官方辅助函数（异步），{文件名: 路径}
    if files:
        if doc_agent is None:
            await cl.Message(content="知识库未初始化，暂时无法接收上传文档。").send()
        else:
            for name, path in files.items():
                key = _upload_key(name, path)
                done = await cl.make_async(ingest_if_needed)(doc_agent, path, key)
                await cl.Message(
                    content=f"文档《{name}》{'已入库' if done else '此前已入库，跳过'}。"
                ).send()
        if not message.content.strip():
            await cl.Message(content="文档已就绪，请直接提问。").send()
            return
    await _handle_question(message.content)


async def _handle_question(question: str):
    """统一问答入口（输入框消息与快捷按钮共用）。

    串行化：agent.message_history 非线程安全，check-then-set 在首个 await
    之前完成（chainlit 同一事件循环线程派发，天然原子）。
    """
    if cl.user_session.get("busy"):
        await cl.Message(content="上一条消息还在处理中，请稍候……").send()
        return
    agent = cl.user_session.get("agent")
    if agent is None:
        # 会话刚建立、on_chat_start 还在初始化（启动入库 + agent 构建）时
        # 用户就发了消息：回友好提示，而不是跑出「处理出错」
        await cl.Message(content="知识库还在初始化，请稍候几秒再问……").send()
        return
    cl.user_session.set("busy", True)
    try:
        # 手动 ReAct 循环整体入线程池；LLM 流式回答与工具 Step 由回调渲染
        # 跨轮记忆：把本会话最近 N 轮问答显式拼进上下文再交给 Agent
        # （框架隐式历史已被 run_react 每轮清空，见 _compose_with_memory）
        history = _normalize_memory(cl.user_session.get("memory"))
        prompt = _compose_with_memory(history, question)
        answer = await cl.make_async(run_react)(agent, prompt)
        if answer is None:
            await cl.Message(content="（未获得有效回答）").send()
        else:
            history.append((question, answer))
            cl.user_session.set("memory", history[-MAX_MEMORY_TURNS:])
            # 会话标题：取首个用户问题前 20 字（侧栏「Past Chats」列表显示）。
            # user_session["name"] 在断线时随线程 metadata 持久化，
            # 会话标题由此跨重启/跨设备保留。
            if not cl.user_session.get("name"):
                cl.user_session.set("name", question.strip()[:20])
        await _send_citations(agent, answer or "")
    except Exception as e:  # 回调已渲染错误 Step 的场景会轻微重复，可接受
        traceback.print_exc()
        await cl.Message(content=f"处理出错：{e}").send()
    finally:
        cl.user_session.set("busy", False)


@cl.action_callback("quick_question")
async def on_quick_question(action: cl.Action):
    """快捷问题按钮：以用户身份补发一条消息（会话历史保持完整），
    再走与输入框相同的统一问答流程。
    """
    question = action.payload.get("question", "")
    await cl.Message(content=question, author="user").send()
    await _handle_question(question)


@cl.action_callback("export_chat")
async def on_export_chat(_action: cl.Action):
    """把当前会话导出为 Markdown 文件。

    cl.chat_context.get() 返回 Message 对象列表（2.12.0 API，不是
    ThreadDict）；恢复的历史会话由框架在 resume 时重新填回
    （socket.py 把历史 step 转成 Message 加回 chat_context），
    因此导出对历史会话同样完整。
    """
    messages = cl.chat_context.get()
    lines = ["# 留学助手对话记录", "", f"- 导出时间：{datetime.now():%Y-%m-%d %H:%M}", ""]
    for message in messages:
        if message.type == "user_message":
            lines.append(f"**问：** {message.content}")
            lines.append("")
        elif message.type == "assistant_message":
            output = message.content or ""
            # 纯界面元素不入导出：欢迎横幅 / 引用卡片（正文摘录在
            # elements 里，此处只有标题，导出会缺内容）
            if output.startswith(("知识库已就绪", "📚 引用来源")):
                continue
            lines.append(f"**答：** {output}")
            lines.append("")
    md = "\n".join(lines)
    name = f"chat-export-{datetime.now():%Y%m%d-%H%M%S}.md"
    # 本地数据层没有云存储客户端（元素不入库），cl.File(path=...) 前端拿
    # 不到下载地址。改走 session.persist_file：文件写入会话临时目录并登记
    # 到会话 files 表，/project/file/{id} 路由直接以 FileResponse 下发。
    file_ref = await cl.context.session.persist_file(
        name=name, mime="text/markdown", content=md
    )
    url = f"/project/file/{file_ref['id']}?session_id={cl.context.session.id}"
    await cl.Message(
        content="📥 对话已导出为 Markdown（点击下载）：",
        elements=[cl.File(name=name, url=url, display="inline")],
    ).send()
