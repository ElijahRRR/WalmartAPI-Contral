"""egress_preview — 只读预览:凭证表里每家店会走代理、走直连,还是因半配被拦。

用法:python cli.py egress_preview
**只读**:只读一次飞书店铺凭证表,不调沃尔玛、不写库、不写表、不碰快照。

它把**顺序判断**原样摆出来(所有者定稿 2026-10-21):

    勾了「启用」 → 才连接 → 连接时按代理规则(有代理走代理,没代理直连)

每一步被挡下来的店都逐个点名,所以一眼能看出"这家店为什么没跑"落在哪一步。

判据不自己写:直接调 `services/stores.is_enabled` / `proxy_of`(双轨禁止)。
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
    """输入:无参数 → 输出:按「启用 → 凭证 → 代理」顺序的出口分布,每步点名被挡的店。"""
    f = resources.STORE_CREDENTIALS.fields
    records = feishu.list_records(resources.STORE_CREDENTIALS)

    full, direct, partial = [], [], []
    off_never, off_explicit, no_cred = [], [], []
    for rec in records:
        fields = rec.get("fields", {})
        name = stores_svc._cell(fields, f.store)
        if not name:
            continue
        row = {"name": name, "proxy": None}
        # 第一步:勾了「启用」才往下走。「从未勾过」与「勾过又取消」分开报 ——
        # 前者飞书整个字段不返回,是 2026-09-21 之前那 431 行的形状,人最容易
        # 以为"我没动它就是开着的"。
        if not stores_svc.is_enabled(fields):
            (off_explicit if f.enabled in fields else off_never).append(row)
            continue
        # 第二步:有没有沃尔玛凭证。
        if stores_svc._blank(stores_svc._cell(fields, f.client_id, "0")):
            no_cred.append(row)
            continue
        # 第三步:按代理规则分流。
        verdict, row["proxy"] = stores_svc.proxy_of(fields)
        {stores_svc.PROXY_FULL: full,
         stores_svc.PROXY_NONE: direct,
         stores_svc.PROXY_PARTIAL: partial}[verdict].append(row)

    callable_n = len(full) + len(direct)
    lines = [f"出口预览(只读):凭证表 {len(records)} 行 → **会连接 {callable_n} 家**"
             f"(走代理 {len(full)} / 直连 {len(direct)})"]
    lines.append(f"  被挡在第一步「没勾启用」:{len(off_never) + len(off_explicit)} 家"
                 f"(从未勾过 {len(off_never)} / 勾过又取消 {len(off_explicit)})"
                 f";第二步「没有 ClientId」:{len(no_cred)} 家"
                 f";第三步「代理只填了一半」:{len(partial)} 家")

    if direct:
        lines.append(f"  ▸ 直连 {len(direct)} 家(勾了启用、有凭证、没填代理):{_names(direct)}")
        lines.append(f"    ⚠ 走的是**本机出口 IP**"
                     + (";多家同时直连 = 共享同一个出口,沃尔玛侧有做店铺关联的料"
                        if len(direct) > 1 else ""))
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

    lines.append("  ⓘ 判据出处 services/stores.is_enabled + proxy_of;"
                 "本工作流只读,没有任何副作用")
    return "\n".join(lines)
