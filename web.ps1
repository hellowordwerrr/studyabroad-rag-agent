# 一键启动 Web 界面（自动设置 UTF-8，避免中文 Windows GBK 编码报错）
# 启动后浏览器打开 http://localhost:8000
# 注意：不加 -w 监听模式——热重载会让新旧两个进程同时打开本地 Qdrant，
# 触发「Storage folder is already accessed」文件锁冲突
$env:PYTHONUTF8 = "1"
& .\.venv\Scripts\python.exe -m chainlit run web_ui.py
