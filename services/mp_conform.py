"""MP_ITEM 载荷 spec 一致化流水线(逐函数移植自旧 auto_listing/mapper.py)。

**这是上架能不能过的关键一层,不是可选优化。** LLM 产出与 spec 的类型/必填/
枚举差异,沃尔玛一律以 DATA_ERROR 拒收;旧系统靠这条流水线在生产上架了几万个
产品,2026-08-09 首跑三条全拒就是因为本层未迁(只迁了强制覆盖/文案截断/图片)。

工序顺序即契约(旧 main.py 5.55→6,顺序错了会互相拆台):
  1 fill_known_required   条件必填(allOf if-then 不动点迭代)+ 实战白名单兜底
  2 fill_missing_required 顶层必填兜底(闭枚举优先 No/None/Not Applicable)
  3 fix_type_mismatches   类型不匹配(标量包成 array / object 子 enum 纠正)
  4 fix_invalid_enums     非法枚举值(必填换 enum[0],非必填直接删)
  4b ensure_variant_bag   变体三件套自洽(给一半会被拒;单品 isPrimary=Yes)
  5 strip_unknown         剔除 spec 外字段(additionalProperties=false 会拒)
  6 clean_state_restrictions  清 LLM 幻觉的 stateRestrictions
  7 drop_empty_optional   删非必填空值
  8 drop_min_items        删数组长度 < minItems 的非必填字段
  9 round_decimals        小数位 ≤ 2
  10 validate             必填校验——**不过就不提交**(省 UPC 与配额)

对应的沃尔玛错误码(注释即事故档案,别删):
  EXT_DATA_ERROR_50716566635066  类型不对(要 JSONArray / JSONObject / Number)
  EXT_DATA_ERROR_72600149546850  条件必填缺失
  EXT_DATA_ERROR_60670554076755  字段不在 spec 里(如 Orderable.productName)
  EXT_DATA_ERROR_74086950555651  必填且最小长度 1(如 color)
  EXT_DATA_ERROR_49505365506868  URL 数组被填了 'No' 之类占位
  EXT_DATA_ERROR_68050064665065  小数位 > 2
  EXT_DATA_ERROR_55506974520167  必填数组不足 minItems(如 keyFeatures ≥3)
  EXT_DATA_ERROR_05570905585050  变体三件套不完整
  IB.VALIDATION.DATA.001         object 子字段 enum 非法(如 unit='ft')
"""

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger("services.mp_conform")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _date_kind(name: str, prop: dict) -> str | None:
    """输入:字段名 + schema → 输出:'date'/'date-time'/None(非日期字段)。

    EXT_DATA_ERROR_00030257670757(第 5 轮,2026-08-12):releaseDate 被
    条件必填兜底填了 'Not Available' → "Enter a valid value in the format
    YYYY-MM-DD"。日期字段**绝不能拿普通字符串兜底**。识别两路:schema 的
    format=date/date-time,或字段名以 date 结尾(releaseDate 这类 spec
    往往不带 format 键);带 enum 的字段不算(那是枚举不是日期)。
    """
    if _type_of(prop) != "string" or _enum_of(prop):
        return None
    fmt = str(prop.get("format") or "").lower()
    if fmt in ("date", "date-time"):
        return fmt                  # spec 显式声明:严格按它
    if name.lower().endswith("date"):
        # 名字推断(spec 没写 format):**两种格式都算合法**——同一个错误码
        # 两个方向的实证:endDate 必须 DateTime(纯日期被拒),releaseDate
        # 要纯日期。只拦"根本不是日期"的垃圾值,兜底默认给纯日期
        return "any-date"
    return None


def _date_ok(kind: str, val: str) -> bool:
    if kind == "date":
        return bool(_DATE_RE.match(val))
    if kind == "date-time":
        return bool(_DATETIME_RE.match(val))
    return bool(_DATE_RE.match(val) or _DATETIME_RE.match(val))


def _date_default(kind: str) -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ") if kind == "date-time" \
        else now.strftime("%Y-%m-%d")

# 沃尔玛实际拒收、但 spec 顶层 required[] 没标的字段(conditional/allOf 链触发)。
# 来源:旧系统实战 EXT_DATA_ERROR_72600149546850 统计。字段名 → (类型, 兜底值)。
# 注:认证文档类(warrantyText/nrtl_information/prop65WarningText)**不进白名单**
# ——零认证强制已把触发字段设为 No,根本不会触发。
CONDITIONAL_REQUIRED_FALLBACK: dict[str, tuple[str, object]] = {
    "finish":             ("string",  "Polished"),
    "container_material": ("array",   ["Plastic"]),
    "leg_color":          ("string",  "Black"),
    "leg_material":       ("string",  "Metal"),
    "pump_type":          ("string",  "Hand Pump"),   # enum 因 PT 而异,后续修枚举兜底
    "powerType":          ("array",   ["Battery"]),   # 同上
    "batterySize":        ("string",  "AA"),
    "numberOfSheets":     ("integer", 1),
    "minimum_height":     ("object",  {"unit": "in", "measure": 0.1}),
    "maximum_height":     ("object",  {"unit": "in", "measure": 100.0}),
    "diameter":           ("object",  {"unit": "in", "measure": 1.0}),
    "core_diameter":      ("object",  {"unit": "mm", "measure": 1.0}),
}

_EMPTY = (None, "", [], {})
_URL_PLACEHOLDER = {"no", "none", "not applicable", "not available", "n/a", "yes"}
_URL_SCHEMES = ("http://", "https://", "ftp://")


def _props(spec: dict | None) -> dict:
    return ((spec or {}).get("properties") or {})


def _required(spec: dict | None) -> set:
    return set((spec or {}).get("required") or [])


def _type_of(prop: dict) -> str:
    return prop.get("type") or "string"


def _enum_of(prop: dict):
    """输入:字段 schema → 输出:枚举列表(array 取 items.enum——旧 field_summary 同义)。"""
    if _type_of(prop) == "array":
        items = prop.get("items") or {}
        return items.get("enum") or prop.get("enum")
    return prop.get("enum")


def _is_url_field(name: str, prop: dict) -> bool:
    fmt = (prop.get("items") or {}).get("format") or ""
    return fmt in ("uri", "url") or "url" in name.lower()


def safe_default_for(prop: dict):
    """输入:字段 schema → 输出:最保守的默认值(推不出返回 None)。

    优先级 'No' > 'None' > 'Not Applicable' > enum[0];object 递归填子字段。
    ⚠ URL 类数组一律返回 None——填 'No' 会被拒(EXT_DATA_ERROR_49505365506868),
    交给 drop_min_items 决定删不删。
    """
    ftype = _type_of(prop)
    if ftype == "array":
        items = prop.get("items") or {}
        if (items.get("format") or "") in ("uri", "url"):
            return None
    enum = _enum_of(prop)
    if isinstance(enum, list) and enum:
        if ftype == "array":
            for v in enum:      # smallPartsWarnings 之类:选 "0 - No warning applicable"
                s = str(v).lower()
                if "no" in s and ("applicable" in s or "warning" in s):
                    return [v]
            # 2026-08-12 旧仓对照补回:旧 _safe_default_for 对所有类型先跑
            # No/None/Not Applicable 优选。此前 array 直接 [enum[0]]——
            # ["Yes","No"] 这类"是否有 X"字段兜底方向反了(填 Yes 又级联
            # 触发 allOf 条件必填)
            for safe in ("No", "None", "Not Applicable"):
                if safe in enum:
                    return [safe]
            return [enum[0]]
        for safe in ("No", "None", "Not Applicable", "Unbranded"):
            if safe in enum:
                return safe
        return enum[0]
    if ftype == "object":
        sub = prop.get("properties") or prop.get("object_properties") or {}
        if not sub:
            return None
        obj = {}
        for sname, sdef in sub.items():
            if not isinstance(sdef, dict):
                continue
            v = safe_default_for({"type": sdef.get("type", "string"),
                                  "enum": sdef.get("enum")})
            if v is not None:
                obj[sname] = v
            else:
                t = sdef.get("type")
                obj[sname] = 1 if t in ("number", "integer") else (
                    "" if t == "string" else ([] if t == "array" else None))
        # 实证特例:netContent 与通用 measure/unit 尺寸
        if "productNetContentUnit" in obj and not obj["productNetContentUnit"]:
            obj["productNetContentUnit"] = "Each"
        if "productNetContentMeasure" in obj and not obj["productNetContentMeasure"]:
            obj["productNetContentMeasure"] = 1
        if "measure" in obj and not obj["measure"]:
            obj["measure"] = 1
        if "unit" in obj and not obj["unit"]:
            u_enum = (sub.get("unit") or {}).get("enum") or []
            obj["unit"] = u_enum[0] if u_enum else "in"
        return obj or None
    return None


def _type_fallback(ftype: str):
    """输入:类型 → 输出:按类型的最后兜底值。"""
    if ftype == "string":
        return "Not Available"
    if ftype in ("number", "integer"):
        return 1
    if ftype == "array":
        return ["Not Available"]
    if ftype == "object":
        return {}
    return None


def evaluate_if(if_clause: dict, visible: dict) -> bool:
    """输入:JSON Schema if 子句 + visible → 输出:条件是否成立。

    支持 spec 实际用到的子集:required / properties.X.{enum,type,contains.enum}。
    """
    for r in if_clause.get("required", []):
        if visible.get(r) in _EMPTY:
            return False
    for fname, fcond in (if_clause.get("properties") or {}).items():
        v = visible.get(fname)
        enum = fcond.get("enum")
        if enum is not None and v not in enum:
            return False
        ftype = fcond.get("type")
        if ftype == "string" and not isinstance(v, str):
            return False
        if ftype == "array" and not isinstance(v, list):
            return False
        if ftype == "object" and not isinstance(v, dict):
            return False
        if ftype in ("number", "integer") and not isinstance(v, (int, float)):
            return False
        contains = fcond.get("contains") or {}
        if contains:
            if not isinstance(v, list):
                return False
            sub_enum = contains.get("enum")
            if sub_enum and not any(x in sub_enum for x in v):
                return False
    return True


def resolve_conditional_required(spec: dict, visible: dict) -> set:
    """输入:PT spec + visible → 输出:当前取值触发的额外必填字段集合。"""
    extra = set()
    for cond in (spec or {}).get("allOf") or []:
        if "if" not in cond or "then" not in cond:
            continue
        if evaluate_if(cond["if"], visible):
            extra.update((cond.get("then") or {}).get("required") or [])
    return extra


def fill_known_required(spec: dict, visible: dict) -> tuple[dict, list[str]]:
    """输入:spec + visible → 输出:(补齐条件必填后的 visible, 说明)。

    **不动点迭代**(旧实证):兜底填入的值本身会触发新的 allOf 条件必填
    (powerType=['Battery'] 之后才触发 displayTechnology),单遍解析会漏掉级联。
    只填空字段、从不清空 → 单调递增必然收敛;6 轮上限纯防御。
    """
    props = _props(spec)
    visible = dict(visible)
    notes: list[str] = []
    for _ in range(6):
        round_notes: list[str] = []
        for fname in resolve_conditional_required(spec, visible):
            prop = props.get(fname)
            if prop is None or visible.get(fname) not in _EMPTY:
                continue
            kind = _date_kind(fname, prop)
            default = _date_default(kind) if kind else safe_default_for(prop)
            if default is None:
                default = _type_fallback(_type_of(prop))
            if default is not None:
                visible[fname] = default
                round_notes.append(f"{fname}={default!r}(条件触发)")
        for fname, (ftype, default) in CONDITIONAL_REQUIRED_FALLBACK.items():
            prop = props.get(fname)
            if prop is None or visible.get(fname) not in _EMPTY:
                continue
            spec_type = _type_of(prop) or ftype
            if spec_type != ftype:      # 白名单类型与该 PT spec 不符 → 按 spec 转
                if spec_type == "string":
                    default = str(default) if not isinstance(default, (list, dict)) else ""
                elif spec_type == "array":
                    default = default if isinstance(default, list) else [str(default)]
                elif spec_type == "object":
                    default = default if isinstance(default, dict) else {}
            visible[fname] = default
            round_notes.append(f"{fname}={default!r}(白名单)")
        if not round_notes:
            break
        notes.extend(round_notes)
    return visible, notes


def fill_missing_required(spec: dict, visible: dict) -> tuple[dict, list[str]]:
    """输入:spec + visible → 输出:(补齐顶层必填后的 visible, 说明)。"""
    props = _props(spec)
    visible = dict(visible)
    notes = []
    for name in sorted(_required(spec)):
        if visible.get(name) not in _EMPTY:
            continue
        prop = props.get(name) or {}
        kind = _date_kind(name, prop)
        default = _date_default(kind) if kind else safe_default_for(prop)
        if default is None:
            default = _type_fallback(_type_of(prop))
        if default is None:
            continue
        # minItems 补齐:只从 enum 取更多值(EXT_DATA_ERROR_55506974520167)。
        # 自由文本数组不拿占位灌数——凑出来的假卖点毫无意义,
        # 宁可让 validate 报出来、由人看到"这个产品资料太薄不该上"。
        mi = prop.get("minItems")
        if mi and isinstance(default, list) and len(default) < mi:
            enum = _enum_of(prop)
            if isinstance(enum, list):
                for v in enum:
                    if len(default) >= mi:
                        break
                    if v not in default:
                        default.append(v)
        visible[name] = default
        notes.append(f"{name}={default!r}")
    return visible, notes


def fix_type_mismatches(spec: dict, visible: dict) -> tuple[dict, list[str]]:
    """输入:spec + visible → 输出:(类型对齐后的 visible, 说明)。

    防 EXT_DATA_ERROR_50716566635066(要 JSONArray/JSONObject)、
    EXT_DATA_ERROR_49505365506868(URL 数组填占位)、IB.VALIDATION.DATA.001。
    """
    props = _props(spec)
    required = _required(spec)
    visible = dict(visible)
    fixes = []
    for name, val in list(visible.items()):
        prop = props.get(name) or {}
        ftype = _type_of(prop) if prop else None

        # 日期字段格式硬闸(第 5 轮 EXT_DATA_ERROR_00030257670757):
        # 'Not Available'/'No' 这类字符串塞进日期字段直达沃尔玛必拒。
        # 必填→换合法默认;非必填→删(垃圾日期没有保留价值)
        kind = _date_kind(name, prop) if prop else None
        if kind and isinstance(val, str) and not _date_ok(kind, val):
            if name in required:
                visible[name] = _date_default(kind)
                fixes.append(f"{name}: {val!r} 非法日期→{visible[name]}")
            else:
                del visible[name]
                fixes.append(f"{name}: {val!r} 非法日期,删除")
            continue

        if ftype == "array" and not isinstance(val, list):
            if val in (None, ""):
                del visible[name]
                fixes.append(f"{name}: 空值删除(spec 要 array)")
            elif isinstance(val, str):
                if _is_url_field(name, prop) and not val.lower().startswith(_URL_SCHEMES):
                    del visible[name]
                    fixes.append(f"{name}: 非 URL 字符串删除")
                elif val.strip().lower() in _URL_PLACEHOLDER:
                    del visible[name]
                    fixes.append(f"{name}: 占位字符串 {val!r} 删除")
                else:
                    visible[name] = [val]
                    fixes.append(f"{name}: 字符串包成 array")
            else:
                visible[name] = [val]
                fixes.append(f"{name}: 标量包成 array")

        elif ftype == "array" and isinstance(val, list) and _is_url_field(name, prop):
            clean = [v for v in val
                     if isinstance(v, str) and v.lower().startswith(_URL_SCHEMES)]
            if len(clean) != len(val):
                if clean:
                    visible[name] = clean
                    fixes.append(f"{name}: 清掉 {len(val) - len(clean)} 个非 URL 元素")
                else:
                    del visible[name]
                    fixes.append(f"{name}: URL 数组全是占位,整字段删除")
                    continue

        if ftype == "object" and isinstance(val, dict):
            sub = prop.get("properties") or {}
            for sname, sdef in sub.items():
                if not isinstance(sdef, dict):
                    continue
                sub_enum = sdef.get("enum")
                sval = val.get(sname)
                if (isinstance(sub_enum, list) and sub_enum and sval is not None
                        and sval not in sub_enum):
                    repl = safe_default_for({"type": sdef.get("type", "string"),
                                             "enum": sub_enum})
                    if repl is None or repl not in sub_enum:
                        repl = sub_enum[0]
                    val[sname] = repl
                    fixes.append(f"{name}.{sname}: {sval!r}→{repl!r}")
        elif ftype == "object" and val is not None and not isinstance(val, dict):
            # 标量喂给 object 字段(assembledProductWeight=5 这类):按 spec 重建
            rebuilt = safe_default_for(prop) or {}
            if isinstance(rebuilt, dict) and "measure" in rebuilt and \
                    isinstance(val, (int, float)):
                rebuilt["measure"] = val
            if rebuilt:
                visible[name] = rebuilt
                fixes.append(f"{name}: 标量 {val!r} 重建为 object {rebuilt!r}")
            else:
                del visible[name]
                fixes.append(f"{name}: 标量无法转 object,删除")

        elif ftype in ("string", "number", "integer") and isinstance(val, list):
            # 反向:spec 要标量却给了数组(LLM 过度套用"数组字段包数组"规则)。
            # EXT_DATA_ERROR_50716566635066 "'Color' … Enter a 'String'"
            items = [x for x in val if x not in (None, "")]
            if not items:
                del visible[name]
                fixes.append(f"{name}: 空数组删除(spec 要 {ftype})")
            else:
                visible[name] = items[0]
                fixes.append(f"{name}: 数组取首元素 {items[0]!r}(spec 要 {ftype})")
                if ftype in ("number", "integer") and isinstance(items[0], str):
                    val = items[0]      # 落到下面的字符串转数字分支
                else:
                    continue

        if ftype in ("number", "integer") and isinstance(val, str):
            try:
                num = float(val.replace(",", "").strip())
                visible[name] = int(num) if ftype == "integer" else num
                fixes.append(f"{name}: 字符串 {val!r} 转 {ftype}")
            except ValueError:
                del visible[name]
                fixes.append(f"{name}: {val!r} 非数字,删除")
    return visible, fixes


def fix_invalid_enums(spec: dict, visible: dict) -> tuple[dict, list[str]]:
    """输入:spec + visible → 输出:(枚举合法化后的 visible, 说明)。

    必填字段换成安全默认/enum[0];非必填直接删(防 IB.VALIDATION.DATA.001)。
    """
    props = _props(spec)
    required = _required(spec)
    visible = dict(visible)
    fixed = []
    for name, val in list(visible.items()):
        prop = props.get(name) or {}
        enum = _enum_of(prop)
        if not (isinstance(enum, list) and enum):
            continue
        ftype = _type_of(prop)
        if ftype == "string" and val not in enum:
            repl = safe_default_for(prop)
            if repl and repl in enum:
                visible[name] = repl
                fixed.append(f"{name}: {val!r}→{repl!r}")
            elif name in required:
                visible[name] = enum[0]
                fixed.append(f"{name}: {val!r}→{enum[0]!r}(enum[0])")
            else:
                del visible[name]
                fixed.append(f"{name}: {val!r}→删(非必填)")
        elif ftype == "array" and isinstance(val, list):
            cleaned = [v for v in val if v in enum]
            if len(cleaned) == len(val):
                continue
            if cleaned:
                visible[name] = cleaned
                fixed.append(f"{name}: 删非法元素")
            elif name in required:
                safe = safe_default_for(prop)
                visible[name] = safe if isinstance(safe, list) else [enum[0]]
                fixed.append(f"{name}: 全非法,回退 {visible[name]!r}")
            else:
                del visible[name]
                fixed.append(f"{name}: 全非法且非必填,删")
    return visible, fixed


_VARIANT_BAG = ("variantGroupId", "variantAttributeNames", "isPrimaryVariant")


def _apply_variant_plan(props: dict, in_spec: list, visible: dict,
                        plan: dict) -> tuple[dict, list[str]]:
    """输入:spec 属性 + 三件套在册列表 + visible + 变体决策 → 输出:(visible, 说明)。

    ⚠ 三件套必须**同时**落进 visible:spec 若只登记了其中一两个(各 PT 不同),
    我们就只发登记的那些——发 spec 没有的字段会被 additionalProperties=false
    整条拒(EXT_DATA_ERROR_60670554076755)。
    ⚠ 属性名要用本 PT 的枚举复核:决策是拿枚举算的,但 spec 可能换版,
    不复核就会发出 PT 不认的属性名。复核不过 → 整套剔除退单品,不猜。
    """
    name = plan.get("attr_name")
    enum = _enum_of(props.get("variantAttributeNames", {}))
    if enum and name not in {str(e) for e in enum}:
        for k in in_spec:
            visible.pop(k, None)
        return visible, [f"变体属性名 {name} 不在本 PT 枚举内,整套剔除退单品"]
    notes = []
    if "variantGroupId" in props:
        visible["variantGroupId"] = str(plan["group_id"])[:300]
    if "variantAttributeNames" in props:
        visible["variantAttributeNames"] = [name]
    if "isPrimaryVariant" in props:
        yn = _enum_of(props["isPrimaryVariant"]) or ["Yes", "No"]
        want = "Yes" if plan.get("is_primary") else "No"
        visible["isPrimaryVariant"] = want if want in yn else yn[0]
    # 维度取值写进同名属性:组内没有差异值 = 沃尔玛看不出这几个有什么不同。
    # 只在 spec 登记了该属性时写(同上,发 spec 没有的字段整条被拒)。
    if name in props and plan.get("attr_value"):
        visible[name] = plan["attr_value"]
    else:
        notes.append(f"⚠ 本 PT 无 {name} 属性,组内差异值无处可写")
    notes.append(f"变体组 {plan['group_id']} 按 {name}={plan.get('attr_value')}"
                 f"(家族 {plan.get('family_size')},"
                 f"{'主' if plan.get('is_primary') else '非主'}变体)")
    return visible, notes


def ensure_variant_bag(spec: dict, visible: dict, sku: str = "",
                       plan: dict | None = None) -> tuple[dict, list[str]]:
    """输入:spec + visible + SKU + 变体决策 → 输出:(三件套自洽的 visible, 说明)。

    EXT_DATA_ERROR_05570905585050 "Variant metadata bag is missing or empty":
    三件套(变体组 ID / 变体属性名 / 是否主变体)要么都给要么都不给,
    给一半会被拒——我们此前必填兜底填了 variantAttributeNames 却没配套。

    `plan` = services.variant_group.plan() 的产出(2026-08-15 接线):
    - `mode='variant'` → **真变体口径**:按决策写三件套,并把维度取值写进同名
      属性(组内没有差异值 = 沃尔玛看不出这几个有什么不同)。
      ⚠ 枚举用本 PT 的 variantAttributeNames 复核一次:决策是拿枚举算的,
      但 spec 可能换版,不复核就会发出 PT 不认的属性名。
    - `mode='single'` / 不给 plan → **单品口径**(下方原逻辑,一字未改)。

    单品口径:非必填时半套整套剔除(旧系统单品从不发变体字段);spec 列为必填时
    才用 SKU 占位补全——沃尔玛报错原文即指引:"If you only have 1 item in a
    variant group, select 'Yes' in Is Primary Variant"。
    """
    props = _props(spec)
    in_spec = [k for k in _VARIANT_BAG if k in props]
    if not in_spec:
        return visible, []
    if plan and plan.get("mode") == "variant":
        return _apply_variant_plan(props, in_spec, dict(visible), plan)
    required = _required(spec) & set(in_spec)
    present = [k for k in in_spec if visible.get(k) not in _EMPTY]
    if not required:
        # 单品口径修正(2026-08-12 旧仓对照):旧系统单品**从不发**变体字段
        # (inject_variant_fields 只对真变体组注入,SKU 占位组 ID 无实证)。
        # 非必填时:三件俱全 = 真变体注入 → 放行;半套/LLM 零星幻觉 →
        # 整套剔除(半套必拒 05570905585050,凑全反而是无实证行为)
        if present and len(present) < len(in_spec):
            visible = dict(visible)
            for k in present:
                visible.pop(k, None)
            return visible, [f"变体字段不完整({','.join(present)}),"
                             f"单品口径整套剔除"]
        return visible, []
    # spec 把三件套列为必填 → 必须补全(第 4 轮实证 PT:沃尔玛报错原文
    # "If you only have 1 item in a variant group, select 'Yes' in Is
    # Primary Variant";组 ID 用 SKU 占位)
    notes = []
    if "variantGroupId" in props and visible.get("variantGroupId") in _EMPTY:
        if not sku:
            # 没有 SKU 就凑不出稳定的组 ID:整套拆掉比给半套安全
            for k in in_spec:
                visible.pop(k, None)
            return visible, ["变体三件套缺组 ID,整套移除(给一半会被拒)"]
        visible["variantGroupId"] = str(sku)[:300]
        notes.append(f"variantGroupId={sku}(单品占位)")
    if "variantAttributeNames" in props and \
            visible.get("variantAttributeNames") in _EMPTY:
        enum = _enum_of(props["variantAttributeNames"])
        # 优先挑我们真有值的属性,保证"说有 color 就真有 color"
        pick = next((n for n in (enum or []) if visible.get(n) not in _EMPTY),
                    (enum or [None])[0])
        if pick:
            visible["variantAttributeNames"] = [pick]
            notes.append(f"variantAttributeNames=[{pick}]")
    if "isPrimaryVariant" in props and visible.get("isPrimaryVariant") in _EMPTY:
        enum = _enum_of(props["isPrimaryVariant"]) or ["Yes", "No"]
        visible["isPrimaryVariant"] = "Yes" if "Yes" in enum else enum[0]
        notes.append("isPrimaryVariant=Yes(单品即主变体)")
    return visible, notes


def strip_unknown(spec: dict, ospec: dict, visible: dict, orderable: dict
                  ) -> tuple[dict, dict, list[str]]:
    """输入:两 spec + 两段 → 输出:(裁剪后 visible, orderable, 说明)。

    spec 的 additionalProperties=false:多一个字段整条被拒
    (EXT_DATA_ERROR_60670554076755,如 Orderable.productName)。
    """
    vkeys, okeys = set(_props(spec)), set(_props(ospec))
    dropped: list[str] = []
    nv: dict = {}
    for k, v in visible.items():
        if k in vkeys:
            nv[k] = v
        else:
            dropped.append(f"visible.{k}")
    no: dict = {}
    for k, v in orderable.items():
        if k in okeys:
            no[k] = v
        else:
            dropped.append(f"orderable.{k}")
    return nv, no, dropped


def clean_state_restrictions(orderable: dict) -> tuple[dict, list[str]]:
    """输入:orderable → 输出:(清理后的 orderable, 说明)。

    stateRestrictions 一旦存在,zipCodes 即必填;LLM 常幻觉出 reason='None'
    却带空 zipCodes 的条目,整条会被拒。
    """
    sr = orderable.get("stateRestrictions")
    if not isinstance(sr, list) or not sr:
        return orderable, []
    kept = [e for e in sr if isinstance(e, dict)
            and str(e.get("stateRestrictionsText") or e.get("reason") or "").strip()
            not in ("", "None", "none")
            and e.get("zipCodes") not in (None, "", [])]
    if len(kept) == len(sr):
        return orderable, []
    orderable = dict(orderable)
    if kept:
        orderable["stateRestrictions"] = kept
        return orderable, [f"stateRestrictions 清掉 {len(sr) - len(kept)} 条无效条目"]
    orderable.pop("stateRestrictions", None)
    return orderable, ["stateRestrictions 全无效,整字段删除"]


def drop_empty_optional(spec: dict, ospec: dict, visible: dict, orderable: dict
                        ) -> tuple[dict, dict, list[str]]:
    """输入:两 spec + 两段 → 输出:删掉非必填空值后的两段 + 说明。

    必填的空值**不删**——留给 validate 报错,让上游知道哪里漏了。
    """
    vreq, oreq = _required(spec), _required(ospec)
    dropped: list[str] = []

    def _keep(payload: dict, req: set, prefix: str) -> dict:
        out = {}
        for k, v in payload.items():
            if v in _EMPTY and k not in req:
                dropped.append(f"{prefix}.{k}")
                continue
            out[k] = v
        return out

    return (_keep(visible, vreq, "visible"),
            _keep(orderable, oreq, "orderable"), dropped)


def drop_min_items(spec: dict, ospec: dict, visible: dict, orderable: dict
                   ) -> tuple[dict, dict, list[str]]:
    """输入:两 spec + 两段 → 输出:删掉数组长度 < minItems 的非必填字段 + 说明。

    典型:productSecondaryImageURL minItems=5,不足 5 张宁可不发。
    必填字段不删,留给 validate 报错。
    """
    def _check(payload: dict, sp: dict, prefix: str):
        req, props = _required(sp), _props(sp)
        out, notes = {}, []
        for k, v in payload.items():
            prop = props.get(k) or {}
            mi = prop.get("minItems")
            if (_type_of(prop) == "array" and isinstance(v, list) and mi
                    and len(v) < mi and k not in req):
                notes.append(f"{prefix}.{k}({len(v)} < minItems {mi})")
                continue
            out[k] = v
        return out, notes

    nv, d1 = _check(visible, spec, "visible")
    no, d2 = _check(orderable, ospec, "orderable")
    return nv, no, d1 + d2


def round_decimals(visible: dict, orderable: dict) -> tuple[dict, dict, list[str]]:
    """输入:两段 → 输出:小数位 ≤ 2 的两段 + 说明(防 EXT_DATA_ERROR_68050064665065)。"""
    fixes: list[str] = []

    def _walk(d, prefix=""):
        if not isinstance(d, dict):
            return d
        out = {}
        for k, v in d.items():
            if isinstance(v, bool):
                out[k] = v
            elif isinstance(v, float):
                r = round(v, 2)
                if r != v:
                    fixes.append(f"{prefix}{k}={v}→{r}")
                out[k] = r
            elif isinstance(v, dict):
                out[k] = _walk(v, f"{prefix}{k}.")
            elif isinstance(v, list):
                out[k] = [_walk(x, f"{prefix}{k}[].") if isinstance(x, dict)
                          else (round(x, 2) if isinstance(x, float)
                                and not isinstance(x, bool) else x)
                          for x in v]
            else:
                out[k] = v
        return out

    return _walk(visible, "visible."), _walk(orderable, "orderable."), fixes


def validate(spec: dict, ospec: dict, visible: dict, orderable: dict
             ) -> list[str]:
    """输入:两 spec + 两段 → 输出:不合格的必填字段列表(空 = 通过)。

    两类:①必填为空 ②**必填数组不足 minItems**——后者是
    EXT_DATA_ERROR_55506974520167(如 keyFeatures 要 ≥3 条),
    只查"非空"会让它一路蒙混到沃尔玛才被拒。
    """
    bad = []

    def _check(payload: dict, sp: dict, prefix: str):
        props = _props(sp)
        for name in sorted(_required(sp)):
            v = payload.get(name)
            if v in (None, "", []):
                bad.append(f"{prefix}.{name}")
                continue
            mi = (props.get(name) or {}).get("minItems")
            if mi and isinstance(v, list) and len(v) < mi:
                bad.append(f"{prefix}.{name}(需≥{mi}条,现{len(v)}条)")

    _check(visible, spec, "visible")
    _check(orderable, ospec, "orderable")
    return bad


def conform(spec: dict | None, ospec: dict | None, visible: dict,
            orderable: dict, sku: str = ""
            ) -> tuple[dict, dict, list[str], list[str]]:
    """输入:PT spec + Orderable spec + 两段 → 输出:(visible, orderable, 说明, 缺失)。

    十道工序按旧 main.py 的顺序执行;缺失非空 = **不该提交**(调用方回收 UPC)。
    spec 缺失时只做不依赖 spec 的工序(小数位),并原样返回。
    """
    notes: list[str] = []
    if not spec:
        visible, orderable, r = round_decimals(visible, orderable)
        return visible, orderable, r, ["spec 缺失,无法校验"]
    ospec = ospec or {}

    visible, n = fill_known_required(spec, visible);        notes += n
    visible, n = fill_missing_required(spec, visible);      notes += n
    visible, n = fix_type_mismatches(spec, visible);        notes += n
    visible, n = fix_invalid_enums(spec, visible);          notes += n
    if _props(ospec):
        # Orderable 段同样过条件必填/类型/枚举一致化(2026-08-12 旧仓对照:
        # 旧系统 Orderable 由 LLM 按 spec 填,新系统交还 LLM 后这层同样要兜;
        # 此前 Orderable 完全不过一致化,LLM 一个非法枚举直达沃尔玛)
        orderable, n = fill_known_required(ospec, orderable)
        notes += [f"orderable:{x}" for x in n]
        orderable, n = fix_type_mismatches(ospec, orderable)
        notes += [f"orderable:{x}" for x in n]
        orderable, n = fix_invalid_enums(ospec, orderable)
        notes += [f"orderable:{x}" for x in n]
    visible, n = ensure_variant_bag(spec, visible, sku);    notes += n
    if _props(ospec):
        visible, orderable, n = strip_unknown(spec, ospec, visible, orderable)
    else:
        # Orderable spec 缺失保护(2026-08-12 旧仓对照):此前 ospec 为空时
        # strip_unknown 会把**整个 Orderable 清空**——_orderable.json 没就位
        # 就是必拒。改为只裁 visible,orderable 原样保留并记日志
        logger.warning("Orderable spec 缺失(_orderable.json 未就位?),"
                       "跳过 Orderable 段裁剪与校验")
        visible, orderable, n = strip_unknown(
            spec, {"properties": {k: {} for k in orderable}},
            visible, orderable)
    notes += n
    orderable, n = clean_state_restrictions(orderable);     notes += n
    visible, orderable, n = drop_empty_optional(spec, ospec, visible, orderable)
    notes += n
    visible, orderable, n = drop_min_items(spec, ospec, visible, orderable)
    notes += n
    visible, orderable, n = round_decimals(visible, orderable)
    notes += n
    return visible, orderable, notes, validate(spec, ospec, visible, orderable)
