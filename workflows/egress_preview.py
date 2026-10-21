"""egress_preview — 只读预览:凭证表里每家店会走代理、走直连,还是因半配被拦。

用法:python cli.py egress_preview
**只读**:只读一次飞书店铺凭证表,不调沃尔玛、不写库、不写表、不碰快照。

为什么要有它(所有者定稿 2026-10-21 的配套闸,见 docs/plan.md 同日条目):
出口规则改成「没填代理就直连」之后,**拉取名单会变大** —— 此前「有 ClientId
但没填代理」的店是被整店过滤掉的,改完全部变成"能调 API"。而凭证表里有大量
行的「启用」是空的(飞书对**从未勾过**的复选框整个字段不返回,缺省视为启用:
2026-09-19「未启用店被拉取」就是这么来的,当时 530 行里 431 行是空的),所有者
上次手工勾选核的是**旧定义**下的名单。所以真跑任何链之前,先用这条把"会新增
进名单的店"逐个点名,人眼过一遍。

判据不自己写:直接调 `services/stores.proxy_of` / `is_enabled`(双轨禁止)。
本工作流只负责把那套判断**摆给人看**,一旦有人改了判据,这里的输出跟着变。
"""

import logging

from api import feishu
from registry import resources
from services import stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.egress_preview")


def _names(rows: list[dict]) -> str:
    return "、".join(sorted((r["name"] for r in rows), key=stores_svc.sort_key)) or "(无)"


def run(params: dict) -> str:
    """输入:无参数 → 输出:出口分布预览(走代理/直连/半配被拦)+ 新增进名单的店。"""
    f = resources.STORE_CREDENTIALS.fields
    records = feishu.list_records(resources.STORE_CREDENTIALS)

    full, direct, partial, no_cred, off = [], [], [], [], []
    for rec in records:
        fields = rec.get("fields", {})
        name = stores_svc._cell(fields, f.store)
        if not name:
            continue
        if not stores_svc.is_enabled(fields):
            off.append({"name": name})
            continue
        client_id = stores_svc._cell(fields, f.client_id, "0")
        if stores_svc._blank(client_id):
            no_cred.append({"name": name})
            continue
        verdict, proxy_url = stores_svc.proxy_of(fields)
        # 「启用」从未勾过 ⇒ 飞书整个字段不返回 ⇒ 缺省视为启用。这类行是
        # "悄悄进名单"的来源,单独标出来(2026-09-19 事故同款)。
        never_ticked = f.enabled not in fields
        row = {"name": name, "proxy": proxy_url, "never_ticked": never_ticked}
        {stores_svc.PROXY_FULL: full,
         stores_svc.PROXY_NONE: direct,
         stores_svc.PROXY_PARTIAL: partial}[verdict].append(row)

    callable_n = len(full) + len(direct)
    lines = [f"出口预览(只读):凭证表 {len(records)} 行 → 能调 API {callable_n} 家"
             f"(走代理 {len(full)} / 直连 {len(direct)});"
             f"代理半配被拦 {len(partial)};缺 ClientId {len(no_cred)};已停用 {len(off)}"]

    if direct:
        lines.append(f"  ▸ 直连 {len(direct)} 家(凭证表没填代理):{_names(direct)}")
        lines.append(f"    ⚠ 这 {len(direct)} 家都是**本次规则改动新增进名单**的"
                     f"(改动前它们被整店过滤);走的是本机出口 IP"
                     + (",多家同时直连 = 共享同一个出口,沃尔玛侧有做店铺关联的料"
                        if len(direct) > 1 else ""))
        quiet = [r for r in direct if r["never_ticked"]]
        if quiet:
            lines.append(f"    ⚠⚠ 其中 {len(quiet)} 家的「启用」**从未勾过**"
                         f"(飞书不返回该字段 → 缺省视为启用):{_names(quiet)}"
                         f" —— 这几家最需要你确认是不是真要跑")
    if partial:
        lines.append(f"  ▸ 代理半配、**不会跑** {len(partial)} 家:{_names(partial)}"
                     f" —— 去凭证表把「{f.proxy_type} / {f.proxy_host} / {f.proxy_port}」补齐")

    shared: dict[str, list[dict]] = {}
    for r in full:
        shared.setdefault(r["proxy"], []).append(r)
    for url, rows in sorted(shared.items(), key=lambda kv: -len(kv[1])):
        if len(rows) > 1:
            lines.append(f"  ▸ 共用代理 {stores_svc._proxy_label(url)}:"
                         f"{len(rows)} 家({_names(rows)})—— 允许,不抢配额")

    lines.append("  ⓘ 判据出处 services/stores.proxy_of;本工作流只读,没有任何副作用")
    return "\n".join(lines)
