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


def _norm(s: str) -> str:
    """规范化:去掉所有非字母数字 + casefold——旧拆分工具的文件名清洗规则
    未入档(生产实证 '3-in-1 Shampoo, Conditioner & Body Washes' 按常见
    规则猜不中),两边同归一后匹配,对任何清洗规则都成立。"""
    return "".join(ch.casefold() for ch in s if ch.isalnum())


@lru_cache(maxsize=1)
def _file_map() -> dict[str, str]:
    """输入:无 → 输出:{规范化文件名: 实际文件名}(目录一次扫描,7k 项廉价)。"""
    m: dict[str, str] = {}
    for p in _spec_dir().iterdir():
        if p.suffix == ".json" and not p.name.startswith("_"):
            key = _norm(p.stem)
            if key in m and m[key] != p.name:
                logger.warning("PT 拆分文件规范化名冲突:%s vs %s(取前者)",
                               m[key], p.name)
                continue
            m[key] = p.name
    return m


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
    fname = idx[product_type]
    if not (fname and (d / fname).exists()):
        # 索引没带文件名/带的不存在 → 按目录真实文件名规范化匹配
        fname = _file_map().get(_norm(product_type))
    if not fname:
        logger.warning("PT 在索引中但拆分文件未找到:%s(规范化键=%s)",
                       product_type, _norm(product_type))
        return None
    with open(d / fname, encoding="utf-8") as f:
        return json.load(f)


def known_pts() -> set[str]:
    """输入:无 → 输出:已收录的全部 PT 名集合。"""
    return set(pt_index().keys())


def coverage() -> tuple[int, int]:
    """输入:无 → 输出:(索引 PT 数, 可解析到拆分文件的 PT 数)。就位自检用。"""
    fm = _file_map()
    idx = pt_index()
    d = _spec_dir()
    ok = sum(1 for pt, fn in idx.items()
             if (fn and (d / fn).exists()) or _norm(pt) in fm)
    return len(idx), ok


def clear_caches() -> None:
    """输入:无 → 输出:无(换版/测试时清缓存)。"""
    pt_index.cache_clear()
    orderable_spec.cache_clear()
    load_pt.cache_clear()
    _file_map.cache_clear()
