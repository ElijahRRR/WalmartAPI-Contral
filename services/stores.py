"""店铺凭证积木:从飞书店铺凭证表读取 → 标准店铺列表;飞书故障时用本地快照兜底。

替代旧仓库 walmart_client.load_stores()(读 店铺API.xlsx);过滤规则与输出形状
与旧版完全一致,便于后续 workflow 迁移时直接替换 import。
"""

import json
import logging
import os
import re
from urllib.parse import quote

from api import feishu
from registry import paths, resources

logger = logging.getLogger("services.stores")

#: 跨店并发的**唯一出处**(所有者定稿 2026-08-17:「跨店线程池都应该设置为 24,
#: 内层对 api 接口的速度才是需要单独注意的」)。
#:
#: 为什么安全:每店有**自己的固定出口代理**,店铺之间不共享出口;沃尔玛配额按
#: `(store, endpoint)` 计,`api/_client` 的令牌桶也按这个维度限流。所以加跨店
#: 并发不会挤占同一个桶 —— 真正要小心的是**店内**对同一接口的并发,那个各链
#: 自己按端点配额定(如 catalog_sync 的 `_FILL_WORKERS=8` 对应 items.get 800/min)。
#:
#: ⚠ 为什么必须是一个常量而不是六个字面量:旧系统 README 那句「店铺级并发不要
#: 调高(代理共享/全局风控)」被所有者 2026-08-16 显式推翻,但那次只改了
#: daily_report(6→16),perf_problems/settlement_sync 连**被推翻的注释**都原样
#: 留着 —— 同一条判断改一处漏两处,而且不报错,只是那两条链一直慢着。
STORE_WORKERS = 24

_NUM_RE = re.compile(r"\d+")


def sort_key(store: str):
    """输入:店铺名 → 输出:排序键。人看的表格一律按它排,**别用 SQL ORDER BY**。

    所有者定稿 2026-08-15:字母开头 → 数字开头 → 其余(中文等),组内按
    「前缀 + 数字大小」自然序(谭总9 在 谭总10 之前,A085 在 A089 之前)。

    ⚠ 为什么不交给数据库:PG 的 collation(生产是 en_US.UTF-8 一类)在主排序级
    **把中文字符整个忽略**,于是「谭总1」当成「1」参与比较,排出来是
    谭总1…谭总8 → 81刘何秀 → 82杨乾良 → 谭总9 → A085(2026-08-15 生产实见)。
    而且 collation 随机器 locale 变,同一条 SQL 换台服务器结果就不同。
    排序是业务规则,得写在代码里、有测试钉着。
    """
    s = str(store or "").strip()
    head = s[:1]
    if head.isascii() and head.isalpha():
        group = 0
    elif head.isdigit():
        group = 1
    else:
        group = 2
    m = _NUM_RE.search(s)
    # 无数字的店铺(如「总仓」)给 -1,排在同前缀的带号店铺之前
    return (group, (s[:m.start()] if m else s).casefold(),
            int(m.group()) if m else -1, s)


def is_enabled(fields: dict) -> bool:
    """输入:凭证表一行的 fields → 输出:这家店**在不在营**(所有者勾的「启用」)。

    ★ **全项目"还在不在营"的唯一判据**(所有者定稿 2026-08-22:「应该以我在
    店铺凭证表中,勾选是否启用为准」)。此前这段判断埋在 `_normalize` 的循环里,
    和 ClientId、代理两条过滤混成一体,没有任何函数单独回答得了这个问题。

    ⚠ **它与"能不能调 API"是两件事**,不许合并:
      · 「启用」= 所有者的**意图**开关(这家店我还做不做);
      · ClientId / 代理 = **技术就绪**(现在能不能发请求)。
    合并的后果两个方向都严重 —— 拿 `load_stores()`(合并版)判在营,
    「在册但代理没配的在营店」会被判成死店而整店下线;拿
    `registered_names()`(不看启用)判在营,勾了停用的店照样占着品牌、
    照样用冻结行拦着别的店上架。

    缺省视为启用:旧 xlsx 无此列,新表才加的(沿用旧系统口径)。
    """
    v = fields.get(resources.STORE_CREDENTIALS.fields.enabled)
    if v is False:
        return False
    return not (isinstance(v, str) and v.strip() in ("否", "false", "0"))


def enabled_names() -> set[str]:
    """输入:无 → 输出:**在营**店名集合(在册 ∧ 启用);飞书失败直接抛。

    与另外两个的分工 —— 三层,别混:

    | 函数 | 回答 | 谁该用 |
    |---|---|---|
    | `registered_names()` | 在不在**册** | 只有"查无此店 vs 在册但停用"这种报错分流 |
    | **`enabled_names()`** | 在不在**营** | **判死店、判占用、判分配范围,一律用这个** |
    | `load_stores()` | 现在能不能**调 API** | 真要发请求的链 |

    不做快照兜底:兜底快照是 `load_stores` 过滤后的结果,拿它当在营名录会把
    「缺代理的在营店」算成停用 —— 而这个名录直通整店下线。要么拿到当下真值,
    要么明说读不到。
    """
    f = resources.STORE_CREDENTIALS.fields
    recs = feishu.list_records(resources.STORE_CREDENTIALS,
                               field_names=[f.store, f.enabled])
    return {n for n in (_cell(r.get("fields", {}), f.store)
                        for r in recs if is_enabled(r.get("fields", {}))) if n}


def load_stores(filter_names: list[str] | None = None) -> list[dict]:
    """输入:可选店铺名列表(None=全部)→ 输出:有效店铺 list[dict](name/client_id/client_secret/proxy)。

    数据源:飞书店铺凭证表(registry.STORE_CREDENTIALS)。
    成功读取后写本地快照(<DATA_ROOT>/cache/stores_snapshot.json,chmod 600);
    飞书不可用时回退读最近一次快照并告警——保证飞书故障不瘫痪全部工作流。

    过滤规则(沿用旧系统):
      1. ClientId 为空或 "0" → 跳过
      2. 无代理(代理类型/IP/端口 任一为空或 "0")→ 跳过(严禁直连,店铺关联风险)
      3. 「启用」字段显式为否 → 跳过(旧 xlsx 无此列,新表新增;缺省视为启用)

    `filter_names` 里**有一个名字落空就抛**,不静默少跑:给三家跑两家、摘要
    照报「2/2 家连通」满绿,人会以为三家都验过了,而两侧都不报错。名字来源
    全是人敲的 `-p store=` / `-p stores=`(10 个调用方逐一核过,没有一处是
    程序从库里算出来的),所以抛错只可能在打错字时触发,不会误伤调度。
    """
    all_names = None            # 凭证表里的全部店名(未经过滤);快照兜底时无从得知
    # ⚠ try 只包**飞书这一跳**(2026-08-27 收窄)。此前解析(_normalize / all_names)
    # 与落盘(_write_snapshot)也在 try 里:飞书完全健康、只是凭证表字段被改名
    # 或单元格形状变了导致 _normalize 抛,照样静默退到**陈旧凭证快照**,还报成
    # 「店铺凭证表读取失败」—— 把本地 bug 伪装成远端故障,指错路。
    # 兜底三要件里的"条件明确而非 catch-all"(conventions §六)指的就是这个:
    # 兜底补的是外部世界的缺陷,不补自己的不确定。飞书故障的兜底行为一字未变。
    try:
        records = feishu.list_records(resources.STORE_CREDENTIALS)
    except Exception as e:
        stores = _read_snapshot()
        if stores is None:
            raise RuntimeError(f"店铺凭证表读取失败且无本地快照可兜底: {e}") from e
        logger.warning("店铺凭证表读取失败,已回退本地快照(%d 家): %s", len(stores), e)
    else:
        f = resources.STORE_CREDENTIALS.fields
        all_names = {n for n in (_cell(r.get("fields", {}), f.store) for r in records) if n}
        stores = _normalize(records)
        _write_snapshot(stores)

    if filter_names:
        wanted = list(dict.fromkeys(filter_names))        # 去重保序
        by_name = {s["name"]: s for s in stores}
        missing = [n for n in wanted if n not in by_name]
        if missing:
            raise ValueError(_missing_msg(missing, by_name, all_names))
        stores = [by_name[n] for n in wanted]
    return stores


def _missing_msg(missing: list[str], by_name: dict, all_names: set | None) -> str:
    """输入:落空的店名 → 输出:分得清「不在册」与「在册但被过滤」的报错文案。

    两者必须分开说:把「在册但没配代理」报成「查无此店」会让人去凭证表找一个
    明明在那儿的店(alloc_audit 的 docstring 专门警告过这一处混淆)。快照兜底
    时手上只有过滤后的名单,分不出来就**明说分不出来**,不硬猜。
    """
    lines = []
    for n in missing:
        if all_names is None:
            lines.append(f"  ✗ {n}:可用店铺里没有(本次是快照兜底,"
                         f"分不清是不在册还是未启用/缺代理)")
        elif n in all_names:
            lines.append(f"  ✗ {n}:在凭证表里,但被过滤掉了"
                         f"(「启用」为否 / 缺 ClientId / 缺代理配置三者之一)")
        else:
            lines.append(f"  ✗ {n}:凭证表里查无此店(名字要一字不差)")
    return ("指定的店铺名有 %d 个没落到实处:\n%s\n  可用店铺(%d 家):%s\n"
            "  ⚠ 不静默跳过:少跑一家而摘要照报成功,比直接失败难发现得多。"
            % (len(missing), "\n".join(lines), len(by_name),
               ", ".join(sorted(by_name, key=sort_key)) or "(空)"))


def registered_names() -> set[str]:
    """输入:无 → 输出:凭证表里**全部**店名(连停用的也算);飞书失败直接抛。

    ⚠ **判"还在不在营"不要用这个,用 `enabled_names()`。** 这一支只回答
    "这个名字在不在凭证表里",**连「启用」都不看** —— 2026-08-22 之前分配链
    六处拿它当在营判据,于是所有者勾了停用的店照样占着品牌、照样用冻结行
    拦着别的店上架。

    现存的正当用途只有一个:报错时分流「查无此店」与「在册但被过滤」
    (`_missing_msg`)—— 把后者报成前者,人会去凭证表找一个明明在那儿的店。

    不做快照兜底:要么拿到当下真值,要么明说读不到。
    """
    f = resources.STORE_CREDENTIALS.fields
    recs = feishu.list_records(resources.STORE_CREDENTIALS, field_names=[f.store])
    return {n for n in (_cell(r.get("fields", {}), f.store) for r in recs) if n}


def _cell(fields: dict, key: str, default: str = "") -> str:
    """多维表格单元格 → 纯文本:文本字段可能返回 [{'text':...}] 段列表,数字字段返回 int/float。"""
    v = fields.get(key, default)
    if isinstance(v, list):
        v = "".join(seg.get("text", "") for seg in v if isinstance(seg, dict))
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return str(v).strip()


def _normalize(records: list[dict]) -> list[dict]:
    f = resources.STORE_CREDENTIALS.fields
    stores = []
    for rec in records:
        fields = rec["fields"]

        if not is_enabled(fields):       # 判据只留一处(见 is_enabled)
            continue

        name = _cell(fields, f.store)
        client_id = _cell(fields, f.client_id, "0")
        client_secret = _cell(fields, f.client_secret, "0")
        if not client_id or client_id == "0":
            continue

        proxy_type = _cell(fields, f.proxy_type, "0")
        ip = _cell(fields, f.proxy_host, "0")
        port = _cell(fields, f.proxy_port, "0")
        username = _cell(fields, f.proxy_user)
        password = _cell(fields, f.proxy_pass)
        if not proxy_type or proxy_type == "0" or not ip or ip == "0" or not port or port == "0":
            continue

        proto = proxy_type if proxy_type in ("socks5", "http", "https") else "http"
        if username and password:
            # URL 编码用户名/密码,防止特殊字符(@:/#)破坏代理 URL
            u = quote(username, safe="")
            p = quote(password, safe="")
            proxy_url = f"{proto}://{u}:{p}@{ip}:{port}"
        else:
            proxy_url = f"{proto}://{ip}:{port}"

        stores.append({
            "name": name,
            "client_id": client_id,
            "client_secret": client_secret,
            "proxy": proxy_url,
        })
    return stores


def _write_snapshot(stores: list[dict]) -> None:
    """快照含密钥:写完立即 chmod 600;写失败只告警,不影响主流程。"""
    try:
        path = paths.stores_snapshot_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stores, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(path, 0o600)
    except Exception as e:
        logger.warning("店铺凭证快照写入失败:%s", e)


def _read_snapshot() -> list[dict] | None:
    path = paths.stores_snapshot_file()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        # 说出来:不记的话"快照文件损坏"会被上面报成「无本地快照可兜底」,
        # 两种截然不同的故障合并成一句话,人按后者去找一个其实存在的文件
        logger.warning("店铺凭证快照损坏,按无快照处理:%s", e)
        return None
