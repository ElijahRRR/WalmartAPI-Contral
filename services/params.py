"""工作流 `-p k=v` 参数的解析积木(cli 只管切成字符串,语义在这里)。

为什么在 services:workflows 不准 import cli(铁律 1 的层次是
cli → workflows → services → api → registry),而 registry 是接线盒、
不收参数语义。

⚠ **本模块只收黑名单形态**(2026-08-27 建):
    '0' / 'false' / 'no' 为假,**其余一律为真**。
仓里另有一种白名单形态(`str(v).lower() in {'1','true','yes'}`,如
sku_normalize 的 apply、alloc_backfill 的 include_ties),两者对
`-p apply=y` 这类输入给出**相反**的答案 —— 合成一个函数就是把两种语义
混成一个开关。要迁哪个站点先看它现在是哪种,别按名字猜。

⚠ 接线时**按名字导入**:`from services.params import flag`。每个工作流的
入口都是 `def run(params: dict)`,`from services import params` 在 run() 体内
必被那个形参遮住,`params.flag(...)` 当场 AttributeError。
"""


def flag(params: dict, key: str, default: bool = False) -> bool:
    """输入:params + 键名 + 缺省 → 输出:布尔(黑名单语义)。

    与全仓 33 个黑名单站点**逐字等价**(2026-08-27 审计计数):
        str(params.get(key, '1' if default else '0')).lower()
            not in {'0', 'false', 'no'}

    ⚠ 迁移时只迁**本来就带 `.lower()`** 的站点。不带 `.lower()` 的那一处
    黑名单写法(daily_report 的 `push`)迁过来会从大小写敏感变成不敏感 ——
    那是行为变更,得单独定夺,不许顺手一起改。同样不带 `.lower()` 的
    catalog_sync(`skip_inventory`/`item_ids`/`skip_feishu`/`strict`)、
    order_history_import / kpi_history_import 的 `apply`、
    order_center_push 的 `reconcile`、list_new 的 `check_spec` 都是**白名单**
    形态,本函数根本不该碰。
    """
    return str(params.get(key, "1" if default else "0")).lower() \
        not in {"0", "false", "no"}
