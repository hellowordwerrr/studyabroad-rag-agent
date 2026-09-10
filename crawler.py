"""
crawler.py — 官网申请要求爬虫（数据采集管道）

解决「知识库文档覆盖不足、大多问题回答不出来」的根因：定向抓取目标院校
官网的申请要求页面，提取正文、写入 docs/ 目录。Web 启动时自动入库
（web_ui.py 按「文件名+内容哈希」去重，重跑爬虫覆盖文件 → 重启 Web
即可让新内容进入知识库）。

设计要点：
- 只抓官方 .edu/.ac.uk 页面：数据权威、可核查，避免二手转载的错误
- 礼貌爬取：浏览器 UA + 单线程 + 页间 sleep 限速 + 有限重试
- 正文提取：trafilatura 优先（专为新闻/正文设计的库），失败回退 bs4
- 来源可溯：每个文件头部写入「来源 URL + 抓取时间」，问答引用自带出处

--dry-run  只打印将抓取的 URL 清单，不请求网络、不写文件

用法示例：
  .\\.venv\\Scripts\\python.exe crawler.py --dry-run
  .\\.venv\\Scripts\\python.exe crawler.py

扩展：在 CRAWL_SOURCES 里加一所学校即可（0.5-1 小时/校，先跑 --dry-run
验证 URL 可达再正式抓取）。
"""

import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import typer

DOCS_DIR = Path(__file__).parent / "docs"

# 浏览器请求头（完整伪装，多数官网 WAF 只拦「明显是脚本」的请求）
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

TIMEOUT = 30       # 单页请求超时（秒）
MAX_ATTEMPTS = 3   # 每页最多尝试次数（1 次 + 重试 2 次）
SLEEP_SECONDS = 2  # 页间限速间隔（秒）
MAX_CHARS = 20_000  # 单文件正文上限（字符），防止超长页面撑爆向量库

# 抓取清单：每项 {school: 中文校名, slug: 文件名用, urls: [(页面名, 中文说明, URL)]}
# 试点：LSE（官方页面直连可达，且示例文档已有其 MSc Finance 数据可交叉核对）
CRAWL_SOURCES = [
    {
        "school": "伦敦政治经济学院（LSE）",
        "slug": "lse",
        "urls": [
            (
                "entry-requirements",
                "入学要求总览",
                "https://www.lse.ac.uk/study-at-lse/Graduate/Prospective-students/Entry-requirements",
            ),
            (
                "english-language-requirements",
                "英语语言要求",
                "https://www.lse.ac.uk/study-at-lse/Graduate/Prospective-students/Entry-requirements/English-language-requirements",
            ),
            (
                "fees-and-funding",
                "学费与资助",
                "https://www.lse.ac.uk/study-at-lse/Graduate/Fees-and-funding",
            ),
            (
                "how-to-apply",
                "如何申请",
                "https://www.lse.ac.uk/study-at-lse/Graduate/Prospective-students/How-to-apply",
            ),
            (
                "msc-finance-full-time",
                "MSc 金融（全日制）项目规章",
                "https://www.lse.ac.uk/resources/calendar2026-2027/programmeRegulations/taughtMasters/2026/MScFinanceFull-time.htm",
            ),
        ],
    },
]

app = typer.Typer(help="抓取官网申请要求页面，写入 docs/ 供知识库自动入库")


# ---------- 第 1 步：抓取 ----------

def fetch(url: str) -> str:
    """下载页面 HTML；每页最多尝试 MAX_ATTEMPTS 次，失败抛异常。"""
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp.text
            # 404 是 URL 写错了，重试没意义；403/202 是 WAF 拦截，重试也没用
            if resp.status_code in (403, 404):
                raise ConnectionError(f"HTTP {resp.status_code}（URL 失效或被反爬拦截）")
            last_err = ConnectionError(f"HTTP {resp.status_code}")
        except ConnectionError:
            raise  # 上面 raise 的，直接抛出不再重试
        except requests.RequestException as e:
            last_err = e
        if attempt < MAX_ATTEMPTS:
            typer.echo(f"    第 {attempt} 次失败（{last_err}），{SLEEP_SECONDS}s 后重试...")
            time.sleep(SLEEP_SECONDS)
    raise ConnectionError(f"请求失败：{last_err}")


# ---------- 第 2 步：正文提取与清洗 ----------

def extract_text(html: str) -> str:
    """正文提取：trafilatura 优先（自动识别正文区），失败回退 bs4 全页取字。"""
    import trafilatura

    text = trafilatura.extract(html)
    if text and len(text.strip()) > 100:  # 提取结果太短视为失败
        return text

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    return soup.get_text("\n")


def clean_text(text: str) -> str:
    """去空行、去首尾空白；超长截断并标注（截断信息本身也入库，检索可知）。"""
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]  # 去掉空行
    result = "\n".join(lines)
    if len(result) > MAX_CHARS:
        result = result[:MAX_CHARS] + "\n\n（正文过长，已截断）"
    return result


def build_document(school: str, url: str, text: str, fetched_at: str) -> str:
    """组装最终文档：头部两行来源元数据 + 正文（引用标注由此获得出处）。"""
    return f"# 来源: {url}\n# 抓取时间: {fetched_at}\n\n{text}\n"


# ---------- 第 3 步：写盘 ----------

def crawl(dry_run: bool) -> None:
    """逐校逐页抓取 → 清洗 → 写入 docs/crawled-<slug>-<页面名>.txt。"""
    total = sum(len(s["urls"]) for s in CRAWL_SOURCES)
    typer.echo(f"共 {len(CRAWL_SOURCES)} 所学校、{total} 个页面\n")

    success, failed = 0, 0
    for school_info in CRAWL_SOURCES:
        school, slug = school_info["school"], school_info["slug"]
        typer.echo(f"=== {school}")
        for page, desc, url in school_info["urls"]:
            out_path = DOCS_DIR / f"crawled-{slug}-{page}.txt"
            if dry_run:
                typer.echo(f"  [dry-run] {desc}: {url}")
                continue
            try:
                html = fetch(url)
                text = clean_text(extract_text(html))
                fetched_at = datetime.now(timezone.utc).astimezone().isoformat(
                    timespec="seconds"
                )
                DOCS_DIR.mkdir(exist_ok=True)
                out_path.write_text(
                    build_document(school, url, text, fetched_at), encoding="utf-8"
                )
                typer.echo(
                    f"  [OK] {desc}（{len(text)} 字符）-> {out_path.name}"
                )
                success += 1
            except Exception as e:
                typer.echo(f"  [失败] {desc}: {e}")
                failed += 1
            time.sleep(SLEEP_SECONDS)  # 页间限速：礼貌爬取

    if dry_run:
        typer.echo("\n[dry-run] 以上为将抓取的 URL 清单，未请求网络、未写文件。")
        return
    typer.echo(f"\n完成：成功 {success} 页，失败 {failed} 页。")
    if success:
        typer.echo("重启 Web（或重新启动 web.ps1）后新文档自动入库即可提问。")


# ---------- 入口 ----------

@app.command()
def main(
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印 URL 清单，不抓取"),
):
    crawl(dry_run)


if __name__ == "__main__":
    app()
