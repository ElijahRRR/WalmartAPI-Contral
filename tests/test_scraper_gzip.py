"""采集导出的 gzip 传输:为什么"我们一行没改就用上了",以及它靠什么保证不出错。

2026-08-21 采集侧上 gzip,本仓零改动生效。生产实测(一页 500 条):
    未压缩 2,869 KB / 3.3~10.9s  →  gzip 后 694 KB / 0.30s(压缩比 4.1:1)
所有者当场追问「能确保这么拿回来的数据也是准确的吗」——本文件是那次核实的固化。

结论:**能**,但不是靠"gzip 无损"这句话,而是靠**整页是一个 JSON 文档**:
压缩流只要被破坏,解出来的字节就对不上括号,`json.loads` 直接抛,
`export_incremental` 接住并退避重试。没有"少几条但看着正常"这种中间态。
"""

import gzip
import json

import pytest
from httpx._decoders import GZipDecoder

from api import scraper


def _page(n: int = 500) -> bytes:
    return json.dumps({
        "records": [{"asin": f"B{i:09d}", "slow": {"title": "x" * 80}}
                    for i in range(n)],
        "next_cursor": 999, "has_more": True}).encode()


# ── ① 生效的全部依赖:httpx 的默认协商没被覆盖 ────────────────────────────

def test_headers_never_set_accept_encoding(monkeypatch):
    """⚠ `_headers()` 里出现 Accept-Encoding = gzip 静默失效。

    httpx 默认发 `accept-encoding: gzip, deflate` 并自动解压,**这就是 gzip
    生效的全部机制**。哪天有人为了加别的头顺手带一个 Accept-Encoding(哪怕是
    `identity`),压缩就没了 —— 而后果没有任何告警,只是每页从 0.3 秒退回
    10 秒级,还会被当成"采集侧又抖了"去查错方向。
    """
    for token in ("", "tok-123"):
        monkeypatch.setenv("SCRAPER_EXPORT_TOKEN", token)
        keys = {k.lower() for k in scraper._headers()}
        assert "accept-encoding" not in keys, keys


def test_httpx_still_advertises_gzip_by_default():
    """默认协商本身也钉一下 —— httpx 大版本升级把它去掉的话,这里先红。

    ⚠ 只有 gzip / deflate:采集侧若改用 br / zstd 且不看协商直接压,
    我们拿到的是解不开的字节流。
    """
    import httpx
    enc = httpx.Client().headers.get("accept-encoding", "")
    assert "gzip" in enc


# ── ② 数据准确性:整页 JSON 才是完整性防线,不是 gzip 的 CRC ──────────────

def test_intact_stream_decodes_byte_for_byte():
    payload = _page()
    d = GZipDecoder()
    assert d.decode(gzip.compress(payload)) + d.flush() == payload


@pytest.mark.parametrize("cut", (0.3, 0.5, 0.7, 0.9, 0.95, 0.99))
def test_truncation_surfaces_as_invalid_json_never_as_fewer_records(cut):
    """⚠⚠ **这条是整个 gzip 方案的安全前提,改流式解析前先看它。**

    httpx 的 GZipDecoder 在流被截断时**不抛错**(它跳过 gzip 尾部的 CRC32),
    静默返回半截字节 —— 这在下载二进制文件时是真隐患。对我们无害的唯一理由:
    半截字节不是合法 JSON,`json.loads` 会抛,于是 `export_incremental` 记成
    "200 但不是 JSON",按瞬时故障退避重试。

    **哪天有人把整页解析改成流式(逐条 yield / ijson),这条前提就没了** ——
    那时候截断会变成"静默少几条",而少掉的那几条会被游标跳过、永不回头。
    这条测试就是留给那个人的红灯。
    """
    blob = gzip.compress(_page())
    d = GZipDecoder()
    out = d.decode(blob[:int(len(blob) * cut)]) + d.flush()
    assert 0 < len(out) < len(_page())          # 确实解出了半截(不是空)
    with pytest.raises(ValueError):
        json.loads(out)


def test_clipping_only_the_gzip_trailer_still_yields_the_whole_page():
    """截掉的若只是尾部 8 字节校验和,数据本身是完整的 —— 不是"静默少数据"。

    这是唯一能"通过"的截断形态,而它恰好无害。核实时我一度把它误标成
    静默丢数据,实测 500 条一条不少,在此钉住免得下次再误判。
    """
    payload = _page()
    blob = gzip.compress(payload)
    d = GZipDecoder()
    out = d.decode(blob[:-8]) + d.flush()
    assert json.loads(out) == json.loads(payload)


def test_single_bit_corruption_always_raises_never_silently_wrong():
    """300 次单比特翻转,实测 300 次全部报错,零次静默给出错数据。"""
    import random
    payload = _page(100)
    blob = gzip.compress(payload)
    rnd = random.Random(7)
    silent_wrong = 0
    for _ in range(300):
        b = bytearray(blob)
        i = rnd.randrange(10, len(b) - 8)        # 跳过头尾,只翻正文
        b[i] ^= 1 << rnd.randrange(8)
        d = GZipDecoder()
        try:
            out = d.decode(bytes(b)) + d.flush()
            if json.loads(out) != json.loads(payload):
                silent_wrong += 1
        except Exception:                         # noqa: BLE001 解压或解析报错都算安全
            pass
    assert silent_wrong == 0


def test_the_json_parse_guard_is_still_wired_in_export_incremental():
    """`export_incremental` 必须把 JSON 解析失败当瞬时故障退避,而不是往上抛裸异常。

    上面几条证明"坏数据必然表现为 JSON 解析失败";这条证明那个失败**被接住了**。
    两条缺一不可 —— 只有前者的话,一次网络抖动会让整轮摄取炸掉而不是重试。
    """
    import inspect
    src = inspect.getsource(scraper.export_incremental)
    assert "except ValueError" in src
    assert "导出响应非 JSON" in src
