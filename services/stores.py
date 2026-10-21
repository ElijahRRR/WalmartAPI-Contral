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
#: 为什么安全:沃尔玛配额按 `(store, endpoint)` 计,`api/_client` 的令牌桶按
#: `(client_id, bucket)` 限流 —— **就是这一条**,店与店不抢同一个桶。所以加跨店
#: 并发不会挤占同一个桶 —— 真正要小心的是**店内**对同一接口的并发,那个各链
#: 自己按端点配额定(如 catalog_sync 的 `_FILL_WORKERS=8` 对应 items.get 800/min)。
#:
#: ⚠ 这里曾写「每店有自己的固定出口代理,店铺之间不共享出口」当论据 —— 2026-10-21
#: 所有者定稿**允许多店共用一个代理、也允许没配代理的店直连**,那句话不再成立。
#: 结论没变、理由变了:支撑 24 的是令牌桶的键,不是出口的独占。共用出口真正共享
#: 的是**那条链路的带宽与连接池**(`_client_pool` 按代理 URL 池化),是吞吐问题,
#: 不是配额问题,也不是封号问题。
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
      · ClientId / 代理 = **技术就绪**(现在能不能发请求;没填代理是直连,
        **不**算不就绪 —— 2026-10-21 起只有"代理填了一半"才拦)。
    合并的后果两个方向都严重 —— 拿 `load_stores()`(合并版)判在营,
    「在册但代理只填了一半的在营店」会被判成死店而整店下线;拿
    `registered_names()`(不看启用)判在营,勾了停用的店照样占着品牌、
    照样用冻结行拦着别的店上架。

    ★ **缺省是"不启用"**(所有者定稿 2026-10-21:「要根据是否勾选启用来决定是否
    连接,没有被勾选的是不允许连接凭证的」)。**只有明确勾了才算启用** ——
    顺序是:勾了启用 → 才连接 → 连接时按代理规则(`proxy_of`)。

    ⚠ 为什么翻这个默认值:飞书对**从未勾过**的复选框**整个字段都不返回**
    (勾过又取消的会老实回 `False`)。原口径是"缺省视为启用",于是"查无此字段"
    被当成了"启用" —— 2026-09-19 生产实见:凭证表 530 行里 431 行是这种空值,
    谭总1/谭总3 因此被拉去跑了,所有者只能手工把复选框点两下(勾上再取消)
    才让飞书开始返回 `False`。原口径的理由是"旧 xlsx 无此列",那个理由已经
    不存在:列在表上,所有者在用它。
    """
    v = fields.get(resources.STORE_CREDENTIALS.fields.enabled)
    if v is True:
        return True
    # 旧 xlsx 迁来的文本写法;其余一切(含字段缺失、False、空串、看不懂的值)
    # 一律按**不启用**。判不准就不连 —— 猜错的那个方向是拿真实凭证去连沃尔玛。
    return isinstance(v, str) and v.strip() in ("是", "true", "True", "1")


def enabled_names() -> set[str]:
    """输入:无 → 输出:**在营**店名集合(在册 ∧ 启用);飞书失败直接抛。

    与另外两个的分工 —— 三层,别混:

    | 函数 | 回答 | 谁该用 |
    |---|---|---|
    | `registered_names()` | 在不在**册** | 只有"查无此店 vs 在册但停用"这种报错分流 |
    | **`enabled_names()`** | 在不在**营** | **判死店、判占用、判分配范围,一律用这个** |
    | `load_stores()` | 现在能不能**调 API** | 真要发请求的链 |

    不做快照兜底:兜底快照是 `load_stores` 过滤后的结果,拿它当在营名录会把
    「代理半配的在营店」算成停用 —— 而这个名录直通整店下线。要么拿到当下真值,
    要么明说读不到。
    """
    f = resources.STORE_CREDENTIALS.fields
    recs = feishu.list_records(resources.STORE_CREDENTIALS,
                               field_names=[f.store, f.enabled])
    return {n for n in (_cell(r.get("fields", {}), f.store)
                        for r in recs if is_enabled(r.get("fields", {}))) if n}


def load_stores(filter_names: list[str] | None = None) -> list[dict]:
    """输入:可选店铺名列表(None=全部)→ 输出:有效店铺 list[dict](name/client_id/client_secret/proxy)。

    `proxy` 可能是 `None` —— 那是**直连**,不是"没取到"(所有者定稿 2026-10-21,
    判据与理由见 `proxy_of`)。消费方一律原样传给 api 层,别自己补默认值。

    数据源:飞书店铺凭证表(registry.STORE_CREDENTIALS)。
    成功读取后写本地快照(<DATA_ROOT>/cache/stores_snapshot.json,chmod 600);
    飞书不可用时回退读最近一次快照并告警——保证飞书故障不瘫痪全部工作流。

    过滤规则:
      1. 「启用」没勾 → 跳过(**缺省不启用**,所有者定稿 2026-10-21;见 `is_enabled`)
      2. ClientId 为空或 "0" → 跳过
      3. 代理按 `proxy_of` 三岔(所有者定稿 2026-10-21):三件套齐 → 走代理;
         五个字段全空 → `proxy=None` 直连;**只填了一半 → 跳过并点名**。
    每轮把"直连几家 / 半配被排除几家 / 哪几家共用同一个代理"点名进日志
    (`_log_egress`);**只告警不拦**(所有者 2026-10-21 定)。

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

    两者必须分开说:把「在册但代理只填了一半」报成「查无此店」会让人去凭证表
    找一个明明在那儿的店(alloc_audit 的 docstring 专门警告过这一处混淆)。快照兜底
    时手上只有过滤后的名单,分不出来就**明说分不出来**,不硬猜。
    """
    lines = []
    for n in missing:
        if all_names is None:
            lines.append(f"  ✗ {n}:可用店铺里没有(本次是快照兜底,"
                         f"分不清是不在册还是没勾启用/缺 ClientId/代理半配)")
        elif n in all_names:
            lines.append(f"  ✗ {n}:在凭证表里,但被过滤掉了"
                         f"(「启用」没勾或为否 / 缺 ClientId / 代理只填了一半 三者之一)")
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


#: 代理配置的三种判定(所有者定稿 2026-10-21)。值本身只在 `proxy_of` 用,
#: 但给了名字,下游测试与日志才能引用同一套词。
PROXY_FULL = "full"            # 三件套齐 → **必须**走这个代理
PROXY_NONE = "none"            # 五个字段一个没填 → 直连
PROXY_PARTIAL = "partial"      # 有人填过,但三件套不齐 → 拒跑并点名


def _blank(v: str) -> bool:
    """凭证表的"没填":空,或者那个当空用的 `"0"`(全表沿用的习惯,别改)。"""
    return not v or v == "0"


def proxy_of(fields: dict) -> tuple[str, str | None]:
    """输入:凭证表一行的 fields → 输出:(判定, 代理 URL 或 None)。

    ★ **全项目"这家店走代理还是直连"的唯一判据**(所有者定稿 2026-10-21:
    「填写了代理的,必须使用代理连接;填写了代理但是代理连不上的按原逻辑处理;
    如果只填写了沃尔玛 api 凭证但是没有填写代理的,就走直连」)。

    | 代理五个字段 | 判定 | 产出 |
    |---|---|---|
    | 三件套(类型/IP/端口)齐,账号密码可选 | `PROXY_FULL` | 代理 URL(行为与 2026-10-21 之前一字不差) |
    | 五个全空 | `PROXY_NONE` | `None` = 直连 |
    | 其余 | `PROXY_PARTIAL` | `None`,**调用方必须排除该店** |

    ⚠ **半配为什么不能当"没填"走直连**(这是本函数存在的全部理由):填了一半
    说明所有者**本来就要走代理**,只是端口打错、或 IP 填成了 `0`。把它当没填 →
    拿真实店铺凭证裸连沃尔玛 → 正是「店铺关联封号」那条红线。所以半配既不代理
    也不直连,**整店拒跑并点名**,人去把字段补齐。判不准就不跑,比猜一个方向跑
    安全得多 —— 猜错的那个方向是不可逆的(封号)。

    ⚠ **代理连不上不在本函数的管辖内**:本函数只看"填没填",不看"通不通"。
    填了就产出 URL,连不上由 api/_client 抛 `StoreProxyError` → `store_retry.diagnose`
    归「代理无效/代理波动」→ 串行补试 → 缺席点名 → 链尾重赛(所有者明确要求
    「按原逻辑处理」)。**不许在这里加"代理 ping 不通就改直连"的兜底** —— 那是
    "写操作永不自动兜底"的同款陷阱:换条链路重试 = 用裸连替代理,风险翻面。

    账号/密码的 URL 编码与取舍(`if username and password`)保持原样,一个字没动:
    现网有店把账号密码填成 `"0"`,改判会把它们从 URL 里摘掉,等于悄悄改坏一家
    在跑的店的代理认证。
    """
    f = resources.STORE_CREDENTIALS.fields
    proxy_type = _cell(fields, f.proxy_type, "0")
    ip = _cell(fields, f.proxy_host, "0")
    port = _cell(fields, f.proxy_port, "0")
    username = _cell(fields, f.proxy_user)
    password = _cell(fields, f.proxy_pass)

    triple = (proxy_type, ip, port)
    if all(_blank(v) for v in triple + (username, password)):
        return PROXY_NONE, None
    if any(_blank(v) for v in triple):
        return PROXY_PARTIAL, None

    proto = proxy_type if proxy_type in ("socks5", "http", "https") else "http"
    if username and password:
        # URL 编码用户名/密码,防止特殊字符(@:/#)破坏代理 URL
        u = quote(username, safe="")
        p = quote(password, safe="")
        return PROXY_FULL, f"{proto}://{u}:{p}@{ip}:{port}"
    return PROXY_FULL, f"{proto}://{ip}:{port}"


def _proxy_label(url: str) -> str:
    """代理 URL → 人看的标签(`1.2.3.4:1080`)。**账号密码必须摘掉**:日志会进
    文件、进摘要、进飞书,代理口令不许跟着跑出去(密钥不进 git 的同款纪律)。"""
    return url.split("://", 1)[-1].rsplit("@", 1)[-1]


def _log_egress(stores: list[dict], direct: list[str], partial: list[str]) -> None:
    """点名:谁在直连、谁的代理只填了一半、哪几家共用同一个代理。**只告警,不拦。**

    为什么必须每轮都点名:2026-09-19 的「未启用店被拉取」就是靠人眼发现的 ——
    凭证表 530 行里有 431 行「启用」是空的(飞书对从未勾过的复选框**整个字段
    不返回**),而当时的口径是"缺省视为启用",于是条件一放宽就有店悄悄进了名单
    (那个默认值已于 2026-10-21 翻成"不启用",见 `is_enabled`)。直连与共用代理
    都是"悄悄进来了也不报错"的那类变化,日志里不点名就只能等出事才知道。
    """
    if partial:
        logger.warning("代理只填了一半,这 %d 家**本轮没跑**:%s —— "
                       "填了一半说明本来要走代理,当「没填」直连就是裸连沃尔玛;"
                       "去凭证表把「代理类型 / IP地址或域名 / 端口」补齐",
                       len(partial), "、".join(sorted(partial, key=sort_key)))
    if direct:
        logger.warning("直连(凭证表没填代理)%d 家:%s —— 走的是**本机出口 IP**%s",
                       len(direct), "、".join(sorted(direct, key=sort_key)),
                       ";⚠ 多家同时直连 = 共享同一个出口 IP,沃尔玛侧有关联的料"
                       if len(direct) > 1 else "")
    shared: dict[str, list[str]] = {}
    for s in stores:
        if s["proxy"]:
            shared.setdefault(s["proxy"], []).append(s["name"])
    for url, names in shared.items():
        if len(names) > 1:
            # 所有者 2026-10-21 明确允许多店共用一个代理,所以这是 info 不是 warning。
            logger.info("共用代理 %s:%d 家(%s)—— 允许;配额桶按 (client_id, bucket) 记,"
                        "共用的只是这条出口的带宽与连接池",
                        _proxy_label(url), len(names),
                        "、".join(sorted(names, key=sort_key)))


def _normalize(records: list[dict]) -> list[dict]:
    f = resources.STORE_CREDENTIALS.fields
    stores: list[dict] = []
    direct: list[str] = []
    partial: list[str] = []
    for rec in records:
        fields = rec["fields"]

        if not is_enabled(fields):       # 判据只留一处(见 is_enabled)
            continue

        name = _cell(fields, f.store)
        client_id = _cell(fields, f.client_id, "0")
        client_secret = _cell(fields, f.client_secret, "0")
        if not client_id or client_id == "0":
            continue

        verdict, proxy_url = proxy_of(fields)      # 判据只留一处(见 proxy_of)
        if verdict == PROXY_PARTIAL:
            partial.append(name)
            continue
        if verdict == PROXY_NONE:
            direct.append(name)

        stores.append({
            "name": name,
            "client_id": client_id,
            "client_secret": client_secret,
            "proxy": proxy_url,         # None = 直连(api/_client 的契约)
        })
    _log_egress(stores, direct, partial)
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
