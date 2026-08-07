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


@lru_cache(maxsize=1)
def pt_index() -> dict:
    """输入:无 → 输出:_pt_index.json 内容(PT 名 → 拆分文件名)。"""
    with open(_spec_dir() / "_pt_index.json", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def orderable_spec() -> dict:
    """输入:无 → 输出:_orderable.json(Orderable 公共段 schema)。"""
    with open(_spec_dir() / "_orderable.json", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=512)
def load_pt(product_type: str) -> dict | None:
    """输入:Product Type 名 → 输出:该 PT 的 Visible 段 schema;未收录 None。

    未收录 PT 返回 None 而非抛错——调用方按"PT 无 spec"淘汰该行并落原因,
    不炸整轮。
    """
    idx = pt_index()
    fname = idx.get(product_type)
    if not fname:
        return None
    fp = _spec_dir() / fname
    if not fp.exists():
        logger.warning("PT spec 索引有名但文件缺失:%s → %s", product_type, fname)
        return None
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def known_pts() -> set[str]:
    """输入:无 → 输出:已收录的全部 PT 名集合。"""
    return set(pt_index().keys())


def clear_caches() -> None:
    """输入:无 → 输出:无(换版/测试时清缓存)。"""
    pt_index.cache_clear()
    orderable_spec.cache_clear()
    load_pt.cache_clear()
