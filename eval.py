"""评测脚本：对整套 Agent 系统做三层指标评测。

三层指标：
1. 检索命中率（doc/mixed 题）：doc_agent.get_relevant_chunks(question, [])
   返回块内容含 evidence_keywords 即命中——只耗本地 embedding，不耗 LLM。
2. 工具路由正确率：捕获每题实际调用的工具名 vs expected_tool。
3. 答案正确率：最终回答归一化（去空白/逗号/星号/下划线，转小写）后
   expected_keywords 全部出现；negative 题 = 含拒答词且不含 forbidden_keywords。

设计要点：
- 题库 eval/questions.json；ranking 题期望值运行时从 data/rankings.json
  自动生成（school 字段按中文名/英文名/别名解析），避免手抄排名出错。
- 向量库用 tmp/eval_qdrant 全新副本，避免与运行中的 Web 服务抢 Qdrant
  文件锁（Qdrant 本地模式同一目录只允许一个客户端）。
- 每题新建主 Agent：langroid 的 ChatAgent.llm_response 会把
  message_history 完整喂给 LLM（隐式跨轮记忆），复用同一个 Agent 会让
  后面的题看到前面题的历史，污染评测结果。

用法:
  python eval.py                 # 全量 46 题
  python eval.py --limit 10      # 只跑前 10 题（冒烟）
  python eval.py --only doc      # 只跑 doc 类题（type 前缀过滤）
输出: eval/report.txt（带时间戳）；有失败时退出码非 0。
"""
import argparse
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import langroid as lr
import langroid.language_models as lm
from langroid.agent.special.doc_chat_agent import DocChatAgent

from doc_qa import DEFAULT_EMBED_MODEL, build_doc_agent
from tools import DeepSeekChatAgent, DocChatTool, RankingTool, set_doc_agent

ROOT = Path(__file__).parent
EVAL_STORE = ROOT / "tmp" / "eval_qdrant"
QUESTIONS_PATH = ROOT / "eval" / "questions.json"
REPORT_PATH = ROOT / "eval" / "report.txt"
DOCS_DIR = ROOT / "docs"
RANKINGS_PATH = ROOT / "data" / "rankings.json"

MAX_TOOL_ROUNDS = 8

load_dotenv(dotenv_path=ROOT / ".env")

SYSTEM_MESSAGE = (  # 与 chat.py / web_ui.py 完全一致
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


def norm(s: str) -> str:
    """归一化：去空白/逗号/星号/下划线/反引号/#，转小写。用于子串匹配。"""
    return re.sub(r"[\s,*_`#]", "", s or "").lower()


class SchoolResolver:
    """rankings.json 解析器：按中文名/英文名/别名找院校条目。"""

    def __init__(self, data: list[dict]):
        self.data = data
        self.by_name = {}
        for school in data:
            keys = [school["name"], school["zh_name"], *school.get("aliases", [])]
            for k in keys:
                self.by_name[k] = school

    def resolve(self, school: str) -> dict:
        entry = self.by_name.get(school)
        if entry is None:
            raise KeyError(f"questions.json 中的 school「{school}」在 rankings.json 中找不到")
        return entry


def build_ranking_expectation(entry: dict, expect: str) -> list[str]:
    """ranking 题期望关键词（自动生成，不手抄）。"""
    if expect == "rank":
        r = entry["qs_rank"]
        return [f"第{r}名", f"第{r}位", f"排名第{r}"]
    if expect == "city":
        city = entry["city"].split("（")[0]
        return [city]
    if expect == "country":
        return [entry["country"]]
    raise ValueError(f"未知 expect 类型: {expect}")


def run_question(agent: lr.ChatAgent, question: str) -> tuple[list[str], str | None]:
    """跑一次手动 ReAct 循环，返回 (实际调用的工具名列表, 最终回答)。"""
    used_tools: list[str] = []
    response = agent.llm_response(question)
    for _ in range(MAX_TOOL_ROUNDS):
        if response is None:
            break
        tms = agent.try_get_tool_messages(response)  # 先存变量：判断与记录共用
        if not tms:
            break
        for tm in tms:
            used_tools.append(tm.request)
        response = agent.agent_response(response)  # 执行工具 handle()
        if response is None:
            break
        response = agent.llm_response(response)  # 工具结果交还 LLM
    return used_tools, (response.content if response else None)


def check_route(used: list[str], expected: str | None) -> tuple[bool, str]:
    if not expected:
        return True, ""
    if expected == "both":
        ok = "ranking_lookup" in used and "doc_qa" in used
        return ok, "" if ok else f"期望两个工具都被调用，实际 {used}"
    ok = expected in used
    return ok, "" if ok else f"期望 {expected}，实际 {used}"


def check_answer(
    q: dict, answer: str | None, resolver: SchoolResolver
) -> tuple[bool, str]:
    """按题型判定答案正确性，返回 (是否通过, 失败原因)。"""
    if not answer:
        return False, "无回答"
    n_ans = norm(answer)
    qtype = q["type"]

    if qtype == "ranking":
        entry = resolver.resolve(q["school"])
        keywords = build_ranking_expectation(entry, q.get("expect", "rank"))
        for kw in keywords:
            if norm(kw) in n_ans:
                return True, ""
        return False, f"期望关键词 {keywords} 均未出现"

    if qtype == "negative":
        refusal = json.loads(
            QUESTIONS_PATH.read_text(encoding="utf-8")
        )["meta"]["refusal_keywords"]
        has_refusal = any(k in n_ans for k in refusal)
        hits = [k for k in q.get("forbidden_keywords", []) if norm(k) in n_ans]
        if not has_refusal:
            return False, f"未出现拒答词（{refusal}）"
        if hits:
            return False, f"出现禁用词（疑似编造数字）: {hits}"
        return True, ""

    # doc / mixed：期望关键词全部出现；列表元素 = any-of 组（组内任一出现即可，
    # 用于知识库内存在多个等价事实的题，如 sample-lse.txt 与官网爬取页措辞不同）
    missing = []
    for k in q.get("expected_keywords", []):
        if isinstance(k, list):
            if not any(norm(x) in n_ans for x in k):
                missing.append("/".join(k))
        elif norm(k) not in n_ans:
            missing.append(k)
    if missing:
        return False, f"缺少关键词 {missing}"
    return True, ""


def check_retrieval(doc_agent: DocChatAgent, q: dict) -> tuple[bool, str]:
    """检索命中检查（doc/mixed 题）：相关块内容含任一 evidence 关键词。"""
    evidence = q.get("evidence_keywords")
    if not evidence:
        return True, "—"
    chunks = doc_agent.get_relevant_chunks(q["question"], [])
    text = "\n".join(c.content for c in chunks)
    hits = [k for k in evidence if k in text]
    if hits:
        return True, f"命中 {hits}"
    return False, f"evidence {evidence} 均未出现在检索块中"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    parser.add_argument("--only", type=str, default="", help="只跑指定 type（逗号分隔）")
    args = parser.parse_args()

    qdata = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    questions: list[dict] = qdata["questions"]
    if args.only:
        types = {t.strip() for t in args.only.split(",")}
        questions = [q for q in questions if q["type"] in types]
    if args.limit:
        questions = questions[: args.limit]

    rankings = json.loads(RANKINGS_PATH.read_text(encoding="utf-8"))
    resolver = SchoolResolver(rankings)

    # 全新 tmp 副本库（避开 Web 服务的 Qdrant 文件锁），重建并入库全部文档
    if EVAL_STORE.exists():
        shutil.rmtree(EVAL_STORE, ignore_errors=True)
    EVAL_STORE.mkdir(parents=True, exist_ok=True)
    model = "deepseek/deepseek-chat"
    print("建库: tmp/eval_qdrant（全新副本）...", flush=True)
    doc_agent = build_doc_agent(model=model, storage_path=str(EVAL_STORE))
    set_doc_agent(doc_agent)
    docs = [
        str(p) for p in DOCS_DIR.iterdir() if p.suffix.lower() in (".txt", ".pdf", ".docx", ".doc")
    ]
    if docs:
        doc_agent.ingest_doc_paths(docs)
    print(f"已入库 {len(docs)} 个文档。开始评测 {len(questions)} 题...\n", flush=True)

    rows = []
    n_retrieval_ok = n_route_ok = n_answer_ok = 0
    n_retrieval_n = n_route_n = 0

    for i, q in enumerate(questions, 1):
        # 每题新建主 Agent：langroid 的 message_history 会跨轮喂给 LLM，
        # 复用同一 Agent 会让后续题看到前面题的历史，污染评测
        agent = DeepSeekChatAgent(
            lr.ChatAgentConfig(
                llm=lm.OpenAIGPTConfig(chat_model=model),
                system_message=SYSTEM_MESSAGE,
            )
        )
        agent.enable_message(RankingTool)
        agent.enable_message(DocChatTool)

        t0 = time.time()
        try:
            retr_ok, retr_msg = check_retrieval(doc_agent, q)
            used_tools, answer = run_question(agent, q["question"])
            route_ok, route_msg = check_route(used_tools, q.get("expected_tool"))
            ans_ok, ans_msg = check_answer(q, answer, resolver)
        except Exception as e:  # 单题异常不中断整轮评测
            retr_ok, retr_msg = False, f"异常: {e}"
            used_tools, answer = [], None
            route_ok, route_msg = False, "跳过"
            ans_ok, ans_msg = False, f"异常: {e}"

        elapsed = time.time() - t0
        retr_show = "—" if retr_msg == "—" else ("✓" if retr_ok else "✗")
        route_show = "—" if route_msg == "—" else ("✓" if route_ok else "✗")
        ans_show = "✓" if ans_ok else "✗"
        print(
            f"[{i}/{len(questions)}] {q['id']:<8} {q['type']:<8} "
            f"检索{retr_show} 路由{route_show} 答案{ans_show} "
            f"({elapsed:.0f}s) {q['question'][:34]}",
            flush=True,
        )
        if not retr_ok or not route_ok or not ans_ok:
            details = []
            if retr_msg != "—" and not retr_ok:
                details.append(f"检索: {retr_msg}")
            if not route_ok:
                details.append(f"路由: {route_msg}")
            if not ans_ok:
                details.append(f"答案: {ans_msg}")
            if answer:
                details.append(f"回答: {answer[:300]}")
            print("      ✗ " + " | ".join(details), flush=True)

        if retr_msg != "—":
            n_retrieval_n += 1
            n_retrieval_ok += retr_ok
        if route_msg != "—":
            n_route_n += 1
            n_route_ok += route_ok
        n_answer_ok += ans_ok

        rows.append(
            {
                "id": q["id"],
                "type": q["type"],
                "question": q["question"],
                "retrieval": retr_msg,
                "route": route_msg if route_msg != "—" else "",
                "answer_ok": ans_ok,
                "answer_msg": ans_msg,
                "answer": answer or "",
                "used_tools": used_tools,
            }
        )

    total = len(questions)
    summary = [
        f"评测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"题库: eval/questions.json（共 {total} 题）",
        "",
        f"检索命中率: {n_retrieval_ok}/{n_retrieval_n}",
        f"工具路由正确率: {n_route_ok}/{n_route_n}",
        f"答案正确率: {n_answer_ok}/{total}",
    ]
    print("\n===== 汇总 =====", flush=True)
    for line in summary:
        print(line, flush=True)

    # 写报告（含每题明细与失败题全文，供修复分析）
    lines = list(summary)
    lines += ["", "===== 逐题明细 ====="]
    for r in rows:
        status = "✓" if r["answer_ok"] else "✗"
        lines.append(
            f"[{status}] {r['id']} ({r['type']}) {r['question']}\n"
            f"     检索: {r['retrieval']} | 路由: {r['route'] or '—'} | "
            f"工具: {r['used_tools']} | 判定: {r['answer_msg']}"
        )
        if not r["answer_ok"] and r["answer"]:
            lines.append(f"     回答: {r['answer']}")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n报告已写入 {REPORT_PATH}", flush=True)

    return 0 if n_answer_ok == total and n_route_ok == n_route_n and n_retrieval_ok == n_retrieval_n else 1


if __name__ == "__main__":
    sys.exit(main())
