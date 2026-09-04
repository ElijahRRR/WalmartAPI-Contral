"""喂入版渲染回归(`services/policy_feed`;口径 `docs/policy_sync.md` §十.3-§十.5)。

这一层是**给 LLM 的判据文本**唯一的产地:库里存官方原文,喂 LLM 的版本渲染时
派生。它错了不会有任何东西报红——只会让 L3 的政策段悄悄少一段、或者多喂几千
token 的 URL。所以断言全部打在**真实转录件**上,不用编造夹具:

  · 剥干净(URL / 页内导览 / 免责声明 / chrome)——**同时**证明判据一条没少;
  · 多行表原样保留 —— 那 3 张表按行承载语义,拆成列会毁掉行对应关系;
  · 幂等 —— 渲染两次必须一模一样(将来接 S4 时可能被重复调用)。
"""

import pathlib
import re

from registry import paths
from services import policy_feed as pf
from workflows import policy_sync as ps

_EN = paths.policy_pages_dir("en")
_DISCLAIMER = "The third-party information found within this policy"


def _body(name: str) -> str:
    """输入:转录件文件名 → 输出:它的 full_policy(与入库同一条路径)。"""
    return ps.parse_policy_file(_EN / name)["full_policy"]


def _feed(name: str) -> str:
    return pf.render_feed_text(_body(name))


# ── 剥干净,但一条判据不少 ────────────────────────────────────────────────

def test_funeral_is_stripped_but_keeps_every_prohibited_item():
    """15-funeral 全流程:URL / 导览 / 免责声明全没了,7 条禁止项一条不少。"""
    out = _feed("15-funeral-products.md")
    assert "https://" not in out and "http://" not in out
    assert "In this guide" not in out
    assert _DISCLAIMER not in out and "legal advice" not in out
    for item in ("Grave markers, headstones, and monuments.",
                 "Human or animal remains.",
                 "Used funeral items.",
                 "Repurposed funeral items (i.e. Urns marketed as a cookie jar)",
                 "Items obtained from government or protected land, grave sites,"
                 " or historical locations.",
                 "Stolen or looted funeral products.",
                 "Sacred items used by Native Americans in ceremonial practices."):
        assert item in out, item
    # 三列都要带列名出现(免得 LLM 把"允许"读成"禁止")
    for col in ("Prohibited:", "Allowed with restriction:", "Allowed:"):
        assert f"\n{col}" in out, col


def test_plain_text_in_this_guide_is_dropped_too():
    """⚠ 02-animals 的导览标题是**纯文本** `In this guide:`(没有粗体、没有 #)。

    官方各页四种形态(`**…**` / 纯文本 / `##` / `###`)都得吃 —— 只认粗体的话,
    9 份纯文本导览会连着一串锚点链接整段喂进提示词。
    """
    raw = _body("02-animals.md")
    assert raw.splitlines()[0] == "In this guide:"      # 转录件确实是纯文本
    out = _feed("02-animals.md")
    assert "In this guide" not in out
    assert "- Live animals]" not in out and "#liveanimals" not in out
    assert "Live animals including pets, livestock, and marine animals" in out


def test_link_text_survives_but_the_url_does_not():
    """链接只留文字:法规名是判据,URL 对判定零贡献(§十.4)。"""
    out = _feed("02-animals.md")
    assert "Migratory Bird Treaty Act of 1918" in out
    assert "fws.gov" not in out and "https://" not in out


def test_all_notes_disclaimers_are_gone_across_the_corpus():
    """41/42 份带同一句免责声明(四种细微变体),一句都不许漏。"""
    left = [p.name for p in sorted(_EN.glob("*.md"))
            if _DISCLAIMER in pf.render_feed_text(_body(p.name))]
    assert left == []


def test_other_notes_sections_are_kept():
    """⚠ 27-PFAS 的 `**Notes:**` 段装的是**真判据**(各州 Covered Products 不同)。

    按 Notes 标题一刀切会把它一起删掉 —— 只删已知免责文那一段。
    """
    out = _feed("27-pfas-chemicals.md")
    assert "Covered Products vary by state and may be updated periodically" in out
    assert "It is the seller’s responsibility to confirm that their product" in out
    assert _DISCLAIMER not in out


def test_known_source_page_stray_is_cut_but_the_item_survives():
    """⚠ 32 号官方源页把 PDF 页眉串进了**一个条目的中间**(refdata README 第 3 条)。

    转录件按忠实纪律照录,清洗在渲染层做:剪掉残句、**保留条目其余文字** ——
    整条丢掉的话,"通过其他方式被认定为不安全的产品"这条禁售判据就没了。
    """
    stray = ("Marketplace Prohibited Product Policy by Category 5 "
             "Walmart Confidential & Proprietary Information – Do Not Distribute")
    raw = _body("32-restricted-illegal-products.md")
    assert stray in raw                                   # 转录件确实照录了
    out = _feed("32-restricted-illegal-products.md")
    assert stray not in out
    assert "Walmart Confidential" not in out and "Do Not Distribute" not in out
    assert "Products determined to be unsafe through other means" in out
    # 同一单元格里残句前后的条目一条不少
    assert "Products identified as hazardous or non-compliant in any recalls, "\
           "regulatory notices, guidance, or warning letters." in out
    assert "Products that require a quantity or age restriction/verification or "\
           "require consumer purchases to be registered and/or reported." in out


def test_the_stray_list_is_a_module_constant_of_literal_strings():
    """已知残句**逐字**登记(出处 refdata README 第 3 条):不许改成通配。

    通配匹配"看着像页眉的行"会连真判据一起吃 —— 这类清洗必须是白名单。
    """
    assert isinstance(pf._KNOWN_STRAY, tuple) and pf._KNOWN_STRAY
    for s in pf._KNOWN_STRAY:
        assert isinstance(s, str) and len(s) > 40
        assert not set(s) & set("*?[]\\^$+")          # 是固定串,不是正则


def test_chrome_lines_are_dropped():
    """05-auto 把 `Guide` / `Reading time: 3 min` / `Last updated on …` 一起转录了。"""
    raw = _body("05-auto-and-motor-vehicles.md")
    assert "Reading time: 3 min" in raw and "\nGuide\n" in "\n" + raw
    out = _feed("05-auto-and-motor-vehicles.md")
    lines = out.splitlines()
    assert "Guide" not in lines and "Bookmark" not in lines
    assert not [ln for ln in lines if ln.startswith("Reading time:")]
    assert not [ln for ln in lines if ln.startswith("Last updated on ")]


# ── 表格:单行拆列、多行原样 ──────────────────────────────────────────────

def test_single_row_table_becomes_column_labelled_items():
    """01-alcohol:`<br>` 拆条目、`&nbsp;` 串转缩进(嵌套层级保留)。"""
    out = _feed("01-alcohol.md")
    assert "<br>" not in out and "&nbsp;" not in out and "| ---" not in out
    assert "\nProhibited:\nAlcoholic beverages, including:\n" in out
    assert "\n  Hard liquors (e.g. vodka, whiskey, brandy, etc.)\n" in out
    assert "\n  Beer\n" in out                       # 二层缩进 = 官方的二层


def test_deeper_nesting_keeps_its_level():
    """17-hazardous 的四个 `&nbsp;` = 二层,不能和一层的条目拍平成一层。"""
    out = _feed("17-hazardous-items.md")
    assert "\nProducts that contain Carbon Tetrachloride, such as, but not "\
           "limited to:\n" in out
    assert "\n    Fire extinguishers.\n" in out


def test_multi_row_tables_are_kept_verbatim():
    """⚠ 33 的 e-Bike 分级表**按行**承载语义(Class 2 = 这一行的三条定义)。

    拆成「列名 → 条目」会把行对应关系毁掉:四个 class 的定义混成一堆。
    这类表(33 / 27 / 26 共 3 张)原样保留 markdown。
    """
    out = _feed("33-ride-ons-and-micromobility-devices.md")
    assert "| e-Bike class | Definition |" in out
    assert "| --- | --- |" in out
    for row in ("| Class 1 | - Bike with pedals<br>- Includes an electric motor "
                "rated up to 750w<br>- Operates with pedal assistance up to "
                "20 MPH |",
                "| Out of class/e-Moto | - Bike with pedals<br>- Includes an "
                "electric motor rated over 750w<br>- Operates under motor power "
                "alone capable of exceeding 20 MPH<br>- Operates with pedal "
                "assistance capable of exceeding 28 MPH<br>- Cannot be marketed "
                "as an e-Bike |"):
        assert row in out, row
    # 同一份里的单行表照常拆列
    assert "\nProhibited:\nToy Ride-Ons that are marketed to be street legal\n" in out


def test_pfas_multi_row_table_survives_too():
    out = _feed("27-pfas-chemicals.md")
    assert "| **Product Category** | **Definition** |" in out
    assert "| **Ski Wax or Related Tuning Products** |" in out


# ── 纯函数性质 ────────────────────────────────────────────────────────────

def test_render_is_idempotent_on_every_transcript():
    """对自身输出再跑一遍必须不变(将来 S4 可能重复调用,漂了没人看得见)。"""
    drifted = []
    for p in sorted(_EN.glob("*.md")):
        once = pf.render_feed_text(_body(p.name))
        if pf.render_feed_text(once) != once:
            drifted.append(p.name)
    assert drifted == []


def test_no_url_survives_anywhere_in_the_corpus():
    """链接不进提示词(§十.4):42 份渲染后一个 http 都不许剩。"""
    left = [p.name for p in sorted(_EN.glob("*.md"))
            if "http://" in pf.render_feed_text(_body(p.name))
            or "https://" in pf.render_feed_text(_body(p.name))]
    assert left == []


def test_bare_url_and_autolink_forms_are_removed():
    """裸 URL 与 `<http…>` 自动链接整体删,链接文字照留。"""
    out = pf.render_feed_text(
        "See https://example.com/x?a=1 for details.\n"
        "<https://example.com/y>\n"
        "Read the [Lacey Act](https://www.fws.gov/lacey-(act)) first.")
    assert "example.com" not in out
    assert "See for details." in out
    assert "Read the Lacey Act first." in out          # 括号在 URL 里也要配对


def test_glued_and_parenthesised_urls_are_swept_too():
    """⚠ **构造输入**(语料里眼下没有这两种形态,所以语料断言证明不了这条)。

    `_strip_urls` 按设计只认"前面是空白"的裸 URL(否则会误伤 `](url)` 的右半),
    于是 `(https://…)` 与 `Ref:https://…` 两种粘连形态漏网。链接不进提示词是
    硬口径(§十.4),末尾兜底那一遍必须把它们扫掉 —— 官方哪天改个写法就用上了。
    """
    out = pf.render_feed_text(
        "See the state list (https://example.com/a) before listing.\n"
        "Ref:https://example.com/b applies.")
    assert "http" not in out and "example.com" not in out
    assert "See the state list before listing." in out
    assert "applies." in out
    assert pf.render_feed_text(out) == out                 # 兜底也得幂等


def test_paren_inside_link_url_does_not_leak():
    """⚠ 官方链接自己带括号(`…intellectual-property-(IP)`)。

    用 `\\(([^)]*)\\)` 的话会在第一个 `)` 上截断,把 `)` 连同半截 URL 留在正文里。
    """
    out = _feed("06-autographs-and-collectibles.md")
    assert "Intellectual Property" in out
    assert "(IP))" not in out and "marketplacelearn" not in out


def test_three_or_more_blank_lines_collapse_to_one():
    assert pf.render_feed_text("a\n\n\n\n\nb") == "a\n\nb"
    assert pf.render_feed_text("a\n\nb") == "a\n\nb"      # 2 个是官方段距,不动


def test_module_has_no_third_party_imports():
    """零第三方依赖(§十.3):这段将来要在 LLM 调用路径上跑,不许拖依赖。"""
    src = pathlib.Path(pf.__file__).read_text(encoding="utf-8")
    imports = set(re.findall(r"^(?:import|from)\s+([a-zA-Z_][\w.]*)", src, re.M))
    assert imports <= {"re"}, imports



# ── 内容族两页(43/44,2026-09-02 A 批)带来的两条新形态 ───────────────────

def test_images_are_dropped_entirely():
    """`![alt](url)` 整个删:alt 只是文件名,留下就是一行噪声(44 号页示意图)。"""
    line = "The table below lists requirements. ![2505_product-detail-page-Color.svg](https://x/y.svg) tail"
    assert pf._strip_links(line) == "The table below lists requirements.  tail"
    out = _feed("44-product-details-policy.md")
    assert ".svg" not in out and "https://" not in out
    assert "Making any promotional claims" in out          # 表格判据一条不少


def test_trailing_empty_table_row_does_not_block_the_list_transform():
    """44 号页「Product title」表末尾有一整行空单元格:不算数据行,表照样转清单。"""
    out = _feed("44-product-details-policy.md")
    assert "Writing brief titles not exceeding 150 characters." in out
    assert "|  |  |" not in out
    assert "| **Allowed** | **Prohibited** |" not in out   # 四张表全转成了清单
    lines = out.splitlines()
    i = lines.index("Prohibited:", lines.index("### Product title"))
    assert lines[i + 1].startswith("Writing in all caps")
