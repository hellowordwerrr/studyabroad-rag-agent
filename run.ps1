# 一键启动文档问答（自动设置 UTF-8，避免中文 Windows GBK 编码报错）
# 用法: .\run.ps1 docs
#       .\run.ps1 "<你的文档目录>" -m deepseek/deepseek-reasoner
$env:PYTHONUTF8 = "1"
& .\.venv\Scripts\python.exe chat.py @args
