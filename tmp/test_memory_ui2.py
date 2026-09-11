"""headless 双轮记忆测试·场景二（干净基线）：牛津大学——不出现在任何工具
few-shot 示例与系统提示里，排除「LLM 靠示例猜测指代」的干扰。

第一问「牛津大学的 QS 排名是多少？」→ 第二问「那这所大学的所在城市是哪里？」
- 无记忆时：第二问的 LLM 看不到上下文，「这所大学」无从指代 → 答非所问/猜错校
- 有记忆时：应指代牛津大学 → 城市「牛津」
"""
import asyncio
import uuid
from datetime import datetime, timezone

import socketio

Q1 = "牛津大学的 QS 排名是多少？"
Q2 = "那这所大学的所在城市是哪里？"
WAIT_SECONDS = 60  # 排名类单轮工具调用，1 分钟足够

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


if __name__ == "__main__":
    asyncio.run(main())
