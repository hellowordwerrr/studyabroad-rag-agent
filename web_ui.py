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

用法: .\\web.ps1  （或 .venv\\Scripts\\python.exe -m chainlit run web_ui.py -w）
      浏览器打开 http://localhost:8000
"""

import asyncio
import hashlib
import os
import threading
import traceback
from pathlib import Path
from typing import Optional

import chainlit as cl
from chainlit import run_sync
from dotenv import load_dotenv

import langroid as lr
import langroid.language_models as lm
from langroid.agent.callbacks.chainlit import get_text_files
from langroid.agent.special.doc_chat_agent import DocChatAgent

from doc_qa import DEFAULT_EMBED_MODEL, build_doc_agent, ingest_if_needed
from tools import DeepSeekChatAgent, DocChatTool, RankingTool, set_doc_agent

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

MODEL = os.getenv("WEB_MODEL", "deepseek/deepseek-chat")
EMBED_MODEL = os.getenv("WEB_EMBED_MODEL", DEFAULT_EMBED_MODEL)
# 默认隐藏工具调用的 JSON Step（界面更简洁）；面试演示想看工具调用过程时，
# 设置环境变量 WEB_SHOW_TOOL_STEPS=1 再启动即可
SHOW_TOOL_STEPS = os.getenv("WEB_SHOW_TOOL_STEPS", "") == "1"
DOCS_DIR = Path(__file__).parent / "docs"
MAX_TOOL_ROUNDS = 8  # 手动 ReAct 循环的保险上限，防止工具调度死循环
# 对话记忆：每会话保留最近 N 轮（问答对）拼进上下文，超出窗口的最旧轮丢弃
MAX_MEMORY_TURNS = 6
# 历史回答截断长度（字符）：控制上下文 token 成本，保留关键结论即可
MAX_MEMORY_ANSWER_CHARS = 500

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
    """隐藏工具调用与工具结果的 JSON Step，界面只保留最终回答。

    官方回调把 Agent 的「思考过程」可视化：LLM 输出的工具调用参数
    （如 {"request": "ranking_lookup", ...}）渲染成一个 Step，工具返回的
    结果 JSON 再渲染成一个 Step。调试很有用，但日常使用显得杂乱。
    本子类把这两类 Step 都去掉：工具结果会由主 Agent 转述进最终回答，
    不影响内容完整性。
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
            reasoning=reasoning,
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
            reasoning=reasoning,
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


def build_main_agent() -> lr.ChatAgent:
    """主 Agent：每会话一个（各自独立的消息历史），挂载两个工具。

    用 DeepSeekChatAgent 子类：兼容 DeepSeek 偶发输出的原生 DSML 工具
    调用格式（langroid 0.67.7 不识别，不处理则用户随机看到 XML 原文）。
    """
    agent = DeepSeekChatAgent(
        lr.ChatAgentConfig(
            llm=lm.OpenAIGPTConfig(chat_model=MODEL),
            system_message=SYSTEM_MESSAGE,
        )
    )
    agent.enable_message(RankingTool)
    agent.enable_message(DocChatTool)
    return agent


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

    agent = build_main_agent()
    # 注入 Chainlit 回调（流式 LLM Step / 工具 Step / 错误 Step 渲染）。
    # 建库在注入之前完成，不会把 ingest 渲染成 Step；回调只绑主 Agent。
    # 默认用 QuietAgentCallbacks（隐藏工具 JSON Step）；WEB_SHOW_TOOL_STEPS=1
    # 时恢复官方默认行为（面试演示工具调用过程用）。
    if SHOW_TOOL_STEPS:
        lr.ChainlitAgentCallbacks(agent)
    else:
        QuietAgentCallbacks(agent)
    cl.user_session.set("agent", agent)
    cl.user_session.set("doc_agent", doc_agent)
    cl.user_session.set("busy", False)
    cl.user_session.set("memory", [])  # 跨轮对话记忆：[(用户问题, 最终回答), ...]

    welcome = (
        "知识库已就绪。试试：\n"
        "- 「UCL 的 QS 排名是多少？」\n"
        "- 「LSE 的雅思要求是多少？」\n"
        "- 「NYU 的 QS 排名和申请截止日期分别是什么？」\n\n"
        "也可以直接在对话框上传 PDF/TXT/DOCX 文档，入库后即可提问。"
    )
    if ingest_error:  # 初始化失败不阻断会话，排名类问题仍可用
        welcome = (
            f"知识库初始化遇到问题（{ingest_error}），文档问答暂不可用。\n\n" + welcome
        )
    await cl.Message(content=welcome).send()


@cl.on_message
async def on_message(message: cl.Message):
    # 串行化：agent.message_history 非线程安全。check-then-set 在首个 await
    # 之前完成，chainlit 在同一事件循环线程上派发，天然原子。
    if cl.user_session.get("busy"):
        await cl.Message(content="上一条消息还在处理中，请稍候……").send()
        return
    cl.user_session.set("busy", True)

    agent = cl.user_session.get("agent")
    doc_agent = cl.user_session.get("doc_agent")
    try:
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

        # 手动 ReAct 循环整体入线程池；LLM 流式回答与工具 Step 由回调渲染
        # 跨轮记忆：把本会话最近 N 轮问答显式拼进上下文再交给 Agent
        # （框架隐式历史已被 run_react 每轮清空，见 _compose_with_memory）
        history = cl.user_session.get("memory") or []
        prompt = _compose_with_memory(history, message.content)
        answer = await cl.make_async(run_react)(agent, prompt)
        if answer is None:
            await cl.Message(content="（未获得有效回答）").send()
        else:
            history.append((message.content, answer))
            cl.user_session.set("memory", history[-MAX_MEMORY_TURNS:])
    except Exception as e:  # 回调已渲染错误 Step 的场景会轻微重复，可接受
        traceback.print_exc()
        await cl.Message(content=f"处理出错：{e}").send()
    finally:
        cl.user_session.set("busy", False)
