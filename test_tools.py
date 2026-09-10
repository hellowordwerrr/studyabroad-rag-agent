r"""
非交互测试：验证主 Agent 的两种工具路由
  1) ranking_lookup —— 排名/院校信息类问题
  2) doc_qa —— 文档内容类问题（转交 DocChatAgent 子 Agent）
  3) 混合问题 —— 期望两个工具协作
用法: .\.venv\Scripts\python.exe test_tools.py
"""

import shutil
from pathlib import Path

from dotenv import load_dotenv
from rich import print

import langroid as lr
import langroid.language_models as lm

from doc_qa import build_doc_agent
from tools import DeepSeekChatAgent, DocChatTool, RankingTool, set_doc_agent

load_dotenv()

MODEL = "deepseek/deepseek-chat"
DOCS = ["docs/sample-lse.txt", "docs/sample-nyu.txt"]

# 每次测试从干净向量库开始，保证结果可复现。
# 用 tmp 专属目录：与运行中的 Web 服务（.qdrant/data）互不抢 Qdrant 文件锁
TEST_STORE = Path(__file__).parent / "tmp" / "test_tools_qdrant"
shutil.rmtree(TEST_STORE, ignore_errors=True)

# 1. 文档问答子 Agent + 建库
doc_agent = build_doc_agent(MODEL, storage_path=str(TEST_STORE))
set_doc_agent(doc_agent)
print("[blue]正在解析文档并建立向量索引...")
doc_agent.ingest_doc_paths(DOCS)

# 2. 主 Agent（ReAct 工具调度；DeepSeekChatAgent 兼容 DeepSeek DSML 格式）
agent = DeepSeekChatAgent(
    lr.ChatAgentConfig(
        llm=lm.OpenAIGPTConfig(chat_model=MODEL),
        system_message=(
            "你是一个留学申请助手。回答规则：\n"
            "1. 文档内容类问题（申请要求、语言成绩、截止日期、材料、费用等）："
            "调用 doc_qa 工具，并把返回答案中的引用标注一并保留。\n"
            "2. 院校排名/所在城市/基本信息类问题：调用 ranking_lookup 工具。\n"
            "3. 用户问题可能同时涉及多个信息点（如既问排名又问截止日期），"
            "此时必须逐个调用所有相关工具，全部查完后再统一组织回答，"
            "不要因为涉及多个方面就反问用户。\n"
            "4. 拿到工具结果后，用中文自然语言组织回答；工具查不到的如实告知。\n"
            "5. 其他闲聊问题直接回答，不要调用工具。"
        ),
    )
)
agent.enable_message(RankingTool)
agent.enable_message(DocChatTool)

questions = [
    "UCL 的 QS 排名是多少？",
    "剑桥大学世界排名第几？",
    "LSE 的雅思要求是多少？",
    "NYU 的 QS 排名和硕士申请截止日期分别是什么？",
]

def run_with_tools(question: str):
    """手动 ReAct 循环（等价于 Task 内部的工具循环，但结束条件确定）：
    LLM 调用 → 检测工具调用 → 执行 handle() → 结果交还 LLM → 重复，
    直到 LLM 响应中不再包含工具调用，返回最终回答。
    """
    response = agent.llm_response(question)
    while response is not None and agent.try_get_tool_messages(response):
        # 执行工具（handle），得到工具结果
        response = agent.agent_response(response)
        if response is None:
            break
        # 把工具结果交还 LLM，继续 ReAct 循环
        response = agent.llm_response(response)
    return response.content if response else "（无响应）"


for q in questions:
    print(f"\n[cyan]问：{q}")
    answer = run_with_tools(q)
    print(f"[green]答：{answer}")
