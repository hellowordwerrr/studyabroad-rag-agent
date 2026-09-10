"""
update_rankings.py — 一键刷新院校排名数据（数据管道雏形）

解决「静态 JSON 数据会过期」问题：程序不再只靠人手动改数据，
而是从这里获取新数据 → 匹配现有学校 → 自动写回 rankings.json 并记录更新时间。

两种数据源：
  --auto              自动模式：从网络下载 QS 排名表（GitHub 公开数据集，
                      直连失败自动走 gh-proxy.com 镜像）
  --file 路径         半自动模式：导入本地表格（QS 官网导出的 Excel/CSV，
                      或任何「排名,校名」两列的表格）
  --dry-run           只预览变更，不写文件（建议先跑一次看效果）

用法示例：
  .\\.venv\\Scripts\\python.exe update_rankings.py --auto --dry-run
  .\\.venv\\Scripts\\python.exe update_rankings.py --auto
  .\\.venv\\Scripts\\python.exe update_rankings.py --file "<你的表格文件>"

流程：读取新数据 → 按校名/别名模糊匹配现有学校 → 更新 qs_rank
      → 写回 data/rankings.json → 更新 data/meta.json（版本/时间/来源）
"""

import csv
import io
import json
import sys
from datetime import date
from pathlib import Path

import typer

from thefuzz import process

DATA_DIR = Path(__file__).parent / "data"
RANKINGS_PATH = DATA_DIR / "rankings.json"
META_PATH = DATA_DIR / "meta.json"

# QS 2026 排名表（GitHub 公开数据集，QS 官网导出格式）
QS_XLSX_URL = (
    "https://raw.githubusercontent.com/olgagaffarova/QS-University-Rankings-2026/"
    "main/02_Data/Original%20Data/2026%20QS%20World%20University%20Rankings.xlsx"
)
QS_EDITION = "QS 2026"  # 该数据集对应的排名版本

app = typer.Typer(help="刷新 data/rankings.json 中的院校排名数据")


# ---------- 第 1 步：拿到「新数据」 ----------

def download_xlsx(url: str, timeout: int = 60) -> bytes:
    """下载表格；直连失败时自动改走 GitHub 代理镜像（国内网络）。"""
    from urllib import request

    proxies = [url, f"https://gh-proxy.com/{url}"]
    for i, target in enumerate(proxies):
        try:
            with request.urlopen(target, timeout=timeout) as resp:
                data = resp.read()
            if i == 1:
                typer.echo("  (直连失败，已通过 gh-proxy.com 镜像下载)")
            return data
        except Exception as e:
            last_err = e
            if i == 0:
                typer.echo("  直连超时/失败，尝试镜像...")
    raise ConnectionError(f"下载失败（直连与镜像均不可用）：{last_err}")


def load_rows_from_xlsx(data: bytes) -> list[tuple[int, str]]:
    """解析 xlsx：自动定位「排名」列和「校名」列，返回 (排名, 英文校名) 列表。"""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("表格为空")

    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    # 找排名列：表头含 rank；找校名列：表头含 institution/name/大学
    rank_col = next((i for i, h in enumerate(header) if "rank" in h.lower()), None)
    name_col = next(
        (i for i, h in enumerate(header) if "institution" in h.lower() or "name" in h.lower()),
        None,
    )
    if rank_col is None or name_col is None:
        raise ValueError(f"无法识别表格列，表头为：{header}")

    result = []
    for r in rows[1:]:
        if r is None or r[rank_col] is None or r[name_col] is None:
            continue
        try:
            # 数据集中部分排名单元格被存成公式（如 "=17"），去掉前导 "=" 再转数字
            rank = int(str(r[rank_col]).strip().lstrip("="))
        except (TypeError, ValueError):
            continue
        result.append((rank, str(r[name_col]).strip()))
    return result


def load_rows_from_csv(path: Path) -> list[tuple[int, str]]:
    """解析 csv/tsv：自动识别「排名」和「校名」列（支持中文表头）。"""
    with path.open(encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        reader = csv.reader(f, dialect)
        header = next(reader)
        header = [h.strip() for h in header]
        # 排名列：表头含 排名/rank；校名列：表头含 学校/大学/名称/institution/name
        rank_col = next(
            (i for i, h in enumerate(header) if "rank" in h.lower() or "排名" in h),
            None,
        )
        name_col = next(
            (
                i
                for i, h in enumerate(header)
                if "institution" in h.lower()
                or "name" in h.lower()
                or "学校" in h
                or "大学" in h
                or "名称" in h
            ),
            None,
        )
        if rank_col is None or name_col is None:
            raise ValueError(f"无法识别 CSV 表头列：{header}")
        result = []
        for row in reader:
            if len(row) <= max(rank_col, name_col):
                continue
            try:
                rank = int(str(row[rank_col]).strip())
            except ValueError:
                continue
            result.append((rank, str(row[name_col]).strip()))
    return result


# ---------- 第 2 步：按校名匹配现有数据并更新 ----------

def load_local() -> list[dict]:
    return json.loads(RANKINGS_PATH.read_text(encoding="utf-8"))


def match_school(rank_rows: list[tuple[int, str]], school: dict, threshold: int = 85):
    """用 thefuzz 把表格里的校名匹配到本地学校（英文名匹配，别名兜底）。"""
    names = [r[1] for r in rank_rows]
    # 先用学校英文全名精确/模糊匹配
    match, score = process.extractOne(school["name"], names)
    # 表格里常用简称/变体（如 "ETH Zurich" 表格写 "ETH Zurich"），
    # 如果全名匹配不上再试英文别名
    if score < threshold:
        for alias in school.get("aliases", []):
            if alias == school["name"] or not alias.isascii():
                continue
            m2, s2 = process.extractOne(alias, names)
            if s2 > score:
                match, score = m2, s2
    if score < threshold:
        return None, score
    idx = names.index(match)
    return rank_rows[idx][0], score


def update_rankings(rank_rows: list[tuple[int, str]], dry_run: bool) -> None:
    """核心逻辑：逐校匹配 → 更新排名 → 写回文件（或仅预览）。"""
    schools = load_local()
    new_rows = []
    for school in schools:
        new_rank, score = match_school(rank_rows, school)
        old_rank = school["qs_rank"]
        status = "不变" if new_rank is None or new_rank == old_rank else f"变化!"
        if new_rank is None:
            new_rows.append(f"  {school['zh_name']:　<10} 未在表格中找到（相似度 {score:.0f}）")
            continue
        if new_rank != old_rank:
            new_rows.append(
                f"  {school['zh_name']:　<10} {old_rank:>3} -> {new_rank:<3}  {status}"
            )
        else:
            new_rows.append(f"  {school['zh_name']:　<10} {old_rank:>3}    确认无误  {status}")
        if not dry_run:
            school["qs_rank"] = new_rank

    typer.echo("\n匹配结果（前 15 所本地院校）：")
    for line in new_rows:
        typer.echo(line)

    if dry_run:
        typer.echo("\n[dry-run] 以上为预览，未写入任何文件。")
        return

    # 写回 rankings.json（保持中文无转义、缩进 2）
    RANKINGS_PATH.write_text(
        json.dumps(schools, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # 更新时效元数据 —— 解决「数据不知道自己是哪年的」问题
    meta = {
        "data_name": "QS 世界大学排名",
        "edition": QS_EDITION,
        "updated_at": date.today().isoformat(),
        "source": "update_rankings.py 自动更新",
        "school_count": len(schools),
    }
    META_PATH.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    typer.echo(f"\n[OK] 已更新 {RANKINGS_PATH.name} 与 {META_PATH.name}")
    typer.echo(f"     数据版本：{meta['edition']}，更新时间：{meta['updated_at']}")


# ---------- 入口 ----------

@app.command()
def main(
    auto: bool = typer.Option(False, "--auto", help="自动从网络下载最新排名表"),
    file: Path = typer.Option(None, "--file", "-f", help="导入本地 xlsx/csv 表格"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只预览变更，不写文件"),
):
    if auto and file:
        typer.echo("--auto 与 --file 二选一", err=True)
        raise typer.Exit(1)
    if not auto and file is None:
        typer.echo("请指定数据源：--auto（网络下载）或 --file 路径（本地表格）", err=True)
        raise typer.Exit(1)

    if auto:
        typer.echo(f"自动模式：下载 {QS_EDITION} 排名表...\n  {QS_XLSX_URL}")
        data = download_xlsx(QS_XLSX_URL)
        rank_rows = load_rows_from_xlsx(data)
        typer.echo(f"  解析成功：共 {len(rank_rows)} 所院校")
    else:
        typer.echo(f"半自动模式：导入本地表格 {file}")
        if file.suffix.lower() == ".csv" or file.suffix.lower() == ".txt":
            rank_rows = load_rows_from_csv(file)
        else:
            rank_rows = load_rows_from_xlsx(file.read_bytes())
        typer.echo(f"  解析成功：共 {len(rank_rows)} 所院校")

    update_rankings(rank_rows, dry_run)


if __name__ == "__main__":
    app()
