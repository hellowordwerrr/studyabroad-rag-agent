"""headless socket.io 客户端：模拟 Chainlit 前端，驱动真实服务复现事件循环报错。

协议（chainlit 2.12.0 socket.py）：
- connect 到 /ws/socket.io，auth={sessionId, threadId, clientType}
- 客户端先发 "connection_successful" → 服务端触发 on_chat_start
- 再发 "client_message"（MessagePayload = {message: StepDict, fileReferences}）
"""
import asyncio
import uuid
from datetime import datetime, timezone

import socketio

QUESTION = "NYU 的 QS 排名和硕士申请截止日期分别是什么？"

sio = socketio.AsyncClient(logger=False, engineio_logger=False)
welcome_seen = asyncio.Event()
events_log = []


@sio.on("*")
async def catch_all(event, data):
    events_log.append((event, data))
    print(f"[evt] {event}: {str(data)[:150]}", flush=True)
    if event == "new_message":
        welcome_seen.set()


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

    # 触发 on_chat_start（首次连接含建库，可能较慢）
    await sio.emit("connection_successful")
    try:
        await asyncio.wait_for(welcome_seen.wait(), timeout=120)
    except asyncio.TimeoutError:
        print("WARN: 未等到欢迎消息（建库较慢？），继续发送问题", flush=True)

    # 发送与用户在浏览器中相同的混合问题
    payload = {
        "message": {
            "id": str(uuid.uuid4()),
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "output": QUESTION,
            "type": "user_message",
        },
        "fileReferences": [],
    }
    await sio.emit("client_message", payload)
    print("已发送问题，等待 ReAct 循环跑完……", flush=True)

    # 混合问题需 2 次工具调用 + 最终回答，留足时间
    await asyncio.sleep(90)
    await sio.disconnect()

    print(f"\n===== 共收到 {len(events_log)} 个事件 =====", flush=True)
    errs = [d for ev, d in events_log if "处理出错" in str(d)]
    print("处理出错消息:", errs if errs else "无", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
