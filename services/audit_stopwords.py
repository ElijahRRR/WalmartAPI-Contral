"""R4/R5 通用词过滤 stopwords(逐字迁自旧仓 pipelines/_stopwords.py)。

目的: blacklist_brands 和 USPTO LIVE 里混入了大量单词通用词 TRO/商标
  (top/finish/amazon/kids/black/metal 等)
按 word-boundary 子串扫, 任何产品都会命中一堆, L3 LLM 会被噪音淹没。

过滤规则 (从强到弱):
  1. 长度 <= 3 字符 (us/or/pc/12/mm 等, 真品牌极少这么短)
  2. 纯数字 (23, 12, 360, 310)
  3. 含数字字符的杂词 (12oz, 1pc, 4x4, 2 x 2, 50 years)
  4. 在 STOPWORDS_COMMON 集合里的通用英文词

对多词短语 (如 "ninja foodi", "strawberry shortcake"), 保留 - 这些通常是真品牌。

移植说明:
  - STOPWORDS_COMMON 的多行字符串**整块逐字符照抄**, 含源文本里的重复词
    (amazon x3 / premium x2 / reach x2 / solid x2)与带标点的 token
    (indoor/outdoor、usb-a、usb-c、ac/dc、amazon.com)。frozenset 自动去重,
    任何"手工整理"都会改变命中集合, 严禁清理。
  - 旧仓 :141 的 _NUMERIC_MIXED_RE 定义后从无引用(死变量), 不迁;
    它的意图由单词分支里的 any(c.isalpha()) 实现。
  - 纯函数, 不碰 DB / 文件 / 网络。

public:
  is_stopword(token, *, min_len=4) -> bool
  STOPWORDS_COMMON
"""

from __future__ import annotations

import re

# 通用英文词 / Amazon 页面噪音 / 产品属性词 / 常见单位
# 初版 ~400 条, 覆盖 100 条样本观察到的命中噪音
STOPWORDS_COMMON: frozenset[str] = frozenset(
    w.lower() for w in """
    the and or not no yes but for from to with of by at in on off up down out re
    all any some each every one two three four five six seven eight nine ten
    our us we you they them their his her my your its it is are was were be been
    this that these those here there where when how why what who which
    if so as also just only still more less most least much many few
    very really quite rather even same other another both either neither
    top best good great nice fine excellent premium classic basic standard special
    unique genuine real original true authentic perfect amazing awesome wonderful
    free easy simple hard strong weak heavy light big small large tiny huge mini jumbo
    high low wide narrow thick thin long short tall round square flat smooth rough
    new old fresh clean dirty dry wet warm cool cold hot bright dark deep shallow
    open closed full empty ready available online offline direct indirect
    fast quick slow steady sturdy durable flexible solid firm soft rigid
    safe safety secure protected reliable trusted healthy natural organic eco friendly
    extra plus max maximum min minimum super ultra mega giant
    made create design custom crafted handmade handcrafted
    black white red blue green yellow pink purple orange brown gray grey silver gold
    clear transparent matte glossy shiny metallic vibrant pastel neon

    kit pack set box item bundle lot case cover frame holder stand board mat
    bag bags bottle cup tool system unit device machine accessory part parts piece pieces
    product item gift items supplies supply kits

    amazon amazoncom amz walmart ebay etsy aliexpress
    kids men women boys girls adult adults baby family people child children teen
    care love like want need help hope

    feature features include includes including designed designer perfect fits fit
    compatible compatibility combine combined wrap wrapping include including
    description specifications specification overview details content contents warranty
    quality usage instructions instruction manual guide image images picture pictures
    package packaging company brand brands company name sku model

    pc pcs pack piece set lb lbs oz kg g mg l ml cm mm m inch inches ft feet yard
    gallon gallons gross net weight diameter size sizes length width height depth
    volume capacity dimension dimensions area level levels
    voltage volt volts amp amps watt watts hertz hz rpm w kw mw va amps

    resistance load response daily weekly monthly yearly
    star stars 360 270 180 90 45 12 24 36 72 100 200 300 400 500 600 700 800 900 1000
    1st 2nd 3rd 4th 5th

    advanced professional industrial commercial residential
    interior exterior indoor outdoor universal portable stationary
    automatic manual electric hydraulic mechanical analog digital smart wireless wired

    for from the with of by and or on in off up down out
    built designed recommended suggested certified approved authorized registered licensed
    complete completed full all in one

    season seasonal year years month months day days week weeks hour hours minute seconds

    store storage stores display displays retail wholesale
    home house kitchen bathroom bedroom living office garage yard garden lawn patio outdoor
    indoor indoor/outdoor

    fiber fibers material materials metal steel aluminum copper brass iron wood plastic
    rubber silicone glass fabric cotton leather vinyl wool polyester nylon
    metal tubing metal frame wire cable rope cord
    thread threads bolt bolts screw screws nut nuts washer

    range accent solid mark shine finish vivid stunning bright
    ball balls base spring grip groove edge point pointer pointing

    surface surfaces structure frame frames
    ac dc ac/dc usb-a usb-c usb
    instant quickly quick fast faster

    barb cm mm kit amazon amazon.com pc range premium amazon

    companion friend friends
    reach reach
    solid premium
    go going

    click clicks clicking account accounts card cards trying try tried
    cordless corded above below next previous first last middle center
    mode modes button buttons switch switches action function functional
    precision precise accurate accuracy
    marine automotive aviation aerospace
    focus focused focusing attention attentive
    carbide carbon steel
    furniture feet furniture leg furniture legs
    screen screens display displays monitor monitors
    flow flows flowing water air oil gas liquid
    valve valves ball grip groove
    handle handles knob knobs trigger triggers
    pitch pitched yard yards rate rates
    mesh net netted strand strands twist twisted
    power powered powering voltage volt volts watt watts
    plug plugs port ports jack jacks pin pins
    wire wires wiring cable cables cord cords wire cutter
    fuse fuses circuit circuits
    element elements core cores ring rings
    barb barbs barbed valve stem stems
    leaf leaves rubber steel vinyl
    rack racks shelf shelves hook hooks

    fast faster fastest slower slowest
    deep deeper deepest
    smooth smoother rough rougher
    cleaner cleanest dirty dirtier
    convenient convenience
    strong stronger strongest weak weaker weakest
    tough tougher toughest durable
    lift lifts lifted lifting stand stands standing stood
    ready readily complete completion completed
    effective efficient efficiency effectiveness
    resistance resistances resistant
    phenomenon moment
    """.split()
)

_NUMBER_RE = re.compile(r"^\d+$")


def is_stopword(token: str, *, min_len: int = 4) -> bool:
    """输入:一个词或多词短语 → 输出:True 表示该 token 是通用词应被过滤掉。

    小写后判断:
      - 单词: 去**尾部**非字母数字后, 长度 < min_len / 纯数字 / 无字母 /
        命中 STOPWORDS_COMMON → True
      - 多词短语 (e.g. "ninja foodi"): 只要有 >=1 个非 stopword 非纯数字且
        长度 >= min_len 的词就保留 (返回 False), 通常是真品牌

    min_len 是 keyword-only 参数, 旧仓所有调用方都用默认值 4。
    """
    if not token:
        return True
    t = token.strip().lower()
    if not t:
        return True

    # 空格分词, 去标点
    words = [w for w in re.split(r"\s+", t) if w]
    if not words:
        return True

    # 多词短语: 若主要词 (非 stopword) >= 1 个, 保留
    if len(words) >= 2:
        significant = [w for w in words if w not in STOPWORDS_COMMON and not _NUMBER_RE.match(w)]
        if len(significant) >= 1:
            # 任一 significant 词长度 >= min_len 即保留
            if any(len(w) >= min_len for w in significant):
                return False
            return True
        return True

    # 单词情况
    w = words[0]
    # 去掉尾部标点
    w_clean = re.sub(r"[^a-z0-9]+$", "", w)
    if not w_clean:
        return True
    # 长度过短
    if len(w_clean) < min_len:
        return True
    # 纯数字
    if _NUMBER_RE.match(w_clean):
        return True
    # 纯非字母 (如 "4x4")
    if not any(c.isalpha() for c in w_clean):
        return True
    # stopword 集合
    if w_clean in STOPWORDS_COMMON:
        return True
    return False


__all__ = ["is_stopword", "STOPWORDS_COMMON"]
