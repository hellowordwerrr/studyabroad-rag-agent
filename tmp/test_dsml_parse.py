"""DSML 解析器单测：两种实测变体 + 混合内容 + 未知工具，不耗 LLM。

变体 A/B 均取自真实抓包（eval/report.txt 与 Web 事件流）。
所有 DSML 字符串由全角竖线码点拼接构造，源文件不含字面标签序列。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

import langroid as lr
import langroid.language_models as lm

from tools import (
    DeepSeekChatAgent,
    DocChatTool,
    RankingTool,
    parse_dsml_tool_messages,
)

agent = DeepSeekChatAgent(
    lr.ChatAgentConfig(
        llm=lm.OpenAIGPTConfig(chat_model="deepseek/deepseek-chat"),
        system_message="test",
    )
)
agent.enable_message(RankingTool)
agent.enable_message(DocChatTool)

BAR = "｜"  # 全角竖线（DSML 标签分隔符）

# 变体 A：单竖线、无 DSML 中缀、invoke 前无空格（eval rank-05 实测）
VAR_A = (
    "<" + BAR + 'invoke name="ranking_lookup">\n'
    + "<" + BAR + 'parameter name="request" string="true">ranking_lookup'
    + "</parameter>\n"
    + "<" + BAR + 'parameter name="school_name" string="true">哈佛大学'
    + "</parameter>\n"
    + "<" + BAR + 'parameter name="country" string="true"></parameter>\n'
    + "</" + BAR + "invoke>"
)

# 变体 B：双竖线夹 DSML、invoke/parameter 前带空格（Web 场景一/三实测）
VAR_B = (
    "<" + BAR + BAR + "DSML" + BAR + BAR + "function_calls>\n"
    + "<" + BAR + BAR + "DSML" + BAR + BAR + ' invoke name="doc_qa">\n'
    + "<" + BAR + BAR + "DSML" + BAR + BAR
    + ' parameter name="query" string="true">LSE MSc Finance 学费是多少？'
    + "</" + BAR + BAR + "DSML" + BAR + BAR + " parameter>\n"
    + "<" + BAR + BAR + "DSML" + BAR + BAR
    + ' parameter name="request" string="true">doc_qa'
    + "</" + BAR + BAR + "DSML" + BAR + BAR + " parameter>\n"
    + "</" + BAR + BAR + "DSML" + BAR + BAR + " invoke>\n"
    + "</" + BAR + BAR + "DSML" + BAR + BAR + "function_calls>"
)

# 未知工具名：应静默跳过
UNKNOWN = (
    "<" + BAR + BAR + "DSML" + BAR + BAR + ' invoke name="no_such_tool">'
    + "<" + BAR + BAR + "DSML" + BAR + BAR + ' parameter name="x">1'
    + "</" + BAR + BAR + "DSML" + BAR + BAR + " parameter>"
    + "</" + BAR + BAR + "DSML" + BAR + BAR + " invoke>"
)

cases = [
    ("变体A(ranking)", VAR_A),
    ("变体B(doc_qa)", VAR_B),
    ("混合A+B", VAR_A + "\n\n" + VAR_B),
    ("未知工具", UNKNOWN),
    ("普通文本", "牛津大学位于英国，请问还有其他问题吗？"),
]

ok = True
for label, s in cases:
    tools = parse_dsml_tool_messages(agent, s)
    desc = [(type(t).__name__, t.request) for t in tools]
    print(f"{label}: {desc}", flush=True)
    if label == "变体A(ranking)" and not (
        tools
        and tools[0].request == "ranking_lookup"
        and tools[0].school_name == "哈佛大学"
    ):
        ok = False
    if label == "变体B(doc_qa)" and not (
        tools
        and tools[0].request == "doc_qa"
        and "LSE MSc Finance" in tools[0].query
    ):
        ok = False
    if label == "混合A+B" and not (
        len(tools) == 2
        and tools[0].request == "ranking_lookup"
        and tools[1].request == "doc_qa"
    ):
        ok = False
    if label == "未知工具" and tools:
        ok = False
    if label == "普通文本" and tools:
        ok = False

print("ALL PASS" if ok else "FAILED", flush=True)
sys.exit(0 if ok else 1)
