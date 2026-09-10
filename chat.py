"""
留学申请文档问答知识库（基于 Langroid 二次开发）

架构（二次开发核心设计）：
    主 ChatAgent（ReAct 工具调度）
      ├── ranking_lookup 工具 —— 本地 QS 排名/院校信息查询
      └── doc_qa 工具 —— 转交给 DocChatAgent 子 Agent（检索 + 带引用回答）
    即：主 Agent 判断问题类型 → 调用对应工具 → 拿到结果后组织最终回答。

用法:
    python chat.py <文档路径或文件夹>          # 交互式问答
    python chat.py <路径> --model deepseek/deepseek-reasoner   # 换 R1 推理模型
    python chat.py <路径> --embed-model BAAI/bge-large-zh-v1.5 # 换 embedding 模型

说明:
    - DeepSeek 密钥从 .env 文件或环境变量 DEEPSEEK_API_KEY 读取
    - embedding 用本地 fastembed（ONNX，免 GPU/torch），首次运行自动下载模型
    - 向量库用 Qdrant 本地模式，数据存在 .qdrant/data，无需 Docker
"""

import typer
from rich import print
from dotenv import load_dotenv

import langroid as lr
import langroid.language_models as lm

from doc_qa import build_doc_agent, ingest_if_needed
from tools import DeepSeekChatAgent, DocChatTool, RankingTool, set_doc_agent

load_dotenv()

app = typer.Typer()


@app.command()
def main(
    doc: str = typer.Argument(..., help="本地文件或文件夹路径（支持 PDF/TXT/DOCX）"),
    model: str = typer.Option("deepseek/deepseek-chat", "--model", "-m", help="LLM 模型名"),
    embed_model: str = typer.Option(
        "BAAI/bge-small-zh-v1.5", "--embed-model", "-e", help="fastembed embedding 模型名"
    ),
):
    # 1. 文档问答子 Agent：负责建库 + 检索 + 带引用回答
    doc_agent = build_doc_agent(model=model, embed_model=embed_model)
    set_doc_agent(doc_agent)  # 注入给 DocChatTool 复用

    print("[blue]正在解析文档并建立向量索引...")
    ingest_if_needed(doc_agent, doc)

    # 2. 主 Agent：ReAct 工具调度（决策调用哪个工具，组织最终回答）
    #    用 DeepSeekChatAgent 子类：兼容 DeepSeek 偶发的原生 DSML 工具调用格式
    agent = DeepSeekChatAgent(
        lr.ChatAgentConfig(
            llm=lm.OpenAIGPTConfig(chat_model=model),
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
    # 二次开发：为主 Agent 挂载自定义工具（多智能体 + ReAct 工具调用）
    agent.enable_message(RankingTool)
    agent.enable_message(DocChatTool)

    print("[green]知识库就绪，开始提问吧（输入 x 或 q 退出）")
    lr.Task(agent).run()


if __name__ == "__main__":
    app()
