"""headless 双轮记忆测试·场景三（决定性基线）：南洋理工大学不出现在任何工具
few-shot 示例与文档内容里，第二问的指代无任何线索可猜。

第一问「南洋理工大学的 QS 排名是多少？」→ 第二问「那这所大学的申请截止日期
是什么时候？」

- 无记忆时：第二问的 LLM 看不到上下文，「这所大学」无从指代 → 只能编一个
  指代（大概率按 doc_qa few-shot 示例泄漏答成 LSE 的截止日期）= 错校+编造
- 有记忆时：解析出「南洋理工大学」→ doc_qa 查无此校 → 诚实拒答
"""
import asyncio
import uuid
from datetime import datetime, timezone

import socketio

Q1 = "南洋理工大学的 QS 排名是多少？"
Q2 = "那这所大学的申请截止日期是什么时候？"
WAIT_SECONDS = 90  # doc 问答单轮含检索+相关句抽取，留足时间

sio = socketio.AsyncClient(logger=False, engineio_logger=False)
welcome_seen = asyncio.Event()
events_log = []


@sio.on("*")
async def catch_all(event, data):
    events_log.append((event, data))
    print(f"[evt] {event}: {str(data)[:120]}", flush=True)
    if event == "new_message":
        welcome_seen.set()


def send_payload(question: str) -> dict:
    return {
        "message": {
            "id": str(uuid.uuid4()),
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "output": question,
            "type": "user_message",
        },
        "fileReferences": [],
    }


async def main():
    await sio.connect(
        "http://localhost:8001",
        auth={
            "sessionId": str(uuid.uuid4()),
            "threadId": str(uuid.uuid4()),
            "clientType": "webapp",
        },
        socketio_path="/ws/socket.io",
        transports=["polling"],
        wait_timeout=15,
    )
    print("connected, sid =", sio.sid, flush=True)

    await sio.emit("connection_successful")
    try:
        await asyncio.wait_for(welcome_seen.wait(), timeout=120)
    except asyncio.TimeoutError:
        print("WARN: 未等到欢迎消息，继续发送问题", flush=True)

    print(f"\n===== 第一问: {Q1} =====", flush=True)
    await sio.emit("client_message", send_payload(Q1))
    await asyncio.sleep(WAIT_SECONDS)

    print(f"\n===== 第二问: {Q2} =====", flush=True)
    await sio.emit("client_message", send_payload(Q2))
    await asyncio.sleep(WAIT_SECONDS)
    await sio.disconnect()

    print(f"\n===== 共收到 {len(events_log)} 个事件 =====", flush=True)
    errs = [d for ev, d in events_log if "处理出错" in str(d)]
    print("处理出错消息:", errs if errs else "无", flush=True)

    # 只打印两条最终回答（assistant_message 的 update_message 事件）
    print("\n===== 最终回答（assistant_message）=====", flush=True)
    for ev, data in events_log:
        if (
            ev == "update_message"
            and isinstance(data, dict)
            and data.get("type") == "assistant_message"
            and data.get("output")
        ):
            print(f"--- {data.get('name', '?')} ---", flush=True)
            print(data["output"], flush=True)

    # 末尾事件全文：工具调用 JSON（doc_qa 的查询串）通常出现在流式事件里
    print("\n===== 最后 30 个事件（全文）=====", flush=True)
    for ev, data in events_log[-30:]:
        print(f"[{ev}] {str(data)[:1000]}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
