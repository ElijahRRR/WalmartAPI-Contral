"""品牌组画像回归(`alloc_plan._brand_profile`)。

这是 §11.3 #5/#6 的**判据**,不是装饰:少数派件数决定"整组归店保排他"这件事
值不值得做。所以每条都盯着"这个数会不会被算小"——算小了就等于告诉所有者
没问题。
"""

from workflows import alloc_plan as wf


def _c(asin, cat, brand="acme", score=90.0, ch="FBA"):
    return {"asin": asin, "brand": brand, "manufacturer": None, "pt": "pt1",
            "category": cat, "channel": ch, "score": score, "base": score,
            "bonus": 0.0, "penalty": 0.0, "why": "", "missing": [],
            "sales": None, "rating": "4.5", "reviews": "10", "lead": 3}


def _g(brand, items, category=None, store=None):
    """组大类默认取**首件**的 —— 传 category 才覆盖。画像不许自己重算组大类。"""
    g = {"key": brand or "(无品牌):x", "brand": brand,
         "score": max(i["score"] for i in items), "size": len(items),
         "category": category or items[0]["category"],
         "channel": items[0]["channel"], "items": items}
    if store:
        g["store"] = store
    return g


def _line(lines, needle):
    hit = [x for x in lines if needle in x]
    assert hit, f"报告里找不到「{needle}」:\n" + "\n".join(lines)
    return hit[0]


# ── 少数派件数:两个口径 ──────────────────────────────────────────────

def test_minority_counted_under_both_taxonomies():
    """26 类拆开、五品类合上的组:两个数必须不一样,否则报一个就够了。

    Furniture 与 Home 都归 Home 品类 —— 26 类口径下 2 件是少数派,
    五品类口径下 0 件。这正是 §11.6 说的"折到上层救回约 5 万件"。
    """
    grp = _g("acme", [_c("B01", "Home"), _c("B02", "Home"),
                      _c("B03", "Furniture"), _c("B04", "Furniture")],
             category="Home")
    lines, _ = wf._brand_profile([grp], [])
    row = _line(lines, "少数派件数")
    assert "26 类口径 2 件" in row
    assert "五品类口径 0 件" in row


def test_minority_survives_when_the_brand_really_spans_two_supers():
    """跨真·两个品类的组(Home vs Hardlines)两个口径都救不回 —— 商标农场。"""
    grp = _g("pro bamboo kitchen",
             [_c("B01", "Home"), _c("B02", "Home"),
              _c("B03", "Home Improvement")], category="Home")
    lines, _ = wf._brand_profile([grp], [])
    row = _line(lines, "少数派件数")
    assert "26 类口径 1 件" in row and "五品类口径 1 件" in row


def test_unmapped_categories_get_their_own_bucket_not_folded_in():
    """`super_category` 的 None 是口径不是漏填。

    并进任何一个品类,都会把"只能给没有确定类目的店"算成"某一家专收"。
    这里 2 件 Everything Else 必须在五品类口径下**也算少数派**。
    """
    grp = _g("acme", [_c("B01", "Home"), _c("B02", "Home"), _c("B03", "Home"),
                      _c("B04", "Everything Else"),
                      _c("B05", "Safety & Emergency")], category="Home")
    lines, rows = wf._brand_profile([grp], [])
    assert "五品类口径 2 件" in _line(lines, "少数派件数")
    assert rows[0][6] == "Home"          # 组大类(五品类)仍是 Home,没被 None 顶掉


def test_all_unmapped_group_majors_to_the_sentinel_not_to_none():
    """整组都不归五品类时,组大类(五品类)是那个显式桶,不是 None/空。"""
    grp = _g("acme", [_c("B01", "Everything Else"),
                      _c("B02", "Safety & Emergency")],
             category="Everything Else")
    _, rows = wf._brand_profile([grp], [])
    assert rows and rows[0][6] == wf.NOT_SUPER


# ── 谁进画像 ──────────────────────────────────────────────────────────

def test_unbranded_solo_groups_are_counted_apart_from_real_brands():
    """无品牌是每 ASIN 一组,混进"真品牌组"里会让组数虚高几十万。"""
    real = _g("acme", [_c("B01", "Home"), _c("B02", "Furniture")],
              category="Home")
    solo = _g(None, [_c("B09", "Home", brand=None)])
    lines, rows = wf._brand_profile([real, solo], [])
    row = _line(lines, "真品牌组")
    assert "真品牌组 1 组 / 2 件" in row
    assert "无品牌单品组 1 组" in row
    assert len(rows) == 1


def test_directed_groups_are_profiled_too_and_labelled():
    """定向流也要量。它后面会按件剪掉少数派,**剪完再量等于量自己的处置结果**
    —— 那个数永远是 0,而所有者要看的正是剪掉了多少。
    """
    grp = _g("acme", [_c("B01", "Home"), _c("B02", "Home Improvement")],
             category="Home", store="A店")
    lines, rows = wf._brand_profile([], [grp])
    assert "真品牌组 1 组 / 2 件" in _line(lines, "真品牌组")
    assert rows[0][0] == "定向流" and rows[0][1] == "A店"


# ── csv ───────────────────────────────────────────────────────────────

def test_csv_holds_only_groups_with_a_minority():
    """没有少数派的组在这张表上没有任何要处置的东西。"""
    clean = _g("clean", [_c("B01", "Home"), _c("B02", "Home")])
    dirty = _g("dirty", [_c("B03", "Home"), _c("B04", "Home Improvement")],
               category="Home")
    _, rows = wf._brand_profile([clean, dirty], [])
    assert [r[2] for r in rows] == ["dirty"]


def test_csv_sorted_by_five_super_minority_first():
    """排序键是**五品类少数派** —— 26 类那个会把"其实同一个品类"的组排到前面,
    而那些组正是折到上层就没事的,不该占着人眼的第一屏。
    """
    spans_super = _g("farm", [_c("B01", "Home"), _c("B02", "Home"),
                              _c("B03", "Home Improvement"),
                              _c("B04", "Office")], category="Home")
    same_super = _g("stencil", [_c("B05", "Home"),
                                _c("B06", "Arts & Crafts"),
                                _c("B07", "Arts & Crafts"),
                                _c("B08", "Furniture")], category="Home")
    _, rows = wf._brand_profile([same_super, spans_super], [])
    assert [r[2] for r in rows] == ["farm", "stencil"]
    assert rows[0][10] == 2 and rows[1][10] == 0      # 少数派件(五品类)


def test_csv_writes_header_and_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    grp = _g("acme", [_c("B01", "Home"), _c("B02", "Home Improvement")],
             category="Home")
    _, rows = wf._brand_profile([grp], [])
    path, n = wf._write_brands(rows)
    assert n == 1
    body = (tmp_path / "alloc_品牌组.csv").read_text(encoding="utf-8-sig")
    assert body.splitlines()[0].startswith("流别,去向店,品牌")
    assert "acme" in body and "Home×1 | Home Improvement×1" in body


# ── 组大类的出处 ──────────────────────────────────────────────────────

def test_group_major_is_taken_from_build_not_recomputed():
    """画像与发牌必须看到**同一个**组大类。这里故意给一个与多数派不符的
    `category`(并列时 `alloc_groups._major` 会按名字定序选 Furniture),
    画像要照用传进来的那个,而不是自己再算一遍。
    """
    grp = _g("acme", [_c("B01", "Home"), _c("B02", "Furniture")],
             category="Furniture")
    _, rows = wf._brand_profile([grp], [])
    assert rows[0][5] == "Furniture"
    assert rows[0][9] == 1                 # 少数派件(26类):那一件 Home


# ── 规模与跨度分布 ────────────────────────────────────────────────────

def test_size_buckets_report_items_not_just_groups():
    """一张大牌打不出去卡住的是**一整块**货 —— 只报组数会让 100 件的组
    看起来跟 1 件的一样轻。"""
    big = _g("big", [_c(f"B{i:04d}", "Home") for i in range(30)])
    small = _g("small", [_c("B9999", "Home")])
    lines, _ = wf._brand_profile([big, small], [])
    assert "30" in _line(lines, "21–100 件")
    assert "1 件" in _line(lines, "1 件")


def test_span_bucket_caps_at_four_plus():
    cats = ["Home", "Furniture", "Office", "Automotive", "Beauty", "Toys"]
    grp = _g("wide", [_c(f"B{i:04d}", c) for i, c in enumerate(cats)],
             category="Home")
    lines, rows = wf._brand_profile([grp], [])
    assert rows[0][7] == 6                 # 跨 26 类数照实写
    _line(lines, "4 类以上")                # 桶封顶在 4
