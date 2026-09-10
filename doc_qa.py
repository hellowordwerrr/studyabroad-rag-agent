"""
文档问答子 Agent 工厂

架构说明（二次开发核心设计）：
- DocChatAgent 的 llm_response 走「检索→回答」专用流程，不支持工具调用；
  无相关文档时直接返回 DO-NOT-KNOW
- 因此采用官方推荐的多智能体模式：
    主 ChatAgent（工具调度）+ DocChatAgent（文档问答，封装为 doc_qa 工具）
- 主 Agent 判断用户问题类型 → 决定调用哪个工具 → 拿到结果后组织最终回答
"""

import os
from pathlib import Path

import langroid.language_models as lm
from langroid.agent.special.doc_chat_agent import DocChatAgent, DocChatAgentConfig
from langroid.embedding_models.models import FastEmbedEmbeddingsConfig
from langroid.vector_store.qdrantdb import QdrantDBConfig

DEFAULT_EMBED_MODEL = "BAAI/bge-small-zh-v1.5"

INGEST_MARKER = Path(__file__).parent / ".qdrant" / "ingested.txt"


def ingest_if_needed(agent: DocChatAgent, doc: str, key: str | None = None) -> bool:
    """建库去重守卫：同一批文档已入库则跳过，避免重复 chunk 污染检索结果。

    Langroid 的 ingest_doc_paths 本身不查重，重复启动会重复入库。
    想强制重建知识库时，删除 .qdrant 目录即可。

    key: 入库标记键，缺省为文档绝对路径（CLI 用法）。Web 上传文件的临时
        路径每次不同，需由调用方传入内容哈希键才能正确去重。
    返回 True 表示本次实际执行了入库。
    """
    key = key or os.path.abspath(doc)
    if INGEST_MARKER.exists() and key in INGEST_MARKER.read_text(
        encoding="utf-8"
    ).splitlines():
        return False
    agent.ingest_doc_paths([doc])
    INGEST_MARKER.parent.mkdir(parents=True, exist_ok=True)
    with INGEST_MARKER.open("a", encoding="utf-8") as f:
        f.write(key + "\n")
    return True


def build_doc_agent(
    model: str,
    embed_model: str = DEFAULT_EMBED_MODEL,
    storage_path: str = ".qdrant/data",
) -> DocChatAgent:
    """创建文档问答子 Agent（本地 Qdrant + fastembed + DeepSeek）

    storage_path: 向量库目录。默认 ".qdrant/data"；评测脚本（eval.py）传
        tmp 副本目录，避免与运行中的 Web 服务抢 Qdrant 文件锁。

    检索配置说明（跨语言场景实测结论）：
    - 关闭 BM25/模糊匹配：它们是同语言关键词匹配，中文问题查英文文档时，
      会把「LSE」等英文关键词密度高的无关页面顶到最前，挤掉真正相关的
      中文/英文块（实测「LSE 的雅思要求」被费用页盖过）。纯稠密检索
      （bge-small-zh）对中英混查反而排序更准。
    - 候选块数 = 语料块数（当前 10）：跨语言检索的相似度分整体不高
      （0.5-0.7），中文 sample 块常排第 6-8 位，任何小于语料规模的
      top-k 截断都会随机漏掉真命中文档——首轮（33/46）与二轮（38/46）
      评测的失败题全部由此引起（含答案侧 DO-NOT-KNOW）。小知识库直接
      全量召回最稳（检索不耗 LLM，只增加最终回答的输入 token），语料
      变大后再调回固定 k。

    回答噪声治理（首轮全量评测 33/46 的根因，逐项实测定位）：
    - assistant_mode=True：本 Agent 由 DocChatTool 封装成子 Agent，主 Agent
      传来的 query 已是独立问题，跳过 followup_to_standalone 改写环节。
      实测该环节会把中文问题随机翻译成英文再检索（「LSE 的申请截止日期」
      → "What is the application deadline..."），英文检索命中不了
      sample-lse.txt 的中文块，直接导致「检索未命中」。
    - relevance_extractor_config=None：关闭逐块相关句抽取（默认开启，
      每个检索块跑一次 LLM 批量抽取）。实测它是最大的随机噪声源——
      会把含答案的句子判为无关而丢弃（抽取器对「LSE MSc Finance 截止日期」
      只留下研究经费截止日期的英文句，丢掉「第一轮 2026 年 11 月 15 日」），
      最终回答退化成 DO-NOT-KNOW。关闭后原始检索块直接进最终回答 LLM，
      由回答模板的「只用 EXTRACTS、证据不足才说 DO-NOT-KNOW」约束兜底。
    - conversation_mode=False：最终回答走 llm_response_forget，不把跨问题
      累积的 message_history 喂给 LLM（Web/评测都是多问共享同一实例，
      默认配置下历史随问题数线性膨胀，既费 token 又污染逐题判断）。
    """
    return DocChatAgent(
        DocChatAgentConfig(
            llm=lm.OpenAIGPTConfig(chat_model=model),
            vecdb=QdrantDBConfig(
                cloud=False,
                storage_path=storage_path,
                collection_name="study-abroad-docs",
                embedding=FastEmbedEmbeddingsConfig(model_name=embed_model),
            ),
            use_bm25_search=False,
            use_fuzzy_match=False,
            n_similar_chunks=10,
            n_relevant_chunks=10,
            # 查询改写关闭（曾试 n_query_rephrases=2）：实测改写会把中文
            # 问题翻译成英文，英文问法把英文官网页顶进合并 top-k，反而把
            # 中文块挤出（doc-03 靠它命中纯属随机，doc-01/02/05 与
            # mix-01/02/03 的 DO-NOT-KNOW 正是它的代价）。k 覆盖全语料
            # 后改写纯属多余开销，关闭后检索完全确定、零 LLM。
            n_query_rephrases=0,
            assistant_mode=True,
            relevance_extractor_config=None,
            conversation_mode=False,
        )
    )
