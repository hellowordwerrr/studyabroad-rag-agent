"""headless 双轮对话测试：验证 Web 服务的跨轮记忆（修复前基线 / 修复后回归）。

同一 sessionId 连发两问：
  第一问「LSE 的雅思要求是多少？」→ 第二问「那这个学校的学费呢？」

- 无记忆时：第二问的 LLM 看不到第一问上下文，无法理解「这个学校」指 LSE
- 有记忆时：应命中 sample.txt 中 LSE 学费 44,928 英镑，并带 [^n] 引用
"""
import asyncio
import uuid
from datetime import datetime, timezone

import socketio

Q1 = "LSE 的雅思要求是多少？"
Q2 = "那这个学校的学费呢？"
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
        "http://localhost:8000",
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

    # 触发 on_chat_start（首次连接含建库，可能较慢）
    await sio.emit("connection_successful")
    try:
        await asyncio.wait_for(welcome_seen.wait(), timeout=120)
    except asyncio.TimeoutError:
        print("WARN: 未等到欢迎消息（建库较慢？），继续发送问题", flush=True)

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

    # 末尾事件全文打印（最终回答通常在靠后的 stream/message 事件里）
    print("\n===== 最后 20 个事件（全文）=====", flush=True)
    for ev, data in events_log[-20:]:
        print(f"[{ev}] {str(data)[:800]}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
