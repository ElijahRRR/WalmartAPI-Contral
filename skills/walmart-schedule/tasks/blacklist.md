# 沃尔玛定时任务:blacklist(每天 02:30,台北时间)

在 `/Users/nextderboy/Projects/WalmartAPI-Contral` 下执行这一行,**原样执行,不要改任何参数**:

```bash
/Users/nextderboy/Projects/WalmartAPI-Contral/.venv/bin/python3 /Users/nextderboy/Projects/WalmartAPI-Contral/cli.py risk_sync blacklist_push
```

这条链跑的是:risk_sync → blacklist_push。

## 这条链在做什么

| 步 | 工作流 | 这一步干什么 |
|---|---|---|
| 1 | `risk_sync` | 风控/黑名单中心四表镜像入库(只读飞书,非危险)。 |
| 2 | `blacklist_push` | PG 黑名单自产行 → 飞书两张收集表(投影,**整表重写**)。 |

**顺序是硬约束**:前一步不成功就不跑后面的,整条链只发一条飞书通知。

备注:黑名单双向同步,排在当天所有上架/审核之前

## 跑完怎么判

看**退出码**,不要靠读输出猜:

- `0` 成功 —— **什么都不用做**。成功/失败飞书都会自己发通知,你再报一遍就是刷屏。
- `3` 上一轮还在跑(没抢到锁)—— **不是失败,不要重试**。下一个整点它自己会再来一次。连着两次 3 才值得说一声。
- `1` 失败 —— 见下。

## 失败了怎么办

1. 取日志末尾(工作流名 = 飞书通知里第一个 ❌ 的那一步):

```bash
cd /Users/nextderboy/Projects/WalmartAPI-Contral && tail -n 60 "$(/Users/nextderboy/Projects/WalmartAPI-Contral/.venv/bin/python3 -c 'from registry import paths; print(paths.logs_dir())')/<工作流名>.log"
```

2. 把**失败的那一步 + 日志最后那几行报错**发给苏里,一次说清。
3. **不要自动重跑。** 这条链会写沃尔玛/写库,重跑的代价可能是重复提交;而且失败原因多半是外部的(凭证过期、代理不通、飞书表被改),重跑一遍还是那样。

## 绝对不许做的三件事

1. **不许加 `--dry-run`。** 缺省就是真跑;加了它每天空转,而且报成功 —— 这是最难发现的一种坏法(真跑至少留痕迹)。
2. **不许改工作流名、参数或顺序。** 要改就改 `registry/schedule.py` 再重新生成这份技能包,不要在提示词里手改 —— 两处不一致时没有任何东西会报错。
3. **不许并发跑第二条。** 同一条链撞上了后到的那条直接退 3 空跑一轮。
