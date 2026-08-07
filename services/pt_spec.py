"""MP_ITEM 按 PT 拆分 spec 加载器(listing L2c)。

目录:registry.paths.mp_item_spec_dir()(<DATA_ROOT>/specs/MP_ITEM/<版本>/),
内容为旧仓库 MPSetup_by_pt 拆分产物:_pt_index.json + _orderable.json +
逐 PT json——451MB 单文件 json.load 膨胀 1.3GB 触发 OOM 的历史事故,
解法就是按 PT 拆 + lru_cache(旧 maxsize=512 约 50MB 常驻,原值沿用)。
"""

import json
import logging
from functools import lru_cache

from registry import paths

logger = logging.getLogger("services.pt_spec")


def _spec_dir():
    d = paths.mp_item_spec_dir()
    if not (d / "_pt_index.json").exists():
        raise FileNotFoundError(
            f"MP_ITEM spec 未就位:{d}/_pt_index.json 不存在。"
            f"请把旧仓库 walmart_official_specs/MPSetup_by_pt/ 的全部内容"
            f"拷入该目录(含 _pt_index.json/_orderable.json/各 PT json)")
    return d


def _sanitize_filename(pt: str) -> str:
    """PT 名 → 拆分文件名候选(旧拆分工具对特殊字符的处理未入档,
    按常见规则生成候选,load_pt 逐个探测存在性)。"""
    return "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in pt)


@lru_cache(maxsize=1)
def pt_index() -> dict:
    """输入:无 → 输出:{PT 名: 文件名或 None(None=按候选规则探测)}。

    兼容 _pt_index.json 的多种形态(旧拆分工具产物,格式未入档):
    dict{pt: 文件名} / list[str PT 名] / list[dict{pt/name…: 文件…}]。
    """
    with open(_spec_dir() / "_pt_index.json", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    out: dict = {}
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            out[item] = None
        elif isinstance(item, dict):
            pt = (item.get("pt") or item.get("product_type")
                  or item.get("productType") or item.get("name"))
            fn = (item.get("file") or item.get("filename")
                  or item.get("path"))
            if pt:
                out[str(pt)] = str(fn) if fn else None
    if not out:
        raise ValueError(f"_pt_index.json 格式无法识别(既非 dict 也非可解析"
                         f" list):{str(raw)[:200]}")
    return out


@lru_cache(maxsize=1)
def orderable_spec() -> dict:
    """输入:无 → 输出:_orderable.json(Orderable 公共段 schema)。"""
    with open(_spec_dir() / "_orderable.json", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=512)
def load_pt(product_type: str) -> dict | None:
    """输入:Product Type 名 → 输出:该 PT 的 spec;未收录/文件缺失 None。

    未收录 PT 返回 None 而非抛错——调用方按"PT 无 spec"淘汰该行并落原因,
    不炸整轮。索引未带文件名时按候选规则探测(原名/清洗名 + .json)。
    """
    idx = pt_index()
    if product_type not in idx:
        return None
    d = _spec_dir()
    candidates = ([idx[product_type]] if idx[product_type] else []) + [
        f"{product_type}.json", f"{_sanitize_filename(product_type)}.json"]
    for fname in candidates:
        fp = d / fname
        if fp.exists():
            with open(fp, encoding="utf-8") as f:
                return json.load(f)
    logger.warning("PT 在索引中但拆分文件未找到:%s(候选=%s)",
                   product_type, candidates)
    return None


def known_pts() -> set[str]:
    """输入:无 → 输出:已收录的全部 PT 名集合。"""
    return set(pt_index().keys())


def clear_caches() -> None:
    """输入:无 → 输出:无(换版/测试时清缓存)。"""
    pt_index.cache_clear()
    orderable_spec.cache_clear()
    load_pt.cache_clear()
