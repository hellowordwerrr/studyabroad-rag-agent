"""
自定义 Agent 工具：院校排名查询（Langroid ToolMessage 模式）

Langroid 的 ReAct 工具调用流程：
1. LLM 根据 request/purpose/字段自动生成工具的 JSON 参数
2. Agent 检测到工具调用 → 实例化本类 → 执行 handle()
3. handle() 的返回文本交还给 LLM，由其结合上下文组织最终回答

二次开发要点：
- 字段名（school_name/country）即工具参数 schema，LLM 会按字段填参
- handle() 是纯 Python 逻辑，可以换成任意数据源（API/数据库）
- examples() 提供 few-shot 示例，帮助 LLM 正确使用工具
"""

import html
import json
import re
import threading
from pathlib import Path
from typing import List, Optional

from thefuzz import process

import langroid as lr
from langroid.agent.base import ChatDocument
from langroid.agent.special.doc_chat_agent import DocChatAgent
from langroid.agent.tool_message import ToolMessage

DATA_PATH = Path(__file__).parent / "data" / "rankings.json"
META_PATH = Path(__file__).parent / "data" / "meta.json"


def _data_meta() -> dict:
    """读取数据时效元数据（update_rankings.py 更新数据时写入）。"""
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

# 由 chat.py 启动时注入：DocChatTool 复用的文档问答子 Agent 实例
_doc_agent: Optional[DocChatAgent] = None
# 共享子 Agent 的调用锁：Web 多会话并发触发 doc_qa 时串行化，避免内部状态竞争
_doc_agent_lock = threading.Lock()
# 引用来源暂存：键 = 主 Agent 实例 id（langroid 会把调用方主 Agent 注入
# handle(agent)），值 = [(引用号, 文档名, 摘录)]。Web 层在同一会话内
# 问答结束后读取并渲染「引用来源」卡片，不泄漏本地绝对路径。
_last_sources: dict[int, List[tuple]] = {}


def set_doc_agent(agent: DocChatAgent) -> None:
    """注册文档问答子 Agent（chat.py 建库后调用）。"""
    global _doc_agent
    _doc_agent = agent


def get_last_doc_sources(agent: lr.ChatAgent) -> List[tuple]:
    """取某主 Agent 最近一次 doc_qa 调用的引用来源，取后即清（防串轮）。

    返回 [(引用号, 文档名, 摘录)]，无则空列表。CLI 入口不读。
    """
    return _last_sources.pop(id(agent), [])


class RankingTool(ToolMessage):
    request: str = "ranking_lookup"
    purpose: str = """
        查询院校的 QS 世界大学排名与基本信息。
        当用户询问某所大学的排名、所在国家/城市等信息，
        或询问知识库文档中没有覆盖的院校信息时，使用本工具。
        使用本工具时，只输出调用 JSON，不要输出其他内容；
        拿到工具返回结果后，再结合结果组织回答。
        """
    school_name: str
    country: str = ""

    def handle(self) -> str:
        """按校名（支持中英文/常见缩写，模糊匹配）查询本地院校数据。"""
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))

        # 建立 别名 -> 院校 的映射，支持 "剑桥大学"/"Cambridge"/"UCL" 等写法
        alias_map: dict[str, dict] = {}
        for school in data:
            for alias in [school["name"], school["zh_name"], *school.get("aliases", [])]:
                alias_map[alias] = school

        match, score = process.extractOne(self.school_name, list(alias_map.keys()))
        if score < 60:
            names = "、".join(s["zh_name"] for s in data)
            return (
                f"未找到与“{self.school_name}”匹配的院校。"
                f"当前可查询的院校包括：{names}。"
            )
        school = alias_map[match]
        meta = _data_meta()
        edition = meta.get("edition", "QS 2026")
        result = {
            "校名": school["name"],
            "中文名": school["zh_name"],
            f"{edition} 排名": school["qs_rank"],
            "国家/地区": school["country"],
            "城市": school["city"],
            "备注": school["note"],
        }
        if meta.get("updated_at"):
            result["数据更新时间"] = meta.get("updated_at")  # 时效元数据，避免用过期的旧数据
        return json.dumps(result, ensure_ascii=False, indent=2)

    @classmethod
    def examples(cls) -> List["ToolMessage"]:
        return [
            cls(school_name="UCL"),
            cls(school_name="剑桥大学"),
        ]


# 引用来源行格式：^[n] 后跟文档路径（本地绝对路径），只保留文件名展示
_CITE_RE = re.compile(r"^\[\^(\d+)\]\s+(.+)$")
# 引用卡片里每篇文档的原文摘录截断长度（安全上限）。上限要能覆盖一个完整
# 检索块（英文块约 200 词 ≈ 1200+ 字符）：卡片在展示层按「与回答最相似的
# 句子」选句（web_ui._send_citations），这里截得太短会把支持句截没，
# 选句无从谈起。2000 字符覆盖 ~300 词，块内句子不会因截断缺席。
_CITE_EXCERPT_CHARS = 2000


def _parse_citations(source_content: str) -> List[tuple]:
    """把 langroid 引用文本解析成 [(引用号, 文档名, 摘录)]。

    输入形如（每篇引用 = 一行「[^n] 文档路径」+ 后续 4 空格缩进的原文摘录）：

        [^4] D:\\study-abroad-qa\\docs\\crawled-lse-....txt
            Graduate programmes at LSE are demanding ...

    路径只取文件名（绝对路径是本地信息，不进界面）；摘录行保留换行
    （展示层按行/句拆开选句），截断到 _CITE_EXCERPT_CHARS 作为安全上限。
    """
    entries: List[List] = []
    current: Optional[List] = None
    for line in (source_content or "").splitlines():
        m = _CITE_RE.match(line.strip())
        if m:
            name = Path(m.group(2).strip()).name or m.group(2).strip()
            current = [int(m.group(1)), name, []]
            entries.append(current)
        elif current is not None and line.strip():
            current[2].append(line.strip())  # 摘录行（缩进已在 strip 中去掉）
    return [
        (num, name, "\n".join(excerpt)[:_CITE_EXCERPT_CHARS])
        for num, name, excerpt in entries
    ]


class DocChatTool(ToolMessage):
    """文档问答工具：把 DocChatAgent 封装为子 Agent 工具（多智能体协作）。

    为什么这样设计：DocChatAgent 的 llm_response 走「检索→回答」专用流程，
    本身不参与主 Agent 的 ReAct 工具循环；无相关文档时直接返回 DO-NOT-KNOW。
    所以由主 ChatAgent 判断问题类型，文档类问题通过本工具交给子 Agent 处理。
    """

    request: str = "doc_qa"
    purpose: str = """
        查询本地知识库中的留学申请文档。
        当用户询问文档内容（申请要求、雅思/托福成绩、截止日期、申请材料、
        费用等）时，使用本工具。
        使用本工具时，只输出调用 JSON，不要输出其他内容；
        拿到工具返回的答案后，转述给用户并保留其中的引用标注。
        """
    query: str

    def handle(self, agent: Optional[lr.ChatAgent] = None) -> str:
        """把问题转交给文档问答子 Agent（检索 + 带引用的回答）。

        langroid 会把调用方主 Agent 注入 agent 参数（base.py 按注解识别）；
        据此把本次回答的引用来源（langroid metadata.source_content 格式：
        「[^n] 文档路径」行 + 缩进原文摘录）解析后按主 Agent 暂存，
        供 Web 层渲染「引用来源」卡片。CLI 等不注入 agent 的入口自动跳过。
        """
        if _doc_agent is None:
            return "知识库尚未初始化，请告知用户稍后再试。"
        with _doc_agent_lock:  # Web 多会话共享同一子 Agent，调用串行化
            response = _doc_agent.llm_response(self.query)
        if response is None or not response.content:
            return "文档中没有找到相关信息，请如实告知用户。"
        if agent is not None:
            # langroid 0.67.7 的 DocChatAgent.llm_response 返回包装只复制了
            # metadata.source（「[^n] 路径」标题行），完整的 source_content
            # （标题行 + 缩进原文摘录）保存在子 Agent 的 .response 里
            # （answer_from_docs 里赋值，见 doc_chat_agent.py）。这里从
            # .response 取完整引用，解析成 [(引用号, 文档名, 摘录)] 按主
            # Agent 暂存，供 Web 层渲染「引用来源」卡片，不泄漏本地绝对路径。
            saved = _doc_agent.response
            src = getattr(getattr(saved, "metadata", None), "source_content", None) or ""
            if not src:
                # 兜底：部分路径（如会话恢复后）没有完整摘录，用标题行
                src = getattr(response.metadata, "source", None) or ""
            _last_sources[id(agent)] = _parse_citations(src)
        return response.content

    @classmethod
    def examples(cls) -> List["ToolMessage"]:
        return [
            cls(query="LSE 的雅思要求是多少？"),
            cls(query="NYU 硕士申请截止日期是什么时候？"),
        ]


# ---------------------------------------------------------------------------
# DeepSeek 原生 DSML 工具调用格式兼容（实测踩坑，见 README「工具调用踩坑记录」）
# ---------------------------------------------------------------------------

# DeepSeek 偶发不按 OpenAI tool_calls 协议输出工具调用，而是把原生 DSML 格式
# 写进 content。实测输出格式不规则，至少两种变体都出现过：
#   变体 A：全角竖线单分隔、无 DSML 中缀、invoke 前无空格（eval rank-05 实测）
#   变体 B：双全角竖线夹 DSML 中缀、invoke 前带空格（Web 场景一/三实测）
# 因此标签名匹配用「宽容前缀」：以 < 开头、到 invoke/parameter 为止的
# 前缀只允许 全角竖线/DSML/空白，不精确假设竖线数量与空格位置；
# 闭合标签同样宽容（参数闭合可能是普通 </parameter> 或带全角竖线的形式）。
_DSML_BAR = "｜"  # 全角竖线（U+FF5C），DSML 标签分隔符
_INVOKE_OPEN_RE = re.compile("<([^>]*?)invoke\\b([^>]*)>")
_PARAM_OPEN_RE = re.compile("<([^>]*?)parameter\\b([^>]*)>")
_INVOKE_CLOSE_RE = re.compile("</[^>]*?invoke[^>]*>")
_ATTR_NAME_RE = re.compile('name="([^"]*)"')


def _dsml_prefix_ok(prefix: str) -> bool:
    """DSML 标签名前缀校验：只允许全角竖线/DSML/空白，杜绝误匹配普通文本。"""
    p = prefix.replace(" ", "")
    return bool(p) and all(c in _DSML_BAR + "DSML" for c in p)


def parse_dsml_tool_messages(
    agent: lr.ChatAgent, content: str
) -> List[ToolMessage]:
    """把 DeepSeek DSML 格式的工具调用解析成 agent 已注册的 ToolMessage 实例。

    langroid 0.67.7 只识别 OpenAI tool_calls 与 JSON/XML ToolMessage 格式；
    DSML 不被识别时工具不执行，DSML 原文会成为最终回答（用户看到 XML）。
    这里按 invoke 的 name（即 request 名）在 agent.llm_tools_map 里找工具类，
    用 parameter 参数实例化；解析失败/未知工具/非 DSML 文本静默跳过。
    """
    if not content or "invoke" not in content:
        return []
    tools = []
    for m in _INVOKE_OPEN_RE.finditer(content):
        if not _dsml_prefix_ok(m.group(1)):  # 标签名前缀必须是 DSML 风格
            continue
        name_m = _ATTR_NAME_RE.search(m.group(2))
        if not name_m:
            continue
        name = name_m.group(1).strip()
        tool_cls = agent.llm_tools_map.get(name)
        if tool_cls is None:
            continue
        # invoke 闭合标签（参数闭合标签不含 invoke，不会误停）
        rest = content[m.end():]
        close_m = _INVOKE_CLOSE_RE.search(rest)
        body = rest[: close_m.start()] if close_m else rest
        params = {}
        for pm in _PARAM_OPEN_RE.finditer(body):
            if not _dsml_prefix_ok(pm.group(1)):
                continue
            pname_m = _ATTR_NAME_RE.search(pm.group(2))
            if not pname_m:
                continue
            val = body[pm.end():]
            nxt = val.find("<")  # 参数值到下一个标签开始为止
            val = val[:nxt] if nxt >= 0 else val
            params[pname_m.group(1).strip()] = html.unescape(val.strip())
        try:
            tools.append(tool_cls(**params))  # pydantic 校验，字段不符直接跳过
        except Exception:
            continue
    return tools


class DeepSeekChatAgent(lr.ChatAgent):
    """兼容 DeepSeek DSML 工具调用格式的主 ChatAgent 子类。

    get_tool_messages 是框架唯一工具提取入口（try_get_tool_messages 与
    Task.run 内部都经由它），框架解析不到工具时回退解析 DSML，并把结果
    缓存进 msg.tool_messages——手动 ReAct 循环里的 agent_response 会直接
    复用并执行。实测 DSML 出现频率约 1/5 轮，不改则用户随机看到 XML 原文。
    """

    def get_tool_messages(
        self,
        msg: str | ChatDocument | None,
        all_tools: bool = False,
    ) -> List[ToolMessage]:
        tools = super().get_tool_messages(msg, all_tools)
        if tools:
            return tools
        content = msg.content if isinstance(msg, ChatDocument) else (msg or "")
        dsml = parse_dsml_tool_messages(self, content)
        if dsml and isinstance(msg, ChatDocument):
            msg.tool_messages = dsml  # 缓存，供 agent_response 复用
        return dsml
