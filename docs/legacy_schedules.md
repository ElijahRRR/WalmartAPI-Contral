# 旧系统调度权威清单(停旧依据;2026-08-12 旧仓全量普查底稿)

> plan.md 回滚预案要求"删除旧调度前先归档"——本文件即归档底稿。
> ⚠ 这是**仓库静态普查**结果;正式停旧前必须在生产 Mac 取证核对:
> `launchctl list | grep -iE 'autolisting|walmart|erp'` + 导出 hermes(AI skill
> 平台)注册表,以实际 loaded 状态为准。backlog:131 判"新旧并跑可能正在发生"。

## A. AI Skill 调度链(14 条,注册在外部 hermes 平台;权威表 = 旧仓 定时任务skill/README.md:52-71)

| 时间 | skill | 跑什么 | 新系统对应 | 停旧动作 |
|---|---|---|---|---|
| 05:00 | tro-daily-scrape | tro-scraper-matrix 5 源采集→merge→飞书 | 无(跨仓,backlog 第五节 🔴 边界未定) | **保留**,不属本仓 |
| 06:00 | trademark-daily-update | 商标数据/daily_update.py(USPTO→PG) | 无(同上) | **保留** |
| 06:02 | daily-tro-pipeline | 商标数据/run_cron.sh pipeline | 无(同上) | **保留** |
| 周一 06:15 | weekly-brand-refresh | 商标数据/run_cron.sh refresh | 无(同上) | **保留** |
| 07:05 | sync-blacklist-brands-daily | 新审核系统 sync.* + 重启 10 worker | risk_sync 只覆盖飞书两表镜像,Phase0 字典/worker 重启无对应 | **保留至边界拍板**(跨仓) |
| 07:30 | erp-online-products-track | reconcile → sync_online_products → sync_status_track | catalog_sync + feed_poll 反哺 + heal_unknown | **停**(与 launchd 上架 5 条同停) |
| 08:00 | walmart-kpi-daily | 店铺日报三脚本(含影刀) | daily_report + perf_problems | **停**(停之前严禁开新系统 yingdao=1) |
| 08:02 | walmart-returns-daily-sync | fetch_walmart_returns.py --all | returns_sync | **停** |
| 12:00 | walmart-maintenance-all-stores | 商品维护三段式 | maintenance + feed_poll | **停**(先收干净旧在途 feed) |
| 13:30 | walmart-daily-order-sync | 订单同步.py | order_sync + order_audit | **停**(连同 launchd :15 双调度一起) |
| 14:00 | walmart-kpi-afternoon | fetch_walmart_performance --no-yingdao | — | **直接停**(实证参数 bug 从未成功写入) |
| 14:02 | dedup-sync-online-products | dedup_sync_to_server 全量 14 万 ASIN → erp_listing_server cache | 无——但那是 erp_worker(旧上架)的去重命脉 | **与旧上架栈同停**;只要 erp_worker 还在跑就不能单停它 |
| 15:00 | walmart-daily-retire | daily_retire_orchestrator.py | product_clear | **停** |
| 0/6/12/18:04 | walmart-daily-cleanup | daily_cleanup.py | problem_product_cleanup | **停**(⚠ 调度器至今没定位,取证时重点找) |

## B. launchd plist(旧仓内 6 个定时 + 20 常驻)

| 时间 | Label | 跑什么 | 新系统对应 | 停旧动作 |
|---|---|---|---|---|
| 06:00 | com.user.autolisting.morning | scheduler full_morning(含**无人值守上架**) | list_new 链 | **停**(backlog:131 判最大风险,优先取证) |
| 每时:05 | com.user.autolisting.store_status_hourly | dedup_sync --store-status-only | 无(同 A 表 14:02 一条链) | **与旧上架栈同停** |
| 每时:15 | com.user.autolisting.reconcile_hourly | scheduler reconcile_due | feed_poll | **停** |
| 每时:15 | com.user.walmart.order_audit_hourly | 订单同步.py 全店 | order_sync | **停**(双调度另一半) |
| 8/12/16/20 | com.user.autolisting.health_4x | scheduler health_report(只读) | 无(cli.py health 待办) | **可暂留**(只读低危)或随栈停 |
| 23:30 | com.user.autolisting.retire_daily | scheduler retire_locked | sku_locked_heal | **停** |
| 常驻×20 | com.nextderboy.erp_worker.{1..20} | 长轮询 erp_listing_server 跑上架 | 前端栈不迁 | 上架切换时**必须停**(否则双写上架表) |

## C. 其它

- crontab:旧仓内无文件;唯一书面建议 erp-core audit_from_lark_sheet"每 15 分钟"
  ——取证时确认是否真注册。
- **erp-core Celery beat(11 条,含 5 条写沃尔玛)**:所有者 2026-08-05 已确认
  **未启用、不在迁移范围**(plan.md Phase 0)。真跑副本在 ~/Projects/erp服务/
  erp-core(本仓是快照)——切换时顺手 `ps aux | grep celery` 复核一次即可。
- 旧「审核服务.py」(DMIT systemd :8901):自称 2026-06-20 退出主流程,
  取证时确认是否仍在监听。

## D. 停旧顺序(上架域切换那天)

1. 生产 Mac 取证:`launchctl list` + hermes 注册表 → 与本文件对账,补漏;
2. 停 A 表 07:30 + B 表上架 5 条 + erp_worker×20 + A 表 14:02/每时:05(dedup 链);
3. 收干净旧在途:旧 pending_feeds 全部 reconcile 到终态;
4. 起新调度(顺序:upc_sync/catalog_sync → product_refresh → product_ingest →
   maintenance → list_new;feed_poll 每 30 分钟);
5. 其余域(kpi/订单/维护/清理/下架)各自按 A 表"停旧动作"逐条切,互不阻塞。
