"""DATA_ROOT 与所有本地路径的唯一出处。

全部以函数暴露而非模块常量:launchd 不读 shell 配置,环境变量可能在
cli.py 加载 .env 之后才就位,call-time 求值保证拿到最终值。
默认值必须能在无任何环境变量时独立工作(launchd 场景)。
"""

import os
from pathlib import Path

_SUBDIRS = ("specs", "cache", "logs", "backups", "locks", "reports")


def data_root() -> Path:
    """输入:无 → 输出:DATA_ROOT 绝对路径(env WALMART_DATA_ROOT 覆盖)。

    默认与仓库平级的 WalmartAPI_data:仓库在 ~/Projects/WalmartAPI-Contral
    则数据在 ~/Projects/WalmartAPI_data。按本文件位置推导,不依赖 cwd,
    换机器/换用户名不用改任何配置。
    """
    override = os.environ.get("WALMART_DATA_ROOT")
    if override:
        return Path(override).expanduser()
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root.parent / "WalmartAPI_data"


def env_file() -> Path:
    """输入:无 → 输出:密钥文件 <DATA_ROOT>/.env 的路径(文件应 chmod 600)。"""
    return data_root() / ".env"


def specs_dir() -> Path:
    return data_root() / "specs"


def mp_item_spec_dir() -> Path:
    """输入:无 → 输出:MP_ITEM 按 PT 拆分 spec 目录(listing L2)。

    <DATA_ROOT>/specs/MP_ITEM/<版本串>/,版本串取 registry.FEED_SPEC_VERSIONS
    的 MP_ITEM 现值——spec 换版 = 并排放新目录 + 改 registry 一个版本串。
    """
    from registry import resources
    return specs_dir() / "MP_ITEM" / resources.FEED_SPEC_VERSIONS["MP_ITEM"]


def cache_dir() -> Path:
    return data_root() / "cache"


def logs_dir() -> Path:
    return data_root() / "logs"


def backups_dir() -> Path:
    return data_root() / "backups"


def locks_dir() -> Path:
    return data_root() / "locks"


def reports_dir() -> Path:
    """输入:无 → 输出:人工处置清单落盘目录(<DATA_ROOT>/reports)。

    只放"给人打开看/照着做"的明细 csv(下架清单、类目建议、冲突处置),
    每次跑覆盖同名文件——它们是报告不是账本,权威始终在库里。
    """
    return data_root() / "reports"


def frontend_scrape_file() -> Path:
    """输入:无 → 输出:影刀 RPA 抓取结果 latest.json 路径。

    env FRONTEND_SCRAPE_JSON 覆盖——并跑期指向旧项目的
    <旧项目根>/data/frontend_scrape/latest.json(影刀应用内部写死该路径,
    不跟脚本走;见 legacy_survey #daily_report 路径陷阱)。
    """
    override = os.environ.get("FRONTEND_SCRAPE_JSON", "").strip()
    if override:
        return Path(override).expanduser()
    return data_root() / "frontend_scrape" / "latest.json"


def yingdao_input_file() -> Path:
    """输入:无 → 输出:影刀 RPA 的**输入**清单 input.json 路径。

    与 frontend_scrape_file()(影刀的输出)配对,构成影刀衔接的两端:
    本仓写 input.json → 影刀读它决定抓哪些卖家页 → 影刀写 latest.json → 本仓读。
    所有者定稿 2026-08-15:新影刀应用不再读飞书总览页,改读本文件
    (旧应用那条"先写飞书→影刀读飞书"的路径同期删除,不留双轨)。

    env YINGDAO_INPUT_JSON 覆盖——影刀应用内部很可能同样写死读取路径
    (latest.json 就是这么踩的坑),对不上就用这个变量指过去,不要改代码。
    """
    override = os.environ.get("YINGDAO_INPUT_JSON", "").strip()
    if override:
        return Path(override).expanduser()
    return data_root() / "frontend_scrape" / "input.json"


def yingdao_app() -> Path:
    """输入:无 → 输出:影刀主程序可执行文件路径(spawn 直启用)。

    env YINGDAO_APP 覆盖。为什么是主程序不是 `open <协议URL>`(2026-08-24
    生产实证):调度沙箱里 `open` 要经 macOS Launch Services 分发协议,正好
    被沙箱边界拦下退 1;旧 walmart-kpi-daily 一直是拿主程序绝对路径直启的,
    旧日志证明这条路能正常拉起 Runner 窗口。协议 URL 作为 argv 传给主程序,
    语义与 `open` 相同,只是不再绕 Launch Services。
    """
    override = os.environ.get("YINGDAO_APP", "").strip()
    if override:
        return Path(override).expanduser()
    return Path("/Applications/影刀.app/Contents/MacOS/影刀")


def audit_seed_file(name: str) -> Path:
    """输入:审核规则种子文件名(如 'nrtl_small_parts.yaml')
    → 输出:refdata/audit/ 下的绝对路径(进 git 的只读参考资料,批次 A 迁入)。

    现存消费方(nrtl_small_parts.yaml 2026-08-21 随 R3 收敛删除):
    pt_nice_class.yaml(L2 R5 的 Nice Class 过滤)。
    forbidden_categories_zh_seller.yaml 已于 2026-08-20 删除 —— 它装的
    13 条 L1 excluded + 18 条 R2 禁售大类,和 L2 R1 的类目准入白名单
    讲同一件事,白名单补齐后整份下线。

    审核规则代码取 yaml 一律经此函数(铁律 3:路径不散落在 services)。
    """
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "refdata" / "audit" / name


def pg_tool(name: str) -> str:
    """输入:PostgreSQL 客户端工具名(pg_dump/pg_restore)→ 输出:可执行路径。

    env PG_BIN_DIR 指定目录:Homebrew 多版本共存时 PATH 里的 pg_dump 可能是
    旧大版本(生产实证 2026-08-13:PATH 是 16、服务器是 17,pg_dump 拒绝
    version mismatch);未设则用 PATH 裸名。
    """
    bin_dir = os.environ.get("PG_BIN_DIR", "").strip()
    if bin_dir:
        return str(Path(bin_dir).expanduser() / name)
    return name


def stores_snapshot_file() -> Path:
    """输入:无 → 输出:店铺凭证本地快照文件路径(含密钥,写入后 chmod 600,不进 git)。"""
    return cache_dir() / "stores_snapshot.json"


def ensure_data_root() -> list[str]:
    """输入:无 → 输出:本次实际新建的目录名列表(幂等,已存在则跳过)。"""
    created = []
    for name in _SUBDIRS:
        p = data_root() / name
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(name)
    return created
