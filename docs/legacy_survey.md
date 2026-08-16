# 旧仓库 erpAPI 全量摸底报告

> 由执行 AI 于 2026-08-04 对旧仓库(commit d5237fb, shallow clone)组织 14 路并行代码通读产出,
> 作为 `docs/legacy_reference.md` 的展开与证据补充。每条结论均带 `文件:行号` 证据位置。
> 迁移某条工作流前,先读本文档对应小节,再读旧代码对应位置。

## 目录

- [walmart_client.py — 沃尔玛共享客户端(移植原型)](#walmart_client)
- [lark_io/ — 飞书读写封装](#lark_io)
- [scraper_client + 结算/商品拉取脚本](#scraper_client)
- [产品ID查询产品详情 → product_query (#1)](#product_query)
- [售后订单同步 → returns_sync (#2)](#returns_sync)
- [沃尔玛店铺日报 → daily_report (#3)](#daily_report)
- [沃尔玛订单审核 → order_audit (#4)](#order_audit)
- [沃尔玛UPC生成器 → upc_generator (#5)](#upc_generator)
- [沃尔玛商品维护 → maintenance (#6, 危险)](#maintenance)
- [沃尔玛批量下架 → daily_retire (#7, 危险)](#daily_retire)
- [沃尔玛问题商品清理 → daily_cleanup (#8, 危险)](#daily_cleanup)
- [auto_listing + match_listing + sync_online_products → listing / catalog_sync (#9/#10)](#catalog_listing)
- [调度全景(定时任务skill + launchd)与 tools/ 救场脚本](#scheduling_tools)
- [官方 specs / PT 模板 / 类目映射产物](#specs_refdata)
- [完整性审查结论(critic)](#critic)


<a id="walmart_client"></a>
## walmart_client.py — 沃尔玛共享客户端(移植原型)

### 模块职责

旧仓库唯一的沃尔玛 Marketplace API 公共客户端(/workspace/erpapi/walmart_client.py，497 行)。职责四块:(1) load_stores() 从仓库根目录 店铺API.xlsx Sheet1 读店铺凭证 + 每店固定出口代理，拼成 proxy URL;(2) get_token() 按 client_id 做进程内 token 缓存(提前 60s 过期)+ 线程安全双检锁;(3) 按 proxy URL 维度池化 httpx.Client(HTTP/2 + keepalive + 连接级 retries=2)，并在 TransportError 时剔除半死连接;(4) safe_get / safe_post / safe_get_ex / safe_post_ex / safe_put_ex 五个请求封装，统一返回 (status, headers, data)，永不抛异常、永不 raise_for_status，内建 401 自愈 + opt-in 的 429/5xx 退避。全仓 30+ 个脚本 import 它。它本身不含任何业务判断，是新仓库 api/_client.py 的直接移植原型。

### 入口与触发

- /workspace/erpapi/walmart_client.py — 纯库，无 __main__、无 argparse、无调度，只被 import
- 公共函数(移植时须逐个保留语义):load_stores(filter_names=None) 行143;get_token(client_id, client_secret, proxy) 行213;make_headers(token, client_id) 行262;safe_get_ex(url, token, client_id, proxy, params, timeout=30, quiet=False, max_retries=0) 行441;safe_post_ex 行458;safe_put_ex 行465;safe_get 行472;safe_post 行485;另导出常量 BASE_URL 行60
- 私有实现:_get_client(proxy) 行82 连接池;_invalidate_client(proxy) 行111;_close_all_clients() atexit 行128;_parse_retry_after(headers) 行274;_request_ex(method, ...) 行313 是所有 safe_* 的唯一内核
- 典型调用者:auto_listing/feed_submit.py:24、沃尔玛商品维护/walmart_maintenance_common.py:34、沃尔玛店铺日报/fetch_walmart_performance.py:49、售后订单同步/fetch_walmart_returns.py:21、fetch_walmart_settlement.py:22、沃尔玛订单审核/沃尔玛异步.py:30

### 调度

none —— walmart_client.py 本身不含任何调度。它被 launchd 定时任务拉起的业务脚本 import(如 沃尔玛店铺日报、auto_listing/main.py、daily_retire_orchestrator.py 等)。注意其模块级副作用(socket.setdefaulttimeout(90)、atexit 注册、h2 探测告警)在 import 时即生效。

### 数据存储

- /workspace/erpapi/店铺API.xlsx — 唯一的店铺凭证与代理源。路径硬编码为脚本同目录(walmart_client.py:58-59 SCRIPT_DIR / XLSX_PATH)。Sheet1 按中文表头索引(walmart_client.py:165-182):店铺 / ClientId / ClientSecret / 代理类型 / IP地址或域名 / 端口 / IP登录账号 / IP登录密码。该文件已进 git，内含明文 ClientSecret 与 socks5 代理账号密码(实测可解出真实凭证行，如 81*** / A085***)。
- 进程内内存态，无落盘:_token_cache(walmart_client.py:51，client_id → {token, expires_at, secret, proxy});_client_pool(walmart_client.py:55，proxy URL → httpx.Client)。两者仅进程生命周期有效，退出即丢，不跨进程共享。
- 无数据库、无 JSON 状态文件、无日志文件。日志走 logging.getLogger('walmart_client')(行45)，默认挂 NullHandler;同时 _request_ex 内大量 print() 直接打 stdout，由 quiet= 参数控制。

### 飞书使用

- walmart_client.py 本身完全不碰飞书，无 app_token / table_id / 字段名。飞书写回全部在调用方(lark_io/、auto_listing/feishu_io.py、各业务脚本内硬编码 LARK_TOKEN，如 沃尔玛店铺日报/fetch_walmart_performance.py:79 LARK_TOKEN = 'CRfC…kb(token已脱敏,见旧仓库代码)')。移植 api/_client.py 时不要把任何飞书逻辑带进来。

### 沃尔玛端点

- 客户端自身只固定一个端点:POST {BASE_URL}/v3/token(walmart_client.py:233)，grant_type=client_credentials + Basic 认证 + 五个 WM_* 头。
- make_headers 生成的标准头(walmart_client.py:264-271):Authorization: Bearer、WM_SVC.NAME='Walmart Marketplace'、WM_QOS.CORRELATION_ID=uuid4(每请求新生成)、WM_CONSUMER.ID=client_id、WM_SEC.ACCESS_TOKEN=token、Accept=application/json。注意同时下发 Authorization 与 WM_SEC.ACCESS_TOKEN 两个 token 头，且不设 Content-Type(靠 httpx json= 自动加)。
- 经由本客户端实际调用的端点(全仓 f"{BASE_URL}/..." 统计):POST /v3/feeds(15 处，feedType 走 query 参数)、GET /v3/items 及 /v3/items/{sku} /v3/items/count /v3/items/spec /v3/items/walmart/search /v3/items/catalog/search、GET /v3/feeds/{feedId}(+ includeDetails=true)、GET /v3/feeds/{feedId}/errorReport、PUT /v3/price、PUT /v3/inventory、GET /v3/inventories、GET /v3/orders(cursor 翻页)、GET /v3/returns、GET /v3/settings/partnerprofile、GET /v3/report/payment/statement、GET /v3/report/reconreport/availableReconFiles、GET /v3/report/reconreport/reconFileJson。
- 经由本客户端提交的 feed 类型:MP_ITEM(auto_listing/feed_submit.py:231-266，json_body 方式)、MP_MAINTENANCE 与价格/库存类(沃尔玛商品维护/walmart_maintenance_common.py:374-392 post_feed 走 safe_post_ex，max_retries=3, timeout=120, quiet=True)。
- 绕过共享客户端的直连(全部仍手动传了 proxy=store['proxy']，所以未违反 IP 隔离，但丢了连接池 / 401 自愈 / 退避):沃尔玛批量下架/retire_walmart_items.py:246 与 沃尔玛批量下架/daily_retire_orchestrator.py:619 提交 DELETE_ITEM(httpx.post content=bytes, timeout=120);沃尔玛问题商品清理/daily_cleanup.py:386 DELETE_ITEM、:414 RETIRE_ITEM(timeout=60);沃尔玛问题商品清理/relisting.py:134 MP_MAINTENANCE(timeout=120)。这四处绕过的真实原因是要用 content= 传预先 json.dumps 的 bytes 并自设 Content-Type，而 _request_ex 只支持 json=(walmart_client.py:346)。
- 二进制/非 JSON 响应导致的绕过:沃尔玛店铺日报/fetch_walmart_problem_orders.py:151(xlsx 绩效报告，注释 :141-142 明写"safe_get_ex 强制解析 JSON 会失败"，并在 :131-176 复制了一份 429/5xx 退避逻辑);auto_listing/check_feed.py:41 GET /v3/feeds/{id}/errorReport 返回 CSV bytes(注释明写"用 raw httpx");fetch_walmart_settlement.py:390/402/420 三处 report 端点用 httpx.get + raise_for_status(此处是历史遗留，响应本是 JSON，可直接换回 safe_get_ex)。
- async 缺口导致的绕过:沃尔玛订单审核/沃尔玛异步.py:54 每店独立 httpx.AsyncClient(proxy=..., timeout=60) 并发拉 /v3/orders，只复用 walmart_client 的 get_token(用 asyncio.to_thread 包同步调用) 与 make_headers、BASE_URL。walmart_client 无任何 async 接口。
- 另一套完整分叉:erp-core/backend/app/services/walmart_client.py 是重写版，加了 WalmartResponse 数据类、submit_feed()/get_feed_status()/get_feed_error_report()/list_feeds()、GCRA 限流、以及关键的"无 proxy 直接拒绝"硬保护(行78-79、行130-135)。其行 297-299 记录了一条重要结论:POST /v3/feeds 必须用 JSON body 直传，multipart 实测经常返 200 但 body 为空。

### 魔数与踩坑参数

- socket.setdefaulttimeout(90) — walmart_client.py:43，模块级全局副作用。注释写明 2026-05-12 事故:走 socks5 时 httpx 自身 timeout 不生效，一次 daily noon reconcile 卡在 _ssl__SSLSocket_read.poll() 2.5 小时。这是进程级兜底，import 即污染整个进程的所有 socket(含飞书/采集器调用)。移植时必须保留该保护，但要意识到它是全局副作用。
- token 提前过期 60 秒 — walmart_client.py:251 (expires_at = now + expires_in - 60);expires_in 缺省值 900 — 行248。
- 连接池 limits — walmart_client.py:101:max_keepalive_connections=20, max_connections=50，写在 HTTPTransport 上(行88 注释:httpx.Client 传了自定义 transport 会忽略顶层 limits，这是踩过的坑)。
- transport retries=2 — walmart_client.py:99，仅覆盖连接级失败(DNS/TCP/TLS 握手)，与 max_retries 是两层，会相乘放大实际请求次数。
- 客户端级 timeout — walmart_client.py:105:httpx.Timeout(30.0, connect=15.0);但每次 request 又传 timeout=timeout(行349)，per-request 覆盖客户端默认，safe_* 默认 timeout=30。
- _DEFAULT_RETRIES — walmart_client.py:75，env WALMART_DEFAULT_RETRIES 默认 2。只作用于旧接口 safe_get(行481);safe_post 恒为 0(行495-496 无 max_retries)，注释行489-493 明确原因:POST 非幂等，自动重试会导致 feed 重复/refund 重复/shipping 重复。
- _parse_retry_after — walmart_client.py:274-310:优先级 Retry-After > X-Next-Replenishment-Time > 兜底 60.0s;所有等待上限 300s(行284、297、305);X-Next-Replenishment-Time 同时兼容 epoch ms(>1e12 则除 1000)与 ISO8601(Z→+00:00)。
- 5xx 与网络异常退避 — min(2 ** attempt, 10) 秒，即 1/2/4/8/10 封顶(walmart_client.py:360、407)。429 不用指数，用 _parse_retry_after 精确等待(行398)。
- 401 自愈上限 1 次 — walmart_client.py:339 refreshed_401 标志，行375-393;且独立于 max_retries，永远生效。
- 非 2xx 时只有 POST/PUT 会截 body 前 200 字符打印(walmart_client.py:430)，GET 失败不打 body — 排障时 GET 4xx 看不到原因。
- HTTP/2 开关 — walmart_client.py:63-71:h2 装了就开，可用 WALMART_HTTP2=0 关。注意 HTTP/2 + SOCKS5 组合要求 httpx[socks](socksio)与 h2 都装上，仓库无 requirements.txt，属于隐式依赖。
- BASE_URL 可被 env WALMART_BASE_URL 覆盖(walmart_client.py:60)，沙箱用。
- 端口列 float 陷阱 — walmart_client.py:177-180:pandas 把数字端口读成 float，.bak 版直接 str() 会得到 '40000.0' 把代理 URL 弄坏;现版本 isinstance(float) → int() 修复，NaN 判为 '0' 从而跳过该店。
- 代理账号密码必须 URL 编码 — walmart_client.py:189-192 quote(safe='')，.bak 版直接拼接(bak:85)，密码含 @ : / # 会破坏代理 URL(表内实测存在 'C008u' 这类账号，未来改密码即可能踩)。
- load_stores 过滤规则:ClientId 空或 '0' 跳过(行170);代理类型/IP/端口 任一为 '0' 跳过(行184) — 即无代理的店铺根本不会进入列表，这是唯一的直连防线，靠数据兜底而非代码断言。
- proto 白名单 socks5/http/https，其余一律降级为 http(行187) — 表里写错代理类型不会报错，会静默按 http 连。

### 防重/幂等语义

客户端层本身只提供两条幂等相关保证，真正的防重语义全在业务层。(1) safe_post 默认 max_retries=0(walmart_client.py:495)，docstring 行489-493 明确写死\"POST 非幂等，自动重试可能导致 feed 重复 / refund 重复 / shipping 重复\";safe_post_ex/safe_put_ex 的 max_retries 默认也是 0，只有调用方显式传才开。(2) 401 自愈最多刷新一次 token 后重试同一请求 —— 注意这对 POST 同样生效且不受 max_retries 控制(walmart_client.py:375-387)，理论上存在 401 场景下 POST 被重发一次的风险，这是移植时要显式确认的边界。业务层的防重范例是 auto_listing/feed_submit.py:132-266:重试前先 GET /v3/feeds?limit=20&feedType=MP_ITEM 反查，按 (itemCount + 时间窗) 匹配，三态 FOUND / NOT_FOUND / UNKNOWN(行39-48)，FOUND 直接当成功返回不再重发，UNKNOWN 保留\"已领\"状态交给 sync_status_track 自愈;4xx(非 408/429) 判定为数据错，绝不重试。新系统 api 层应保持\"POST 默认不自动重试\"，把反查 + pending 落库放到 services/workflows。

### 危险操作

- 本模块自身无 dry-run 概念，是纯传输层 —— 它会忠实发出任何传给它的请求，包括 DELETE_ITEM / RETIRE_ITEM / 清库存 feed。危险性完全由调用方把关(如 auto_listing/feed_submit.py:227-228 的 dry_run 早退)。新仓库应把 dry-run 强制放在 cli.py + workflow 层，api/_client.py 不必也不应内建。
- 最大安全缺口:get_token 与 _get_client 都接受 proxy=None 并会正常直连(walmart_client.py:82 key = proxy or '' ; 行231 无任何断言)。旧系统仅靠 load_stores 过滤掉无代理店铺(行184)来保证不直连 —— 一旦有人手动构造 store dict 或 xlsx 填错，就会用真实店铺凭证裸连沃尔玛，触发店铺关联封号。erp-core 分叉版已把这条补成硬拒绝(erp-core/backend/app/services/walmart_client.py:78-79 抛 ValueError、行130-135 返回 REFUSE)。新仓库 api/_client.py 必须默认 proxy 必填、缺失即抛异常。
- 客户端层完全没有速率限制。x-current-token-count 在 walmart_client.py 里一次都没出现;所有配额控制都在外部 auto_listing/rate_limiter.py(滑动窗口 + 响应头自适应:acquire 行46-72、update_from_response 行76-99，仅当 token-count==0 且有 next-replenishment 时才设 _next_avail)。端点配额表在 auto_listing/rate_limiter.py:122-134:PUT /v3/price 100/3600、PUT /v3/inventory 200/60、POST /v3/feeds?MP_ITEM 10/3600、MP_MAINTENANCE 10/3600、PRICE_AND_PROMOTION 6/86400、feeds?inventory 50/3600、POST /v3/items/spec 3/60。注意:任何忘记 limiter.acquire 的调用路径(所有直连绕过点都忘了)都会裸奔撞限流。
- DELETE_ITEM feed 有 payload 大小硬门槛:沃尔玛批量下架/daily_retire_orchestrator.py:611-613 单批 >100_000 字节直接拒绝并分批(split_rows_by_feed_size)。这条保护在客户端外部，移植时别丢。

### 事故教训与必须保留的行为

- 2026-05-12 事故记录在案(walmart_client.py:40-43):走 socks5 代理时 httpx 的 timeout 不可靠，一次 daily noon reconcile 卡在 SSL read 2.5 小时不返回。修复手段是模块级 socket.setdefaulttimeout(90)。这是必须照搬的经验值，不要因为"全局副作用不优雅"而删掉。
- 半死连接自愈(walmart_client.py:111-125 注释)记录了三种真实故障:keep-alive 被代理静默 RST、SOCKS 隧道挂掉但 socket 仍存活、HTTP/2 共享连接进入 GOAWAY 后所有 stream 都失败。对应处理是捕获 TransportError/ProxyError 后主动把该 proxy 的 Client 踢出池(行351-353)。注意分支划分很讲究:只有网络层异常才动连接池，其他异常(JSON 序列化错等)不动(行363-369)。
- httpx.Client 传了自定义 transport 后会忽略顶层 limits —— 所以 limits 必须写在 HTTPTransport 上(walmart_client.py:88 注释)。这是踩过的坑，移植时容易写回错的位置。
- _client_pool 与 _token_cache 都不是 fork-safe:多个脚本用 ThreadPoolExecutor(线程安全，OK)，但若将来用 multiprocessing fork，子进程会继承已建立的 socket 导致串扰。旧代码全部是线程池，没踩到;新系统若引入进程池必须在子进程里清池。
- POST /v3/feeds 必须用 JSON body 直传，不要用 multipart —— multipart 实测经常返 200 但 body 为空导致 json_parse_error(erp-core/backend/app/services/walmart_client.py:297-299 记录)。
- 2xx 但响应体缺 feedId 的情况真实发生过(auto_listing/feed_submit.py:276-277 注释"罕见但发生过")，处理是进反查而不是重发。
- 401 自愈依赖 _token_cache 里存的 secret(walmart_client.py:52 注释、行376-378):如果缓存被外部清空或从未写入(例如调用方自己拿 token 后进程重启)，secret 为 None 就无法自愈，直接返回 401。移植时建议改成由调用方持有凭证或让 client 持有 store 对象，别依赖缓存副作用。
- 没有任何单元测试覆盖 walmart_client:/workspace/erpapi/tests/ 下只有 test_lark_io.py。全部行为规格只存在于代码与注释里 —— 移植时应边搬边补测试(可 mock httpx.MockTransport)。
- GET 请求失败时不打印响应体(walmart_client.py:430 只对 POST/PUT 截 200 字符)，线上排查 GET 4xx 曾只能看到状态码。新实现建议一律带上截断后的 body。
- 仓库无 requirements.txt / pyproject.toml，httpx[socks](socksio)、h2、pandas、openpyxl 都是隐式依赖;h2 缺失时只是 logger.warning 后静默回落 H1.1(walmart_client.py:70-71)，容易在新环境无声降级。

### 切换时必须迁移的状态

- 店铺API.xlsx 的全部内容(店铺名、ClientId、ClientSecret、代理类型/IP/端口/账号/密码)必须迁到 registry + <DATA_ROOT>/.env 或 PG 的 store 表;这是全系统唯一凭证与代理映射源。迁移后必须把该 xlsx 从新仓库 git 中彻底排除，且旧仓库的凭证应视为已泄露(明文进过 git)，建议随迁移轮换 ClientSecret 与代理密码。
- 店铺名 → 出口代理的绑定关系(每店固定出口 IP)是账号安全生死线，必须原样搬过去，不能重新分配。
- token 缓存与连接池是纯内存态，无需迁移;但要注意新旧系统并跑时是两套独立 token 缓存，同一 client_id 会各自换 token(Walmart 侧允许多 token 并存，不构成阻塞)。

### 迁移建议

建议整体照搬到 api/_client.py，但做四处强化。照搬(几乎逐行)：socket.setdefaulttimeout(90) 全局兜底及其事故注释；按 proxy 维度池化 httpx.Client + HTTPTransport(retries=2, http2, limits 写在 transport 上)；_invalidate_client 的半死连接自愈与异常分类(仅 TransportError/ProxyError 动连接池)；token 缓存提前 60s 过期 + 双检锁；make_headers 的六个头(含每请求新 uuid4 的 WM_QOS.CORRELATION_ID 与同时下发 Authorization/WM_SEC.ACCESS_TOKEN)；_parse_retry_after 的优先级与 300s 上限、epoch-ms/ISO8601 双解析；429 精确等待 + 5xx 指数退避 min(2**n,10)；\"永不抛异常、返回 (status, headers, data)\" 的契约；POST 默认不自动重试的幂等纪律。必须改：(1) proxy 必填 —— 参照 erp-core 版(erp-core/backend/app/services/walmart_client.py:78-79、130-135)，proxy 为空直接抛异常，把\"不直连\"从数据兜底升级为代码硬保证，同时加 [wm-call] store/endpoint/proxy_id 追踪日志；(2) 凭证与代理来源换成 registry(store 表 / .env)，废弃 店铺API.xlsx 与 pandas 依赖，连带消除端口 float 与代理密码 URL 编码两个坑(但保留 quote() 编码逻辑)；(3) 把 auto_listing/rate_limiter.py 的滑动窗口 + x-current-token-count / X-Next-Replenishment-Time 自适应内建进 api/_client.py，按 (store, endpoint) 维度在每次请求前 acquire、请求后 update —— CLAUDE.md 已承诺\"api/_client.py 已内置\"，旧客户端其实没有，端点配额表照搬 rate_limiter.py:122-134 并与 refdata/walmart_rate_limits.tsv 对齐；(4) 补齐三个能力缺口以消灭现存所有绕过:raw bytes body(content= + 自定义 Content-Type，供 DELETE_ITEM/RETIRE_ITEM/MP_MAINTENANCE feed 提交)、二进制/文本响应(xlsx 绩效报告、CSV errorReport 需返回 resp.content 而非强解 JSON)、以及 async 客户端(订单并发拉取)。新 workflow 映射:api/feeds.py 收编所有 POST /v3/feeds + GET /v3/feeds/{id} + errorReport(参考 erp-core 版的 submit_feed/get_feed_status/list_feeds 签名)；api/orders.py 收编 async 订单拉取；api/reports.py 收编 settlement/recon/绩效 xlsx 下载；api/items.py、api/prices.py、api/inventory.py 各自对应。dry-run 与反查防重不要下放到 api 层，留在 cli.py 与 workflows(反查语义照抄 auto_listing/feed_submit.py 的 FOUND/NOT_FOUND/UNKNOWN 三态)。移植过程必须补单元测试(旧代码零覆盖)，用 httpx.MockTransport 覆盖 401 自愈、429 退避解析、连接池剔除三条路径。

### 待确认问题

- safe_* 的 401 自愈对 POST 同样会重发一次请求(walmart_client.py:375-387 的 continue 不区分 method)。是否曾因此产生过重复 feed?新实现是否应把 POST 的 401 重试也交给业务层反查后决定?需与业务方确认。
- x-current-token-count 从未被 walmart_client 消费，限流全靠外部 rate_limiter 且只在 token-count==0 时才生效(auto_listing/rate_limiter.py:92-95)。新版是否要做成"剩余配额低于阈值即主动减速"?阈值取多少?
- auto_listing/rate_limiter.py 的端点配额表与 refdata/walmart_rate_limits.tsv 是否完全一致(尤其 PUT /v3/inventory 200/分钟、POST /v3/items/spec 3/分钟)?以哪个为准需要核对。
- 店铺API.xlsx 中的明文 ClientSecret 与代理密码已进 git 历史。迁移时是否同步轮换?谁来执行?
- erp-core/backend/app/services/walmart_client.py 这套分叉是否也在生产运行?若在，切换时属于"新旧并跑"风险点，需要先确认它的调度状态(本次读取范围外，建议单独排查)。
- 沃尔玛异步.py 的 async 路径复用同步 get_token(asyncio.to_thread)。新 async 客户端是否要独立 token 缓存,还是与同步版共享一份带锁缓存?共享更省配额但需要 asyncio 友好的锁。


<a id="lark_io"></a>
## lark_io/ — 飞书读写封装

### 模块职责

旧仓库中「所有飞书电子表格读写」的统一封装层(feishu_migration_plan 的 Phase 0 产物),把此前散落在 4+ 处的 _run_cli 实现收敛成一个模块。两种传输后端:(1) 默认走 lark-cli 子进程(macOS worker,`--as bot|user --format json`,lark_io/_core.py:207);(2) direct-HTTP tenant_access_token 后端(lark_io/_http.py,给没有 lark-cli 的主机/订单审核用)。公开 API 只有 8 个函数:workbook_info / read_range / read_ranges / read_csv / write_range / append_rows / batch_write / api,外加低层 run_cli 和统一异常 LarkError(lark_io/__init__.py:9-33)。它统一了:瞬时错误重试与退避、四种读响应形状归一化、写操作三重切块、超大表窗口化并行读、facade(50502)自动降级到 raw v2、token/sheet_id 注册表。**重要边界:整个模块只支持电子表格(sheets v2 + Doubao 的 +cells-* facade),完全没有多维表格(bitable)支持——全仓库 grep 不到 bitable/多维表格任何调用**;新系统若用多维表格做人机界面,api/feishu.py 的 bitable 部分是全新代码,只能复用这里的认证/重试/分批骨架,不能复用读写形状归一化。

### 入口与触发

- 库模块,无 CLI / 无调度入口:lark_io/__init__.py:9-33 导出全部公开函数,由 26 个业务脚本 import(auto_listing/feishu_io.py、upc_pool.py、沃尔玛订单审核/飞书客户端.py、match_listing/feishu_io.py、沃尔玛店铺日报/*、定时任务skill/notify.py 等)
- lark_io._core.run_cli(args, identity=, timeout=, retries=) — 低层逃生口,直接透传 lark-cli 参数,旧调用点(auto_listing/quota.py:43、auto_listing/pricing.py:147)用它保持原命令行不变
- lark_io.api(method, path, data=, params=, backend='cli'|'http', stdin_payload=) — 通用飞书 OpenAPI 透传(lark_io/_core.py:1227),raw v2 读写和图片单元格写入都走它
- 切换开关是环境变量 LARK_IO_SHIM(默认 '1' 启用 lark_io;设 '0' 回落到各模块的旧 subprocess 实现)。见 auto_listing/feishu_io.py:129、auto_listing/quota.py:40、auto_listing/pricing.py:144 —— 每个迁移点都保留了双实现,是灰度回滚机制

### 调度

none —— lark_io 是被调用的库,自身不含任何 launchd/cron/定时逻辑。调度全在调用方(auto_listing 主流水线、店铺日报、商品维护等)。

### 数据存储

- 无数据库、无落盘状态文件。lark_io 是纯 I/O 封装
- lark_io/sheets_registry.py:1-122 = 硬编码的飞书 token / sheet_id 清单(9 个 workbook),声明「唯一事实来源」,源文件是 docs/feishu_sheets_registry.md(探针时间 2026-06-17),规则是先改 MD 再同步 .py
- lark_io/sheets_registry.py:111-113 PREFER_RAW_V2 集合 = {(ONLINE_TOKEN, ONLINE_MAIN)},路由状态,决定哪些 (token,sheet_id) 强制绕开 +cells-* facade
- lark_io/_http.py:38-39 _token_cache = {'token','expires_at'} 进程内内存缓存 + threading.Lock,进程重启即失效,无持久化
- 凭据来源:环境变量 FEISHU_APP_ID(默认值 cli_a9561a4f8dfadcd2 硬编码在 lark_io/_http.py:21)/ FEISHU_APP_SECRET(无默认,缺失时 _http.py:42-48 直接抛 LarkError);沃尔玛订单审核/飞书客户端.py:29-46 会在 import lark_io 之前从 gitignored 的 沃尔玛订单审核/deploy/secrets.env 兜底加载

### 飞书使用

- 【认证】两条路:(1) lark-cli 子进程持有凭据,Python 侧只传 `--as bot` 或 `--as user`(_core.py:207),bot=tenant_access_token 自动刷新、user=用户授权 token;(2) direct-HTTP:POST /open-apis/auth/v3/tenant_access_token/internal,body={app_id,app_secret},线程安全双检缓存 + 提前 5 分钟刷新(_http.py:25,51-115),Bearer 头(_http.py:136-139)
- 【只支持电子表格】facade 快捷命令:sheets +workbook-info / +cells-get / +cells-set / +csv-get / +batch-update / +append;raw v2 端点:GET /open-apis/sheets/v2/spreadsheets/{token}/metainfo(_core.py:764)、GET .../values_batch_get(_core.py:573)、POST .../values_batch_update(_core.py:592)。**没有任何 bitable(多维表格)端点**
- 【range 语法差异】facade 的 --range 是裸 A1(如 'A1:E418'),sheet 用 --sheet-id 单独传;raw v2 的 range 必须带前缀 '{sheet_id}!{A1}'(_full_range,_core.py:555)。混淆会静默写错表
- WB1 LISTING token=PDsR…Ph(token已脱敏,见旧仓库代码):ea2b2a(Sheet1 主表,auto_listing 写 F/G/H/O/P/Q 列 + append A:Z 26 列)、NxlS1J(UPC 池,mark_used/mark_claimed 写 B 列单格批量)、m8I92(跟卖结果,bot append)(sheets_registry.py:13-17)
- WB2 ONLINE token=MO2e…mI(token已脱敏,见旧仓库代码):e7834a(在线产品总表 148k+ 行,强制 raw v2)、38df0D(下架表)、899f65(店铺状态总览);动态页「维护记录_YYYY-MM-DD」运行时构造(sheets_registry.py:22-27)
- WB3 ERROR_PRODUCTS token=YlA1…dd(token已脱敏,见旧仓库代码):d4593b(店铺汇总)、aCz4c(错误统计)、WvPTz2(禁止品牌收集,risk_gate 用 +csv-get 读)、eGjQRX(监管合规删除);动态页「YYYY.M.D问题商品」约 70 张(sheets_registry.py:32-38)
- WB4 PRICING token=X4vM…bh(token已脱敏,见旧仓库代码)(别名 E1p9…Kh(token已脱敏,见旧仓库代码)):40383c(店铺API)、2FJ2Np(定价和上下架,只读)(sheets_registry.py:52-56)
- WB5 CATEGORY token=Gx9H…wc(token已脱敏,见旧仓库代码):0bdc8b(沃尔玛类目)、OJSrkV(禁止品类)、2p5sL6(映射明细)、2NgLNm(待人工复核)(sheets_registry.py:61-66)
- WB6 RETURNS token=Q2LF…f8(token已脱敏,见旧仓库代码):f83a79(售后订单同步 bot 写)(sheets_registry.py:71-73)
- WB7 ORDER_AUDIT token=YnUH…ea(token已脱敏,见旧仓库代码):980eaf(订单,direct-HTTP 写回含 AK 图片列)、OGBTUB(采购方)、3OGVQk(_meta 状态)、ZLUqxi(黑名单地址)、NOn5x7(黑名单邮编)(sheets_registry.py:78-84)
- WB8 PERF_PROBLEM token=VbVQ…zd(token已脱敏,见旧仓库代码):0271b5;WB9 STORE_KPI token=CRfC…kb(token已脱敏,见旧仓库代码):899f65(总览)+ 每店一页共 63 张运行时构造。注意 899f65 这个 sheet_id 在 WB2 和 WB9 里都出现,属不同 workbook,不能只按 sheet_id 索引(sheets_registry.py:89-99)
- 【字段名】lark_io 层不涉及任何列名/字段常量——它只认 A1 坐标。列语义(如「主表 F/G/H/O/P/Q 列」)全靠 sheets_registry.py 的注释和各业务模块自己的常量,这正是 CLAUDE.md 要求「飞书字段名只准引用 registry 常量」要解决的问题

### 沃尔玛端点

- none —— lark_io 不调用任何沃尔玛端点,不涉及出口代理/店铺凭据。它唯一的代理相关开关是 LARK_CLI_NO_PROXY=1(_core.py:209),那是给飞书访问用的,与沃尔玛固定出口 IP 规则无关

### 魔数与踩坑参数

- lark_io/_core.py:27 LARK_CLI 默认 '/opt/homebrew/bin/lark-cli'(可用 env LARK_IO_BIN 覆盖)—— 硬编码 macOS Homebrew 绝对路径,Linux 主机上必然 FileNotFoundError→LarkError('lark-cli not available on this host')
- lark_io/_core.py:28 DEFAULT_IDENTITY='bot'(env LARK_IO_IDENTITY 覆盖);但 auto_listing 全线用 identity='user'(quota.py:43、pricing.py:153)。飞书权限按身份独立、不共享,同一文件的读和写必须端到端同一身份,中途换身份是经典 forbidden 根因(docs/feishu_migration_plan.md:60-74)
- lark_io/_core.py:29-32 四档超时:DEFAULT_TIMEOUT=120(env LARK_IO_TIMEOUT)、BATCH_TIMEOUT=180、LIGHT_TIMEOUT=60、HEAVY_TIMEOUT=600(HEAVY_TIMEOUT 定义了但模块内没人用,留给 148k 行超大表调用方显式传)
- lark_io/_core.py:34-35 MAX_ATTEMPTS=4、BACKOFF=(1,2,4,8) 秒;_invoke 里 max_attempts=max(1,min(retries,MAX_ATTEMPTS)),即调用方永远无法把重试次数调到 4 以上(_core.py:215)
- lark_io/_core.py:37 TRANSIENT_CODES={90235, 90217, 50502};90235=data not ready(表格后端索引未就绪,多进程并发读写时高发)、90217=TooManyRequest(限流)、50502=Doubao facade 超时
- lark_io/_core.py:38-46 TRANSIENT_TEXTS=('90235','90217','50502','data not ready','too many request','timeout','request timeout') —— 子串兜底轨。_is_transient(_core.py:143) 是 int code + 小写子串双轨判定,两条都必须保留:新 sheet_ai/v2 路由是否暴露 int code 未经验证,子串兜底不可删(docs/feishu_migration_plan.md:43 差异矩阵行)
- lark_io/_core.py:48-50 写操作三重切块上限,取最先触发者:MAX_RANGES_PER_OP=3000(生产实测 4000 ranges 单包被飞书拒收,见 auto_listing/feishu_io.py:124-125 与 :145)、MAX_CELLS_PER_OP=4500(飞书单次写 5000 格上限留 buffer)、MAX_BYTES_PER_OP=120000(防 macOS ARG_MAX)。实现 _chunk_ops(_core.py:461)
- lark_io/_core.py:60-62 超大表读参数:CHUNK_ROWS=5000(每窗口约 1MB JSON)、BATCH_GET_RANGES=4(每次 values_batch_get 打包 4 个窗口)、READ_WORKERS=4(ThreadPoolExecutor 并发数)。注释明确写:并发读不会触发飞书限流,且刻意不加读节流 sleep(_core.py:687-688)
- lark_io/_core.py:66 FACADE_TIMEOUT_CODE=50502 —— +cells-get/+cells-set/+workbook-info/+batch-update 这些 Doubao facade 快捷命令服务端硬上限约 14s(sheets_registry.py:104-106),超时报 50502,_core 在 read_range/write_range/batch_write/workbook_info 各自 catch 一次并降级到 raw v2(_core.py:760、846、1052、1173)
- lark_io/_http.py:28-33 HTTP 后端独立的一套常量:_TRANSIENT_CODES={90235,90217,50502,99991400,99991663}、_MAX_ATTEMPTS=4、_BACKOFF=(1,2,4,8)、_EARLY_REFRESH_SECS=300(提前 5 分钟刷 token);token 默认有效期取响应 expire,兜底 7200s(_http.py:95)
- lark_io/_http.py:178-183 code 99991663/99991664(token 失效)→ 清空缓存并用新 token 重试,与普通瞬时码分开处理
- lark_io/_http.py:189-191 关键契约陷阱:非瞬时业务错误**不抛异常**,原样返回飞书 envelope(code!=0 的 dict)。调用方必须自己判 code,否则错误被静默吞掉。cli 后端行为相反(_core.py:277 抛 LarkError)——两个后端错误模型不一致
- lark_io/_core.py:207 每次调用都追加 '--as {identity} --format json';no_proxy=True 时注入 LARK_CLI_NO_PROXY=1(_core.py:209-210),仅 auto_listing/risk_gate.py:47 用到
- 读响应四种形状必须全部归一化(_normalize_read,_core.py:341):(a) data.ranges[].cells(+cells-get facade)、(b) data.valueRange.values(单数,单范围 values API)、(c) data.valueRanges[].values(复数,values_batch_get)、(d) data.annotated_csv(+csv-get 的 CSV 字符串)。旧 _cells_to_values 只处理第一种是历史 bug 源
- lark_io/_core.py:314-338 _flatten_cell:飞书富文本单元格返回 [{'text':...,'segmentStyle':...}] 段列表,必须拼接成纯文本;{'type':'#UNSUPPORT VALUE'} 之类不透明 dict → None;空串 '' → None。明确不用 valueRenderOption=ToString(会把数字变字符串)
- lark_io/_core.py:72-111 _try_json 三段式解析:整体 json.loads → 从最后一个 '\n{' 起 → 从第一个 '{' 起。因为 lark-cli 会在 JSON 前打印非 JSON banner 行;且 lark-cli 1.0.54 成功走 stdout、失败走 stderr,_parse_envelope(_core.py:114) 先 stdout 后 stderr
- lark_io/_core.py:134 ok 的推导规则:ok = envelope.get('ok', envelope.get('code')==0) —— 两种 envelope 形状混存
- 大 payload 一律走 stdin,绝不入 argv(macOS ARG_MAX):_invoke_with_stdin(_core.py:1269)、+batch-update 用 '--operations -'(_core.py:1219)、api(stdin_payload=True) 用 '--data -'(_core.py:1259-1263)。图片单元格 base64 写入也必须走这条路(docs/feishu_migration_plan.md:244-248)

### 防重/幂等语义

lark_io 本身**没有任何防重/幂等机制**——没有 request-id、没有 pending 落库、没有写前状态检查。它的重试是「同一 payload 原样重发」,依赖飞书写操作的天然幂等性:write_range / batch_write 都是按 A1 range 定位覆盖写(values_batch_update / +cells-set),重发同一 range 结果一致。唯一有并发语义的是 append_rows(_core.py:1063-1122):强制走原生 +append(服务端原子 append-to-end),注释明确「NEVER do row_count+cells-set」,因为 grid 的 row_count ≠ 数据末行且 read-modify-write 有 TOCTOU 竞态(docs/feishu_migration_plan.md 差异矩阵「写/追加命令」行)。但 +append 本身**不幂等**:一次成功但响应丢失后的重试会写重复行 —— 而 _invoke 的重试逻辑对写命令和读命令一视同仁,所以 +append 遇 90235/90217 重试时理论上存在重复追加风险(旧代码用「宁可重复也不丢行」的取舍,见 auto_listing/feishu_io.py:61-63 记录的丢行事故)。新系统写飞书前应自建幂等键(如按业务主键先读后写,或落 ops 表记 pending)。

### 危险操作

- write_range 默认 allow_overwrite=True(_core.py:1006),即默认覆盖已有单元格;flag 用 '=' 语法传给 lark-cli('--allow-overwrite=true',_core.py:1034)。新系统应把默认改成显式传参或 False
- batch_write 的 atomic 参数(_core.py:1130)语义要注意:atomic=True 是「遇错即抛」而非事务回滚 —— 前面已成功的 chunk 已经写进飞书了,不会撤销。atomic=False 是 continue-on-error,把失败 chunk 收进 summary['errors'](_core.py:1178-1190),调用方必须检查 summary['ok'],不检查就会静默丢数据(auto_listing/feishu_io.py:135-140 就是靠数 errors 里的 ranges 反推成功数)
- append_rows 的 --range 只声明容量不决定位置:传 '{start_col}1:{end_col}{len(rows)}',真实插入位置是该列服务端算出的最后非空行(_core.py:1080-1089)。误以为它写 A1 会导致灾难性误判
- append_rows 的 concurrent_safe=False 分支是空实现(_core.py:1094-1097,只有 pass 和注释),即永远走原生 +append,这是刻意的:绝不允许 read-then-write 追加
- +append / +add-dimension / +create-sheet 在 lark-cli 1.0.54 会返回 _notice.deprecated_command,这是**预期内且被显式豁免**的(_core.py:1119-1120、docs/feishu_migration_plan.md:186-195),不得因此改用 +cells-set 替代
- lark_io 无 dry-run 概念,任何 write_range/batch_write/append_rows 调用即真写。新系统的 dangerous=True + --execute 强制机制必须在 workflow 层实现,不能指望 api 层
- _http.http_api 对非瞬时错误返回 dict 而不抛异常(_http.py:189-191)——写操作失败可能被当成功,危险等级最高的契约差异

### 事故教训与必须保留的行为

- 【90221 大读事故】单次读 148k 行撞飞书 'data exceeded 10485760 bytes',UPC 分配全部失败。修复=窗口化并行读(_core.py:55-62 注释、auto_listing/upc_pool.py:138 与 :238)。新 api/feishu.py 必须内置同款窗口化,不能让调用方自己拼
- 【50502 facade 超时】+cells-* 走 Doubao tools/sheet_ai facade,服务端硬上限约 14s,大表必超时;raw v2(values_batch_get/values_batch_update/metainfo)走老的高性能后端不受限(sheets_registry.py:104-109)。四个公开写/读函数各自实现了一次降级
- 【90235 丢行事故 2026-06-10/11】6-10 多 worker 并发跑,+append 遇 90235 直接失败,1000+ 行 feed 已提交、UPC 已耗、Walmart 上真实在售,但飞书无记录(「幽灵商品」);tools/rescue_lost_appends.py 事后从日志反扫补录 6241 行。教训:重试必须覆盖写路径,不能只修读路径(auto_listing/feishu_io.py:59-64、auto_listing/README.md:382)
- 【90235 读空索引事故 2026-06-09】在线产品总表被 90235 读出空索引,状态表大面积误写 NOT_FOUND。教训:读到空必须硬中止而不是当成「数据真没了」(auto_listing/sync_status_track.py:729、:517)。api/feishu.py 层可提供「空结果标记」,但判定必须在 workflow 层
- 【格式化值回归 2026-06-11】+cells-get 读百分比格式列拿到 '275%' 字符串,float() 失败被静默跳过,导致整批店铺误判「无倍数配置」被淘汰(2355 行)。教训:facade 返回的是**显示值**不是原始值(auto_listing/README.md:382、auto_listing/pricing.py 的 _parse_multiplier)
- 【90217 限流是时间窗口制】约 10s 窗口,串行重试时每个请求间隔 8s 让窗口过期(auto_listing/reconcile.py:385);逐条调用必然触发限流,必须批量(auto_listing/main.py:621「之前 for 循环逐条 mark_used 触发 90217」、upc_pool.py:584-586)
- 【读并发安全、写必须串行】_core.py:62 明确「concurrent reads do NOT trigger Feishu rate-limit」并刻意不加读节流;而写路径 batch_write 是 for chunk in chunks 的**纯串行循环**(_core.py:1164),没有线程池、也没有 chunk 间 sleep。auto_listing/main.py:688 记录「4 worker 并发写触发 90217」。新系统必须保持:读可并行 4 worker,写严格串行,且写失败降级路径要考虑加 sleep
- 【降级逐条时的跨表误写风险】旧 feishu_io.batch_write_ranges 批量失败后降级逐条写,但逐条函数写死默认 sheet_id,跨 sheet 时降级会把数据写到主表 —— 旧代码为此显式放弃降级(auto_listing/feishu_io.py:184-192)。新实现的降级路径必须带 sheet_id
- 【凭据泄露】APP_SECRET 曾硬编码在 沃尔玛订单审核/飞书客户端.py,且完整明文写进 docs/feishu_migration_plan.md:29,已入 git 历史。迁移=轮换密钥,不是搬运旧值
- 【惰性 binary 探测】import lark_io 不探测 lark-cli 是否存在(_core.py:7-9),只有真正调用 cli 后端且缺失时才抛 LarkError。这是刻意设计(评审 Gap 15),让无 lark-cli 的主机也能 import 并只用 http 后端
- 【_notice 只记不失败】_parse_envelope 把 _notice 记录下来但从不据此报错(_core.py:136-139);实际上模块里连 WARNING 日志都没打 —— lark_io 全程零 logging,所有可观测性靠调用方。新 api/feishu.py 应补日志
- 【sheet_id 不唯一】899f65 同时是 WB2 的店铺状态总览和 WB9 STORE_KPI 的总览页(sheets_registry.py:26 与 :98),必须永远用 (token, sheet_id) 二元组做 key —— PREFER_RAW_V2 就是这么设计的
- 【两个后端的错误模型不兼容】cli 后端抛 LarkError,http 后端返回 {code!=0} dict,而 沃尔玛订单审核 那一侧的历史契约又是 {'_error': ...} dict(沃尔玛订单审核/飞书客户端.py:64-70 做边界翻译)。迁移必须逐个 except / .get('_error') 站点审计(docs/feishu_migration_plan.md:117-120)

### 切换时必须迁移的状态

- lark_io/sheets_registry.py 全部 9 个 workbook token 与约 25 个 sheet_id 常量 → 新仓库 registry/resources.py(注意 CLAUDE.md 铁律 3:token/表 ID 只准从 registry 取)
- PREFER_RAW_V2 路由集合(sheets_registry.py:111-113):目前只有 (MO2e…mI(token已脱敏,见旧仓库代码), e7834a) 在线产品总表 148k 行。新系统若继续读写该表必须保留同等路由,否则 +cells-* 必然 50502
- PRICING_TOKEN 与 PRICING_ALIAS_TOKEN 的别名对照关系(sheets_registry.py:43-53):X4vM…bh(token已脱敏,见旧仓库代码) 与 E1p9…Kh(token已脱敏,见旧仓库代码) 疑为同一物理 workbook 的 wiki-node-token 与 obj-token 两种 locator,两者 workbook-info 返回相同 sheet 列表(40383c + 2FJ2Np)。迁移时必须确认收敛到哪一个
- 每个 workbook / 每种操作类型的身份决策结论(bot vs user)。auto_listing 系历史上是 user 身份、其余是 bot;docs/feishu_migration_plan.md:60-74 强调必须逐表做「写探针」(实写一格→读回校验→分别验 +cells-set/+append/+batch-update),读探针通过不代表能写
- FEISHU_APP_ID / FEISHU_APP_SECRET 凭据:必须搬到新仓库的 <DATA_ROOT>/.env。**该 secret 曾明文硬编码在 沃尔玛订单审核/飞书客户端.py 并被完整抄进 docs/feishu_migration_plan.md:29,已进 git 历史 → 迁移时必须先在飞书后台轮换密钥,不要直接搬旧值**
- 无运行时状态需要搬(token 缓存是内存的,无 pending/游标/去重表)

### 迁移建议

给 api/feishu.py 实现者的建议,按「照搬/重做/丢弃」三类:

【必须照搬(都是血换来的常量与算法)】
1. 瞬时码集合与双轨判定:{90235, 90217, 50502} + 小写子串兜底('data not ready'/'too many request'/'timeout'),退避 1/2/4/8 秒最多 4 次。照抄 lark_io/_core.py:34-46 与 _is_transient(_core.py:143)。子串轨不可删。
2. 写切块三重上限取最先触发:ranges<=3000、cells<=4500、序列化字节<=120000。照抄 _chunk_ops(_core.py:461)。3000 这个数是 2026-05-19 实测 4000 被飞书拒收后降下来的。
3. 超大表窗口化并行读:CHUNK_ROWS=5000 / BATCH_GET_RANGES=4 / READ_WORKERS=4,按 batch index + window index 确定性排序合并。照抄 _windowed_raw_v2_read(_core.py:663)。这是 90221 事故的修复。
4. 读响应形状归一化(四种)与富文本 flatten(段列表拼 text、'' → None、不透明 dict → None)。照抄 _normalize_read(_core.py:341)与 _flatten_cell(_core.py:314)。
5. facade 50502 → raw v2 的一次性降级,以及 (token, sheet_id) 粒度的 prefer_raw_v2 路由表。
6. 大 payload 走 stdin/请求体、绝不入 argv 的规则(即便新系统不用 lark-cli,base64 图片写入仍要走请求体)。
7. 「并发追加只用服务端原子 +append,绝不 row_count 后 cells-set」这条硬规矩。
8. 读可 4 路并发、写严格串行 的节奏。

【必须重做】
1. 传输层:丢掉 lark-cli 子进程后端,只保留 direct-HTTP tenant token(lark_io/_http.py 是现成骨架:双检锁缓存 + 提前 300s 刷新 + 99991663 清缓存换 token)。理由:LARK_CLI 硬编码 /opt/homebrew/bin 是 macOS 绑定,envelope 要靠三段式 JSON 抠字符串(_try_json,_core.py:72),ok/code 两种形状并存,这些复杂度全部来自子进程,HTTP 后端一律没有。同时也就不再需要 identity=bot|user 双身份的 CLI 语义 —— 但要先确认 auto_listing 那批表能不能被 bot 写(见 open_questions)。
2. 错误模型统一成「抛异常」,把 _http.py:189-191「非瞬时错误原样返回 dict」这个静默吞错的行为改掉。
3. 补 logging:lark_io 全程零日志,_notice.deprecated_command 也只记不打。新层至少要打:重试次数、降级发生、切块数、每次写的 range 数与 cells 数。
4. 加多维表格(bitable)支持,这是全新代码,只能复用认证/重试/分批骨架。
5. token/sheet_id 从 sheets_registry.py 搬进 registry/resources.py,并且按 CLAUDE.md 要求把「列语义」也常量化 —— 旧的 sheets_registry 只有注释描述「写 F/G/H/O/P/Q 列」,没有常量。

【丢弃】
- LARK_IO_SHIM 双实现开关(灰度用完即弃)、PRICING_ALIAS_TOKEN 向后兼容别名(收敛到一个)、concurrent_safe/grid_grow 这两个已经名存实亡的形参(_core.py:1070-1071,一个是空分支一个是「隐式生效」)。

【对应新 workflow】
lark_io 本身不对应任何 workflow,它是纯基础设施,应整体落到 api/feishu.py(遵守铁律 2:里面不写任何业务判断——比如「读到空要不要中止」属于 workflow 层,「facade 超时降级 raw v2」属于 api 层)。所有 token/sheet_id 落 registry/resources.py。它是 auto_listing 上架、状态回写、店铺日报、售后同步、订单审核、问题商品清理等几乎全部 workflow 的共同依赖,应优先于业务 workflow 实现并配单测(旧仓库 tests/test_lark_io.py 有 20+ 个针对形状归一化/瞬时判定/切块边界(3001 ranges、4501 cells、120KB)的用例,值得整套移植)。

### 待确认问题

- 新系统的人机界面按 CLAUDE.md 是「飞书多维表格」,但 lark_io 只支持电子表格,旧仓库全无 bitable 代码。多维表格的 API(/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/*)的分页、批量上限(500 records/batch)、字段类型映射全部没有先例可抄,需要新做并单独探针
- auto_listing 系是否最终保留 identity='user'?migration plan 的现实预期是「统一 wrapper 不统一身份」(docs/feishu_migration_plan.md:60-74),但没看到写探针的最终结论文档。新系统若要全 bot,必须先把 bot 加为 LISTING_TOKEN / UPC 池的可写协作者
- PRICING_TOKEN 与 PRICING_ALIAS_TOKEN 是否确为同一物理 workbook 仅有「疑为」的推断(sheets_registry.py:47-50),未证实
- HEAVY_TIMEOUT=600 定义了但 lark_io 内部无引用,不清楚哪个调用点应该用它
- +batch-update 是否真支持 --operations -(stdin)在 plan 里是待确认项(docs/feishu_migration_plan.md 差异矩阵「批量写」行),而 _core.py:1219 已经这么用了 —— 若实际不支持,facade 批量路径可能一直在靠 50502 降级 raw v2 兜底,需实测
- 1204 这个错误码在 沃尔玛批量下架/daily_retire_orchestrator.py:125 与 50502 并列被当超时处理,但没进 lark_io 的 TRANSIENT_CODES(plan 说「仅在实测确认为瞬时后才纳入」),悬而未决


<a id="scraper_client"></a>
## scraper_client + 结算/商品拉取脚本

### 模块职责

三个文件其实是两件互不相关的事,被打包在同一个迁移单元里。(1) `scraper_client.py` 是全仓唯一的 DMIT 亚马逊采集器 (amazon-scraper-v3, `:8899`) 客户端:封装提交批次 / 按 ASIN 或 batch 取结果 / 查批次状态 / 轮询到完成 / 拉失败原因 / 全量 CSV 导出 / 拉截图,并提供一套异步孪生 API;还内置任务命名前缀登记表和 `normalize_amazon_data` 纯函数(把采集器原始字段裁剪成 LLM 友好字典)。它是 auto_listing、沃尔玛订单审核、erp_listing_server 的共同底座,自身不碰沃尔玛 API、不碰飞书、不落任何库,是纯无状态 HTTP 客户端。(2) `fetch_walmart_settlement.py` 是沃尔玛结算/对账拉取脚本:多店铺并发拉 Payment Statement 快照 + 按账期增量拉 Recon 明细,落本地 SQLite `walmart_settlement.db`,并提供 query/export 子命令;它是 erp-core 订单页"佣金真值"的唯一数据来源,对应新库 `orders.settlement`。(3) `fetch_my_walmart_items.py` 是一次性调研脚本:翻页拉全店铺 ACTIVE 商品,统计 productType 分布导出 xlsx,用于建立"亚马逊类目 → 沃尔玛 productType"映射表。

### 入口与触发

- scraper_client.py — 无 __main__,纯库。对外契约由 __all__ 声明 (scraper_client.py:56-65);同步入口 submit/fetch_one/fetch_batch(_items)/batch_status/wait_until_done/export_all/fetch_screenshot/fetch_batch_failures,异步孪生 asubmit/afetch_one/afetch_batch(_items)/abatch_status/apoll_until_done/afetch_screenshot,另有未列入 __all__ 但被订单审核使用的 asubmit_pairs (556)、aprioritize (594)、afetch_errors (608)
- scraper_client 的使用方(确认迁移影响面):auto_listing/dmit_client.py(已退化为薄兼容层,文件头明确要求新代码直接 import scraper_client)、沃尔玛订单审核/审核服务.py、沃尔玛订单审核/V3客户端.py、erp_listing_server/server/api_tasks.py
- fetch_walmart_settlement.py:799-820 — 手写 sys.argv 分发,无 argparse。四个子命令:(默认/fetch)[店铺名逗号分隔] [--force]、query、export [recon]。注意 802 行把裸 `--force` 也当作 fetch 入口
- fetch_my_walmart_items.py:153-177 — argparse 仅一个 --stores nargs=*,不传拉全部店铺
- 调度:三者均无 launchd/cron。仓库 README.md:206-207 明确标注前两者为『一次性脚本』;settlement 实际是人工按需运行(erp-core 只读它落下的 SQLite,不负责触发)

### 调度

none —— 三个脚本均无 launchd/cron 条目(仓库内 plist 只覆盖 auto_listing 与订单审核)。README.md:206-207 标注 fetch_my_walmart_items / fetch_walmart_settlement 为『一次性脚本』,实际由人工按需运行;erp-core 只被动只读 walmart_settlement.db,不触发拉取。新系统应给 settlement 配一条真正的定时(建议每日一次即可:Recon 账期按日发布,Payment Statement 变化也不快);fetch_my_walmart_items 属调研性质,按需手动。

### 数据存储

- SQLite `<repo>/walmart_settlement.db`(路径由 fetch_walmart_settlement.py:24-25 按脚本所在目录拼出)。生产实际路径 /Users/nextderboy/Projects/erpAPI/walmart_settlement.db,被 erp-core/backend/app/api/v1/orders.py:89 以只读模式硬编码引用
- 表 settlement_snapshots(fetch_walmart_settlement.py:40-132):80 列宽表,账户摘要/销售汇总/退款汇总/调整项/WFS/合作伙伴交易全部拍平成列 + raw_json 原始载荷。**无任何唯一约束**,每次 fetch 每店铺无条件 append 一行(save_snapshot:212),表随运行次数无界增长;读取时靠 `id IN (SELECT MAX(id) ... GROUP BY store)` 取每店最新(cmd_query:626)
- 表 recon_details(fetch_walmart_settlement.py:135-194):45 列,UNIQUE(store, report_date, transaction_key, amount_type) 是唯一的幂等键;raw_json 保留整行原始 JSON
- xlsx 输出(均为硬编码 macOS 绝对路径,迁移必须改为 registry/paths):settlement 摘要导出 /Users/nextderboy/Downloads/settlement_export_{ts}.xlsx(:775)、对账明细导出 /Users/nextderboy/Downloads/recon_export_{ts}.xlsx(:790)、商品清单 /Users/nextderboy/Downloads/walmart_all_stores_{ts}.xlsx(fetch_my_walmart_items.py:176)
- 店铺凭证与代理来源:walmart_client.load_stores 读 `店铺API.xlsx` Sheet1(walmart_client.py:143-206),列名为中文『店铺/ClientId/ClientSecret/代理类型/IP地址或域名/端口/IP登录账号/IP登录密码』;ClientId 为 0 或无代理的行被静默跳过——即『少拉了店铺』不会报错,只是数量变少
- scraper_client 自身零持久化:无状态文件、无数据库、无缓存落盘。唯一进程内状态是模块级同步 httpx.Client 单例(:115-136)

### 飞书使用

- 无。本次范围内三个文件均不读写飞书多维表格,也不引用任何飞书 token / app_token / table_id / 字段名。人机界面在这里是 SQLite + 终端打印 + Downloads 目录下的 xlsx。
- 迁移含义:settlement 现在没有任何飞书出口。若新系统要把回款/账期摘要做成飞书表,是**新增**能力,字段可直接照搬 cmd_query 的展示分组(卖家信息 / 回款信息 / 账户摘要 / 销售 / 退货 / 调整&WFS,fetch_walmart_settlement.py:682-743),这组字段是人工实际在看的口径。

### 沃尔玛端点

- GET /v3/report/payment/statement — fetch_payment_statement (fetch_walmart_settlement.py:388-397)。⚠️ **绕过 walmart_client 的 safe_get,直接 httpx.get**(:390)。配额 15/min(refdata/walmart_rate_limits.tsv:123)。响应结构:payload.{sellerInfo, accountSummary, transactionDetails.{saleAggregate, refundDetails, adjustmentAggregate, wfs, partnerTxns}} + 顶层 partnerId / payload.outstandingMCABalance(字段映射见 save_snapshot:200-298)
- GET /v3/report/reconreport/availableReconFiles?reportVersion=v1 — fetch_available_recon_dates (:400-410)。⚠️ 同样直连 httpx.get(:402)。返回 `availableApReportDates` 数组,**这就是增量的账期清单/游标源**(:475)。该端点未出现在 refdata/walmart_rate_limits.tsv 中,配额未知
- GET /v3/report/reconreport/reconFileJson?reportDate=&offset=&noOfRecords= — fetch_recon_json (:413-439)。⚠️ 直连 httpx.get(:420)。offset/noOfRecords(1000)分页,响应取 `reportData`。同样**不在 rate_limits 表里**(表里只有 legacy 的 /v3/report/reconreport/reconFile,100/min,:122)——迁移时需实测配额,8 并发 × 分页可能触限
- GET /v3/items?limit=1000&nextCursor=&lifecycleStatus=ACTIVE — fetch_my_walmart_items.fetch_all_items (:41-43)。这个**走的是 walmart_client.safe_get**(唯一合规的一个)。带 query 参数配额 60/min(rate_limits.tsv:88-89)
- **未使用任何 feed**:三个文件全部只读,不提交 feed、不调 /v3/feeds、不写价格库存。
- **绕过共享客户端的后果**(settlement 三个直连点):丢掉 walmart_client._request_ex 的 401 自动刷新 token + 重建会话(walmart_client.py:353,381)、按代理维度池化的长连接(:82-98,每次调用都重新做一次 SOCKS5 握手)、以及 x-current-token-count / X-Next-Replenishment-Time 自适应退避。**代理本身没丢**——三处都显式传了 proxy=store['proxy'](:393,406,427),所以不构成『直连沃尔玛导致店铺关联』的红线事故,但迁移必须改走 api/_client.py。

### 魔数与踩坑参数

- scraper_client.py:82 — 采集器 base 默认硬编码 `http://<SCRAPER_VPS_IP,见旧仓库>:8899`(明文 HTTP + 裸 IP)。:78 的解析优先级 SCRAPER_BASE_URL > V3_BASE > DMIT_SCRAPER_BASE,注释说明这是收编历史上 4 套各写各的配置源的结果。文件头 :4-6 特别警告:同一台 DMIT 上还跑着 erp_listing_server(`/erp` `:9080` `:9090`),别混淆
- scraper_client.py:86 — DEFAULT_TIMEOUT=30s,env SCRAPER_HTTP_TIMEOUT。各函数另有覆盖:submit/asubmit 60s(:271,533)、fetch_batch_items 120s(:318)、export_all 600s(:473)
- scraper_client.py:87 — RESULTS_PAGE_LIMIT=200,注释标明是【服务端硬约束】,不是可调参数,调大无效
- scraper_client.py:88-89 — wait_until_done 轮询间隔 POLL_SEC_DEFAULT=30s,总上限 POLL_TIMEOUT_DEFAULT=7200s(2h);超时抛 ScraperError
- scraper_client.py:94-97 — 读重试:READ_RETRIES=2(共 3 次尝试,env SCRAPER_READ_RETRIES),仅对传输错误与 {429,500,502,503,504} 重试(_RETRY_STATUS:95);退避 0.5→1→2 秒、封顶 4.0s(_BACKOFF_BASE/_BACKOFF_CAP:96-97)。404 等其余状态码原样返回交调用方判断(_get docstring:145-148)
- scraper_client.py:92-93 + 288 — **POST 永不自动重试**,注释写明原因:非幂等,重试会重复建 batch。这是刻意行为,迁移时必须保留
- scraper_client.py:480 — export_all 显式 `retries=1`(而非默认 2),注释:全量导出约 20 万行,反复重拉会拖垮采集器
- scraper_client.py:318 — fetch_batch_items max_pages=10000 死循环兜底;:223 _parse_results_page 额外防御 next_cursor 与上一页相同导致的翻页卡死
- scraper_client.py:402 — fetch_batch_failures limit 被 clamp 到 [1,100000],默认 100000(即『全要』)
- scraper_client.py:508-510 — make_batch_name 用**毫秒**精度 `{prefix}_%Y%m%d_%H%M%S_{ms:03d}`,注释写明原因:同秒提交的批次会被服务端合并。迁移时降精度会直接造成批次串台
- scraper_client.py:596-598 — aprioritize 把批次 pending 任务 priority 提到 10(默认 0),worker 只拉 MAX(priority) 的任务,即插队到所有常规任务之前;best-effort,失败只返回 False
- fetch_walmart_settlement.py:549 — ThreadPoolExecutor(max_workers=8)。这个数字已被 沃尔玛商品维护/设计方案.md:280,336 引用为跨店并发的标准做法(『每 worker 独立 token + socks5 代理』)
- fetch_walmart_settlement.py:417 — recon 分页 page_size=1000,offset += len(records),`len(records) < page_size` 时停止;**无 max_pages 兜底**,服务端若始终返回满页即无限循环
- fetch_walmart_settlement.py:394,407,429 — 三个请求的 timeout 分别是 30/30/60 秒,写死在 httpx.get 调用里
- fetch_my_walmart_items.py:38 — GET /v3/items limit=1000。⚠️ 带 query 参数的 /v3/items 配额是 60/min(refdata/walmart_rate_limits.tsv:88-89),本脚本每页只 sleep 0.3s(:91),多店铺串行时贴着配额跑
- fetch_my_walmart_items.py:37,86-90 — 游标从 `nextCursor="*"` 起步;下一页游标 `data['nextCursor'] or data['meta']['nextCursor']`,并用 `meta == next_cursor` 判定结束(防游标卡死)
- fetch_my_walmart_items.py:48-53 — 响应体三种形状兜底 `ItemResponse` / `items` / `list.ItemResponse`,说明该端点返回结构在不同店铺/版本下不一致
- fetch_my_walmart_items.py:82 — `_raw_json` 被 `[:500]` 截断,是**有损**留存,不能当作原始数据源
- fetch_my_walmart_items.py:34 — 每页循环开头都重新调 get_token(依赖 walmart_client 的缓存去重),属冗余但无害的写法

### 防重/幂等语义

["settlement recon 明细:两级。(a) **拉取级**:按账期跳过——`SELECT DISTINCT report_date FROM recon_details WHERE store=?` 得到已有账期集合(:446-452),available 列表中命中的直接 skip(:477-479),--force 时不预加载该集合即退化为全量重拉(:536-538)。(b) **落库级**:UNIQUE(store, report_date, transaction_key, amount_type) + INSERT OR IGNORE(:192,309),重复行静默丢弃。","缺陷(必须在新系统修掉):账期级状态只有『存在/不存在』二态,没有 pending/done,分页中途失败会留下部分数据并被永久判为已完成;且 INSERT OR IGNORE 使得 except IntegrityError 永不触发,inserted/skipped 计数失真(见 pitfalls)。","settlement_snapshots:**无防重**,每次运行无条件 append 一行;消费侧靠 MAX(id) per store 取最新。","scraper_client 侧:客户端自身不做防重,防重靠命名与服务端。(a) submit 的 asins 在客户端保序去重(_clean_asins:175-186,不做大小写转换);(b) asubmit_pairs 按大写 ASIN 在 batch 内去重、首个邮编为准(:566-572);(c) batch_name 毫秒精度避免同秒批次被服务端合并(:508-510);(d) POST 不重试以免重复建 batch(:92-93)。fetch_batch/afetch_batch 以 ASIN 为 key 归并(结果表按 ASIN 唯一存储),export_all 同样按 asin 建索引(:488-490)。"]

### 危险操作

- 本次范围内**没有任何破坏性沃尔玛操作**:不提交 feed、不 DELETE_ITEM、不清库存、不改价格。三个脚本对沃尔玛全是 GET。因此新系统对应的 workflow 不需要 dangerous=True / --execute 门禁。
- 唯一的本地写风险:fetch_walmart_settlement.py --force(:804,536-538)会跳过增量判定,对所有店铺所有账期全量重拉 —— 不损坏数据(INSERT OR IGNORE 兜底),但会打满 reconFileJson 配额且耗时很长。新系统里建议把 --force 做成显式确认项。
- scraper_client.aprioritize(:594)会把某批次插队到所有常规采集任务之前(priority 0→10),对采集器整体队列有副作用,属于『别人的资源被抢占』类操作,应受控使用。
- scraper_client.export_all(:472)单次拉约 20 万行 CSV,注释明确警告反复调用会拖垮采集器(故 retries 特意降到 1);新系统不应把它放进高频循环。

### 事故教训与必须保留的行为

- **增量游标语义有漏数据缺口(最重要)**:增量判定只看 `SELECT DISTINCT report_date FROM recon_details WHERE store=?`(:446-452),即『该账期只要落过一行就算已完成』。而 fetch_recon_json 分页中途异常时,已入库的部分行仍然存在,异常只被记进 result['errors'] 打印一行 ⚠(:483-484),该账期从此被永久跳过。没有 per-(store,date) 的完成状态,只能靠 --force 全量重拉纠正。新系统必须引入账期级 pending/done 状态(符合项目铁律『防重状态先落库再调接口』)
- **inserted/skipped 计数是假的**:save_recon_records 用 `INSERT OR IGNORE`(:309),重复行被静默忽略、不抛 IntegrityError,因此 `except sqlite3.IntegrityError` 分支(:361-362)永远不会命中——skipped 恒为 0,inserted 把被忽略的重复行也算成新增。日志里的『新增 N 条』不可信
- **UNIQUE(store, report_date, transaction_key, amount_type) 可能吞数据**:若同一 transaction_key 在同一账期下有两行相同 amount_type(多行费用拆分),第二行会被 INSERT OR IGNORE 静默丢弃。迁移到 PG 前应先用 raw_json 验证该四元组在历史数据里的真实唯一性,别照抄这个主键
- settlement_snapshots 无唯一约束,每次运行每店无条件 append(:212),且带 raw_json 全量载荷 —— 长期运行是无界膨胀。读侧只用 MAX(id),旧行纯属死重量
- 下游强耦合:erp-core 订单页的佣金真值直接 sqlite3 只读打开这个 db 文件(orders.py:89-115),口径是 `amount_type='Commission on Product'`、`SUM(ABS(amount))`、按 (purchase_order, purchase_order_line) 聚合(orders.py:110-113)。Settlement 是 **T+14** 账期,查不到就 fallback 到 unit_price×15% 并标记 commission_source='estimated'(orders.py:618-622)。切库时这三处口径必须一起搬,否则订单页佣金会静默变成全估算
- fetch_batch / afetch_batch 的 key 大小写**故意不一致**:同步版保留原样、异步版统一大写(scraper_client.py:346-348 与 :694 的显式 .upper())。docstring 写明是为各自调用方定制(sync_online 按原始大小写比对在线表;订单审核要对回大写 ASIN)。跨用会静默丢匹配
- normalize_amazon_data 的 sort_images 默认 True(:227-231),注释写明是**防御性**行为:采集器端用 set() 去重导致主图顺序横跳,排序是为了保证幂等。关掉会让上架主图随机变动
- fetch_batch_errors 走的旧接口 /api/batches/{name}/errors **服务端只返回最近 200 条失败**(:376),不完整;新逻辑应走 fetch_batch_failures(batch_id),后者在 404 时才自动回退到旧接口(:407-411)
- fetch_screenshot / afetch_screenshot 拉不到时返回 None 而不抛异常,docstring 注明是『保持与历史行为一致』(:28,494)——与其余函数一律抛 ScraperError 的约定相反
- 异步侧无连接池:每次 asubmit/afetch_* 都新建一个 httpx.AsyncClient(:544,655,673 等),高频调用会反复握手;同步侧才有单例 Client + atexit 关闭(:115-136)
- load_stores 静默跳过无 ClientId 或无代理的行(walmart_client.py:170-186),脚本层无任何『预期 N 家、实际加载 M 家』的校验,少店铺不会报错
- fetch_my_walmart_items 单店铺异常被 try/except 吞掉后继续(:167-168),且 fetch_all_items 内部请求失败只 break 并打印『已获取 N 条后中止』(:45-46)——会产出一份看起来正常、实则不完整的 xlsx
- fetch_my_walmart_items.cross_match_with_amazon(:120-149) 是死代码(main 里从未调用),且依赖亚马逊 xlsx 的中文列名字面量 `UPC 列表`(:127)、`ASIN (商品ID)`(:143)。迁移时按需求重写,别照搬

### 切换时必须迁移的状态

- 生产机上的 /Users/nextderboy/Projects/erpAPI/walmart_settlement.db 全库(仓库克隆里没有该文件,只在生产 Mac 上):recon_details 全部历史行 → orders.settlement 明细;settlement_snapshots 至少保留每店最新一行(历史快照可选归档,因为无唯一约束、含大量重复)
- recon_details 里已存在的 (store, report_date) 组合集合 —— 这是增量游标本身。新系统首跑前必须先导入,否则会把所有历史账期重拉一遍(每账期一次全量分页,量很大)
- 无其它状态:scraper_client 完全无状态;fetch_my_walmart_items 无状态(每次全量)

### 迁移建议

分三条独立路径迁移,不要当成一个模块。

**A. scraper_client.py —— 几乎照搬,只改配置来源。** 这是全仓质量最高的文件:配置单点解析、同步/异步共享纯函数、重试与退避已收敛、任务命名集中登记、危险行为都有注释解释原因。落位 `api/scraper.py`(符合铁律 2:它确实只做接口适配,零业务判断)。必须改的只有:(1) BASE_URL 默认值 `http://<SCRAPER_VPS_IP,见旧仓库>:8899` 与 env 回退链(scraper_client.py:78-85)搬进 registry;(2) TASK_PREFIXES(:102-111)搬进 registry 做常量登记,新增任务类型在 registry 加行。必须**原样保留**的行为(每条都有事故背景):POST 不重试、batch_name 毫秒精度、sort_images 默认 True、export_all retries=1、RESULTS_PAGE_LIMIT=200 是服务端硬上限、fetch_screenshot 返回 None 不抛。**唯一建议改掉**的是 sync/async 版 fetch_batch 的 ASIN 大小写不一致(:346-348 vs :694)——新系统统一大写并在调用方显式处理,别继承这个坑。异步侧可顺手加连接池(现在每次新建 AsyncClient)。注意 `auto_listing/dmit_client.py` 已是薄兼容层,迁移时直接删除该层、调用方全部改直连新 api/scraper。另:采集侧正在按 docs/scraper_migration_brief.md 新增『增量导出 + source_id + 游标』接口,catalog_sync 工作流将用新接口,本客户端的现有函数仍需保留服务旧链路(新旧并存数月)。

**B. fetch_walmart_settlement.py —— 重做,只保留字段映射表。** 拆成 `workflows/settlement_sync.py`(run(params) 返回摘要)+ services 里的字段映射 + `api/reports.py` 里的三个端点。必须重做的部分:(1) 三处 httpx.get 直连(:390,402,420)改走 api/_client.py,拿回 401 刷新、代理长连接池和自适应退避;(2) 增量语义从『账期存在即完成』升级为账期级 pending/done 状态,先落 pending 再拉数据,分页全部成功才置 done,程序重启时 pending 账期重拉——这正是项目铁律『防重状态先落库再调接口』的场景;(3) 落 PG `orders.settlement`,统计计数改用 `INSERT ... ON CONFLICT DO NOTHING RETURNING` 拿真实新增数(现在 inserted/skipped 是假的);(4) 唯一键别照抄四元组,先用历史 raw_json 验证 (store, report_date, transaction_key, amount_type) 是否真唯一;(5) settlement_snapshots 那张 80 列宽表不要照搬——快照本质是文档,建议 PG 里存 jsonb + 少量提取列,并加 (store, 快照日期) 唯一约束止住无界增长;(6) 所有硬编码 Downloads 路径改走 registry/paths;(7) max_workers=8 的并发风格可保留(每 worker 独立 token + 独立代理,已被其它模块引为标准),但 reconFileJson/availableReconFiles 的配额未在 rate_limits.tsv 中登记,上线前需实测并补进 refdata。**切换前置动作**:先把生产机 walmart_settlement.db 的 recon_details 全量 + 已有 (store, report_date) 集合导入新库,再起新调度;同时 erp-core 订单页那三处佣金口径(amount_type='Commission on Product'、SUM(ABS(amount))、按 PO+line 聚合、T+14 内 fallback 15% 估算)必须同批切到新库,否则订单页佣金会静默退化为全估算。

**C. fetch_my_walmart_items.py —— 大部分丢弃。** 它的价值只有两点:GET /v3/items 的翻页与响应形状兜底写法(:37-53,86-90,三种响应结构 + 游标卡死判定,值得抄进 api/items.py),以及『productType 分布统计』这个调研目的。真正的商品同步应由新的 listing 域工作流承担并落 PG,不是导 xlsx。cross_match_with_amazon(:120-149)是死代码且依赖中文列名字面量,直接删。`_raw_json[:500]` 截断绝不能带进新系统(有损)。

### 待确认问题

- reconFileJson / availableReconFiles 两个端点的官方配额未知(refdata/walmart_rate_limits.tsv 只有 legacy 的 reconFile 100/min)。8 并发 × 分页 1000 条是否会触限?上线前需实测并把结果补进 refdata。
- recon_details 的 UNIQUE(store, report_date, transaction_key, amount_type) 在历史数据里是否真唯一?需要用生产库的 raw_json 反查『被 INSERT OR IGNORE 静默丢掉过多少行』,再决定新库主键。这直接影响佣金真值是否曾经少算。
- Payment Statement 只有『当前』视图、没有历史账期参数?现有代码每次全量拉最新一份并 append 快照(:468),看不出是否支持按账期回溯。若不支持,新库的快照表就必须靠定时抓取积累历史,漏跑一天即永久缺一天。
- settlement_snapshots 的历史快照(可能已积累很多)是否需要全部迁入新库?下游只用 MAX(id) per store,历史行是否有分析价值需业务确认;不迁可大幅简化。
- scraper 侧的增量导出接口(docs/scraper_migration_brief.md 第三节要求的游标 + source_id)契约是否已定稿?catalog_sync 的实现依赖它,而本客户端现有函数(fetch_batch_items 的 keyset 翻页)与之是两套机制,需明确二者共存还是替换。
- fetch_my_walmart_items 建立的『亚马逊类目 → 沃尔玛 productType 映射表』最终落到哪里(pt_templates? catalog 表? 飞书?)——决定这个脚本的调研产物要不要在新系统里有正式归宿。


<a id="product_query"></a>
## 产品ID查询产品详情 → product_query (#1)

### 模块职责

用任意产品标识（关键词/UPC/GTIN/ASIN/SKU/wpid/isbn/ean/itemId）查询沃尔玛商品信息的一次性只读查询工具，按 ID 类型自动路由到三个端点之一：Item Search DEFAULT（全站目录，可查别家卖家的品，返回标题/品牌/评分/类目/变体，但不含 UPC/GTIN）、Item Search SPEC（responseFormat=SPEC，即后台“按匹配上架 mpsetupbymatch”同款接口，输入 upc/gtin/asin 之一返回 productId=UPC/GTIN + productIdType + 匹配状态 feedType + 预填属性，可做 ASIN→沃尔玛 GTIN 转换与跟卖可行性判断）、Catalog Search（仅本店铺自有目录）。三路响应归一化成一套 25 列统一表，控制台打印 + 默认导出 ~/Downloads 的 xlsx。零数据库、零状态、零调度、零飞书、无写操作，这正是它被排为迁移顺位 #1 练手项的原因。

### 入口与触发

- /workspace/erpapi/产品ID查询产品详情/query_product_detail.py:546 main() —— 唯一入口，纯手动 CLI（python3 query_product_detail.py --xxx），无 launchd/skill/cron 挂载，全仓 grep 无其他模块 import 它
- /workspace/erpapi/产品ID查询产品详情/query_product_detail.py:383 run_query(ctx, id_type, value, via) —— 单个 ID 的端点路由 + 执行，是迁移到新 workflow run(params) 的核心函数
- /workspace/erpapi/产品ID查询产品详情/query_product_detail.py:444 build_worklist(args) —— 把多个 --xxx 参数与 --file 汇成 [(id_type, value)] 工作队列
- /workspace/erpapi/产品ID查询产品详情/query_product_detail.py:160/172/188 call_item_search / call_catalog_search / call_item_search_spec —— 三个端点适配函数，对应新 api/items.py
- /workspace/erpapi/产品ID查询产品详情/query_product_detail.py:219/254/296 normalize_item_search / normalize_catalog / normalize_spec —— 三路响应归一化，属于 services 层职责

### 调度

none —— 无 launchd plist、无 cron、无定时任务 skill 引用，纯人工按需触发。plan.md:59 也标注“零状态零调度”。新版同样不需要调度，只需 cli.py 手动/未来网页触发。

### 数据存储

- 无数据库。全模块不碰 SQLite/PostgreSQL，无任何 CREATE TABLE / connect 调用
- 无状态文件、无游标、无缓存文件、无断点续跑
- 唯一持久化产物：Excel 导出，默认 ~/Downloads/walmart_product_detail_YYYYMMDD_HHMM.xlsx（query_product_detail.py:618-619 时间戳精确到分钟，同一分钟内重跑会静默覆盖），--out 可指定路径，--no-excel 只打印
- xlsx 结构：Sheet『产品详情』(UNIFIED_COLUMNS 25 列，query_product_detail.py:108-114)、Sheet『变体明细』(仅 Item Search 有变体时生成，query_product_detail.py:518-519)、Sheet『原始JSON』(仅 --raw，query_product_detail.py:521-525)
- 凭证来源：/workspace/erpapi/店铺API.xlsx，由 walmart_client.py:59 XLSX_PATH 定位（相对 walmart_client.py 所在目录，Sheet1）；本脚本不自行读表
- 示例输入文件 /workspace/erpapi/产品ID查询产品详情/ids_example.txt（每行一个 ID，# 为注释；解析见 query_product_detail.py:461-465）

### 飞书使用

- 无。本模块完全不接触飞书：无 app_token / table_id / 字段名，输出只有控制台与本地 xlsx
- 迁移建议：新版 product_query 若需人机界面，输入输出应改走飞书多维表格（一张“查询请求表”+ 结果回写列），字段名走 registry/resources.py 常量，而不是继续产 ~/Downloads/*.xlsx

### 沃尔玛端点

- GET /v3/items/walmart/search（DEFAULT）——query_product_detail.py:98 ITEM_SEARCH_URL，调用见 :160-169。只认 query / upc / gtin 三个参数；响应无 UPC/GTIN。refdata 限额 200/min（refdata/walmart_rate_limits.tsv:92）
- GET /v3/items/walmart/search?responseFormat=SPEC ——query_product_detail.py:188-202。同一端点同一配额；只认 upc / gtin / asin 三选一，不能带 query、不收 itemId，违反即 400；标准 token 即可，实测无需 WM_MARKET 等额外头
- POST /v3/items/catalog/search ——query_product_detail.py:99 CATALOG_SEARCH_URL，调用见 :172-185。body 固定 {"query":{"field":<字段>,"value":<值>}}；只搜本店铺自有目录。限额 200/min，与 Get Item Associations 共享配额（refdata/walmart_rate_limits.tsv:83-86）——新系统若同时跑 associations 需合并计账
- POST /v3/token —— 由 walmart_client.get_token 内部调用（walmart_client.py:227）
- 不提交任何 feed、不做任何写操作。SPEC 返回的 feedType（MP_ITEM_MATCH / MP_ITEM / 空）只是匹配状态标签，不是本模块提交的 feed 类型；下游跟卖 feed 是 POST /v3/feeds?feedType=MP_ITEM_MATCH，匹配键为 productIdentifiers(GTIN/UPC) 而非 itemId（README.md:69-73），属 listing 模块范畴
- 无直连绕过：全部走项目根 walmart_client.py 的 safe_get_ex / safe_post_ex（query_product_detail.py:56-59），每店固定出口代理保持完整。这是全仓少见的合规样板，不是需要修的旁路

### 魔数与踩坑参数

- --sleep 默认 0.35 秒（query_product_detail.py:569），≈171 req/min，压在两端点各 200/min 之下。注意 sleep 只在工作项之间执行（query_product_detail.py:605-606），且是固定间隔而非基于 x-current-token-count 的自适应节流
- 三个端点调用一律 max_retries=3（query_product_detail.py:166 / 181 / 197）。Catalog Search 是 POST 但属只读查询，代码 docstring 显式论证“非写操作，开 429/5xx 退避安全”（query_product_detail.py:174-175）——这条“只读 POST 可重试、写 POST 不可”的判据要带进新 api 层
- Item Search 截断告警阈值 len(raw) >= 20（query_product_detail.py:427-428），但同目录 README.md:192 写的是“单次最多 ~40 条”。两处数字自相矛盾，迁移前须实测确认真实上限，别照抄 20
- token 缓存提前 60 秒过期，expires_in 默认 900s（walmart_client.py:243-247）
- socket.setdefaulttimeout(90) 全局兜底（walmart_client.py:43）——事故背景注释：httpx timeout 走 socks5 时不可靠，实测一次 reconcile 卡在 SSL read 2.5 小时
- httpx 超时 Timeout(30.0, connect=15.0)、连接级 retries=2、连接池 max_keepalive=20 / max_connections=50（walmart_client.py:98-108）
- 429 退避：Retry-After 优先于 X-Next-Replenishment-Time，上限 300s，兜底 60s（walmart_client.py:274-310）；5xx 与网络异常指数退避 min(2**attempt, 10)（walmart_client.py:360 / 405 / 412）
- 401 自愈：清 token 缓存重换一次，独立于 max_retries，全程最多刷新 1 次（walmart_client.py:375-395）
- _autofit_columns 列宽上限 60（query_product_detail.py:539），纯观感参数

### 防重/幂等语义

none —— 纯只读查询，无写操作、无 feed、无幂等需求。重复执行只是重复消耗配额并覆盖同名 xlsx。唯一需要注意的“重复”是 build_worklist 不去重（query_product_detail.py:444-467）：--file 里重复的 ID 和命令行重复给的值都会各查一次，浪费 200/min 配额。新版建议在入队前按 (id_type, value) 去重。

### 危险操作

- 无破坏性操作。全模块只有 GET 与只读查询型 POST，无 feed 提交、无 DELETE_ITEM、无库存/价格写入，因此不需要 dry-run 保护，新版 workflow 应标 dangerous=False
- 唯一副作用是本地文件写入：export_excel 直接覆盖同路径 xlsx，且 os.makedirs(dirname, exist_ok=True) 会创建目录（query_product_detail.py:514-515）
- 配额消耗是唯一的“外部伤害面”：批量 --file 若行数很大且 --sleep 被调小，会打满 200/min 并 429 拖慢全店其它任务（token 桶是店铺维度共享的）。新版应对 worklist 长度设上限或强制最小间隔

### 事故教训与必须保留的行为

- 【字段大小写双兜底】线上返回是 camelCase，本地 walmart_official_specs/*.yml 规格写的是 snake_case，两者不一致。_get() 同时兜底两种写法（query_product_detail.py:140-153），实际用于 numReviews/num_reviews、nextDayEligible/next_day_eligible、variantItemsNum/variant_items_num（:239/242/243）。结论：本地 yml 规格不可信，以实测为准
- 【Catalog 货币字段】线上是 price.currency，规格写的 price.unit 是错的，代码双兜底（query_product_detail.py:279 + 注释 :278）
- 【SPEC 的 productId 位置随 feedType 变】MP_ITEM_MATCH 在 MPItem[0].Item，MP_ITEM 在 MPItem[0].Orderable，代码 core = mp.get('Item') or mp.get('Orderable') 兼容两者（query_product_detail.py:307）。MP_ITEM 才有 Visible.<productType>.{productName,brand}（:322-324），MP_ITEM_MATCH 的标题/品牌会是空——这不是 bug
- 【feedType 即匹配状态】MP_ITEM_MATCH=已在售可只建 offer 跟卖；MP_ITEM=目录有但未在售需完整建品（响应已预填属性）；{} 空=目录里没有需全新建品（MATCH_STATUS 表 query_product_detail.py:102-105，语义见 README.md:65-73）
- 【itemId 换 UPC 是死路】walmart.com 数字 itemId 在 Marketplace 无任何路径能换到 UPC/GTIN，已穷尽实测：?ids= / ?itemId= 返 400，GET /v3/items/walmart/{itemId} 返 404，SPEC 不收 itemId，仅 ?query=<itemId> 能找到品但无 UPC（README.md:77-96）。Seller Center 后台能做是因为内部解析，未开放。可行替代：Affiliate Product Lookup（需另行申请 + 签名认证）或第三方数据 API。爬 walmart.com 商品页虽有 UPC 但会用店铺固定代理 IP，属关联封号风险，明确不建议
- 【ROUTING 表里 catalog 的 itemId 字段不可用】README.md:189 记录连自有商品都 404，自有目录只能用 sku 查。但 ROUTING 里 itemid→'itemId' 仍保留（query_product_detail.py:89），是个已知无效的路由
- 【Catalog Search 只搜自有目录】用非 GLOBAL_TYPES 的 ID 走 catalog 时代码会打印警告（query_product_detail.py:432-434）；只有 query/upc/gtin 能查别家（GLOBAL_TYPES，:96）
- 【SPEC 参数互斥】必须 upc/gtin/asin 三选一，不能带 query、不收 itemId，违反即 400（README.md:191）
- 【Item Search 关键词结果不含 price】价格只在 Catalog 自有目录稳定有；UPC/GTIN 精确匹配可能 0 命中（索引问题），关键词最稳（README.md:187）
- 【HTML 标签污染】Item Search 的 title/description 带 <mark> 等标签，需 clean_text 去标签+反转义（query_product_detail.py:119-123）
- 【Catalog shelf 是 JSON 数组字符串】形如 '["A","B"]'，_fmt_shelf 美化成 'A > B'（query_product_detail.py:126-137）
- 【前导零风险】README.md:168 声称 UPC/GTIN 以文本存储保留前导零（如 05125794211458），但代码未对 DataFrame 显式指定 dtype=str，仅依赖 JSON 返回本身是字符串。新版落 PG 时 UPC/GTIN 必须是 text 列，绝不能用数值类型
- 【normalize_spec 少了两个键】它不返回 '主图'/'全部图片'（对比 :328-354 与 UNIFIED_COLUMNS :108-114），靠 export_excel 的补空列逻辑（:509-511）兜住。新版归一化应显式补全所有列，不要依赖导出层兜底
- 【sys.path hack】脚本靠“上一级目录”定位项目根来 import walmart_client（query_product_detail.py:51-54），因此目录不能移到更深层级（README.md:108）。新架构用 uv 可编辑安装消灭这一 hack
- 【单店铺认证】默认取 load_stores 返回的第一个有效店铺（query_product_detail.py:581），因为查的是公开目录、任一有效店铺 token 都行；--store 可指定。load_stores 会跳过 ClientId 为空/0 或代理任一字段为 0 的行（walmart_client.py:167-177）——即无代理的店铺被硬性排除，这是防关联的第一道闸

### 切换时必须迁移的状态

- 无状态需要迁移。无数据库表、无游标、无防重记录、无缓存文件——切换时直接停用旧脚本即可，不存在新旧并跑风险
- 唯一“数据”是历史导出的 ~/Downloads/walmart_product_detail_*.xlsx，属一次性查询产物，无需迁移
- 需要延续的是凭证来源：旧版从 /workspace/erpapi/店铺API.xlsx 取店铺+代理，新版改为飞书店铺凭证表 + 本地快照兜底（plan.md Phase 0）

### 迁移建议

照搬：(1) 三个端点的 URL、参数约束与响应结构解析逻辑（run_query 路由 + 三个 normalize_*），这是全模块真正的知识资产，尤其 SPEC 的 Item/Orderable 双结构兼容和 feedType→匹配状态映射，后续 listing 跟卖工作流会直接复用；(2) walmart_client 的认证/代理/退避内核，按 plan.md 已定的“移植而非重写”入 api/_client.py；(3) 所有实测坑点注释（camelCase 双兜底、price.currency、shelf 数组字符串、itemId 死路）原样带进新代码注释或 docs/legacy_reference.md，否则下一个人会重踩。

重做：(1) 分层——call_item_search / call_catalog_search / call_item_search_spec 三个函数纯粹是接口适配，原样落 api/items.py（新增 search_items / search_catalog / search_spec 三个函数，不带任何业务判断）；normalize_* 三个函数是“把三种响应拍平成一套字段”的可复用积木，落 services/product_normalize.py；run_query 的端点选择属业务决策，落 workflows/product_query.py 的 run(params)。(2) 去掉 argparse，run(params) 接 {ids, id_type, via, store, sleep, raw} 字典，参数解析交 cli.py。(3) 输出改向：不再默认写 ~/Downloads/*.xlsx（路径硬编码违反铁律 3），结果落 PG catalog schema 或回写飞书表；本地导出降级为可选调试开关，路径经 registry/paths.py 的 DATA_ROOT 取。(4) 凭证从 店铺API.xlsx 改为飞书店铺凭证表 + 本地快照兜底。(5) 入队前对 (id_type, value) 去重，并为 worklist 长度设上限。(6) 节流应基于响应头 x-current-token-count 自适应（api/_client.py 已内置），而不是固定 0.35s sleep；固定 sleep 可保留为下限。

新 workflow 对应：workflows/product_query.py，dangerous=False（无写操作，不需 --execute 门禁）。作为顺位 #1 练手项，它的真实验收价值在于打通 registry(凭证/路径) → api/_client(代理+退避) → api/items(端点适配) → services(归一化) → workflows(路由决策) → cli.py(锁/ops.runs/飞书通知) 这条完整链路，而不是业务本身——业务上它零状态零调度，切换时不需要停旧调度、不需要搬状态、不存在新旧并跑风险，可以随时上线。

两处必须实测确认的遗留矛盾：Item Search 单次返回上限到底是 20 还是 40（代码 :427 与 README:192 打架）；ROUTING 里 catalog 的 itemId 路由是否要直接删掉（README:189 说连自有都 404）。

### 待确认问题

- Item Search DEFAULT 单次返回上限：代码 query_product_detail.py:427 用 20，README.md:192 写 ~40，且端点是否支持分页（offset/limit）未在旧代码中体现——新版若要做全量拉取必须先实测清楚
- ROUTING 中 itemid→catalog 'itemId' 路由（query_product_detail.py:89）README.md:189 已判定不可用，新版是保留并给出明确报错，还是直接从路由表删除
- 新版 product_query 的人机界面形态未定：是继续 CLI 出 xlsx，还是走飞书“查询请求表→结果回写”。这决定了要不要在 registry/resources.py 里登记新表和字段常量
- SPEC 返回的预填属性（MP_ITEM 的 Visible.<productType> 全量属性）目前只取了 productName/brand（query_product_detail.py:335-336），其余属性被丢弃。listing 模块迁移时是否需要在这里就整包存下来（落 catalog schema 的 jsonb 列）以省一次调用
- Catalog Search 与 Get Item Associations 共享 200/min 配额（refdata/walmart_rate_limits.tsv:83-86），新系统若并行跑多条 workflow，配额是店铺维度共享还是 client_id 维度共享、要不要在 api 层做集中式令牌桶,需要确认


<a id="returns_sync"></a>
## 售后订单同步 → returns_sync (#2)

### 模块职责

每天 08:02 把全部有效店铺（README 称 57 家，实际由 店铺API.xlsx 决定）的沃尔玛售后单（RMA）用 GET /v3/returns 全量拉回，按 returnOrderLine 展开成 27 列的扁平行（一个 RMA 跨多 SKU 会占多行），合并所有店铺后按"退货创建时间"(returnOrderDate) 字符串倒序排序，再整表覆盖 PUT 写入飞书电子表格（非多维表格）Q2LF…f8(token已脱敏,见旧仓库代码) / sheet f83a79 的 A:AA。全程无本地状态、无游标、无 diff、无 upsert：靠"每次全量重写"实现等效增量（状态/退款/物流单号变化被刷新）。沃尔玛 returns 端点在本脚本用法中不带任何时间过滤参数，所以只能全量。脚本只读不写沃尔玛（从不调用退款端点），唯一破坏性动作是覆盖飞书表。

### 入口与触发

- /workspace/erpapi/售后订单同步/fetch_walmart_returns.py:231 main() — 唯一入口，无 argparse，靠 sys.argv[1]
- 全量模式：`python3 售后订单同步/fetch_walmart_returns.py --all`（fetch_walmart_returns.py:232-255），load_stores() 全量 + 8 路线程池
- 单店模式：`python3 fetch_walmart_returns.py <店铺名>`（:257-273）；不传参默认店铺硬编码 "I015陈道义"(:258)。⚠ 单店模式同样调用 write_to_sheet(rows)(:273)，写的是同一张全店共享表且从 A1 起，等于用一家店的数据覆盖全表——调试即事故
- 调度入口：/workspace/erpapi/定时任务skill/walmart-returns-daily-sync/SKILL.md:46 `cd /Users/nextderboy/Projects/erpAPI && LARK_IO_SHIM=1 /opt/homebrew/bin/python3 售后订单同步/fetch_walmart_returns.py --all`（Claude scheduled-task，非 launchd/cron）
- 通知入口：SKILL.md:30-40 强制在任务前后调用 定时任务skill/notify.py start / done --status ok|warn|fail
- sys.path 注入：fetch_walmart_returns.py:17-19 把项目根塞进 sys.path，以便任意 cwd 都能 import walmart_client；但 walmart_client 用 __file__ 找同目录 店铺API.xlsx，README 仍建议 cwd=项目根

### 调度

每天本地时间 08:02（README 写 8:00、错峰约 08:03 启动；SKILL.md 写『建议每天 08:02』）。调度器不是 launchd/cron，而是 Claude Code 的 scheduled-tasks skill：/workspace/erpapi/定时任务skill/walmart-returns-daily-sync/SKILL.md，注册位置 ~/.claude/scheduled-tasks/walmart-returns-daily-sync/SKILL.md。任务前后必须调 定时任务skill/notify.py 发飞书开始/完成简报（不依赖平台自带投递）。SKILL.md 还规定：单店失败=warn 不是 fail；部分成功即成功；不要因行数低于基线而重跑。

### 数据存储

- 本地状态：无。脚本不写任何文件、不写数据库、不留游标或已同步集合（README『本地状态』章节明确『无』），日志只走 stdout
- 唯一数据落点：飞书电子表格（sheets v2/v3，不是多维表格）token Q2LF…f8(token已脱敏,见旧仓库代码)，sheet_id f83a79（名 Sheet1），范围 A1:AA{n}，第 1 行为表头（fetch_walmart_returns.py:24-25,38,198,212）
- 店铺凭证与代理来源：/workspace/erpapi/店铺API.xlsx Sheet1，经 walmart_client.load_stores() 读取（walmart_client.py:143-210），列名硬依赖:『店铺/ClientId/ClientSecret/代理类型/IP地址或域名/端口/IP登录账号/IP登录密码』
- token 缓存：进程内内存字典 _token_cache（walmart_client.py:51,249-253），expires_in 默认 900s 再提前 60s 过期；进程退出即失效，无持久化
- 飞书表 token/sheet_id 在旧仓库已有登记表：/workspace/erpapi/lark_io/sheets_registry.py:71-73（RETURNS_TOKEN / RETURNS_MAIN），文档 /workspace/erpapi/docs/feishu_sheets_registry.md:16（RETURNS，bot 身份，bot 写已生产验证）

### 飞书使用

- 电子表格（sheets，非 bitable）：spreadsheet_token=Q2LF…f8(token已脱敏,见旧仓库代码)，sheet_id=f83a79（Sheet1），wiki 链接 https://my.feishu.cn/wiki/JuOf…sk(token已脱敏,见旧仓库代码)（README『数据存储』章）
- 读元信息：GET /open-apis/sheets/v3/spreadsheets/{token}/sheets/query（:165），取 grid_properties.row_count 判断是否需要扩行
- 扩行：POST /open-apis/sheets/v2/spreadsheets/{token}/dimension_range，body {dimension:{sheetId,majorDimension:ROWS,length:add}}（:179-181）
- 写值：PUT /open-apis/sheets/v2/spreadsheets/{token}/values，body {valueRange:{range:"f83a79!A{a}:AA{b}", values:[...]}}，100 行一批（:208-217）
- 调用方式：lark_call()（:134-161）默认走 lark_io.api(backend="cli", stdin_payload=True, identity="bot")；环境变量 LARK_IO_SHIM=0 可回退到直接 subprocess 调 lark-cli（旧实现保留在 :149-161）。成功判定两条路径不同:shim 路径看 parsed["ok"]（注释指出成功时顶层 code 常缺省），旧路径看 code==0
- 列是位置索引而非字段名:第 1 行写 HEADERS（:27-37,198），所有下游消费者按列位置读。表头一旦被人手工改名/插列，脚本仍按位置覆盖，静默错位——新系统必须把字段名收敛为 registry 常量
- 27 列表头（写入顺序即列 A→AA）:店铺 / RMA(returnOrderId) / 客户订单ID / 状态 / 退款状态 / 物流状态 / 退货方式 / 客户姓名 / 客户邮箱 / 退货创建时间 / 状态更新时间 / 退货截止时间 / 退款方式(refundMode) / 总退款金额 / 币种 / SKU / 商品名称 / 商品成色 / 数量 / 已退款数 / 退货原因 / 退货描述 / 销售行号 / 采购单号 / 单价 / 承运商 / 跟踪号（fetch_walmart_returns.py:27-37）

### 沃尔玛端点

- GET https://marketplace.walmartapis.com/v3/returns —— 唯一调用的沃尔玛端点（fetch_walmart_returns.py:56）。参数 limit=200 & replacementInfo=true，无任何时间过滤（脚本没用 returnCreationStartDate / returnLastModifiedStartDate，虽然官方支持，见 Walmart_Marketplace_API_Guide.md:1998-2011）
- 限速 50/min per store（refdata/walmart_rate_limits.tsv:144），脚本以 1.3s 页间隔留余量
- POST /v3/returns/{returnOrderId}/refund（60/min，rate_limits.tsv:143）—— 本模块从不调用，退款由人工/其他系统执行（README『速率限制』章）
- 无 feed 提交、无 DELETE_ITEM、无库存/价格写操作
- 是否绕过共享客户端：没有绕过。全部经 walmart_client.safe_get_ex → _request_ex → 按 store['proxy'] 池化 httpx 客户端（walmart_client.py:82-110,441-455），每店固定出口代理，符合新仓库铁律；token 也走共享 get_token。飞书侧则完全不走 HTTP，而是 lark-cli 子进程/lark_io

### 魔数与踩坑参数

- fetch_walmart_returns.py:49 `limit=200`（沃尔玛该端点单页上限 200）+ `replacementInfo=true`（当前 27 列并未使用返回的 replacement 字段，属预留）
- fetch_walmart_returns.py:72 `time.sleep(1.3)` 翻页节流 ≈46 req/min，对应 GET /v3/returns 限速 50/min（refdata/walmart_rate_limits.tsv:144）。注意 sleep 在『判定还有下一页之后』执行，最后一页不 sleep
- fetch_walmart_returns.py:55-60 `timeout=60, max_retries=2`：429 按 Retry-After / X-Next-Replenishment-Time 退避，5xx 指数退避 2**attempt 上限 10s（walmart_client.py:396-411）；Retry-After 解析上限 300s、兜底 60s（walmart_client.py:274-311）
- fetch_walmart_returns.py:237 `ThreadPoolExecutor(max_workers=8)` — 按店铺并发，注释写明『上限 8 路避免代理过载』。每店独立 token 桶，不互相消耗配额
- fetch_walmart_returns.py:196 `batch_size=100` 行/批 × 27 列 ≈2700 cells，低于飞书单次上限（lark_io/_core.py:49-51 MAX_CELLS_PER_OP=4500 / MAX_BYTES_PER_OP=120000）
- fetch_walmart_returns.py:200 `ensure_rows(end_row + 50)` — 多留 50 行余量的魔数；ensure_rows 只增不减（:173-181，cur_rows>=needed 直接 return）
- fetch_walmart_returns.py:38 `END_COL = "AA"` 与 HEADERS 27 列强耦合，改列必须同步改 END_COL
- fetch_walmart_returns.py:258 默认店铺字符串 "I015陈道义" 硬编码
- walmart_client.py:249-253 token 有效期 expires_in(默认 900s) - 60s 提前量；401 自愈最多刷新 1 次（walmart_client.py:374-392），与 max_retries 无关
- lark_io 重试常量：/workspace/erpapi/lark_io/_core.py:34-38 MAX_ATTEMPTS=4、BACKOFF=(1,2,4,8)、TRANSIENT_CODES={90235,90217,50502}+文本匹配；LARK_CLI 硬编码 /opt/homebrew/bin/lark-cli(:27)，默认身份 bot(:28)
- SKILL.md 运维阈值：预期耗时 30–90s、基线约 3800 行；若写入行数比基线少 50%+ 明确要求『不要重跑』，只汇报 --status warn

### 防重/幂等语义

["无显式防重/幂等状态。幂等性完全来自『全量拉取 + 从 A1 起整表覆盖』：同一 RMA 行每次都被重写为最新值，重跑安全（SKILL.md 明确称本任务为幂等覆盖写）。","但幂等只在『新数据行数 ≥ 上次』时成立，见 pitfalls 的残留缺陷。","行粒度是 returnOrderLine，不是 returnOrder：同一 returnOrderId 会出现多行（README『27 列定义』末注 + flatten :94）。因此 docs/db_schema.md:124 写的『主键 return_order_id』是错的，会在多 SKU RMA 上主键冲突。","脚本没有采集 returnOrderLine 自身的行号（只取了 salesOrderLineNumber，第 23 列 :125）。新表可用的候选主键：(store, return_order_id, sales_order_line_number)，若该字段可空则退化为 (store, return_order_id, sku, sales_order_line_number)；更稳妥的做法是在 api 层额外抓取 returnOrderLine 的行标识（旧脚本未取）后再定主键。","翻页用 nextCursor 里的 offset（:51-54 用 urlparse+parse_qs 把 cursor URL 解析回 params 并覆盖原 params），offset 分页在数据变动时可能漏行/重行，无去重兜底。"]

### 危险操作

- 整表覆盖写飞书 RETURNS 表（fetch_walmart_returns.py:196-218）：从 A1 起 PUT，无备份、无快照、无 diff。旧代码没有任何 dry-run 开关，跑起来就写生产表
- 单店模式误伤全表（:257-273）：调试一家店会把全表首部覆盖成这家店的数据，其余 56 家的旧行残留在下方，表面看仍有数据，实则半新半旧。旧代码没有任何防护
- 分批 PUT 非原子（:208-217）：100 行一批多次 PUT，中途失败留下半新半旧；README 明确要求失败后重跑整个脚本而非补写
- ensure_rows 只扩不缩、显式跳过 clear（:173-181, :202-203）：见 pitfalls 的残留缺陷
- 已有的保护措施只有两处:全量模式 `if not all_rows: return`(:249-250) 在全店失败时跳过写入，保住旧表；单店模式 `if not rows: 跳过写入`(:270-272)。除此之外无保护
- 不调用任何沃尔玛写接口（无 feed、无退款、无 DELETE_ITEM），所以对沃尔玛侧零破坏性
- 新系统落位建议：本 workflow 对沃尔玛只读，可标 dangerous=False；但『覆盖人机界面表』这一动作仍应支持 --dry-run 打印『将写入 N 行 / 与线上表差异 M 行』

### 事故教训与必须保留的行为

- 【整表覆盖残留旧行 — 缺陷精确位置】/workspace/erpapi/售后订单同步/fetch_walmart_returns.py:202-203 注释 `# 清掉旧的多余行（如果新数据比旧数据短）` / `# 此次新数据更长，不会有残留，跳过 clear` —— clear 逻辑从未实现，只写了注释。配套原因在 ensure_rows(:173-176) `if cur_rows >= needed: return` 只扩不缩。后果：当本次行数 < 上次（店铺批量失败、沃尔玛归档旧 RMA、单店模式误跑）时，第 len(rows)+1 行以后仍是上一轮的旧数据，且因为整表按时间倒序，残留行落在表尾看起来像『更早的售后单』，肉眼无法分辨真假。这是迁移顺位 #2 要顺手修的缺陷（docs/plan.md:60）
- 【静默截断】fetch_all_returns 翻页时若某页 status!=200，只 print 一行 `第 N 页失败` 然后 break 返回已拿到的部分数据（:61-63），fetch_one_store 视为成功（:221-228），主流程打印 `✓ N 行`。即店铺数据被悄悄截断且不计入失败清单，再叠加上一条的『不清行』，就产生新旧混杂
- 【carrier/tracking 取值粗糙】flatten 只取 returnLineGroups[0] 的第一条 label 的第一个 carrierInfoList（:76-81, :92-93），且把这一组承运商/跟踪号写给该 order 的所有行。多 group / 多标签 RMA 的物流信息会丢失或张冠李戴
- 【排序依赖字符串字典序】:252 与 :268 用 `r[9] or ""` 反向排序 returnOrderDate（ISO8601 字符串）。沃尔玛一旦改时间格式或混入不同格式，排序静默错乱。新系统应解析成 timestamptz 再排
- 【空值全部写空字符串】所有 .get(...,"") 兜底（:102-130），飞书表里空字符串与真实空值不可区分；入 PG 时需把 "" 归一化为 NULL，数值列（金额/数量/已退款数/单价）还需类型转换，旧代码原样写字符串
- 【load_stores 过滤顺序陷阱】walmart_client.py:184-197：先按『无代理』跳过，filter_names 过滤在最后。指定一家没配代理的店会得到空列表并打印『找不到店铺』，误导为店名写错
- 【沃尔玛端点本可增量却没用】官方支持 returnCreationStartDate / returnLastModifiedStartDate（Walmart_Marketplace_API_Guide.md:2005-2008），README 却断言『没有 since 字段只能全量』——这是旧文档的错误认知。新系统可用 returnLastModifiedStartDate 做真增量 upsert，大幅降低配额与耗时
- 【该目录未纳入 git】README 明确说明目录未 git add，变更历史只能靠文件 mtime 与 SKILL.md 推断；迁移时不要指望 git log
- 【生产时间禁改脚本】README/SKILL.md 均要求不要在生产时段改 fetch_walmart_returns.py（调度触发即读最新代码），也不要改飞书表列定义
- 【硬编码绝对路径】README 与 SKILL.md 中大量 /Users/nextderboy/Projects/erpAPI 与 /opt/homebrew/bin/python3、/opt/homebrew/bin/lark-cli（lark_io/_core.py:27），迁移时全部收进 registry/paths
- 【replacementInfo=true 拉了却没用】响应里的换货信息当前 27 列一列都没落库（README『关键点』3），新表若要做换货语义需要新增列

### 切换时必须迁移的状态

- 无本地状态文件/数据库需要搬运——这是本模块迁移最轻的一点
- 必须搬的只有飞书 RETURNS 表现有内容（约 3800 行 × 27 列）：把它一次性导入 orders.returns 作为历史底座。理由:沃尔玛 /v3/returns 是否长期保留全部历史 RMA 未验证，且旧表『覆盖但不清空』可能残留已被沃尔玛归档、接口不再返回的旧 RMA 行——这些行只存在于飞书表里，重跑拿不回来
- 导入时需人工确认残留行:导入前对表内 RMA 做一次与最新全量拉取的差集，差集里的行标注来源为 legacy_sheet 而非 api，避免把脏残留当权威数据
- 飞书表 token/sheet_id 需登记进新 registry（对应旧 lark_io/sheets_registry.py:71-73）；若按 plan.md:60 改用多维表格，需要新建 bitable 并保留旧 sheet 只读归档一段时间

### 迁移建议

照搬的部分：GET /v3/returns 的调用形态（limit=200、replacementInfo=true、nextCursor 用 urlparse+parse_qs 还原成 params）、1.3s 页间节流对应 50/min、按店铺 8 路并发、每店独立 token+固定出口代理（继续走新 api/_client.py，旧代码本就没绕过共享客户端）、单店失败隔离不拖垮整轮、以及 flatten 的 27 个字段映射（unit_price 取 charges 中 chargeCategory=PRODUCT & chargeName=ItemPrice 的 chargePerUnit.currencyAmount）。

必须重做的部分：
1) 落点改为 PostgreSQL orders.returns 为权威，飞书表退化为展示层。请修正 docs/db_schema.md:124——行粒度是 returnOrderLine 而非 returnOrder，主键应为 (store, return_order_id, sales_order_line_number)（建议在 api 层补抓 returnOrderLine 的行标识后再定），并加 first_seen_at / last_seen_at / raw jsonb。
2) 写入语义从『整表覆盖』改为 upsert（PG ON CONFLICT DO UPDATE；飞书侧按 plan.md:60 改多维表格按 record_id 更新），残留旧行缺陷（fetch_walmart_returns.py:202-203 的空注释 + ensure_rows 只扩不缩）自然消失。若因过渡期仍写电子表格，必须补 clear 到旧行尾部，或写入前记录上次行数。
3) 单店模式与全量模式必须分离写入目标：旧代码单店模式直写全店共享表（:273）是随时会触发的事故；新 workflow 的 run(params) 里，store 过滤只影响拉取范围，写入必须走 upsert 而非区间覆盖。
4) 翻页页级失败当前是 break + 部分数据当成功（:61-63）；新实现要么整店标记失败并放弃该店本轮 upsert，要么记录 partial 标记，绝不能让截断数据静默覆盖。
5) 可升级为真增量：用官方的 returnLastModifiedStartDate（Walmart_Marketplace_API_Guide.md:2005-2008，README 关于『只能全量』的说法是错的），日常增量 + 每周一次全量对账，配额和耗时都下降一个量级。
6) 表 token/sheet_id/字段名全部进 registry（旧仓库已有事实底表 lark_io/sheets_registry.py:71-73），代码里不得再出现 Q2LF…f8(token已脱敏,见旧仓库代码) / f83a79 / 中文表头字面量。
7) 调度改由 cli.py + launchd 承担（含 flock、ops.runs、飞书成功/失败通知），旧的 notify.py start/done 与 scheduled-task skill 一并废弃；保留 SKILL.md 里的运维判据作为告警阈值：预期 30–90s、基线约 3800 行、行数骤降 50%+ 不自动重试只告警。
8) 切换步骤：先停 ~/.claude/scheduled-tasks/walmart-returns-daily-sync → 把飞书现表 3800 行导入 orders.returns 作历史底座（标注 legacy_sheet 来源）→ 起新调度。对拍期两边可并跑（本模块对沃尔玛只读、对飞书是覆盖写，并跑期新系统先只写 PG 不写飞书，避免两个写者互相覆盖同一张表）。

### 待确认问题

- orders.returns 的行主键到底用什么：旧脚本没采集 returnOrderLine 的行号，只有 salesOrderLineNumber（可能为空）。需要先抓一份真实响应确认 returnOrderLine 是否有稳定唯一标识（如 returnOrderLineNumber）
- GET /v3/returns 不带时间参数时沃尔玛是否返回全部历史 RMA、有无保留期上限？这决定飞书旧表里的 3800 行是否真的存在只此一份的历史数据
- 飞书侧最终形态：按 plan.md:60 改多维表格（bitable，record_id upsert），还是保留电子表格只补 clear？两者字段常量与 api/feishu 的实现路径不同，需先定
- replacementInfo=true 返回的换货字段是否要落库（当前 27 列一列没用）；若要，orders.returns 需要扩列或用 raw jsonb 兜住
- 多 returnLineGroups / 多标签 RMA 的承运商与跟踪号正确归属方式（旧实现只取 groups[0] 并广播给所有行），是否需要拆出独立的退货物流子表
- 是否要迁移『退款』能力：POST /v3/returns/{returnOrderId}/refund 目前完全人工，新系统若纳入就变成危险工作流，需要 dry-run + ops 防重表


<a id="daily_report"></a>
## 沃尔玛店铺日报 → daily_report (#3)

### 模块职责

每天把全部沃尔玛店铺（生产约 50-63 家）的运营快照汇总成日报。三阶段串行流水线：Phase 1 `fetch_walmart_performance.py` 并发拉 Walmart API（8 项绩效 summary / payment statement / recon 上期回款 / items 库存统计 / 24h 窗口订单），再触发影刀 RPA 抓沃尔玛前台的"卖家名称+销售状态"两列，合并后写飞书「店铺KPI」总览 sheet 与每店独立历史 sheet（32 列 A-AF）；Phase 2 `fetch_walmart_problem_orders.py` 并发下载 8 个绩效报告 xlsx，解析成 13 列（A-M）问题订单行，按 5 字段联合 key 全局去重后累积写飞书「绩效问题订单」；Phase 3 `walmart_daily_summary.py` 反过来读这两张飞书表，比对昨日状态变化、汇总 KPI、做物流/承运商分析，生成 markdown 经 lark-cli 推送给运营（苏里）。全程只读 Walmart，不做任何写回沃尔玛的操作；唯一的持久化在飞书表格里，没有任何本地数据库。

### 入口与触发

- /workspace/erpapi/沃尔玛店铺日报/fetch_walmart_performance.py:1173 main() — `python3 fetch_walmart_performance.py [--no-yingdao] [店铺名...]`，位置参数即 load_stores 的过滤名单
- /workspace/erpapi/沃尔玛店铺日报/fetch_walmart_problem_orders.py:698 main() — `python3 fetch_walmart_problem_orders.py [店铺名...]`
- /workspace/erpapi/沃尔玛店铺日报/walmart_daily_summary.py:349 main() — `python3 walmart_daily_summary.py [--dry-run]`；异常时在 :383-393 兜底发一条飞书文本告警后 exit(1)
- 调度入口是 AI 执行的 SKILL.md（mcp scheduled-tasks），不是 shell/launchd：/workspace/erpapi/定时任务skill/walmart-kpi-daily/SKILL.md（Phase1→2→3 全跑）与 /workspace/erpapi/定时任务skill/walmart-kpi-afternoon/SKILL.md（仅 Phase1 --no-yingdao）
- 任务生命周期通知：/workspace/erpapi/定时任务skill/notify.py start|done --task walmart-kpi-{daily,afternoon}，与 Phase 3 的业务日报是两回事，两者都发
- 环境开关 LARK_IO_SHIM（默认 "1"）：=1 走 /workspace/erpapi/lark_io 共享层，=0 回落 subprocess 调 lark-cli 二进制。三个脚本各自实现了一遍这个分支（perf.py:567-614、problem_orders.py:449-551、summary.py:74-130）

### 调度

mcp scheduled-tasks（由 AI 按 SKILL.md 执行，非 launchd/cron 进程）：walmart-kpi-daily `0 8 * * *` 跑 Phase 1（含影刀）→ Phase 2 → Phase 3；walmart-kpi-afternoon `0 14 * * *` 只跑 Phase 1 的 --no-yingdao 轻量补刷，不拉问题订单、不发日报。任务定义在 ~/.claude/scheduled-tasks/walmart-kpi-{daily,afternoon}/SKILL.md（仓库副本 /workspace/erpapi/定时任务skill/）。两个 SKILL.md 都要求先后调 notify.py start / done 发任务生命周期简报，并规定「部分成功 = 成功」：40+/50 店铺成功即 --status warn 而非 fail，个别店铺 ProxyError（约 5-6 个凭证失效）属已知正常。调度时间与 WINDOW_END_HOUR/MINUTE=06:30 强耦合，建议 ≥06:35。

### 数据存储

- 飞书电子表格「店铺KPI」spreadsheetToken=CRfC…kb(token已脱敏,见旧仓库代码)（fetch_walmart_performance.py:80）。结构：一个按 title 查找的『总览』sheet（perf.py:81 LARK_OVERVIEW_TITLE="总览"；lark_io/sheets_registry.py:98 记为 KPI_OVERVIEW="899f65"）+ 每店一个以店铺名为 title 的 sheet（perf.py:803 ensure_store_sheet 缺失即建，写表头并冻结首行）
- 飞书电子表格「绩效问题订单」spreadsheetToken=VbVQ…zd(token已脱敏,见旧仓库代码)，单 sheet_id=0271b5（fetch_walmart_problem_orders.py:53-54；walmart_daily_summary.py:34-35；lark_io/sheets_registry.py:89-91）
- 本地唯一状态文件：<项目根>/data/frontend_scrape/latest.json（perf.py:94 FRONTEND_JSON_PATH，用 PROJECT_ROOT 拼接）。结构 {"scraped_at": ISO8601+08:00, "stores": {sellerId: {"seller_name":..., "sales_status":"可售"}}}。当前样本 17 个店铺、1685 字节，sales_status 取值只见到"可售"（脚本另会补"不可售"）
- 店铺凭证与代理：/workspace/erpapi/店铺API.xlsx，经 walmart_client.load_stores（walmart_client.py:143-206）读取，ClientId 为空/0 或 代理类型/IP/端口 任一为 0 的行会被静默跳过
- 没有任何 SQLite/PG。所有历史（KPI 每日快照、问题订单永久累积集合）只存在于飞书表格里，飞书表就是数据库
- notify.py 的运行标记：tempfile.gettempdir()/erp_skill_notify/<task>.json（定时任务skill/notify.py:66,99-124），仅用于算耗时，不含业务数据

### 飞书使用

- 店铺KPI：app_token/spreadsheetToken=CRfC…kb(token已脱敏,见旧仓库代码)。总览 sheet 按 title "总览" 动态查 sheet_id（perf.py:81,1201；sheets_registry.py:98 另记死值 899f65，两者需核对一致）；每店一个 sheet，title 就是店铺名
- 店铺KPI 32 列（A-AF，perf.py:131-141，LAST_COL_LETTER="AF"）：日期/店铺/卖家名称/partnerId/sellerId/店铺状态/支付状态/销售状态/在线商品/有库存/无库存/昨日出单/昨日销售额($)/准时送达(90%)/取消率(2%)/有效追踪(99%)/卖家回复率(95%)/退款率(6%)/差评率(2%)/退货率(6%)/未收到商品(2%)/账期销售额($)/销售佣金($)/退款金额($)/期末余额($)/迄今备用金($)/本期回款($)/回款日/收款方/结算周期/是否不押款/上期回款($)。注释明写『新增列一律追加到末尾，不要移动已有列』
- 绩效问题订单：spreadsheetToken=VbVQ…zd(token已脱敏,见旧仓库代码)，sheet_id=0271b5，13 列 A-M（problem_orders.py:85-89）：数据日期/店铺/Sales Order #/PO #/下单日期/指标/子分类/计入绩效/问题描述/商品/承运商/物流单号/备注。指标列带 emoji 前缀（🚚 OTD / 🛰 VTR / ❌ 取消率 / 💰 退款率 / ⭐ 差评率 / 📦 退货率 / 📭 未收到 / 💬 SRR），计入绩效列取值 "✅ 是" / "⚪ 否"
- 读写模式：lark-cli sheets +info / +read / +write / +append（覆盖式 write 为主，KPI 与问题订单都不用 append 追加）；v2 API POST /open-apis/sheets/v2/spreadsheets/{token}/sheets_batch_update 用于 addSheet（建店铺 sheet）、updateSheet（改 title/index/frozenRowCount/frozenColCount）；POST .../dimension_range 的 appendDimension 用于扩行（problem_orders.py:484-508）
- sheet 排序：reorder_store_sheets（perf.py:923-936）每次把总览固定 index=0，其余店铺 sheet 按店铺名字典序重排 index=1..N
- IM 推送：lark-cli im +messages-send --markdown（正常日报）/ --text（异常兜底告警），接收人 open_id = ou_36c5f91668c42a735e7b9d4ae74eedc1（苏里 = **所有者本人**，2026-08-16 澄清；freafish006@gmail.com），summary.py:38 硬编码
- 身份统一 --as bot，AppID cli_a9561a4f8dfadcd2（README:185）。LARK_IO_SHIM=1 时改走 lark_io.run_cli/lark_io.api（identity="bot"），超时 60/120/180s 不等

### 沃尔玛端点

- GET /v3/insights/performance/{otd,cancellations,vtr,srr,refunds,negativeFeedback,returns,itemNotReceived}/summary（perf.py:151-160）— 8 项绩效指标；单店内 8 端点并发。rate 值提取优先级 sellerAccountableRate > cumulativeRate > overallRate，round 2 位（perf.py:184-187）。官方限速前 5 个为 1/min/端点/ClientId，后 3 个未列入官方表（refdata/walmart_rate_limits.tsv:183-188），按 1/min 自我节流
- GET /v3/insights/performance/{otd,vtr,cancellations,refunds,negativeFeedback,returns,itemNotReceived,srr}/report（problem_orders.py:63-72）— 同上 8 项的 xlsx 报告，1/min。**这是绕过 walmart_client 的直连点**：problem_orders.py:151 用 httpx.get(url, headers=make_headers(token,cid), proxy=proxy, timeout=90) 自行发请求，理由是 safe_get_ex 会强解析 JSON、破坏 xlsx 二进制响应（:141-142 注释）。仍走每店固定代理，但绕过了 401 自愈与连接池剔除
- GET /v3/report/payment/statement（perf.py:225）— partnerId / sellerInfo.sellerStatus / paymentStatus / storeFrontUrl（正则 /seller/(\d+) 提取 sellerId，perf.py:239-241）/ accountSummary（closingBalance, reserve, holdAmount, reserveToDate, scheduledSettlementDate, paymentProcessor, settleCycle）/ transactionDetails.saleAggregate.productPrice、netComm / refundDetails.productPrice。官方 15/min
- GET /v3/report/reconreport/availableReconFiles?reportVersion=v1（perf.py:323）— 返回 availableApReportDates，日期格式 MMDDYYYY
- GET /v3/report/reconreport/reconFileJson?reportDate=MMDDYYYY&offset=0&noOfRecords=5（perf.py:337-341）— 在 reportData 里找 Transaction Type == "PaymentSummary" 的行取 Total Payable，负值按规则归 0
- GET /v3/items/count?status=PUBLISHED（perf.py:375-378）— 在线商品总数；published_total==0 时直接跳过后续翻页，省 100s+/店
- GET /v3/items?limit=200&offset=N&lifecycleStatus=ACTIVE（perf.py:405-410）— 统计 publishedStatus==PUBLISHED 下 availability==In_stock / 非 In_stock。带 query 时限速 60/min。nextCursor 是会话 ID，同一次遍历所有页返回同值，不能用来判结束，真正翻页靠 offset
- GET /v3/orders?createdStartDate&createdEndDate&limit=200(&nextCursor)（perf.py:475-482）— 24h 中国时间窗口；总单数取首页 list.meta.totalCount，销售额累加 orderLines.orderLine[].charges.charge[] 中 chargeType==PRODUCT 的 chargeAmount.amount。限速 5000/min
- 本模块不提交任何 feed，不调用 DELETE_ITEM、价格/库存写接口，全部是 GET
- 弃用提醒：refunds/summary 官方已标 Deprecated，被 returns/summary 取代。当前两个都拉（refunds→退款率列，returns→退货率列，维度不同），新代码不应再用 refunds 系列

### 魔数与踩坑参数

- perf.py:84 API_CONCURRENCY=6 — 店铺级并发。README 明确写『不要随意调高店铺并发』，代理 IP 共享/全局风控会触发降速
- perf.py:201 ThreadPoolExecutor(max_workers=8) — 单店内 8 个 insights summary 端点并发。依据：不同端点限速桶独立，同店同端点才是 1/min
- perf.py:951 PHASE_TIMEOUT=300 — Phase 1a 全局墙钟超时；超时的店铺记为失败并 exe.shutdown(wait=False, cancel_futures=True)（:973）直接弃线程，避免卡在 executor 退出
- perf.py:89-90 YINGDAO_TIMEOUT_SEC=600 / YINGDAO_POLL_INTERVAL=15 — 影刀最多等 10 分钟，每 15s 轮询一次 latest.json
- perf.py:100-101 WINDOW_END_HOUR=6 / WINDOW_END_MINUTE=30 — 24h 销售窗口锚点（中国时间今天 06:30，往前 24h）。改这两个常量必须同步改两个定时任务时间；建议调度 ≥06:35
- perf.py:391 OFFSET_CAP=9800 / :397 page_size=200 / :399 max_pages=60 / :440 time.sleep(0.2) — /v3/items 翻页。offset 硬上限 10000，超过返回 400（注释点名 A150黄朝政 踩过）；:412 收到 400 直接停翻页不重试
- perf.py:478 orders limit=200 / :499 time.sleep(0.3) — /v3/orders 翻页节流；总单量取首页 meta.totalCount（:489），销售额只累加 chargeType=="PRODUCT" 的 chargeAmount.amount（:493）
- perf.py:316 prev_dt = dt - timedelta(days=14) / :340 noOfRecords=5, offset=0 — 上期回款严格取『当前 scheduledSettlementDate 减 14 天』那一期，不在 availableReconFiles 里就填 0
- perf.py:784 KEEP_LEFT_COLUMNS=8 — --no-yingdao 模式下 A-H 列全部保留 sheet 旧值（merge_preserve_afternoon :787）
- perf.py:708/884/914 飞书总览读写范围硬编码 A2:AF200 → 总览最多 199 家店铺；:914 每次先写 199 行空白再写数据
- perf.py:834 店铺历史 sheet 读 A2:AF10000，:864 全量重写（先排序再整体覆盖），单店历史上限 9999 行
- perf.py:577/585 lark-cli 超时 60s
- problem_orders.py:58-60 STORE_CONCURRENCY=6 / ENDPOINT_CONCURRENCY=8 / HTTP_TIMEOUT_SEC=90（单次 xlsx 下载超时）
- problem_orders.py:131 fetch_one_report(max_retries=3)：429 按 _parse_retry_after 退避，5xx 与网络异常 min(2**attempt,10) 秒；:104-128 _parse_retry_after 优先级 Retry-After > X-Next-Replenishment-Time（>1e12 视为毫秒），上限 300s，兜底 60s
- problem_orders.py:668/680 BATCH=500 — 清空与写入都按 500 行分块，理由是 cli 参数过长
- problem_orders.py:489 扩容多加 100 行缓冲 / :657 needed_rows = len+1+100
- problem_orders.py:190 short(s, n=30) — 先按 '$$' 切取前半（退货报告 Item name 带 $$ 分隔的子分类），再截断；调用处分别用 20/25/30
- problem_orders.py:689 lark_freeze(rows=1, cols=2) — 冻结表头 + 前两列
- summary.py:172 读每店 sheet 只扫 A2:AF6（5 行）找昨日行；:144 总览读 A2:AF200；:188 问题订单读 A2:M50000
- summary.py:322 严重 OTD 阈值 days>=3；:329 只列 TOP 5；:338 问题订单 TOP 5 店铺
- perf.py:63-73 本地包装把 walmart_client.safe_get/safe_get_ex 的 max_retries 默认值从 0 提到 3（walmart_client._request_ex 默认 max_retries=0，401 刷新 token 与代理连接池剔除则与 max_retries 无关，永远生效）

### 防重/幂等语义

两套语义，完全不同，迁移时别混。
(1)「绩效问题订单」= 永久累积 + 全局去重。key = (Sales Order #, 指标, 子分类, 物流单号, 商品) 五字段联合（problem_orders.py:562-587 make_dedup_key，同时兼容 dict 新行与飞书读出的 list 行，list 分支取下标 2/5/6/11/9）。merge_to_lark（:590-691）流程：读现有全表 → 对现有行也跑一遍去重（防御性清理重复，:619-631）→ 新行 key 已存在则跳过，不存在则加入并把「数据日期」定为今天 → 排序（日期降序、店铺升序、指标升序，:650-654）→ 扩容 → 分块清空 → 分块写。语义是「数据日期 = 该问题订单首次被发现的日期，永不被后续跑覆盖」；Walmart 报告滚动 90 天，同一订单会连续多天出现，表里只留首次。
(2)「店铺KPI」= 按 key 覆盖（upsert），不是累积去重。总览按店铺名 key 合并（perf.py:867-920 rewrite_overview，本次没跑到的店铺保留旧行）；店铺 sheet 按日期 key upsert（perf.py:821-864 upsert_store_history，同日期行覆盖、否则插入后按日期降序整体重写）。
(3) 幂等性：三个 Phase 都是「拉数据 + 覆盖/累积写飞书 + 发日报」，SKILL.md 明确标注为幂等、可安全重跑。但重跑 Phase 3 会重复发一条日报到飞书 IM，没有防重。
(4) 影刀结果的新鲜度校验：轮询时要求 latest.json 的 scraped_at > 本次触发时刻（perf.py:1113），旧数据继续等；这既是防重也是「不要在影刀已在跑时再手动 spawn 同一应用」的原因——两次 spawn 互抢会让校验反复失败。

### 危险操作

- 无沃尔玛侧破坏性操作（无 feed 提交、无 DELETE_ITEM、无清库存），本模块对沃尔玛纯只读；cli.py 的 dangerous=True 强制 dry-run 对它不适用
- 真正的危险在飞书侧：problem_orders.py:663-686 merge_to_lark 先把 A2:M{clear_end} 分块清空（BATCH=500）再分块写回全部行。这是「全表重写」，无事务、无备份；清空之后写入之前任何失败（飞书 429/50502/超时/进程被杀）都会留下被清空的表，而这张表是永久累积历史的唯一存储。保护措施：仅 sheet 容量扩容时多留 100 行缓冲，没有任何回滚机制
- perf.py:914-919 rewrite_overview 每次先写 199 行空白（A2:AF200）再写合并后的数据，同样是先清后写的窗口期风险；缓解措施是「合并模式」——先读现有总览按店铺名建 map，本次没跑到的店铺保留旧行，不会因单次 Phase 1 失败丢店
- perf.py:864 upsert_store_history 对每个店铺 sheet 做全量重写（读 A2:AF10000 → 排序 → 覆盖写）
- perf.py:644-653 lark_clear_sheet 实现可疑：名为清空却调 values_prepend 写一行空值，且全脚本无调用点——迁移时不要照抄
- dry-run 覆盖不全：只有 walmart_daily_summary.py 有 --dry-run（:350，只打印不发 IM）；两个写飞书的脚本没有任何 dry-run/--execute 保护
- 并跑风险：早间 daily 与下午 afternoon 若重叠，afternoon 的 --no-yingdao 路径会用 merge_preserve_afternoon 保 A-H，但两个进程同时对同一 sheet 做「读-改-写」仍会互相覆盖（无锁）。新系统必须用 cli.py 的 flock 单实例锁
- README 明确警告：不要把 --no-yingdao 当主跑用，否则 A-H 列全量数据会被冷启动覆盖

### 事故教训与必须保留的行为

- 【A147 事故】空 sellerId 的店铺绝不能喂给影刀：phase1_write_overview_only（perf.py:987-1006）在写总览前过滤 sellerId 为空的行，否则影刀会打开 https://www.walmart.com/seller//cp/shopall（路径中段为空）导致整条 RPA 循环崩溃，后续店铺全被跳过。这些店铺在 Phase 1e 会照常写入总览，只是不参与前台抓取
- 【A150黄朝政 事故】/v3/items 的 offset 硬上限 10000，超过直接 400。脚本设 OFFSET_CAP=9800 主动停（perf.py:391），并在收到 400 时立即终止翻页不重试（:412）。店铺总 SKU > 10000 时剩余 SKU 不在统计内，只打 ⚠ 警告，需要人工补齐
- 【故意不回填】fill_missing_frontend_fields（perf.py:726-765）：ACTIVE 店铺的『销售状态』为空时**故意不从 cache 回填**。因为 cache 是 main 开头读的旧值，影刀失败时回填会把「昨天的值」传染成今天的数据；留空后由 merge_preserve 在写入时保留 sheet 现值。而『卖家名称』变化频率极低，允许从 cache 补。这段反直觉逻辑必须原样保留
- 【A-H 列保护】merge_preserve_afternoon（perf.py:787-800）+ KEEP_LEFT_COLUMNS=8：--no-yingdao 模式对已存在的同店铺/同日期行，A-H（日期/店铺/卖家名称/partnerId/sellerId/店铺状态/支付状态/销售状态）整段保留 sheet 旧值，只刷新 I 列及以后。注意：影刀实际只产出 C（卖家名称）和 H（销售状态）两列，A-H 是「保护范围」不是「影刀产出范围」——README 标题里的说法容易误导
- 【204 不是错误】Walmart 对「无合规数据的店铺」返回 204 而非空 payload，两个脚本都显式把 204 当作「该指标为空」（perf.py:177-180 返回空串；problem_orders.py:179 返回 204,b""）。当成失败会导致绩效列全空
- 【非 ACTIVE 强制 payout=0】perf.py:253-267：API 的 closingBalance 只是账面余额，HOLD/INACTIVE/SUSPENDED 实际不会打款，因此 paymentStatus.upper()!="ACTIVE" 时本期回款强制写 0；payout<0 也归 0；『是否不押款』只在 is_active 且 payout>=closing 时才标
- 【上期回款 -14 天是业务规则】perf.py:291-334：只查 scheduledSettlementDate 减 14 天那一期，不在 availableReconFiles 里（节假日顺延/跳期）就填 0。README 明写『这是用户业务规则，不要改成找最近的可用日期』
- 【影刀前置条件】应用必须已在『我获取的应用』里跑过至少一次（首次有授权弹窗），必须在 macOS /Applications/影刀.app；spawn 用 shadowbot:Run?robot-uuid=xxx 协议 URL 非阻塞启动（perf.py:1048-1058）。不要在影刀已在跑时再手动启动同一应用，两次 spawn 互抢会让 scraped_at > trigger_time 的校验反复失败直到超时
- 【影刀降级链】超时/spawn 失败 → _read_frontend_fallback 读旧 latest.json（可能是几天前的）→ 读不到则返回空 dict，卖家名称/销售状态留空，不阻塞主流程（perf.py:1082-1137）。降级用旧数据时没有任何标记写进飞书，看表的人分不清是今天抓的还是上周的
- 【latest.json 路径陷阱】影刀 RPA 内部把结果写死到 <项目根>/data/frontend_scrape/latest.json，不跟脚本走。脚本搬到子目录后必须用 PROJECT_ROOT 拼接，否则读 沃尔玛店铺日报/data/... → 永远超时（perf.py:91-94 注释即为此事故的修复记录）
- 【emoji 是隐式契约】problem_orders.py:74-83 写入的『🚚 OTD』『🛰 VTR』等带 emoji 的指标名，被 summary.py:302 用字符串相等匹配来筛物流问题订单。改 emoji 或改空格会让日报的承运商分析静默变空
- 【SRR 字段未实测】problem_orders.py:371-379 注释说明用户店铺多为 100% 回复率、基本触发 204，所以 SRR 分支是通用兜底：把所有非空字段 json.dumps 后截断 100 字符塞进备注。迁移时不要以为这是正式映射
- 【xlsx 解析的两个跳过】首行若含 'Data current as of' 视为信息行、表头下移一行（:230-234）；任何单元格以 '=' 开头的行视为 Excel SUM 公式行整行跳过（:245）。sheet 名 == 'Not Accountable' → 计入绩效='⚪ 否'，其余 sheet 名直接作为『子分类』写入
- 【短字符串的 $$ 语义】short()（:190-195）先按 '$$' 切取前半——退货报告的 Item name 常带 $$ 分隔的子分类
- 【summary 只回看 5 行】summary.py:172 每个店铺 sheet 只读 A2:AF6 找昨日行。若某天任务没跑或某店连续失败，昨日行被挤出前 5 行就找不到，『vs 昨日』静默显示为无变化
- 【列定义重复三份】perf.py:131 的 COLUMNS、summary.py:44 的 KPI_COLUMNS 是人工复制的同一份 32 列；problem_orders.py:85 与 summary.py:56 是同一份 13 列。改一处忘另一处会导致 dict(zip(...)) 静默错位，不报错
- 【排序的空日期】problem_orders.py:651 排序 key 用 -int(str(r[0]).replace('-','') or 0)，日期为空时 int('0')=0 排到最后；若日期含非数字字符会直接 ValueError 崩在排序里
- 【飞书 429/50502 是常态】SKILL.md 把飞书超时/429/50502/9020x/9023x/1061045 列为已知瞬时故障，要求命令层再重试；lark_io 的 prefer_raw_v2 集合就是为绕开 +cells-* facade 的 14s 服务端硬上限而存在

### 切换时必须迁移的状态

- 飞书「绩效问题订单」整表（0271b5）的全部历史行 —— 这是去重的唯一真相源。切换时若不把历史 key 集合导入新库，所有仍在 Walmart 90 天滚动报告里的老订单会被当成新订单再写一遍，且『首次发现日期』全被改成切换当天，历史彻底失真
- 飞书「店铺KPI」每个店铺 sheet 的历史行（日期降序）—— 日报的『vs 昨日』对比只从这里读
- 飞书「店铺KPI」总览 sheet 的 C 列卖家名称 / H 列销售状态 —— 是 fill_missing_frontend_fields 的 cache 来源（perf.py:697-723），也是影刀失败时的降级值
- <项目根>/data/frontend_scrape/latest.json —— 影刀最近一次抓取结果，影刀失败时的降级数据源（perf.py:1126 _read_frontend_fallback）
- 影刀 RPA 应用 UUID 0df955ab-ecbc-4b5d-a215-223f32c237c9（『读取沃尔玛店铺页』）及其 macOS 授权状态：必须在『我获取的应用』里已跑过至少一次，且路径固定 /Applications/影刀.app
- 两个 SKILL.md 的调度注册（walmart-kpi-daily 0 8 * * *、walmart-kpi-afternoon 0 14 * * *）需在新系统重新登记；notify.py 的 /tmp marker 不需要迁移

### 迁移建议

拆成三条 workflow（daily_kpi_collect / daily_problem_orders / daily_report_push），由 cli.py 串。核心改造是把飞书从「数据库」降级为「展示层」：新增 PG 表 ops.store_kpi_daily(store, data_date, 32 个字段, PK(store,data_date)) 与 ops.perf_problem_orders(全部 13 列 + first_seen_date, UNIQUE(sales_order_no, indicator, sub_category, tracking_no, item))，写库用 INSERT ... ON CONFLICT DO NOTHING 天然实现现有的「永久累积 + 保留首次发现日期」语义，first_seen_date 用 created_at；飞书改为从 PG 渲染刷新。这样能消掉当前最大的风险——merge_to_lark 的「读全表 → 内存去重 → 清空 → 分块重写」（problem_orders.py:606-691）和 rewrite_overview 的「写 199 行空白再写数据」（perf.py:914-919），中途崩溃就是整表数据丢失，且没有任何备份。

照搬（业务规则，别优化）：24h 窗口锚定中国时间 06:30（perf.py:100-101，改了要同步改调度时间）；上期回款严格 -14 天、不在 availableReconFiles 就填 0（perf.py:291-359，README 明写「不要改成找最近的可用日期」）；支付状态非 ACTIVE 强制 payout=0、payout<0 归 0、reserveToDate 取绝对值、「不押款」只在 ACTIVE 且 payout>=closing 时标（perf.py:243-268）；204 视为「该店无合规数据」而非错误（perf.py:177-180、problem_orders.py:179）；xlsx 解析里跳过首行 "Data current as of"、跳过以 "=" 开头的 SUM 公式行、sheet 名 == "Not Accountable" 即「⚪ 否 不计入绩效」（problem_orders.py:230-245）；8 个指标各自的字段映射与 emoji 前缀（problem_orders.py:74-83, 268-380，emoji 是下游 summary.py:302 的匹配依据，改了日报的物流分析会静默失效）。

重做：(1) 所有 Walmart 调用收归 api/_client.py。problem_orders.py:151 直接 httpx.get(url, headers=make_headers(...), proxy=proxy) 绕过共享客户端——它确实带了每店固定代理（不违反封号铁律），但绕过了 401 自动换 token、代理连接池剔除、统一日志；新 api 层需要一个「二进制响应」分支（不强解析 JSON）来承接 xlsx 下载，然后删掉这里手写的 _parse_retry_after 与重试。(2) 三份重复的 lark 封装 + LARK_IO_SHIM 分支 + 三处硬编码的列名数组（perf.py:131、problem_orders.py:85、summary.py:44/56 —— summary 的 KPI_COLUMNS 是 perf 的复制品，改一处忘另一处就静默错位）全部收进 registry/resources.py 的字段常量，token/sheet_id 同理。(3) 影刀部分按 plan.md 保持原样（仅 macOS），但把 latest.json 路径、影刀二进制路径、APP_UUID 登记进 registry/paths.py；衔接方式建议反转：现在是「先把总览写飞书 → 影刀读飞书拿 sellerId」（perf.py:987-1006），迁移后应从 PG 导出 sellerId 清单给影刀，让飞书写入变成纯展示，Phase 1b 那次半成品写入（卖家名/销售状态留空）就可以取消。(4) --dry-run 目前只有 summary.py 有；本模块无 feed/DELETE，dangerous=False，但整表重写这类操作建议给 --execute 保护或至少写库前落 ops.runs。

### 待确认问题

- walmart-kpi-afternoon/SKILL.md 的命令传了 `--no-yingdao --skip-summary`，但 fetch_walmart_performance.py:1175-1180 只识别 --no-yingdao，剩余参数一律当作店铺名过滤 → load_stores(['--skip-summary']) 返回空 → 打印『未找到店铺』并 sys.exit(1)。按代码推断下午任务应当是**每次都直接失败**的。需要确认生产日志：下午补刷到底有没有真正跑成功过？如果从没跑成功，A-H 列保护逻辑（merge_preserve_afternoon）实际可能从未在生产生效，迁移时不必按它的行为做兼容
- 影刀 RPA 应用（UUID 0df955ab-ecbc-4b5d-a215-223f32c237c9）的内部逻辑不在仓库里：它如何读飞书总览 sheet（用哪个身份、读 E 列还是全表）、如何决定抓哪些店、是否只抓 ACTIVE、写 latest.json 的原子性（脚本 :1108 已在防读到写一半的文件）——全部未知。迁移前需要导出/记录这个应用的流程定义，否则新系统无法复现衔接
- lark_io/sheets_registry.py:98 把 KPI 总览 sheet_id 记为 899f65，而脚本一律按 title '总览' 动态解析。两者是否一致未验证；注释还提到它与 ONLINE_STORE_STATUS 的 sheet_id 相同（不同 workbook），迁移进 registry 时容易张冠李戴
- 32 列表头里的 8 个阈值（90%/2%/99%/95%/6%/2%/6%/2%）只是列名文案，代码里没有任何超阈值告警逻辑。新系统是否要基于这些阈值做主动预警，需要跟业务确认
- 总览读写范围硬编码 A2:AF200（≤199 家店铺），当前约 50-63 家。扩店到 200 家时会静默截断——新系统是否直接改为按实际行数动态计算
- latest.json 当前样本只有 17 个店铺、sales_status 全是『可售』。销售状态的完整取值域（除脚本补的『不可售』外还有哪些）未知，影响新库的枚举设计
- 日报只发给单个 open_id ou_36c5f91668c42a735e7b9d4ae74eedc1，新系统是否要改成群/多人分发
- SRR report 的真实字段结构从未实测（店铺基本都 100% 回复率触发 204），迁移后如果有店铺跌破 100%，解析结果只会是一坨 json 字符串塞在备注里


<a id="order_audit"></a>
## 沃尔玛订单审核 → order_audit (#4)

### 模块职责

把 57 家沃尔玛店铺订单按 lastModifiedStartDate 增量拉到一张飞书电子表格（A:AD 自动列），再对【本次新增订单 + 上轮采集失败原因为 network/zip_switch_failed 的旧单】内联调 amazon-scraper-v3 采集对应亚马逊 ASIN（按收件邮编）的 buybox 价/运费/FBA-FBM/库存/配送天数/卖家/截图，据此跑四道审核（钓鱼地址与邮编黑名单、采购方匹配、限价、标题一致性，外加第五道配送时长）把结论写回 AE:AN。单进程一条流水线：拉单→钓鱼检测→整表合并写飞书→轮次去重推采集→轮询到终态→回填→更新 _meta。只输出「建议拒绝/待人工/✓ 通过」文本，永不调 Walmart API 真拒单（docs/审核服务架构设计.md:35）。

### 入口与触发

- 沃尔玛订单审核/订单同步.py:639 main() —— 唯一生产入口；参数 --days/--stores/--max-concurrent(默认10)/--timeout(默认60)/--dry-run/--no-collect；非 dry-run 先取 fcntl 单实例锁再 asyncio.run（订单同步.py:649-655）
- 沃尔玛订单审核/deploy/run_hourly.sh:16 —— launchd 包装脚本，硬编码 PY=/opt/homebrew/bin/python3，日志 logs/launchd/order_audit.YYYYMMDD.log，find -mtime +30 -delete 保留 30 天（:22）
- 沃尔玛订单审核/deploy/com.user.walmart.order_audit_hourly.plist:68-71 —— launchd StartCalendarInterval Minute=15（每小时第 15 分），RunAtLoad=false，plist 内注入 AUDIT_POLL_TIMEOUT=3600 / AUDIT_POLL_INTERVAL=20，路径硬编码 /Users/nextderboy/Projects/erpAPI/…
- 定时任务skill/walmart-daily-order-sync/SKILL.md:50-52 —— 第二套调度（AI 定时任务 skill，建议每天 13:30），命令 `cd 沃尔玛订单审核/ && env -u FEISHU_APP_ID -u FEISHU_APP_SECRET /opt/homebrew/bin/python3 订单同步.py`，并要求用 定时任务skill/notify.py start/done 发飞书简报
- 沃尔玛订单审核/审核服务.py:326 —— FastAPI :8901，已退出主流程（审核服务.py:4-7 注明 2026-06-20 退役），保留 POST /手动重审/{po_id}/{line_no}?batch_id= 与 /v3-callback、/health；其回填逻辑是内联化前的旧实现，与 审核回填.py 有分叉
- 沃尔玛订单审核/重排清图.py:60 main() —— 一次性运维脚本，--dry-run / --confirm 两态，无参数直接退出码 2（:66-70）
- 沃尔玛订单审核/test_采集回填.py —— 离线冒烟测试，monkeypatch 模块级函数，不连飞书与 V3

### 调度

双重调度并存，两条都跑同一个 订单同步.py：(1) launchd 每小时第 15 分钟（deploy/com.user.walmart.order_audit_hourly.plist:68-71 → run_hourly.sh:16）；(2) AI 定时任务 skill 每天 13:30（定时任务skill/walmart-daily-order-sync/SKILL.md 头部 description 与 :50-52 命令）。二者靠 /tmp/walmart_order_sync.lock 的 fcntl 排他锁互斥，后到者直接 exit 0（订单同步.py:624-655）。Mac 睡眠时 launchd 不触发，唤醒只补跑一次；靠窗口 1h 重叠 + 30d 兜底不丢单（plist 注释:31、README.md:235）。skill 那条还额外要求 `env -u FEISHU_APP_ID -u FEISHU_APP_SECRET` 清掉调度平台注入的错误飞书凭据（SKILL.md:46-51）。

### 数据存储

- 无任何本地状态文件/SQLite/PG —— 全部持久化在飞书（README.md:196 明示「无本地状态文件」）。状态即表格，这是本模块最大迁移特征
- 飞书电子表格 spreadsheet_token=YnUH…ea(token已脱敏,见旧仓库代码)，硬编码于 5 处：订单同步.py:68、飞书表.py:16、状态.py:20、钓鱼检测.py:26、采购方匹配.py:17（违反新仓库 registry 铁律）
- 增量游标：飞书 _meta 隐藏 sheet id=3OGVQk，每店铺一行 A:E = 店铺 / 上次同步时间(UTC) / 上次拉取数 / 上次错误 / 累计连续错误次数（状态.py:20-22；读范围写死 A1:E1000，状态.py:33）
- 订单主表 sheet id=980eaf：A:AD 同步列、AE:AJ 采集字段、AK 截图图片单元格、AL 采购方、AM 限价、AN 审核结果、AO+ 人工列（README.md:183-192）
- /tmp/walmart_order_sync.lock —— fcntl 排他锁文件（订单同步.py:624-636）
- 沃尔玛订单审核/logs/launchd/order_audit.YYYYMMDD.log + order_audit.launchd.boot.log（run_hourly.sh / plist:62-66）
- 店铺凭据：仓库根 店铺API.xlsx，经 walmart_client.load_stores 读出 name/client_id/client_secret/proxy（walmart_client.py:143-203）
- 飞书 secret：沃尔玛订单审核/deploy/secrets.env（gitignored，飞书客户端.py:29-43 在 import lark_io 前加载；FEISHU_APP_ID 内置默认 cli_a9561a4f8dfadcd2，飞书客户端.py:28）

### 飞书使用

- app_token(spreadsheet_token) = YnUH…ea(token已脱敏,见旧仓库代码)，全部为『电子表格 sheets』不是多维表格 bitable —— 按列字母/行号定位，不是按字段名，改列顺序会静默错位
- sheet 980eaf 订单主表：表头行 row1，数据从 row2；主键 = (B 沃尔玛订单号, C 行号)（飞书表.py:206-219）。列语义：D 下单时间 / H 收件电话 / I 地址1 / J 地址2 / M 邮编 / P SKU / Q 商品名 / R 数量 / S 单价 / T 运费 / AE 亚马逊标题 / AF 总价 / AG 配送方式 / AH 库存 / AI 配送时长 / AJ 卖家 / AK 截图 / AL 采购方 / AM 限价 / AN 审核结果
- sheet 3OGVQk = _meta 隐藏表（每店铺同步游标，状态.py）
- sheet ZLUqxi = 黑名单地址（表头在 row5，数据从 row6，A=律所、C=街道用于匹配；钓鱼检测.py:27,30-32）
- sheet NOn5x7 = 黑名单邮编（A 列，无表头；钓鱼检测.py:28）
- sheet OGBTUB = 采购方表（A 采购方 / B 配送方式 FBA|FBM / C 区间起 / D 区间止 / E 汇率 / F 是否启用；采购方匹配.py:4-6,47-58）
- 读写全部经 飞书客户端.调用() → lark_io.api(backend='http') tenant_access_token（飞书客户端.py:49-66）。用到的 OpenAPI：sheets/v2 values GET|PUT、sheets/v2 values_image POST（写 AK 图片，飞书表.py:345-360）、sheets/v2 dimension_range POST（扩行扩列）、sheets/v2 insert_dimension_range POST（顶部插行，飞书表.py:107-110）、sheets/v3 sheets/{id} GET（查 grid_properties 行列数）、sheets/v3 …/find POST（按 PO 定位行，飞书表.py:255-295）
- 列名字符串在代码里硬编码（钓鱼检测.构建列索引 需要 地址1/地址2/邮编/审核结果 四个中文表头，钓鱼检测.py:155）；找不到就跳过钓鱼检测（订单同步.py:563-565）——表头改名会静默失效，正是新仓库要求用字段常量的原因

### 沃尔玛端点

- GET {BASE_URL}/v3/orders —— 唯一调用的沃尔玛业务端点。首页参数 lastModifiedStartDate=since_iso、createdStartDate=179天前、limit=200；翻页直接把 meta.nextCursor 当 query string 拼接 f'{BASE_URL}/v3/orders{cursor}'（沃尔玛异步.py:69-83）。终止条件：无 cursor / cursor 含 hasMoreElements=false / 本页为空（:109-110）
- token 端点经 walmart_client.get_token（900s 进程内缓存）+ make_headers（沃尔玛异步.py:57-63）
- 不提交任何 feed、不调 items/prices/inventory —— 本模块对沃尔玛只读
- 【绕过共享客户端】沃尔玛异步.py:54 自建 httpx.AsyncClient(proxy=store['proxy'])，只复用 walmart_client 的 BASE_URL/get_token/make_headers，绕开了 walmart_client._request_ex 的 401 重取 token、429 退避（Retry-After / X-Next-Replenishment-Time 解析，walmart_client.py:274-398）与按代理池化的长连接（walmart_client.py:82-98）。代理仍是每店铺绑定的固定出口，未违反禁直连红线，但限速自适应完全缺失
- 对采集服务的直连：V3客户端.py 全部经仓库根 scraper_client（POST /api/upload、POST /api/batches/{batch_id}/prioritize、GET /api/batches/{name}/status、GET /api/batches/{name}/errors、GET /api/results?batch_id=&cursor=、GET /static/screenshots/…），默认 http://<SCRAPER_VPS_IP,见旧仓库>:8899，无鉴权、明文 HTTP

### 魔数与踩坑参数

- 订单同步.py:58 采集最长等待秒=AUDIT_POLL_TIMEOUT 默认 3600（单轮轮询上限；文件 docstring:22 仍写 900，已过期，以代码为准）
- 订单同步.py:59 采集轮询间隔秒=AUDIT_POLL_INTERVAL 默认 20
- 订单同步.py:60 采集最大轮次=AUDIT_MAX_ROUNDS 默认 6 —— 同一 ASIN 最多采几个不同邮编。最坏耗时 6 轮 × 3600s = 6h，会跨过多个 launchd 触发点（被锁跳过）
- 订单同步.py:61 采集状态错误上限=AUDIT_STATUS_ERR_LIMIT 默认 5 —— 连续 5 次拿不到 batch status 就提前收尾该轮（_轮询终态 订单同步.py:315-334）
- 订单同步.py:63-64 兜底天数=30、重叠小时=1 —— 增量窗口 since = max(last_sync - 1h, now - 30d)（计算每店铺窗口 :218-257）
- 订单同步.py:65 自动列数=30（A:AD 归脚本管，AE 起归审核/人工）
- 沃尔玛异步.py:76-83 createdStartDate 强制传 179 天前 + limit=200 —— 不传时 Walmart 默认 now-7d，会让 lastModifiedStartDate 形同虚设（docs/订单同步架构设计.md:11 记录为关键陷阱）
- 订单同步.py:643-644 --max-concurrent 默认 10，Round2 并发 = max(10//3,1)=3（订单同步.py:514）；单店铺 timeout 默认 60s（沃尔玛异步.py:47）
- 飞书表.py:18 每批行数=1000 —— 飞书 PUT body 实测 ~2MB 上限、超了报 90227（docs/订单同步架构设计.md:505）
- 飞书表.py:83 图片列索引=37（AK）；写全表分两段 A:AJ + AL:末列，跳过 AK（飞书表.py:100-102,147-168）
- 飞书表.py:19 电话列索引=7（H 列）—— 全 0 或空电话不覆盖存量真实号（_合并自动列 :36-42）
- 钓鱼检测.py:30-33 地址_数据起始行=6、地址_街道列=C、地址_律所列=A、街道最短长度=8（标准化后 <8 字符的黑名单条目跳过，防 “123 Main St” 误伤）
- 限价计算.py:16-17 利润安全比=0.75（曾是 0.85，2026-06-23 改）、美元汇率=6.8（写死的 USD→RMB 市场汇率）
- 商品一致性.py:11 相似度阈值=0.9（SequenceMatcher）
- 审核决策.py:22 配送时长上限=12（>=12 天即建议拒绝，保守口径，因为采集器只给整数天）
- V3客户端.py:38-39 HTTP_TIMEOUT=30、结果分页=200
- scraper_client.py:85-86 BASE_URL 解析顺序 SCRAPER_BASE_URL > V3_BASE > DMIT_SCRAPER_BASE > 默认 http://<SCRAPER_VPS_IP,见旧仓库>:8899；SCRAPER_HTTP_TIMEOUT 默认 30
- 采购方匹配.py:38 读采购方范围写死 A1:F500；钓鱼检测.py:63,79 黑名单范围写死 A6:C500 与 A1:A500 —— 数据超出即静默截断
- walmart_client.py:6 token 进程内缓存 900s；回填每单约 4 次飞书写（AE:AJ / AK / AL:AM / AN，审核回填.py:109-163），上百新单要注意飞书 QPS

### 防重/幂等语义

主键 = (B 沃尔玛订单号, C 行号)，即 orderLine 粒度。写飞书用「读全表→内存合并→整表 PUT 覆盖」的 upsert：存量行保持物理顺序、A:AD 用新数据覆盖、AE+ 保留旧值；新订单按下单时间倒序前置到顶部并由 写全表(插入行数=N) 在 row2 前物理插入等量行，使 AK 图片单元格随存量行一起下移（飞书表.合并新旧 :176-240、写全表 :86-115）。飞书已有但本次未拉到的行原样保留。采集侧幂等靠三层：①「轮次去重」——采集器 asin_data 按 ASIN 唯一存储，同 ASIN 并发采会互相覆盖，故每轮每个 ASIN 只提交一个邮编，本轮回填完再推下一轮（订单同步.采集回填 :373-474；scraper_client.asubmit_pairs 批内再对 ASIN 去重）；②回填前硬校验结果 zip_code 与请求邮编前 5 位一致，不符判为「结果未刷新」直接跳过、绝不回填旧数据（订单同步.py:350-353）；③轮询超时立刻停止后续轮次，本轮及剩余全部标「采集未完成」，避免并发采同一 ASIN 撞车（订单同步.py:445-453）。钓鱼检测本身幂等：命中则写、旧值含「钓鱼」但本次不命中则清空、其它审核结果不动（钓鱼检测.遍历检测 :165-197）。无任何「先落 pending 再调接口」的防重记录——重启后没有 pending 可对账。

### 危险操作

- 整表 PUT 覆盖订单主表（飞书表.写全表 :86-173）—— 一次写全部行 × 全部列（跳 AK）。两个实例并发跑会互相覆盖导致数据损坏，这正是 fcntl 锁存在的原因（订单同步.py:625-628 注释明写）。保护：只有 flock 一层，无 dry-run 拦截
- 顶部 insert_dimension_range 物理插行（飞书表.py:107-115）—— 插入失败即中止写入以免图文错位；插了但后续 PUT 失败会留下 N 行空白行
- 重排清图.py 清空全表 AK 截图 + 整表重排 —— 破坏性一次性运维，必须显式 --confirm，无 --confirm 无 --dry-run 直接退出码 2（:66-70）
- 钓鱼检测 遍历检测 会清空旧的钓鱼标记（钓鱼检测.py:192-194）——黑名单里删掉一条，历史行的钓鱼结论会被自动抹掉
- 写采集失败 会清空 AK 单元格（审核回填.py:171）
- --dry-run 只覆盖「不写飞书 / 不更新 _meta」（订单同步.py:536-538），采集提交阶段在 dry-run 下根本不会执行；但正常执行路径没有任何『先打印将做什么』的确认环节
- 对沃尔玛无写操作，无 feed、无 DELETE_ITEM、无清库存

### 事故教训与必须保留的行为

- 【createdStartDate 陷阱】不传时 Walmart 默认 now-7d，lastModifiedStartDate 形同虚设，必须同传 179 天前（沃尔玛异步.py:73-78，docs/订单同步架构设计.md:11）
- 【图文错位事故】旧版整表重排让 AK 图片焊死在物理行、与订单错位。根治方案是「存量保位 + 新单前置 + 顶部物理插行」，且 写全表 必须跳过 AK 列（飞书表.py:89,100-102；重排清图.py:5-9 记录了这次事故的清理）
- 【同 ASIN 并发串数据】采集器结果表按 ASIN 唯一存储，旧版「按邮编分批」会让同款不同邮编的订单互相串数据 —— 改为轮次去重（V3客户端.py:16-18、订单同步.py:374-378）
- 【电话被冲成 0000000000】Walmart 取消/状态更新会返回全 0 占位电话，覆盖会毁掉真实收件电话 —— 存量有效号码保护（飞书表.py:30-42，test_采集回填.py 测试合并保留旧电话）
- 【钓鱼标记不可覆盖】AN 一旦含「钓鱼」二字，采集回填与写采集失败都跳过不写（审核决策.是钓鱼标记 :86-88，审核回填.py:147,172）。复审需人工清空 AN
- 【提交 V3 失败不进重采白名单】可自动重采失败原因只有 network / zip_switch_failed（订单同步.py:66）。采集器宕机那一小时的新单会写「待人工（采集失败：提交 V3 失败）」（:442），此后永不自动重采，只能人工。同理「采集未完成(轮询超时)」「超最大轮次」「邮编不符」也都是死标记
- 【空 AN 孤儿单】若进程在写完飞书、回填前被杀，这批新单 AN 为空；下一轮它们已是存量行、不算新增，也不在重采白名单，将永远不被采集（订单同步.挑选自动重采订单 :298-312 只扫 AN 里的失败文案）
- 【_meta 只在最后更新】写飞书失败直接 return 1 不更新 _meta，下次同窗口重试（订单同步.py:577-582）；但 _meta 更新排在采集回填之后（:611），采集阶段挂掉 = 本轮 _meta 不落，窗口重复
- 【ASIN 从复合 SKU 提取】取第一个 B0[A-Z0-9]{8}，否则退化到整串 10 位裸串，都不满足跳过（订单同步._提取ASIN :263-273）。提取错就会回填到错的商品
- 【钓鱼双向 substring】单向匹配会被钓鱼者故意漏写 Suite 绕过，故改为双向；标准化后 <8 字符的条目两个方向都跳过防误伤（钓鱼检测.py:93-116，docs/审核服务架构设计.md:12-16）
- 【zip+4】订单邮编 60606-6771 必须识别为 60606，需要邮编专用标准化，不能用通用去标点标准化（钓鱼检测.邮编标准化 :47-52）
- 【数值列类型】R/S/T/AB 与 AH/AI 经 _数字化 尽量写成 int/float 便于飞书求和；邮编/订单号等含前导零的标识列绝不能过 _数字化（订单同步.py:107-124 注释）
- 【命名遗留混乱】多处注释/docstring 仍写「AM 审核结果」，实际审核结果是 AN（钓鱼检测.遍历检测 :166,173 变量名 AM_idx；审核服务.py 返回体 key 也叫 AM）。列位置以 表头名『审核结果』索引为准，兜底常量 自动列数+9=39（0-based，即 AN，订单同步.py:282-284）
- 【两套回填实现分叉】生产走 审核回填.py；审核服务.py 是内联化之前的旧实现（如库存/配送时长写成字符串而非数字，审核服务.py:176-177），手动重审会写出与主流程不一致的格式
- 【飞书 90227】PUT body >~2MB 报 request too large，故分批 1000 行（docs/订单同步架构设计.md:11,505）
- 【采集失败 vs 不可售必须区分】真失败走 /errors（captcha/404/timeout/blocked）→ 待人工；采到了但 buybox 为 N/A 要靠 current_price/stock_status 分成 不可售 / 无 Buy Box / 需入购物车 / 无价异常（审核回填.判可售状态 :61-83）

### 切换时必须迁移的状态

- _meta sheet(3OGVQk) 全部 57 行的 last_sync（每店铺增量水位线）—— 不搬会触发 30 天兜底全量重拉，代价大但不致命；搬错会漏单
- 订单主表 980eaf 的全部历史行，尤其 AE:AN 审核结果与 AK 截图图片单元格（图片单元格无法通过 values 读写迁移，只能重采或放弃）
- AN 列里现存的『待人工（采集失败：network / zip_switch_failed）』标记 —— 新系统若不识别同样文案，这批单会永久卡住不再重采（订单同步.py:66 可自动重采失败原因）
- AN 列里的钓鱼标记（含『钓鱼』二字）—— 新系统必须继续遵守『不可覆盖』语义（审核决策.py:86-88）
- AO+ 人工备注列（脚本永不触碰，README.md:192）
- 黑名单地址(ZLUqxi)/黑名单邮编(NOn5x7)/采购方表(OGBTUB) 三张配置表内容
- /tmp/walmart_order_sync.lock 不需要搬，但切换期必须保证新旧不并跑

### 迁移建议

照搬（业务规则，逐字保留常量与文案）：限价公式与 0.75/6.8（限价计算.py）、相似度阈值 0.9（商品一致性.py）、配送时长上限 12（审核决策.py:22）、审核决策 11 级优先级链与输出字符串（审核决策.综合判断 :32-83，AN 文案被下游人工和自动重采筛选双重依赖，改一个字就破坏 挑选自动重采订单 的正则 订单同步.py:289）、钓鱼双向 substring + 街道最短长度 8 + zip 前 5 位标准化（钓鱼检测.py）、采购方多候选取最低汇率（采购方匹配.py:80-87）、可售状态四态判定（审核回填.判可售状态）、createdStartDate=179d workaround、电话全 0 保护。

重做：①三条铁律相关——spreadsheet_token/sheet_id/中文表头全部上收 registry/resources.py，路径上收 registry/paths.py，/tmp 锁与日志目录改 DATA_ROOT；②沃尔玛异步.py 的自建 httpx client 必须换成 api/_client.py（拿回 429 退避、x-current-token-count 自适应、每店铺代理池化），api/orders.py 只做翻页与分批，窗口计算属于 workflow；③状态从飞书 _meta 迁到 PostgreSQL ops schema（per-store 水位线 + 连续错误次数），飞书表退化为纯人机界面；④订单行迁到 orders schema，用 (purchase_order_id, line_number) 做主键，用 SQL upsert 替掉「读全表→内存合并→整表 PUT」这套脆弱且 O(全表) 的写法，飞书只增量刷可见列；⑤补上新仓库要求的『防重状态先落库再调接口』——现在完全没有 pending 记录，采集提交/回填中断就丢，应在 ops 里记 (order_line, batch_name, state=pending|done|failed, reason)，重启时先查 V3 实际状态再决定补交，顺带解决「提交 V3 失败/空 AN 永不重采」两个洞；⑥AK 截图改存对象存储/本地并只在飞书放链接或按需嵌入，别再让整表写入绕着 AK 走。

拆成的新 workflow 建议：orders_sync（拉单 + 落库 + 刷飞书 A:AD）、orders_phishing_scan（黑名单检测，可独立跑、幂等）、orders_enrich（推采集 + 轮询 + 落 amazon 快照，dangerous=False 但要有 pending 表）、orders_audit_decide（纯计算 + 写 AE:AN，可对任意历史行重跑）。四者用数据库解耦后，采集器宕机不再造成永久死标记。

切换顺序（新旧严禁并跑）：先 launchctl unload 那个 plist 并停用 walmart-daily-order-sync skill（两套调度都要停，容易漏掉第二套）→ 把 _meta 的 57 行 last_sync 导入新库 → 起新调度。

### 待确认问题

- scraper_client 的 amazon-scraper-v3（http://<SCRAPER_VPS_IP,见旧仓库>:8899）本身是否也在迁移范围内？order_audit 对它是硬依赖，采集不可用时整条审核链只能产出死标记
- 提交批次 把 res.get('inserted',0) <= 0 判为失败（订单同步.py:438）——若采集器对已存在的 ASIN 返回 inserted=0，会把正常批次误判为提交失败并写死标记，需在采集器侧确认语义
- AK 图片单元格在飞书里无法通过 values API 读出，历史截图能否迁移？还是接受清空重采（重排清图.py 已有清空先例）
- 限价公式里的 6.8 是写死的市场汇率（限价计算.py:17），新系统是否需要可配置/取实时汇率
- 四道审核里『采购方表』OGBTUB 由谁维护、是否要迁到数据库
- 审核服务.py:8901 是否还有人在用手动重审？若保留，需与新回填逻辑对齐，否则会写出旧格式
- 订单同步 dry-run 只覆盖写飞书，采集阶段无 dry-run 语义；新仓库若把 orders_enrich 标 dangerous，需要定义『将对哪些 SKU 推采集』的预览输出


<a id="upc_generator"></a>
## 沃尔玛UPC生成器 → upc_generator (#5)

### 模块职责

批量"造"沃尔玛可用 UPC 并补充到飞书 UPC 池。四段流水线：① 用 secrets.SystemRandom 随机生成 12 位 UPC-A（首位落零售白名单 0/1/6/7/8/9，第 12 位 GS1 Mod10 校验位，拒绝 ≥4 位连续递增/递减段）；② 本地 SQLite（upc_history.db）做主键去重 + "与已有 UPC 数值距离 ≥1000"的相邻间隔过滤，入库标 pending；③ 多店并发调 GET /v3/items/walmart/search?upc= 做全站目录校验，回写 free/conflict/error；④ 把 free 且未推送的批量 append 到飞书上架表的 UPC 池子表（A=UPC，B 留空=未领）。注意：本模块只负责"生产"UPC，"领取/消费"语义完全不在本目录，而在 auto_listing/upc_pool.py + erp_listing_server/server/api_upc.py。模块状态：feature 1-10 冒烟通过但**从未上生产全量**（progress.md:6,30-34），没有任何调度。

### 入口与触发

- /workspace/erpapi/沃尔玛UPC生成器/cli.py:168-230 — 唯一 CLI。默认 `--count 10000` 会串起 生成→校验→推送 三段全跑；`--check-only` / `--push-only` / `--init-from-feishu` / `--stats` 为单点动作，执行完直接 return（cli.py:194-205）
- ⚠ 必须 cd 到 沃尔玛UPC生成器/ 再 `python3 cli.py`：全目录用扁平 import（cli.py:23-29 `from config import ...` / `import generator` / `import storage`，generator.py:18，storage.py:17），没有 __init__.py，不能 `python -m`，从别处调会 ImportError
- 库级入口：walmart_check.run_check(limit, store_filter, flush_every)（walmart_check.py:105）；feishu_push.import_seeds(dry_run)（feishu_push.py:55）；feishu_push.push_free_to_feishu(limit, dry_run)（feishu_push.py:141）
- /workspace/erpapi/沃尔玛UPC生成器/generator.py:172-173 — 直跑做自检 benchmark（1 万样本 Mod10/连续段/前缀分布断言），是唯一的"测试"
- /workspace/erpapi/沃尔玛UPC生成器/main.py:110-113 + ean13.py — 与流水线完全无关的遗留物：从 生成器.exe 逆向还原的 tkinter EAN-13 顺序号生成器（git commit d5237fb）。顺序递增、无去重、无校验，不要迁移
- 调度：**无**。没有 launchd plist、没有 cron、没有 scheduler 注册。全靠人手敲命令（progress.md:34 "上 cron / 加入日常运维流程" 仍是待办）

### 调度

none —— 没有 launchd plist、没有 cron、没有 scheduler 注册；全部人工命令行触发。progress.md:34 把"上 cron / 加入日常运维流程"列为待用户决策的下一步，至今未做。因此本模块是**唯一一条切换时不需要"先停旧调度"**的工作流。

### 数据存储

- SQLite 主库 upc_history.db —— 路径 config.py:8 `DB_PATH = PACKAGE_DIR/upc_history.db`（写死在代码目录内）。表 upc_history schema 见 storage.py:27-42：upc TEXT PRIMARY KEY / upc_int INTEGER / walmart_status（pending·free·conflict·error·unknown）/ walmart_info TEXT(JSON) / pushed_to_feishu INTEGER 0|1 / source（'generated'|'feishu_seed'）/ created_at / checked_at / pushed_at，三个索引 idx_upc_int·idx_status·idx_pushed。时间戳统一 UTC+8 字符串 'YYYY-MM-DD HH:MM:SS'（storage.py:20-24）
- ⚠ upc_history.db **不在 git 里**（`ls 沃尔玛UPC生成器/` 无该文件），只存在于运维那台 Mac 上。它是整个去重记忆的唯一载体，丢了等于所有历史 UPC 忘光
- output/ 目录在 import config 时被无条件 mkdir（config.py:27-28），实际没有任何代码往里写；README:48/191 说的 Excel 留档不存在
- logs/check_*.log、push_*.log（README:49,192-193）**是文档幻觉**：cli.py:33-37 只配了 logging.basicConfig 输出到 stderr，没有 FileHandler
- 飞书 UPC 池子表本身是真正的下游存储（见 feishu_usage）
- 消费侧（本模块之外，但同一份 UPC 状态）：auto_listing/state/upc_pool.lock（upc_pool.py:354-357，fcntl 跨进程锁）、auto_listing/state/upc_claimed_runtime.txt（upc_pool.py:363-367，本地实时声明簿，mtime>1h 自动作废）、erp_listing_server SQLite 表 upc_claimed(row_index PK, claimed_at, worker_id, task_id, upc)（server/db.py:69-75）

### 飞书使用

- 上架表 spreadsheet token `PDsR…Ph(token已脱敏,见旧仓库代码)`（硬编码 config.py:12），UPC 池子表 sheet_id `NxlS1J`（硬编码 config.py:13，upc_pool.py:17 也各写一份）。⚠ 历史坑：config.py 最初误填成"在线产品总表"的 token（progress.md:17-18）——迁 registry 时务必确认这是**上架表**不是在线产品总表
- 列结构按位置索引，没有表头概念：A=UPC（数字或字符串），B=使用标记（空=未领；非空即占用，内容不参与判断）。代码里全是 row[0]/row[1] 与 "A1:B{n}" 字面量（feishu_push.py:78,125-126；upc_pool.py:290-292,573）——新系统的"字段常量"在这里退化成列位置常量，registry 要登记的是 A/B 列语义
- 读范围：`_upc_range()` = A2:B{实际行数}，行数从 +workbook-info 动态取并按进程缓存，取不到则兜底 100000（upc_pool.py:76-101，config.py:14 也留了一份 UPC_RANGE_FALLBACK='A2:B100000' 但本模块没用）
- 读命令：feishu_push.import_seeds 用 `sheets +read`（feishu_push.py:64-69）。⚠ upc_pool.py:164 注明 2026-06-10 起 lark-cli **已废弃 +read**，upc_pool 改成了 +cells-get，但 feishu_push 没跟着改 → 种子导入这条路径大概率已经跑不通了
- 写命令：`sheets +append --range A1:B{len}`（feishu_push.py:115-138）。飞书 +append 自动追到末尾，range 只是形状声明，不会覆盖前 N 行
- 身份：feishu_push.py:36 `FEISHU_IDENTITY = "user"`（README:210 说是 bot 身份，**文档错**）。docs/feishu_sheets_registry.md:11 也确认 "UPC生成器 user(写 NxlS1J)"。迁移到 bot 前必须先对 NxlS1J 做 bot 写探针（docs/feishu_migration_plan.md:291）
- 调用层：默认走 lark_io.run_cli(identity=user, timeout=60) shim，`LARK_IO_SHIM=0` 回退 auto_listing.upc_pool._run_cli（feishu_push.py:39-50）。即 UPC 生成器**反向依赖了 auto_listing 包**（feishu_push.py:18-26、walmart_check.py:21-34 都靠 sys.path.insert 把 PROJECT_ROOT 塞进去）

### 沃尔玛端点

- GET /v3/items/walmart/search?upc=<12位>（Item Search / 全站目录查询）—— 唯一调用的沃尔玛端点。URL 定义 upc_audit.py:49，调用 upc_audit.py:105-149，本模块只是复用 check_one_upc（walmart_check.py:26-30,80）
- **没有绕过共享客户端**：走 auto_listing.upc_audit.check_one_upc → safe_get_ex(url, token, client_id, store['proxy'])（upc_audit.py:118-126），token/代理来自 walmart_client.get_token/load_stores（walmart_check.py:32,55,70）。每店固定出口代理这条命保住了
- 结果归一（upc_audit.py:129-149）：网络失败→error{reason:network}；2xx 且 body 空或 items 空→free；2xx 且 items 非空→conflict{matched:{itemId,title,brand,productType,isMarketPlaceItem,price}, totalMatches}；其它 HTTP→error{reason:http_<code>}
- 配额：refdata/walmart_rate_limits.tsv 第 92 行 —— Item Search 200/min per client_id，**独立桶**。⚠ README:201 和 README:227 声称它与 /v3/items/catalog/search、/v3/items/associations 共享 200/min —— **是错的**，共享的是 catalog/search 与 associations 两者之间（tsv 第 83-86 行），walmart/search 自成一桶。别把这个错误结论抄进新文档
- 本端节流 RateLimit(180,60)（upc_audit.py:53），响应头 x-current-token-count / X-Next-Replenishment-Time 由 limiter.update_from_response 自动消费（upc_audit.py:127）
- **不涉及任何 feed**：本模块不提交 PRICE_AND_PROMOTION / MP_ITEM / inventory，也不做 DELETE_ITEM

### 魔数与踩坑参数

- ADJACENT_MIN_GAP = 1000（config.py:17，用在 cli.py:103 → storage.py:102-144）：候选 UPC 若与库中任一 UPC、或与本批已保留候选的数值距离在 ±1000 内就丢弃，目的是抹掉"批量生成"痕迹。实现细节两处要注意：(a) 判据是 `existing[j] <= ival + min_gap`（storage.py:134-136），即拒绝距离 ≤1000，比 docstring 写的 "<" 严一格；(b) 每轮都 `SELECT upc_int FROM upc_history ORDER BY upc_int` 全表拉进内存（storage.py:115），10 万行没事，迁到 PG 千万级必炸
- CONSECUTIVE_RUN_LIMIT = 4（config.py:18，算法 generator.py:74-102）：拒绝长度 ≥4 的等差 ±1 段，且 9→0 / 0→9 按取模也算连续（generator.py:84-88）。过滤跑两遍：先查 11 位本体，算出校验位后再查完整 12 位（generator.py:117,122）
- GEN_RETRY_MULTIPLIER = 3（config.py:19）：注释说是"总尝试次数上限"，实际在 cli.py:86-88 当成**主循环最大轮数**用。3 轮凑不够 target 时 cmd_generate 返回 rc=1，cli.py:216-217 直接中止整条流水线（不会带着少量 UPC 继续校验/推送）
- GEN_OVERSAMPLE_RATIO = 1.5（config.py:20）：每轮候选量 = max(还差数 × 1.5, 100)（cli.py:91）
- generate_candidates 内部防卡死上限 max(n*5, 1000) 次尝试（generator.py:135）——达到上限就静默返回不足量的集合，不报错
- SQLITE_BATCH_SIZE = 500（config.py:23）：executemany 分片 + `IN (?,?,...)` 占位符分片（storage.py:66,82,169,184）。后者是硬约束——SQLite 默认变量上限 999，超了会直接报错
- FEISHU_APPEND_BATCH = 5000（config.py:24 → feishu_push.py:163）：飞书单次写入行数硬上限。注意 feature_list.json 和 README:33 里还残留"每批 500 行"的旧说法，以代码为准
- feishu_push.py:96 种子导入把 pending 改 unknown 时**又写死了一个 500**，没有引用 SQLITE_BATCH_SIZE
- TOKEN_REFRESH_SEC = 800（walmart_check.py:38）：沃尔玛 token 900s 有效，提前 100s 刷。同一常量在 upc_audit.py:55 重复定义了一份
- QUEUE_TIMEOUT = 3（walmart_check.py:39,64）：worker 连续 3s 取不到任务就退出。本模块队列是一次性预灌满的所以安全，但改成流式喂任务会静默杀掉 worker
- FLUSH_EVERY = 200 + 主线程 `time.sleep(5)` 轮询 + `tick % 6` 即每 30s 打进度（walmart_check.py:40,160,163）：校验结果先攒在内存 list 里，攒够 200 条或每 5s 落一次库。SIGKILL 会丢这个窗口内的结果 → 那些 UPC 留在 pending，下次重跑（安全但白烧配额）
- RateLimit(180, 60)（upc_audit.py:53）：官方 200/min，留 10% 余量。per store × per endpoint 一个桶（walmart_check.py:125），所以 N 个店总吞吐 = 180N/min
- 单次 search 请求 timeout=30s（upc_audit.py:124）
- ThreadPoolExecutor(max_workers=len(stores))（walmart_check.py:150）：一店一线程，无上限。店多了就是几十条线程同时打飞书/沃尔玛
- sqlite3.connect(..., timeout=30.0)（storage.py:49）；每次 flush 都新开一个连接（walmart_check.py:145），且每次 connect 都重跑一遍 SCHEMA executescript（storage.py:51）
- lark-cli 子进程 timeout=60s（feishu_push.py:48、upc_pool.py:52）。注意 upc_pool 的 _run_cli 走 shim 时带 retries=4（upc_pool.py:45），而本模块自己的 _lark（feishu_push.py:46-50）**没有 retries**，飞书抖一下这批就整批失败
- 消费侧（迁移 UPC 池必须一起照搬的数字）：CHUNK_ROWS=5000 / BATCH_GET_RANGES=4 / READ_WORKERS=4（upc_pool.py:140-142，是撞飞书 90221 "data exceeded 10485760 bytes" 后定的）；批量标记 BATCH=4000（upc_pool.py:486,626，飞书真实上限 5000 行/次、QPS 100/s）；90217/90235 指数退避 1s/2s/4s（upc_pool.py:514-515, 209-210）；upc_lock timeout=600s（upc_pool.py:408）；本地声明簿 TTL 3600s（upc_pool.py:367）；server claim TTL 7200s（api_upc.py:34）；worker 提交 free 快照 `max(needed*10, 5000)`（main.py:1242-1246，2x 不够会导致 server 返回 0 个分配）

### 防重/幂等语义

分三层，都是"状态先落库、失败就重来"的弱幂等，没有真正的事务：
(1) 生成层：算法内 set 去重（generator.py:127-140）→ SQLite 主键 + exists_batch 差集（storage.py:77-90，cli.py:101-102）→ 相邻间隔过滤（storage.py:102-144）→ `INSERT OR IGNORE`（storage.py:69）。同一 UPC 永远只有一行。⚠ insert_batch 靠"插入前后 COUNT(*) 差值"算新增条数（storage.py:65,73-74），并发下这个返回值不可信，且每次调用两次全表 COUNT。
(2) 校验层：只消费 walmart_status='pending' 的行（storage.py:193-198）。中断/崩溃后重跑自动捡起未落库的 pending，因为调的是幂等 GET，重复校验只损失配额不损失正确性（feature_list #5）。error 状态也会在下次 --check-only 时被重新拉起吗？——**不会**：list_pending 只查 pending，error 行永远不会自动重试，README:92 声称"下次 --check-only 自动重跑捡起"是错的，需要人工把 error 改回 pending。
(3) 推送层：只推 `walmart_status='free' AND pushed_to_feishu=0`（storage.py:201-210）。刻意采用"先写飞书、成功才更新 SQLite"的顺序（feishu_push.py:165-173）——与新项目"防重状态先落库再调接口"的铁律**相反**。它保证的是"绝不漏推"，代价是"可能重复推"：飞书 append 实际成功但返回超时/进程被杀 → SQLite 没标 pushed → 下次 --push-only 再 append 一遍 → 池里出现重复 UPC 行。README:240 声称这个设计"不会出现状态漂移"，只说对了一半。
种子导入的幂等：import_seeds 靠 INSERT OR IGNORE + 一条 `UPDATE ... WHERE source='feishu_seed' AND walmart_status='pending'`（feishu_push.py:99-104）把新种子从 pending 改 unknown，避免它们被 walmart_check 白白校验一遍。重复跑 --init-from-feishu 是安全的。

### 危险操作

- **向生产飞书 UPC 池 append 行**（feishu_push.py:115-138 → 池子 NxlS1J）：不可撤销，写进去的号立刻可能被 auto_listing 领走上架。防护只有 `--dry-run`，而且**默认关闭**——`python3 cli.py`（不带任何参数）= 生成 10000 个 + 烧 10000 次沃尔玛 Item Search 配额（180/min 下约 1 小时）+ 实写生产池（cli.py:174,215-223）。没有 dangerous=True 之类的强制门禁，与新项目"危险工作流默认 dry-run，真跑必须 --execute"的铁律正好相反
- **大额配额消耗**：一次全量校验 = 1 万次 Item Search。跑的时候不能同时跑别的 Item Search 类脚本（同 client_id 共享 200/min）
- --init-from-feishu：只读飞书 + 写本地 SQLite，本身不危险，但一次拉 94k~126k 行，读侧压力大
- 历史事故（README:249，2026-05-25）：旧版 make_random_upc 用 randint(1,9) 选首位，约 44% 落在 GS1 受限前缀 2/3/4/5，沃尔玛提交报 EXT_DATA_ERROR_54514906640101 拒收。**6,665 个受限前缀 UPC 已经推进生产池**，事后在 SQLite 里改写成 conflict/gs1_restricted_prefix，并在 upc_pool.list_unused/claim_upc 里加了首位白名单过滤兜底（upc_pool.py:126-133,292,573）
- **不涉及** feed 提交 / DELETE_ITEM / 清库存

### 事故教训与必须保留的行为

- **写飞书时把 UPC 转成了 int**：`values = [[int(u), ""] for u in upcs]`（feishu_push.py:125）。RETAIL_SAFE_PREFIXES 含 0（generator.py:29），所以约 1/6 的号是 0 开头，写进飞书就变 11 位丢前导零。读回来时 _normalize_upc 会 zfill(12) 补回（upc_pool.py:110-123），所以历史上没炸；但 docs/feishu_migration_plan.md:359 已把这条列为迁移风险项。新系统必须按字符串写
- **feature_list.json #3 的验收标准写着"make_random_upc 返回首位非 0"**，与实际白名单含 0 矛盾。是 2026-05-25 修前缀事故时没同步的陈旧断言，别当规格用
- **README 有三处与代码不符**，抄文档会踩：(a) README:210 说 lark-cli 是 bot 身份，实际 identity='user'（feishu_push.py:36）；(b) README:201/227 说 walmart/search 与 catalog/search 共享 200/min，实际独立桶（refdata tsv:83-92）；(c) README:92 说 error 状态下次自动重试，实际 list_pending 只查 pending（storage.py:195），error 是死状态
- **feishu_push 用的 `sheets +read` 已被 lark-cli 废弃**（upc_pool.py:164 的 2026-06-10 注释），upc_pool 早改成 +cells-get 了，本模块没跟。种子导入路径很可能已经是坏的——迁移前别假设它能跑
- **全站校验是时点快照，没有有效期**：今天 free 的 UPC 明天可能被别家绑定，代码里没有 TTL、没有推送前复检。free → 推池 → 躺池里几个月 → 领取上架时才发现冲突（走 upc_pool.mark_conflict，upc_pool.py:806-812）
- **推送前不查池子里是否已存在**：只信 SQLite 的 pushed_to_feishu 标记。SQLite 丢了或被重建，就会往池里灌重复 UPC 行；池里同一个 UPC 占两行 = 可被领两次 = 两个 SKU 撞同一 UPC
- **所有 worker 取 token 都失败时会静默无操作**：_worker 拿不到 token 就 return 空 counters（walmart_check.py:55-58），队列没人消费，主循环发现 futures 都 done 直接收尾，run_check 返回 total=len(pending) 但 free/conflict/error 全 0，pending 一条没动。CLI 打印看起来像"跑完了"
- **flush 用的是每次新建 SQLite 连接**（walmart_check.py:145），并且每次 connect 都重跑一遍 executescript(SCHEMA)（storage.py:51）——迁 PG 时这套 per-flush 连接模式要重写成连接池/单连接
- **目录名是中文、import 是扁平的**：`沃尔玛UPC生成器` 不是合法 Python 包名，两个文件靠 `sys.path.insert(0, PROJECT_ROOT)` 手动接线（feishu_push.py:18-20、walmart_check.py:21-23）才能 import auto_listing/walmart_client。这也意味着本模块与 auto_listing 是硬耦合，迁移时 check_one_upc / _normalize_upc / _upc_range 三个函数要一并搬或重写
- **main.py + ean13.py 是无关遗留物**：逆向 exe 还原的 tkinter EAN-13 顺序号生成器（顺序自增、无去重、无沃尔玛校验），与流水线零交集。别误迁，也别把 ean13.calculate_ean13 当成 UPC 校验位算法用（EAN-13 与 UPC-A 的奇偶权重从相反方向数）
- **"领取即永不释放"的真实语义**（在 auto_listing/upc_pool.py，不在本目录）：领取 = 往 B 列写 `已领 | ASIN | 店铺 | 时间`（upc_pool.py:447-453,456-549），此后 list_unused / claim_upc 判定条件只是"B 列非空"（upc_pool.py:292,573），标记内容完全不参与判断。升级为 `已用 | ...`（mark_used，upc_pool.py:691-698）或 `冲突-... `（mark_conflict，upc_pool.py:806-812）只是换文案。**唯一的释放路径**是 unmark_used_batch → B 列写空串（upc_pool.py:701-803,815-819），只在 auto_listing/main.py:1511-1527 被调用，且只对三类行生效：提交前就失败、已回滚、沃尔玛明确拒收。**verify 返回 Unknown 的一律不回收**（main.py:1495-1502,1512）——设计原则是"宁可永久烧掉一个 UPC，也不冒重复使用的风险"。这就是"领取即永不释放"的准确表述：默认永久占用，只有在"沃尔玛高置信没收到"时才归还
- **两层 claim 的 TTL 不一致会漏号**：server 端 _claimed 簿 TTL 7200s 到期自动 purge（api_upc.py:34,52-57），本地 runtime 簿 TTL 3600s（upc_pool.py:367），但**这两层过期都不会去清飞书 B 列**。也就是说 server 认为号已释放、飞书里那行仍是"已领"永久占用。task 崩在 mark_claimed_batch 之后、mark_used/unmark 之前，那个 UPC 就永久卡死。这是池子里"已领"行只增不减的主要来源
- **mark_claimed_batch 的"降级逐条"分支会吞异常**（upc_pool.py:546-549 注释"server 端已记账，可忽略"）：飞书没标上但 server 已记账，重启后 server 簿一清，这行就会被重新分配 → 同一 UPC 两次上架

### 切换时必须迁移的状态

- upc_history.db 全表（约 104,688 行：94,638 条 source='feishu_seed' + ~10,050 条 source='generated'，见 progress.md:28）。这是**唯一**的"这个 UPC 造过没"记忆，也是相邻间隔过滤的比对基准，不搬 = 新系统可能重造已在池子里的号
- 其中 6,665 条 2026-05-25 事故后被改写成 walmart_status='conflict' + walmart_info.reason='gs1_restricted_prefix' 的记录（README:249）——这些是"已经推到飞书池但不能用"的黑名单，必须原样带过去
- pushed_to_feishu / pushed_at 两列：决定哪些 free UPC 已经写进飞书池。丢了会重复 append 同一批 UPC 到池子 → 池里出现重复 UPC 行 → 两个 row_index 各被领一次 → 同一 UPC 上两个 SKU，沃尔玛必冲突
- 飞书 UPC 池子表 NxlS1J 全量（126k+ 行，upc_pool.py:137）A/B 两列。B 列非空即"已被占用"，是领取状态的事实来源；导入 PG 时必须逐行保留 row_index（row_index 是所有 mark/unmark 的主键，upc_pool.py:822-830）
- 如果同时接管消费侧：auto_listing/state/upc_claimed_runtime.txt、upc_pool.lock，以及 erp_listing_server 的 upc_claimed 表（server/db.py:69-75）。这三个都是短 TTL 的在途防抢占状态，切换时最好等它们自然清空而不是搬

### 迁移建议

迁移顺位 #5，属于"低风险、可整段重写"的模块——它没有调度、没上过生产、不碰 feed、唯一破坏性动作是往飞书池 append 行。建议拆成两条独立 workflow，不要照搬 cli.py 的三段串联。

【照搬（逐字节复制，别自己重推）】
1. generator.py 的三个纯函数：calc_check_digit / is_valid_upc / has_consecutive_run / is_retail_safe_upc + RETAIL_SAFE_PREFIXES（generator.py:29-124）。Mod10 算法和 9→0 取模连续判定已被 1 万样本自检覆盖，前缀白名单是 6,665 个 UPC 的血换来的。放 services/upc_codes.py，纯函数无副作用。
2. 四个魔数照抄进 registry 常量：ADJACENT_MIN_GAP=1000 / CONSECUTIVE_RUN_LIMIT=4 / GEN_RETRY_MULTIPLIER=3 / GEN_OVERSAMPLE_RATIO=1.5，并把它们的"为什么"写进注释。
3. RateLimit(180,60) 与 Item Search 200/min 独立桶的事实（注意别抄 README 的错误说法）。

【重做】
1. upc_history.db → PG `catalog.upc_registry`（upc CHAR(12) PK, upc_int BIGINT, status, info JSONB, source, pushed_at, checked_at, created_at + upc_int/status 索引），走 registry/db.py 唯一连接入口。相邻间隔过滤别再全表拉进内存，改成一条 `WHERE upc_int BETWEEN %s AND %s` 的 EXISTS 或用 btree 范围查（PG 完全扛得住），生成侧改成候选批一次 anti-join。
2. 推送顺序倒过来符合铁律：先把该批标 pending_push 落库 → 调飞书 append → 成功改 pushed。程序启动时所有 pending_push 先去飞书池反查该 UPC 是否已存在再决定补推——这正好也修掉旧版"可能重复 append"的缺陷。
3. 写飞书强制 str，禁止 int(u)（旧 bug 位置 feishu_push.py:125）。
4. 飞书调用走新仓 api/feishu.py，不要再 sys.path.insert 反向依赖 auto_listing；_normalize_upc（zfill 到 12 位）的逻辑要复刻进 api/feishu.py 的读侧。
5. 校验并发：run_check 的"内存 buffer + 每 5s flush"改成每批 N 条直接落库；补上"所有 worker 取 token 失败 → 抛错而不是静默返回 0"。error 状态要有明确的重试入口（旧版没有）。
6. `sheets +read` 换成新 API（旧的已废弃）。

【对应新 workflow】
- `workflows/upc_generate.py`：生成 + 入库，纯本地，不危险，可以随便跑。
- `workflows/upc_verify.py`：消费 pending 跑 Item Search，只花配额不破坏数据，dangerous=False 但要在 cli 里提示预计耗时/配额。
- `workflows/upc_push_pool.py`：**必须 dangerous=True**，默认 dry-run 打印"将向池子 append 哪 N 个 UPC"，加 --execute 才真写。旧版默认就实写是最大的规范违背。

【切换步骤】
本模块没有旧调度要停，所以只需：① 把 upc_history.db 全表（含 6,665 条 gs1_restricted_prefix 黑名单和 pushed_to_feishu 标记）导入 PG；② 把飞书池 NxlS1J 全量（含 row_index 与 B 列原文）快照进 PG 作为对账基准；③ 用 --dry-run 跑一轮 push，人眼比对候选集与 SQLite pushed 标记一致后再 --execute。

【明确不迁】main.py + ean13.py（逆向 exe 的 tkinter EAN-13 顺序号工具，与业务无关）。

【范围提醒】"领取即永不释放"的实现整个在 auto_listing/upc_pool.py + erp_listing_server/server/api_upc.py，属于 auto_listing 那条工作流的迁移范围。但本模块与它共用同一张飞书表和同一套前缀白名单，两边迁移的时间差里必须保证白名单过滤两边都在——否则受限前缀号会重新流入分配。

### 待确认问题

- upc_history.db 现在实际存在吗、有多少行？progress.md:28 记的是 2026-05-21 冒烟时的 104,688 行，此后三个月没有 session log。需要在那台 Mac 上跑 `cli.py --stats` 拿到真实基线，否则不知道要迁多少
- 2026-05-25 事故里那 6,665 个受限前缀 UPC，在**飞书池**里是什么状态？SQLite 改成了 conflict，但 README:249 只说 upc_pool 加了读取时过滤，没说是否回头把 B 列标掉。如果 B 列还是空的，新系统若不复刻首位白名单过滤就会重新分配它们
- feishu_push 的 `sheets +read` 到底还能不能跑？需要实测一次 --init-from-feishu --dry-run 确认
- free 状态该不该有有效期？新系统要不要在"领取时"再做一次 Item Search 复检（多花 1 次配额换掉一次上架失败）？这是产品决策
- UPC 池的"领取"要不要一起迁进 walmart_data？如果迁，飞书池就从事实来源退化成人机界面，row_index 主键要换成 upc 主键 + 状态机（unclaimed/claimed/used/conflict/burned），锁从 fcntl+HTTP server 换成 PG 行锁/SELECT FOR UPDATE。这会顺带解决上面所有 TTL 不一致和漏号问题，但改动面超出本模块
- 飞书身份统一到 bot 的写探针（docs/feishu_migration_plan.md:291-292）做了没有？NxlS1J 是 user 苏里创建的历史表，可能根本加不了 bot 协作者
- 池子还剩多少可用号、日均消耗多少?——决定这条工作流的调度频率（目前无调度）


<a id="maintenance"></a>
## 沃尔玛商品维护 → maintenance (#6, 危险)

### 模块职责

把飞书「在线产品总表」里已经算好的三类维护结果（新标题 / 新价格 / 新库存）搬到 Walmart。三段式流水线：sync_lark.py 全量拉飞书表 → 按触发规则解析成 MaintInput → 全量替换 SQLite inputs 表；submit.py 从 inputs 读 pending，按店分组、8 并发跨店提交（单店内 title→price→inventory 串行），小批量走同步 PUT、大批量走 feed，同时把「实际上传值 + feedId」写进飞书当日「维护记录_YYYY-MM-DD」sheet；poll_yesterday.py 次日拉 D-1 所有 feed 的 itemIngestionStatus 回填 SQLite 与 sheet 结果列。含一条独立业务规则：配置表里「库存特殊要求=0」的 stockzero 店整店强制清库存、跳过标题与价格。

### 入口与触发

- /workspace/erpapi/沃尔玛商品维护/sync_lark.py:137 main() — 飞书→SQLite，参数 --workers(默认2) --page-rows(默认10000) --range；幂等，可安全重跑
- /workspace/erpapi/沃尔玛商品维护/submit.py:185 main() — SQLite→Walmart，参数 --execute --confirm-zeroing --stores --only{title|price|inventory} --max-workers(默认8)；非幂等
- /workspace/erpapi/沃尔玛商品维护/poll_yesterday.py:122 main() — 参数 --date(默认D-1) --batch --max-workers(默认8) --per-feed-timeout(默认180) --export-failures --include-all；只读 Walmart
- /workspace/erpapi/沃尔玛商品维护/test_sandbox_d052.py:139 main() — 名字叫 sandbox 但 load_stores() 读的是生产 店铺API.xlsx，--send title|price|inventory [--sync] 会真改 D052张凤霞/B01EC5DD4S 的线上数据；只有不带 --send 时才是 dry-run
- /workspace/erpapi/tools/backfill_daily_sheet_20260611.py — 一次性救场脚本，2026-06-11 sheet 写挂后从 SQLite 重建 daily rows 补写；不迁移，但其存在本身是事故证据
- 调度入口：/workspace/erpapi/定时任务skill/walmart-maintenance-all-stores/SKILL.md — 由 AI agent 按 SKILL.md 顺序执行 poll→sync→submit，不是 cron 也不是 launchd

### 调度

生产由「Claude Code / agent scheduled task」按 SKILL.md 执行，不是 cron 也不是 launchd。当前登记：walmart-maintenance-all-stores，每天 12:00（/workspace/erpapi/定时任务skill/README.md 调度登记表，对齐 2026-06-17 hermes 现状；cron 表达式 `0 12 * * *` 仅作文档建议）。单次任务内按序三步（SKILL.md:50-72）：① poll_yesterday.py（只读，报错也不中止）② sync_lark.py（重试 2-3 次仍失败则整轮中止）③ submit.py --execute --confirm-zeroing（全店，不再带 --stores）。历史沿革：原名 walmart-maintenance-a116-trial，灰度期只跑单店（A116林世强 → A093陈兴勇），时间 14:00，试运行结束后改全店并挪到 12:00（定时任务skill/README.md 命名说明段）。任务首尾各调一次 定时任务skill/notify.py start|done 发飞书简报，与调度平台自带投递无关。

### 数据存储

- SQLite /workspace/erpapi/沃尔玛商品维护/maintenance.db（DB_PATH 定义在 walmart_maintenance_common.py:691，写死在模块同目录；.gitignore 忽略，生产机实际路径 /Users/nextderboy/Projects/erpAPI/沃尔玛商品维护/maintenance.db）
- 表 inputs（walmart_maintenance_common.py:704-723）：飞书表镜像，UNIQUE(store_name, sku)，每次 sync 走 DELETE FROM inputs + executemany INSERT 全量替换（upsert_inputs at :757-782，函数名叫 upsert 实为 replace）。字段 trigger_title/trigger_price/trigger_inventory/inventory_force_zero 为 0/1；index idx_inputs_store
- 表 submissions（:725-741）：提交历史，累积不清空。feed_type 取值 MP_MAINTENANCE|price|inventory|sync_put_price|sync_put_inventory；status 取值 submitted|processing|processed|error；sync 路径 walmart_feed_id 为 NULL（poll 靠 `walmart_feed_id IS NOT NULL` 过滤，poll_yesterday.py:141-142）；index idx_subs_date(submitted_date, status)
- 表 feed_items（:743-752）：PRIMARY KEY(submission_id, sku)，ingestion_status 取值 SUCCESS|DATA_ERROR|SYSTEM_ERROR|NULL(feed 路径待 poll)|SYNC_ERROR:<status>(sync 路径)
- payload 归档目录 沃尔玛商品维护/payloads/YYYY-MM-DD/<batch_id>/<store>_<feed_type>_chunk<N>.json（save_payload at :785-795，同样 .gitignore）；文件名对 store 名做了 /\\:*?"<>| 剔除；sync 路径不落盘
- 飞书当日 sheet「维护记录_YYYY-MM-DD」本身就是一份业务状态：poll_yesterday 回填结果列时并不重读 SQLite 的 C/F/I 列，依赖 sheet 里 submit 当天写的值还在
- 失败明细导出 .xlsx（poll_yesterday.py:309-331，需要 pandas，缺则静默跳过）

### 飞书使用

- 主表「在线产品总表」spreadsheet_token=MO2e…mI(token已脱敏,见旧仓库代码), sheet_id=e7834a（walmart_maintenance_common.py:47-48）。读 A:T 20 列，142,680 行量级。0-indexed 列映射 COL dict 在 :58-79
- 主表关键列语义：A store / B sku / D upc / G productType / I availToSellQty / J publishedStatus / M 处理后amz标题 / N 相似度 / P walmart价格 / Q 更新价格 / R 库存 / S 更新库存
- 配置表「综合数据源 / 定价和上下架」token=E1p9…Kh(token已脱敏,见旧仓库代码), sheet_id=2FJ2Np（:51-52）。A 列店铺名 + J 列「库存特殊要求」=="0" → stockzero 店；load_stockzero_stores() at :477-500
- 当日记录 sheet「维护记录_YYYY-MM-DD」，与主表同一 spreadsheet（MO2e…mI(token已脱敏,见旧仓库代码)）不同 sheet_id；ensure_daily_sheet(:515-555) 按需创建
- 当日 sheet 11 列 DAILY_HEADER（:430-435）：store / sku / 更新标题 / 更新标题feedId / 更新标题结果 / 更新价格 / 更新价格feedId / 更新价格结果 / 更新库存 / 更新库存feedId / 更新库存结果
- 新 sheet 插入位置 insert_index = 非「维护记录_」前缀 sheet 的数量（:532-533）——曾经用 --index 0 把当日 sheet 排到了在线产品总表前面（设计方案 §9）
- 读写全部经 lark-cli（Bot 身份）子命令 sheets +info / +read / +create-sheet / +write / +append，封装在 _run_lark(:446-474)；默认经 LARK_IO_SHIM 转发 lark_io.run_cli(identity="bot")
- upsert 语义（upsert_daily_rows :580-676）：先 +read 全表按 (store,sku) 建行号索引 → 命中的合并连续行号成块 +write 覆盖，未命中的 +append 追加。_take()(:679-684) 保证 None/空串不覆盖原值——这是 submit 写值列、poll 写结果列能共存的关键
- 当日 sheet 写入严格串行：所有 worker 只往内存 daily_buffer 攒行，main 里一次性 upsert（submit.py:286-291），设计上就是避免飞书后端并发写竞争

### 沃尔玛端点

- POST /v3/feeds?feedType=MP_MAINTENANCE — 标题维护，MPItemFeedHeader(businessUnit=WALMART_US, locale=en, version=5.0.20260304-22_45_32-api) + MPItem[{Orderable{sku, productIdentifiers{productId,productIdType:UPC}}, Visible{<productType>:{productName}}}]。build_mp_maintenance_payload at walmart_maintenance_common.py:242-272
- POST /v3/feeds?feedType=price — PriceFeed v1.7，顶层直接 {PriceHeader:{version}, Price:[{sku, pricing:[{currentPrice:{currency:USD,amount}, currentPriceType:BASE}]}]}，无外层包装。builder :275-286
- POST /v3/feeds?feedType=inventory — InventoryFeed v1.4，顶层 {InventoryHeader:{version}, Inventory:[{sku, quantity:{unit:EACH, amount}}]}，Inventory 首字母必须大写。builder :289-297
- PUT /v3/price — 单 SKU 同步改价（put_price_sync :395-407），body 见 build_price_sync_body :300-308
- PUT /v3/inventory?sku=X — 单 SKU 同步改库存（put_inventory_sync :410-423），body 见 build_inventory_sync_body :311-316
- GET /v3/feeds/{feedId}?includeDetails=true&offset=N&limit=50 — 状态与逐 SKU 详情（poll_yesterday.py:68-75）；feedId 用 urllib.parse.quote(safe="") 转义（:59）因为含 @ 号
- 三个 POST/PUT 都走 walmart_client 的 safe_post_ex / safe_put_ex（walmart_maintenance_common.py:34-40 导入），poll 走 safe_get_ex——没有绕过共享客户端，代理与 token 由 walmart_client 统一管；但 feed body 明确不走 safe_post 而走 safe_post_ex 的 json_body，直传 JSON object，不嵌 {"payload": "..."}
- ⚠ 速率现实与旧文档不符：refdata/walmart_rate_limits.tsv:136 显示 POST /v3/feeds(legacy 批量改价) 是 10/hour；:126 显示 PUT /v3/price 是 100/hour，而旧 README 写的是「200/分钟/店」——旧文档错了，新实现别照抄。PUT /v3/inventory 200/min（:78）是对的
- ⚠ 代码里没有任何客户端节流：submit_one_store 的 chunk 循环（submit.py:136-164）连续 POST 不 sleep，一个大店 title/price/inventory 三类合计可能一轮就打出十几个 feed，直接撞 10/hour；只能靠 walmart_client 的 429 退避事后补救

### 魔数与踩坑参数

- LIMITS 单 feed 双约束（walmart_maintenance_common.py:95-99）：MP_MAINTENANCE (1000, 25MB)、price (1000, 25MB)、inventory (4000, 25MB)
- SYNC_THRESHOLDS（:102-105）：price=5、inventory=10。pick_route(:362-367) 单店该类型 SKU 数 ≤ 阈值走同步 PUT，否则走 feed；MP_MAINTENANCE 永远 feed（标题无同步接口）
- chunk() 字节检查频率 200（:347 `if len(cur) % 200 == 0`）——不是每条都序列化，所以 25MB 上限最多可被超出约 200 条商品的体积；超限时 pop 最后一条另起新块
- MP_MAINTENANCE_VERSION = "5.0.20260304-22_45_32-api"（:86）——Walmart enum 锁死，改一个字符 feed 直接 ERROR；PRICE_FEED_VERSION="1.7"(:87)、INVENTORY_FEED_VERSION="1.4"(:88)
- post_feed 默认 max_retries=3, timeout=120（:374）；put_price_sync / put_inventory_sync 默认 max_retries=3, timeout=60（:395, :410）。⚠ POST 带 max_retries=3 违反 walmart_client.py:489 自己写的警告「POST 非幂等，自动重试可能导致重复提交 feed」——429/5xx/网络异常都会重发同一个 feed body
- FEED_DETAIL_PAGE = 50（poll_yesterday.py:43）——Walmart 对 includeDetails=true 的 limit 硬限制 50，1000 SKU 的 feed 要翻 20 页；页数由 max(1, ceil(item_count/50)) 预算（:66），返回不足 50 行提前 break（:91）
- --per-feed-timeout 默认 180 秒（poll_yesterday.py:127）；超时只是 discard + fut.cancel()，已在跑的线程 cancel 不掉，最后 pool.shutdown(wait=False, cancel_futures=True)（:214）也拦不住已启动线程，进程退出时仍会 join，所谓「跳过」并不真的解除阻塞
- LARK_WRITE_BATCH = 1000（walmart_maintenance_common.py:443）——⭐ 2026-06-11 从 4000 下调到 1000。注释写明：values JSON 走命令行 argv 传给 lark-cli，macOS ARG_MAX 下 ~7700 行 × 11 列（~900KB）把 lark-cli 直接打挂（stdout/stderr 全空）。_write_block(:558-577) 与 appends(:669-676) 都按这个值切
- _run_lark 对飞书 90235「data not ready」重试 5 次、sleep 1.5*(attempt+1)（:468-470），单次 timeout 默认 60s。⚠ 这段重试只在 LARK_IO_SHIM=0 的 legacy 分支生效；默认 LARK_IO_SHIM="1"（:448）直接走 lark_io.run_cli，退化为无重试
- sync_lark 的 _read_range legacy 分支：max_retries=5、retry_delay 3s 起、×1.5 递增封顶 30s，只对 err code 90235 / 50502 重试（sync_lark.py:77-107）；同样默认被 LARK_IO_SHIM 绕过
- stockzero 配置表读取范围写死 A1:J500、跳过前 2 行表头（STOCKZERO_HEADER_ROWS=2，:55；range 在 :487）——店铺数超过 498 家会静默截断
- 当日 sheet 读取范围写死 A1:K100000（upsert_daily_rows :599）——单日维护行数超 10 万会静默丢索引，导致本该 update 的行变成 append 重复
- sync_lark 默认 --workers 2 / --page-rows 10000（sync_lark.py:141-144），README 与设计方案里写的「4 并发 5000 行」是旧值，代码为准
- 跨店并发默认 8（submit.py:195、poll_yesterday.py:126）。单店内三类严格串行，设计方案 §7 给的理由：同店 Walmart token 桶共享，并发会互挤 429
- TITLE_PLACEHOLDERS = {"[商品不存在]"}（:153）——M 列这个占位符要跳过标题维护，否则 Walmart 退回
- 飞书列位置全部是 0-indexed 硬编码（COL dict，:58-79，A..T 共 20 列）；parse_row_to_input 要求 len(row) >= 19（:163）
- DAILY_COL_END = chr(ord('A')+11-1) = 'K'（:437）——11 列写死，加一列这个算式还能用，但 upsert_daily_rows 里 old[2]..old[10] 的下标是手写的（:622-633），必须同步改

### 防重/幂等语义

几乎没有。inputs 表虽然建了 last_submitted_at 与 last_batch_id 两列（walmart_maintenance_common.py:719-720），但全仓库没有任何一处写入它们（grep 确认只有 DDL 命中）。因此：submit.py 重跑一次就把全部 pending 原样再提交一遍，防重完全依赖「人不要重复跑」这条约定，SKILL.md:22 也只能写成「submit 非幂等，不要盲目整条重跑」。更严重的是写序反了——submit.py:146-159 是先 save_payload → 再 post_feed → 成功返回后才 save_submission，进程在 POST 与 INSERT 之间崩溃会留下「Walmart 已收单但本地无记录」的孤儿 feed：poll 不到、结果丢失、下轮还会重发。这直接违反新项目 CLAUDE.md 的「防重状态先落库再调接口」。唯一算得上幂等的两处：sync_lark 的 DELETE+INSERT 全量替换，以及当日 sheet 按 (store,sku) 的 upsert。post_feed 自带 max_retries=3 反而是负资产——POST 非幂等却开了自动重试（walmart_client.py:489 有明确警告），429/5xx/网络异常会把同一个 feed 重发，Walmart 侧产生重复 feedId 而本地只记最后一个。

### 危险操作

- 【库存清零】force_zero 批次。两道闸门：默认 dry-run（submit.py:187-188 的 --execute，:201-202 打印 [DRY-RUN]，:251-253 直接 return）+ 二次确认（submit.py:231-233：zero_count>0 且 --execute 且没 --confirm-zeroing 就 sys.exit(1)）。触发来源两类：普通店 S 列 =="库存调0"（common:207-210），stockzero 店整店强制归零（common:211-217）
- 【stockzero 整店清零】名单实时读飞书配置表，改一格 J 列就能让一整店几千个 SKU 下一轮全部归零，代码里没有任何「名单变动幅度告警」或上限保护。唯一的减免是 I 列 availToSellQty 已经是 0 的行整行跳过（common:214）——注意 qty_now 解析失败返回 None，`None != 0` 为真，也会触发清零
- 【改价】没有独立闸门。--execute 一个 flag 就能改全店价格，价格没有任何上下限/涨跌幅校验（common:191-198 只校验能不能 float()）。改错价的杀伤力不比清库存小，闸门却弱一级
- 【改标题】同上，--execute 即可；唯一防线是 TITLE_PLACEHOLDERS 与「productType/upc/title 三缺一就跳过」（submit.py:139-141）
- 【dry-run 的盲区】print_plan(submit.py:170-182) 只打各店三类计数与清零数，不打路由决策（sync/feed）、不打 chunk 数、不打具体 SKU 与新旧值对比。人眼在 dry-run 阶段看不到「哪个 SKU 从多少改成多少」，达不到新项目 CLAUDE.md 要求的「打印将对哪些 SKU 做什么」
- 【test_sandbox_d052.py 名不副实】叫 sandbox 但 load_stores() 读的是生产 店铺API.xlsx（:98），--send 直接改线上。设计方案 §8 记录了实际事故：当时不知道 D052 是 stockzero 店，sandbox 验证时把它的价格 19.98→25.72、库存 0→30 都改了，事后靠 PUT /v3/inventory 手工回滚。新仓库不要迁这个脚本
- 【无并跑保护】没有 flock 单实例锁，两个 submit 同时跑会双份提交；新系统 cli.py 的 flock 正好补上这一环
- 【客户端零校验】common 里没有任何 schema 校验（README 明写「所有结构校验交给 Walmart 端」），错数据要到 D+1 poll 才以 DATA_ERROR 现形

### 事故教训与必须保留的行为

- `str(v or "")` 的 0-falsy 陷阱：飞书 J 列（库存特殊要求）返回 integer 0，`0 or ""` 得空串，导致 stockzero 名单读出来是空集合、整个清零规则静默失效。修法是统一用 _cell_str()（common:131-138，函数 docstring 里专门写了这条）。注意 parse_row_to_input 里 upc / product_type 两个字段仍在用 `str(row[...] or "").strip()`（common:225-226），是漏网的旧写法
- Price feed 曾多包一层 {"PriceFeed": {...}} → feedStatus=ERROR, itemsReceived=0；Inventory feed 曾多包 {"InventoryFeed": {...}} 或把 Inventory 写成小写 inventory → ERR_EXT_DATA_0503009 "Can not find any valid inventory in Feed"（设计方案 §9）。v1.4 大写 Inventory，v1.5 多节点才是小写
- MP_MAINTENANCE 的 Visible 直接以 productType 名作命名空间，中间没有 productCategory 那一层（早期按文档猜错过）；Orderable 与 Visible 是并列的顶级对象，不是 MPProduct
- feed body 用 application/json 直传 JSON object，不要嵌 {"payload": "..."}——官方文档误导过一次
- 2026-06-11 生产事故：全店 submit（batch 40481dfa）Walmart 侧全部提交成功，但收尾 upsert_daily_rows 撞上一个 7733 行的连续行组，单次 +write 的 values JSON 经 argv 超 macOS ARG_MAX，lark-cli 被打挂（stdout/stderr 全空），「维护记录_2026-06-11」一行没写上。后果是次日 poll 无处回填。修法 = _write_block 内部分块 + LARK_WRITE_BATCH 4000→1000。证据：common:439-443 与 :560-563 的 ⭐ 注释、tools/backfill_daily_sheet_20260611.py 的 docstring
- backfill 脚本自身有个未被发现的 bug：它往 row 里塞的是 title_flag / price_flag / inv_flag（tools/backfill_daily_sheet_20260611.py:51-68），而 upsert_daily_rows 只认 title_value / price_value / inv_value（common:624-632）。这些 flag 键被静默忽略，补写出来的 C/F/I 三列其实是空的——迁移时若要复算历史日 sheet，别照抄这个脚本的字段名
- POST /v3/feeds 开了 max_retries=3 与 walmart_client.py:489 自己的警告直接冲突（见 dedupe_semantics）
- poll 的「feed 整体 ERROR 且 itemDetails 为空」分支（poll_yesterday.py:263-274）：这时逐 SKU 详情拿不到，只能回查 feed_items 里 submit 时预写的 SKU 列表，给每个 SKU 打 FEED_ERROR: <code> <desc>。这条路径依赖 submit 阶段一定预写了 skus——是 save_submission(skus=...) 存在的唯一理由，别在重构里砍掉
- ingestionErrors 有两种形态：裸 list 和 {"ingestionError": [...]}，两种都实际见过，_first_error(poll_yesterday.py:96-107) 专门兼容
- poll 结果字符串截断到 200 字符塞进 sheet（_short_result :119、FEED_ERROR label :265）
- --per-feed-timeout 的「跳过」是假的：fut.cancel() 对已启动线程无效，pool.shutdown(wait=False, cancel_futures=True) 也拦不住，进程退出时仍会 join 这些线程
- sync 路径与 feed 路径在 sheet 上的 feedId 列语义不同：feed 写真 feedId，sync 写 'sync:200' / 'sync:err' 这种伪标记（submit.py:119）；失败的 feed 路径写 'err:<status>'（submit.py:149）。下游任何按 feedId 反查的逻辑都要认这三种形态
- 缺凭证的店铺被静默跳过（submit.py:236-242 只打印前 10 个），飞书里标了触发但 店铺API.xlsx 里没这店 = 这批数据永远不会被提交、也不会报错
- submit 的 --stores 同时传给 load_stores 和 load_pending_inputs（submit.py:205, 212），两边过滤逻辑不同（一个匹配 xlsx 店名，一个匹配 SQLite store_name），店名有空格/不一致时会静默两头落空
- LARK_IO_SHIM 环境变量（默认 "1"）会让 common 与 sync_lark 里精心写的两套飞书重试逻辑全部失效，改走 lark_io。排查飞书失败时先确认走的是哪条分支

### 切换时必须迁移的状态

- maintenance.db 全库（生产机 /Users/nextderboy/Projects/erpAPI/沃尔玛商品维护/maintenance.db，不在 git 里）。其中 submissions + feed_items 是必须搬的历史，inputs 可以不搬（新系统跑一次 sync 就重建）
- ⚠ 切换前必须先把所有在途 feed 收干净：`SELECT * FROM submissions WHERE walmart_feed_id IS NOT NULL AND status NOT IN ('processed','error')`。这些 feed 的结果只能靠旧 poll_yesterday 拿；停旧调度后没人 poll，结果永久丢失。正确顺序：停 submit → 等 4-6h → 跑 poll_yesterday --include-all → 再停旧系统
- 飞书当日 sheet 系列「维护记录_YYYY-MM-DD」——新系统若改用多维表格，历史 sheet 要么留档要么导入；至少最近一天的 sheet 必须保留到最后一轮 poll 回填完成
- payloads/ 归档目录（排查 DATA_ERROR 时回查原始提交内容用），建议整体打包进 <DATA_ROOT>
- stockzero 店名单不需要迁移（每次运行实时读飞书配置表 E1p9…Kh(token已脱敏,见旧仓库代码)!2FJ2Np），但要在 registry 里登记这张表与 A/J 两列的语义

### 迁移建议

照搬（业务事实，重写等于重新踩坑）：三类 feed 的 payload 结构与版本号常量（MP_MAINTENANCE 5.0.20260304-22_45_32-api / PriceFeed 1.7 / InventoryFeed 1.4，含「无外层包装」「Inventory 大写」两条）；触发规则表（PUBLISHED 全局过滤 + N≠100、Q=是、S=是/库存调0）与 stockzero 店的三条特例；_cell_str 的 0-falsy 处理；GET feed 详情 limit≤50 与分页早停；ingestionErrors 双形态兼容；「feed 整体 ERROR 时用预写 SKU 列表标 FEED_ERROR」这条兜底路径。

重做：① 防重语义整个翻过来——按新 CLAUDE.md「先落库再调接口」，改成 INSERT ops.feed_log(status=pending) → POST → UPDATE done/failed，启动时把所有 pending 拿去 Walmart 查真实状态再决定补交；顺带把 post_feed 的 max_retries 降到 0，重试交给 pending 恢复机制。② inputs.last_submitted_at / last_batch_id 这两个空壳列要么真正写起来，要么删掉换成 ops 里的提交水位。③ 飞书列引用全部改成 registry 常量，禁止再出现 COL 字典这种 0-indexed 位置硬编码；四个 token（主表/配置表/当日 sheet 同 spreadsheet）登记进 registry/resources.py。④ maintenance.db 三表并入 PG：inputs → listing 域的维护意图表（或干脆不落库，sync 直接产出内存快照），submissions/feed_items → ops.feed_log + ops.feed_item_result，别再各自建 SQLite。⑤ 补客户端节流：POST /v3/feeds 按 refdata 的 10/hour/店 做令牌桶，PUT /v3/price 按 100/hour（不是旧文档写的 200/min）；submit 的 chunk 循环现在完全没有 sleep。⑥ 加 flock 单实例锁与 ops.runs 记录（cli.py 已统一提供，删掉脚本里的自建逻辑）。⑦ dry-run 输出必须升级到「逐 SKU 打印 store/sku/字段/旧值→新值/路由(sync|feed)/所属 chunk」，现在的 print_plan 只有计数，不满足人眼确认的要求。⑧ 改价应当和清库存同级，加独立 --confirm-price 或统一的价格涨跌幅上限校验。⑨ 当日 sheet 的 11 列在多维表格里天然按 record_id 更新，argv/ARG_MAX 那套分块与「连续行号合并」逻辑可以整个丢掉——这是本模块最脏的一块代码，也是唯一一次生产数据丢失的根因。

不迁移：test_sandbox_d052.py（名为 sandbox 实为生产改数，已造成过误改）、tools/backfill_daily_sheet_20260611.py（一次性救场，且字段名有 bug）。

拆成新 workflow 的建议：maintenance_sync（飞书→PG，安全、可 cron）、maintenance_submit（dangerous=True，PG→Walmart）、maintenance_poll（只读，回填结果）三条独立 workflow，共享 services 里的 payload builder / chunker / router。切换顺序严格按 CLAUDE.md：先停 12:00 的 walmart-maintenance-all-stores → 等 4-6h 让在途 feed 处理完 → 旧 poll_yesterday --include-all 收干净 → 搬 maintenance.db → 起新调度；期间绝不能新旧同时 submit。

### 待确认问题

- stockzero 名单当前是 13 家还是 14 家？README:84-86 列 14 家、设计方案:16-18 列 13 家（少了 A132高佳棋），且名单实时从飞书读。迁移时以飞书配置表实时值为准，但需要人确认这个名单的变更是否需要审批流
- MP_MAINTENANCE 与 inventory feed 在 refdata/walmart_rate_limits.tsv 里查不到明确条目，只有 :136「Update bulk prices (Legacy) 10/hour POST /v3/feeds」这一条通用限制。旧 README 声称三类 feed 都是「10/小时/店」——需要确认这 10/hour 是 POST /v3/feeds 整体共享桶还是按 feedType 分桶，直接决定大店一轮能不能跑完
- 飞书主表 142k 行、submit 一轮 65k 触发是 2026-05 的实测快照，现在的量级未知；如果 inputs 触发数长期在几万量级，新系统是否还要把中间态整表落 PG（vs 直接流式提交）需要定夺
- 当日 sheet「维护记录_YYYY-MM-DD」在新系统改用多维表格后，历史 sheet 怎么处理——留在旧 spreadsheet 归档，还是导入多维表格？运营是否还在人工看这张表
- poll_yesterday 只 poll D-1；如果某天的 feed 到 D+1 仍是 INPROGRESS（SKILL.md:80 承认这种情况存在），现在只能人工跑 --date --include-all 补救。新系统是否要改成「按 status 扫所有未终态 feed」而不是按日期
- LARK_IO_SHIM 与 lark_io 模块的行为边界（read_range 的 prefer_raw_v2 registry、大表分窗）不在本次读取范围内，但 sync_lark 拉 142k 行整个依赖它；新 api/feishu.py 需要单独确认这块的分页与重试策略


<a id="daily_retire"></a>
## 沃尔玛批量下架 → daily_retire (#7, 危险)

### 模块职责

飞书表格驱动的沃尔玛商品「永久删除」工作流。运营在飞书「在线产品/下架表」填 A=ASIN、B=店铺,编排器每天 15:00 跑一次:分页读全表 → 扫脏数据 → 从「综合数据源/定价和上下架」I 列读每店单日上限 → 按店铺 8 线程并发:对已有 feedid 的行调 GET /v3/feeds/{feedId}?includeDetails=true 逐 SKU 判定(SUCCESS→「是」/DATA_ERROR|SYSTEM_ERROR|TIMEOUT_ERROR→「否N」入重试/未在 itemDetails 返回→「未查到」入重试/feed 未完成→「处理中」),对无 feedid 的新行按单日上限截取,重试+新合并成一个 DELETE_ITEM Feed 提交 → 把 feedid/日期/状态原子写回飞书 C/D/E 三列。另有一个脱离飞书的手动 CLI(retire_walmart_items.py),支持从文件或按 publishedStatus 拉 SKU,同店铺批次间隔 360s。DELETE_ITEM 是从沃尔玛目录永久删除、不可恢复,且仅 Seller-Fulfilled(FBM)商品支持,WFS 会报错。

### 入口与触发

- /workspace/erpapi/沃尔玛批量下架/daily_retire_orchestrator.py:970 main() —— 生产主入口。参数:--audit(仅扫脏数据) --repair(配合 audit 补查 Type1) --dry-run --max-workers(默认8) --batch-size(默认4000) --stores
- 调度触发:Claude scheduled-task skill `walmart-daily-retire`,cron `0 15 * * *`,实际命令见 /workspace/erpapi/定时任务skill/walmart-daily-retire/SKILL.md:46 —— `cd .../沃尔玛批量下架 && LARK_IO_SHIM=1 /opt/homebrew/bin/python3 daily_retire_orchestrator.py`。不是 launchd,是 skill 里写死的 cron 表(定时任务skill/README.md:71 登记)
- 任务前后各发一条飞书简报:定时任务skill/notify.py start|done(SKILL.md:30-39),与业务脚本解耦,脚本本身不发通知
- /workspace/erpapi/沃尔玛批量下架/retire_walmart_items.py:323 main() —— 手动 CLI。--from-file(.xlsx/.xls/.csv/.txt) 或 --status(默认 UNPUBLISHED,SYSTEM_PROBLEM,与 --from-file 互斥) --stores --batch-size --wait --dry-run --yes
- /workspace/erpapi/recover_lark_writeback.py —— 2026-05-07 事故一次性救场脚本(plan.md 已列为不迁移),但其恢复逻辑说明了本模块的状态语义,值得读 recover_lark_writeback.py:1-19

### 调度

每天 15:00,cron `0 15 * * *`,由 Claude scheduled-task(skill)机制触发,不是 launchd。定义在 /workspace/erpapi/定时任务skill/walmart-daily-retire/SKILL.md,登记在 定时任务skill/README.md:71。实际命令带 `LARK_IO_SHIM=1` 前缀(SKILL.md:46)。SKILL.md 里还有一段执行韧性约定:失败重试 2-3 次(5s→15s→30s)、重试不成就跳过该店、部分成功算 warn 不算 fail,并且明确写了「非幂等,提交阶段失败不要重跑,留到下一轮」(SKILL.md:18-19)。手动 CLI retire_walmart_items.py 无调度,纯人工触发。

### 数据存储

- **本模块没有任何本地数据库/JSON/状态文件**。两个脚本全程不写磁盘,全部状态只存在飞书表的 C/D/E 三列里。这是整个模块最大的结构性风险(见 pitfalls)。
- /workspace/erpapi/店铺API.xlsx Sheet1 —— 店铺凭证与代理,经 walmart_client.py:59 XLSX_PATH + walmart_client.py:143 load_stores() 读。过滤规则:ClientId 为空或 '0' 跳过;代理类型/IP/端口 任一为 '0' 跳过(即无代理店铺直接不参与,天然阻止直连)
- access_token 只在 walmart_client.py 进程内内存缓存(_token_cache),提前 60s 过期,无落盘
- 手动 CLI 的 SKU 输入文件:.xlsx/.xls/.csv/.txt,retire_walmart_items.py:97 read_skus_from_file(),自动找名为 'sku' 的列,找不到取第一列;去空白+去重+保留大小写
- 沃尔玛官方 spec 参考文件(只读):/workspace/erpapi/walmart_official_specs/DELETE_ITEM/5.0.20250919-16_45_47-api_DELETE_ITEM.json

### 飞书使用

- 在线产品 / 下架表 —— app_token(spreadsheet_token)= MO2e…mI(token已脱敏,见旧仓库代码),sheet_id = 38df0D(daily_retire_orchestrator.py:63-64,recover_lark_writeback.py:36-37 又抄了一份)。**是电子表格不是多维表格**,按 A1 坐标定位。列义:A=ASIN(直接当沃尔玛 SKU 用) B=店铺 C=下架feedid D=日期 E=是否删除。读 A{n}:E{n},写 C{n}:E{n}。
- E 列表头强制为「是否删除」:ensure_header()(daily_retire_orchestrator.py:538-549)每次非 audit 运行都检查 E1,不对就写回去。
- E 列取值枚举(daily_retire_orchestrator.py:76-80):是 / 处理中 / 未查到 / 非ASIN / 否N(N 为历史失败次数+1,正则 ^否(\d+)$)。
- 综合数据源 / 定价和上下架 —— spreadsheet_token = E1p9…Kh(token已脱敏,见旧仓库代码),sheet_id = 2FJ2Np(daily_retire_orchestrator.py:66-67)。A 列=店铺名,I 列(index 8)=『单日最大下架数量 fbm』。读取范围 A3:I{row_count},数据从第 3 行起(:348-366)。
- 上限表必须用 valueRenderOption=ToString + dateTimeRenderOption=FormattedString 走 v2 values 接口读显示值(_read_display_values,:261-296),因为 I 列是公式,raw-v2 会返回公式文本而不是结果。这是一个容易在迁移时踩的坑。
- 读:lark-cli `sheets +cells-get` / `+workbook-info`,在 LARK_IO_SHIM=1(默认)时被 _lark() 劫持转成 lark_io.read_range(prefer_raw_v2=True) / lark_io.workbook_info,再手工重建成旧的 {data:{ranges:[{cells}]}} 信封给 _extract_values 用(daily_retire_orchestrator.py:126-150、:252-258)。
- 写:read-patch-write 原子写回。batch_write_changes()(:468-535)把 {(row, col_letter): value} 按**行区间**切片(不是按受影响行数),每批先读回整块 C:E 二维,再打 patch,再整块写回,保证同一行三列要么全成、要么全败;完全没有受影响行的区间跳过。
- 行数不写死,从 +workbook-info 的 row_count 动态取(_get_sheet_row_count,:300-307);LARK_IO_SHIM=0 旧路径下 1204 超时会回退到原生 REST /open-apis/sheets/v3/.../sheets/query 与 /open-apis/sheets/v2/.../values/{range}(:182-249)。
- **所有 token/sheet_id 都是硬编码字面量**,且 recover_lark_writeback.py 里重复了一遍。新仓库必须全部登记进 registry/resources.py,字段也用常量而非「是否删除」这类字符串字面量。

### 沃尔玛端点

- POST /v3/feeds?feedType=DELETE_ITEM —— 核心破坏性调用。body = {ItemFeedHeader:{locale:'en', version:DELETE_ITEM_VER, businessUnit:'WALMART_US'}, Item:[{Deletable:{sku}}]}(daily_retire_orchestrator.py:636-646、retire_walmart_items.py:220-228)。**两处都是裸 httpx.post,绕过 walmart_client 的 safe_post_ex**(daily_retire_orchestrator.py:619-624、retire_walmart_items.py:246-253)。代理仍然按店铺传了 proxy=store['proxy'],所以没违反出口 IP 铁律;但丢掉了 _request_ex 提供的 401 自动换 token、429 按 Retry-After/X-Next-Replenishment-Time 退避、5xx 指数退避、TransportError 时剔除坏连接池(walmart_client.py:313-437)。绕过的原因很可能是 safe_post_ex 只接受 json_body,而这里需要发预先序列化的 bytes。
- GET /v3/feeds/{feedId}?includeDetails=true&offset=&limit=50 —— 逐 SKU 判定,走 safe_get_ex(quiet=True, timeout=60, max_retries=3),分页直到 offset+本页数 >= itemsReceived(daily_retire_orchestrator.py:568-603)。SKU 取 it['sku'] or it['martId']。
- GET /v3/feeds/{feedId}(不带 includeDetails)—— 手动 CLI --wait 轮询,30s 一次最多 1 小时,终止状态 PROCESSED/ERROR/COMPLETE(retire_walmart_items.py:266-286)。
- GET /v3/items?publishedStatus={UNPUBLISHED|SYSTEM_PROBLEM}&limit=100&offset= —— 仅手动 CLI --status 模式,走 safe_get,翻页 sleep 0.3s,offset 上限 10000(retire_walmart_items.py:168-187)。注意新仓库 refdata 记的是带 query 参数的 /v3/items 限 60/min。
- POST /v3/token —— 经 walmart_client.get_token(),进程内缓存、提前 60s 过期、双检锁保证并发只换一次(walmart_client.py:213-256)。
- feed 类型只用 DELETE_ITEM。**不要与 RETIRE_ITEM 混淆**:RETIRE 是可恢复下架,DELETE 是永久删除;RETIRE_ITEM 只出现在兄弟模块 沃尔玛问题商品清理/daily_cleanup.py:416。
- 配额相关:README:159 声称 PRICE_AND_PROMOTION(6/day)与 MP_ITEM/DELETE_ITEM 共享同一店铺的 feed 提交通道,建议大批量删除当天避开价格批量同步 —— 这条未在 refdata 中找到出处,需复核。

### 魔数与踩坑参数

- INTERVAL_SEC = 360(retire_walmart_items.py:65,注释 3600/10=360s)—— **只在手动 CLI 生效**,同店铺批次之间 sleep,最后一批不睡(retire_walmart_items.py:449-452)。编排器完全没有这个 sleep:process_store 的多分片提交循环(daily_retire_orchestrator.py:876-889)是连发的,理由是「单店通常只提交一个 feed」。迁移时不要盲抄 360s 到编排器路径,也别忘了给编排器补一个真正的限速器。
- RATE_LIMIT_PER_HOUR = 10(retire_walmart_items.py:64)—— DELETE_ITEM feed 10/hour 的说法只在旧代码注释和 README(沃尔玛批量下架/README.md:157)里,新仓库 refdata/walmart_rate_limits.tsv 里**没有 DELETE_ITEM 这一行**,最接近的是第 136 行 'Update bulk prices (Legacy) 10/hour POST /v3/feeds'。360s 这个魔数的出处就是这条,需要复核。
- DELETE_ITEM_VER = '5.0.20250919-16_45_47-api' —— **抄了 3 份**:daily_retire_orchestrator.py:70、retire_walmart_items.py:61、沃尔玛问题商品清理/daily_cleanup.py:57。walmart_spec_version_check.md:75 记录 2026-03 那次官方 spec 大更新时 DELETE_ITEM 未变。新仓库按 legacy_reference.md:47 的要求只在 api/feeds.py 留一份。
- MAX_BYTES_PER_FEED = 95_000 在 daily_retire_orchestrator.py:71 **声明了但从未被使用**:实际切分用的是硬编码 100_000(daily_retire_orchestrator.py:661、:666),提交前的保护也是 >100_000(:609)。README:158 宣称的『留 5KB 余量』在编排器里是假的,只有手动 CLI 真用了 95_000(retire_walmart_items.py:194、:382)。迁移时统一用 95_000。
- DEFAULT_BATCH_SIZE = 1000(retire_walmart_items.py:63)SKU 数硬上限;chunk_by_bytes 每追加 50 个才粗检一次字节数(retire_walmart_items.py:208),超了再逐个 pop 回退。极端长 SKU 下首批可能被 pop 空 → 空 batch 不 yield 且 i 回退到原位 → 理论死循环(10 字符 ASIN 场景不会触发)。
- DEFAULT_DAILY_LIMIT = 300(daily_retire_orchestrator.py:73)—— 飞书上限表里查不到该店铺时的兜底值(:1063 limits.get(name, DEFAULT_DAILY_LIMIT))。**店铺名拼写不一致就会静默落到 300**,没有告警。
- FEED_DETAIL_PAGE = 50(daily_retire_orchestrator.py:72)—— GET /v3/feeds/{feedId} 的 limit 上限;翻页之间 sleep 0.2s(:602);翻页终止条件是 offset+len(items) >= itemsReceived 或本页不足 50(:597-601)。
- READ_BATCH_ROWS = 5000(daily_retire_orchestrator.py:310)—— 飞书单次读 5 列 × 5000 行 = 25000 单元格,注释写『实测 OK』。真实行数从 +workbook-info 的 row_count 动态取(:300-307),不是写死。
- FEISHU_MAX_CELLS_PER_WRITE = 5000(daily_retire_orchestrator.py:465)是写入硬上限,与读的 25000 不对称。effective_rows = min(batch_size, 5000 // 列宽)(:491),C:E 三列 → 每批实际只有 1666 行,--batch-size 4000 会被自动收紧。
- --max-workers 默认 8(daily_retire_orchestrator.py:980);--repair 路径独立用 min(8, 店铺数)(:957)。
- --batch-size 默认 4000(daily_retire_orchestrator.py:982),飞书写回每批行数(会被上一条收紧)。
- WAIT_POLL_SEC = 30 / WAIT_TIMEOUT_SEC = 3600(retire_walmart_items.py:66-67)—— 手动 CLI --wait 的轮询间隔与单批最长等待;终止状态判定为 PROCESSED/ERROR/COMPLETE(:282),超时返回 {'feedStatus':'TIMEOUT'}。
- FETCH_LIMIT = 100 / MAX_OFFSET = 10000 / 翻页 sleep 0.3s(retire_walmart_items.py:69-70、:186)—— --status 模式拉 GET /v3/items。注意新仓库 refdata 里带 query 参数的 /v3/items 是 60/min,0.3s 间隔刚好压线。
- HTTP 超时:feed 提交 timeout=120(daily_retire_orchestrator.py:623、retire_walmart_items.py:252);feed 明细查询 timeout=60 + max_retries=3 + quiet=True(daily_retire_orchestrator.py:573-574);--wait 轮询 timeout=30 + max_retries=3(retire_walmart_items.py:275-276)。
- 飞书重试:error_code=1204(大表后端超时)→ 回退原生 REST API(daily_retire_orchestrator.py:158、:182);90235 'data not ready' 最多 5 次、退避 2**attempt(:154、:166-169、:196-203)。LARK_IO_SHIM=1(默认开)时走 lark_io raw-v2 分窗,绕开 facade 超时(:126-150),置 0 回退旧 lark-cli 路径。
- 写回超长时 ARG_MAX 溢出 → 二分递归重写(daily_retire_orchestrator.py:449-458),仅在 LARK_IO_SHIM=0 旧路径需要。
- ASIN_RE = ^B0[A-Z0-9]{8}$(daily_retire_orchestrator.py:83、retire_walmart_items.py:73)—— 只对标准亚马逊 ASIN 提交 DELETE_ITEM,其余标 E='非ASIN' 跳过。这是 2026-05-26 才加的护栏,说明历史上误删过非 ASIN 的 SKU。
- 上限表读取范围写死 A3:I{row_count}(daily_retire_orchestrator.py:351-354),即数据从第 3 行开始、店铺名在 A(index 0)、上限在 I(index 8);且必须用 valueRenderOption=ToString 读『显示值』,因为 I 列是公式,raw-v2 会返回公式文本(:261-272 _read_display_values 的 docstring 明确说明)。
- token 提前 60s 过期(walmart_client.py get_token);手动 CLI 每批提交前都重新 get_token(retire_walmart_items.py:420-424),编排器每店只取一次(daily_retire_orchestrator.py:790)。

### 防重/幂等语义

**唯一的防重键是飞书 C 列 feedid 非空**(daily_retire_orchestrator.py:808-809:`to_check = 有 feedid 的行`,`new_skus = 无 feedid 的行`)。有 feedid 的行只会被「查询」不会被重新提交;没有 feedid 的行才算新提交。

细节:
- is_pending_status(:682-684):E 列不等于「是」的一律算待处理(含空、处理中、未查到、否N、非ASIN)。已完成的行每天都会被重新读进来但立刻被过滤掉。
- 重新提交只发生在两种情况,且都是由**本轮实时 feed 查询结果**驱动的,而不是读 E 列:SKU 的 ingestionStatus ∈ {DATA_ERROR, SYSTEM_ERROR, TIMEOUT_ERROR} → 进 retry_rows 并把 E 改成「否N+1」(:843-847);SKU 根本没出现在 itemDetails 里 → 进 retry_rows 并标「未查到」(:851-855)。needs_resubmit()(:687-689)看起来是干这事的,但实际上没有任何调用点,是死代码。
- 「处理中」不会重新提交(:829-834、:848-850),等下一轮再查。
- feed 查询整体失败(网络/代理)→ 这组行原地跳过、不写任何状态(:824-826),因为 feedid 还在 C 列,下轮自然重查 —— 这一步是安全的。
- 重试 SKU **不计入单日上限**:daily_limit 只截取 new_skus(:858),retry_rows 全量参与合并提交(:866)。
- 「否N」的 N 从 E 列文本正则解析出来再 +1(_FAIL_RE `^否(\d+)$`,:80、:675-679),没有上限、永不放弃。
- 提交成功后一次性写 C=feedid、D=today、E(若原来是「非ASIN」则清空,允许运营改回有效 ASIN 后重新走流程,:891-896)。
- **幂等性缺口(致命)**:写回发生在提交之后,且写回本身可能失败。提交成功 + 写回失败 = 沃尔玛已删、飞书不知情 → 下一轮这些行仍是「无 feedid 的新行」→ 再次提交 DELETE_ITEM。2026-05-07 事故就是这个链路(recover_lark_writeback.py:4-7)。另外整个流程没有单实例锁,并发跑会重复提交。

### 危险操作

- **POST /v3/feeds?feedType=DELETE_ITEM = 从沃尔玛目录永久删除商品,不可恢复**,与可恢复的 RETIRE_ITEM 完全不同(retire_walmart_items.py:5-6 文件头警告、README:155)。仅 Seller-Fulfilled(SFF/FBM)支持,对 WFS 商品调用会报错。
- **旧编排器的 --dry-run 不安全,是本次迁移最需要警惕的一点。** daily_retire_orchestrator.py:1084 的 --dry-run 只跳过飞书写回;真正的 DELETE_ITEM 提交在 process_store 内(:878),早在 dry-run 判断之前就执行完了。用 --dry-run 跑一次的后果是:商品真被删了,而 feedid/日期一个字都没落回飞书 —— 比正常跑还糟。新系统的 dangerous workflow 必须在任何副作用之前返回计划。
- 编排器**没有任何二次确认**,15:00 无人值守直接删,唯一的闸门是「运营已经审核过飞书表」这个人工约定(README:155、SKILL.md:66)。
- 手动 CLI 的保护是对的、可以照搬:--dry-run 在提交前就 return(retire_walmart_items.py:399-401);非 --dry-run 时打印警告并要求手输 'YES',除非带 --yes(retire_walmart_items.py:404-410)。--yes 是给脚本化场景开的后门,2026-05-16 加的。
- 唯一的量级闸门是飞书 I 列的单店单日上限,店铺名对不上时静默兜底 300(daily_retire_orchestrator.py:1063);而且**重试 SKU 完全不受这个上限约束**(:858、:866),历史「否N」堆积会一次性放出远超上限的删除量。
- ASIN 格式白名单是事后补的护栏(2026-05-26):只有 ^B0[A-Z0-9]{8}$ 才提交,其余标「非ASIN」跳过(daily_retire_orchestrator.py:799-806、retire_walmart_items.py:76-90)。说明历史上曾把 UPC/自建编码当 SKU 提交过删除。
- **没有单实例锁**。编排器和手动 CLI 都没有 flock,同时跑两次会对同一批行各提交一次 DELETE_ITEM。
- 并发度 8 个店铺同时提交(:1057),配合每店固定出口代理;单店内多分片提交无任何间隔(:876-889)。
- 兄弟模块 沃尔玛问题商品清理/daily_cleanup.py 也提交 DELETE_ITEM(:388),两条工作流共享同一店铺的 feed 通道 —— 切换 daily_retire 时必须确认不会和它、以及和旧版自己并跑。

### 切换时必须迁移的状态

- 飞书「在线产品/下架表」(app_token MO2e…mI(token已脱敏,见旧仓库代码),sheet 38df0D)**全表**:每行的 A=ASIN、B=店铺、C=feedid、D=提交日期、E=是否删除。这是本模块唯一的状态源,必须整表导入新库(建议 listing 下的 retire 队列表 + ops.feed_log)。
- **在途 feed 必须先清干净**:所有 C 列有 feedid 且 E 列不是「是」的行(即 空/处理中/未查到/否N),切换前要先用旧系统或一次性脚本把它们查到终态,否则新系统会因为不认识这些 feedid 而当成新行重新提交 DELETE_ITEM。
- 「否N」失败计数器(N 从 E 列文本解析,daily_retire_orchestrator.py:675-679)必须原样搬成整数列,否则重试次数归零。
- 「非ASIN」标记行(E='非ASIN'):这些是 A 列不符合 ^B0[A-Z0-9]{8}$ 的行,新系统要保留同样的跳过语义,否则会把 UPC/自建编码当 SKU 提交删除。
- 飞书「综合数据源/定价和上下架」(E1p9…Kh(token已脱敏,见旧仓库代码),sheet 2FJ2Np)A 列店铺名 → I 列『单日最大下架数量 fbm』的映射,以及运营继续在飞书维护它的习惯。
- 店铺API.xlsx 的 店铺名 → ClientId/ClientSecret/代理 映射(新系统进 registry + .env)。店铺名字符串是飞书表与凭证表之间唯一的连接键,拼写必须逐个核对。

### 迁移建议

迁移顺位 #7,危险级别最高之一(DELETE_ITEM 不可恢复)。建议:

**必须重做,不要照搬的:**
1. **防重顺序反了,这是本模块的头号缺陷。** 旧实现是「先提交 feed → 成功后才写 C/D/E」(daily_retire_orchestrator.py:878-896)。2026-05-07 就因为提交后本地代理拒连,14610 个单元格没写进去,feed 已经发到沃尔玛但飞书完全不知道(recover_lark_writeback.py:4-7),只能靠反查 /v3/feeds/{feedId} 手工对账。新系统必须严格执行 CLAUDE.md 的「防重状态先落库再调接口」:提交前对每个 SKU 写 ops.feed_log(state=pending, 幂等键=店铺+SKU+日期),提交成功改 done+feedid,启动时所有 pending 先去沃尔玛查真实状态再决定补交。
2. **旧编排器的 --dry-run 是假的、有害的。** daily_retire_orchestrator.py:1084 的 --dry-run 只跳过飞书写回,提交 DELETE_ITEM 的动作在 process_store(:878)里早就发生了 —— 也就是说 --dry-run 会真删商品且不留任何记录,是最坏组合。新系统的 dangerous=True workflow 必须在**产生任何副作用之前**返回「将对哪些店哪些 SKU 做什么」。
3. **飞书电子表格 → 多维表格。** 现在用的是 sheets v2/v3 电子表格,靠 A1 列字母+行号定位,整套 read-patch-write 原子写回(:468-535)、5000 cells 收紧、ARG_MAX 二分、1204/90235 重试(:152-249)全是为了对抗这个模型。改用多维表格按 record_id 更新后,这几百行基础设施代码可以整块删掉。E 列表头自动纠正(ensure_header,:538-549)也随之作废。
4. **三处 DELETE_ITEM_VER 常量收敛到 api/feeds.py 一处**(legacy_reference.md:47 已点名)。
5. **绕过共享客户端的直连要收回。** 两处 submit_delete_feed 用裸 httpx.post(见 walmart_endpoints),虽然传了 store['proxy'] 没违反出口 IP 铁律,但丢了 401 自愈、429/5xx 退避、坏连接池剔除。新 api/_client.py 需要支持「原始 bytes body」的 POST,这样 feeds 提交也能走统一客户端。
6. **加单实例锁。** 旧编排器没有任何 flock,两次并发跑会对同一批行各提交一次 DELETE_ITEM。新 cli.py 已有 flock,天然解决。
7. **重试量不受单日上限约束**(:858、:866 —— daily_limit 只截取 new_skus,retry_rows 全量加进去)。历史「否N」堆积多了会一次性放出远超上限的删除量。新系统建议对 retry 也设独立上限,并对 N 超过阈值(比如 5)的行停止重试、转人工。
8. dead code:needs_resubmit()(:687-689)定义了但从未被调用,实际重试队列是由实时 feed 查询结果直接构造的(:846、:854)。别照抄。

**可以照搬的:**
- 状态机语义(是/处理中/未查到/否N/非ASIN)和 ASIN 正则护栏 —— 运营已经习惯这套词汇,新系统建议保留同样的枚举值回写飞书。
- feed 未终态时把整组标「处理中」而不是重试(:829-834);feed 查询失败时整组跳过、不动状态(:824-826)—— 这两条都是正确的保守行为。
- 单店合并成一个 feed 提交(重试+新)的做法,天然规避 feed 数配额。
- 按字节切分 payload,以及「单个 SKU 也超限」时直接抛错(:663、:668)。

**新 workflow 对应关系:**
- `workflows/daily_retire.py` run(params) → dangerous=True,默认 dry-run 打印计划。
- `api/feeds.py`:submit_feed(feed_type=DELETE_ITEM, version=...)、get_feed_status(feed_id, include_details, 分页 limit=50)。
- `services/`:feed 状态 → 行状态的判定函数、单日上限取数、ASIN 校验。
- `registry/resources.py`:两张飞书表的 token/table_id 与字段常量;`refdata/walmart_rate_limits.tsv` 需要补一行 DELETE_ITEM feed 的真实配额。
- 与 #8 daily_cleanup 强相关:那个模块也提交 DELETE_ITEM(沃尔玛问题商品清理/daily_cleanup.py:57、:388)且还会发 RETIRE_ITEM。两条工作流对同一店铺的 feed 通道是共享的,新系统里 feed 提交必须走同一个每店限速器,且切换时**不能让新旧两套 DELETE_ITEM 同时跑**(CLAUDE.md 安全铁律)。

### 待确认问题

- DELETE_ITEM feed 的真实配额到底是多少?refdata/walmart_rate_limits.tsv 里没有 DELETE_ITEM 行,旧代码的 10/hour + 360s 间隔很可能是从第 136 行 'Update bulk prices (Legacy) 10/hour POST /v3/feeds' 推的。需要查官方文档确认后补进 refdata,否则新系统的限速器没有依据。
- README:159 说 PRICE_AND_PROMOTION(6/day)与 DELETE_ITEM/MP_ITEM 共享同一店铺的 feed 提交通道 —— 这条约束是真的吗?如果是,daily_retire(15:00)和价格同步类工作流需要在新系统里做跨 workflow 的每店 feed 配额协调。
- 「未查到」(SKU 没出现在 feed 的 itemDetails 里)到底意味着什么?旧实现无条件当失败重新提交(:851-855)。如果实际含义是「该 SKU 在这个店铺根本不存在」,那每天都会为它白提交一次 DELETE_ITEM,永远不收敛。需要抽样查几个长期「未查到」的行。
- 「否N」没有上限,N 可以无限增长。运营是否需要一个「否N 超过阈值就转人工/停止重试」的规则?
- 编排器的 --dry-run 只跳写回、照样提交,是设计如此还是历史 bug?需要跟使用者确认有没有人真的用它「预览」过(如果用过,可能已经造成过误删)。
- E 列标了「非ASIN」的行,运营的预期是什么?是等着改 A 列,还是这些行本来就该用别的方式下架(比如真实的沃尔玛 SKU 而非 ASIN)?目前它们被永久跳过,且每轮都会重新扫一遍。
- 单日上限表(2FJ2Np 的 I 列)的口径:是「每店每天最多删多少」还是「每店每天最多提交多少条新的」?旧代码按后者实现(只截 new_skus)。
- 切换时在途 feed 怎么清?建议先跑旧系统的 --audit --repair 直到 Type1 脏数据归零、且没有「处理中」行,再停旧起新。这个前置条件需要写进切换 checklist。


<a id="daily_cleanup"></a>
## 沃尔玛问题商品清理 → daily_cleanup (#8, 危险)

### 模块职责

每 6 小时（0/6/12/18 点）无人值守地清理全部沃尔玛店铺的问题商品：先执行飞书「监管合规删除」表里人工录入的定点删除；再并发拉取每家 ACTIVE 店铺的 UNPUBLISHED + SYSTEM_PROBLEM 商品；把"A 过期"类（约占 46%）走 MP_MAINTENANCE Feed 把 endDate 延到 2049-12-31 做反补 republish，把"Stage 待发布"类排除，其余 SKU 批量提交 DELETE_ITEM Feed；然后把明细写入本机 PostgreSQL walmart_cleanup 库（runs + error_items）、发邮件、同步飞书三张表（店铺汇总/错误统计/{日期}问题商品）、把 C 品牌与 E 知产 ASIN 异步推给 DMIT amazon-scraper 采品牌回写飞书、把 B/C/E/F/G/K 六类永久禁售 ASIN 追加进 Amazon 选品黑名单表供 auto_listing 拦截。全流程 7 个 Step 之间用 try/except 隔离，任一步失败不阻断后续。

### 入口与触发

- /workspace/erpapi/沃尔玛问题商品清理/daily_cleanup.py:584 main() — 唯一入口，无 argparse，只认 sys.argv 里的 --dry-run（daily_cleanup.py:585）
- 定时调度：/workspace/erpapi/定时任务skill/walmart-daily-cleanup/SKILL.md — taskId walmart-daily-cleanup，cron `0 0,6,12,18 * * *`（SKILL.md 里写「每6小时 00/06/12/18:04」），命令 `cd 沃尔玛问题商品清理 && /opt/homebrew/bin/python3 daily_cleanup.py`；由 AI 跑 skill，前后各调一次 定时任务skill/notify.py start|done 发飞书简报
- 子模块入口（都被 daily_cleanup 以 import + run() 方式动态调用）：relisting.split_and_relist(all_rows, store_tokens)（relisting.py:172）、db_store.save_run(all_rows, delete_results, ts)（db_store.py:172）、feishu_sync.sync_to_feishu(...)（feishu_sync.py:814）、brand_collector.run(all_rows)（brand_collector.py:352）、blacklist_sync.run(all_rows, dry_run=False)（blacklist_sync.py:117）
- blacklist_sync.py:183 有 __main__ 独立入口：默认 DRY-RUN，只扫监管合规表，加 --write 才真写
- 一次性脚本：scripts/backfill_db_from_excel.py（历史 Excel → PG，可重复跑）、scripts/init_error_stats.py（categorize 规则变更后重建错误统计 + 重置 seen 缓存）、scripts/backfill_brands.py（历史 C/E ASIN 回填 DMIT 队列）、scripts/test_relisting_live.py

### 调度

cron `0 0,6,12,18 * * *`（本地时间，每 6 小时一轮），taskId `walmart-daily-cleanup`，由 AI 执行 SKILL.md（/workspace/erpapi/定时任务skill/walmart-daily-cleanup/SKILL.md，生产机上另有一份 ~/.claude/scheduled-tasks/walmart-daily-cleanup/SKILL.md）。SKILL.md 要求任务前后各调 定时任务skill/notify.py start / done 发飞书简报，并明确写"非幂等，绝不盲目整条重跑——重复删除/重复延期"，个别店铺失败只记录跳过、等下一轮（最多 6h）自然补。历史上从单次/每日 1 次迭代为每 6h（README.md:248）。

### 数据存储

- PostgreSQL 库 walmart_cleanup（本机 homebrew postgresql@17，peer 认证免密）——db_store.py:31 DB_NAME，db_store.py:70 `psycopg2.connect(dbname=DB_NAME)` 无 host/user/password，直接绕过任何共享连接封装
- 表 runs：run_ts TIMESTAMP PRIMARY KEY / source TEXT('live'|'backfill') / total_rows / created_at（db_store.py:34-39）
- 表 error_items：id BIGSERIAL / run_ts / store / sku / wpid / gtin / upc / product_name / product_type / status / lifecycle / price NUMERIC / inventory INT / last_updated TEXT / reason / category CHAR(1) / delete_time TEXT / feed_id TEXT，UNIQUE(run_ts, store, sku)，另有 store/sku/category 三个单列索引（db_store.py:41-66）。当前体量约 41.7 万行明细，起始 2026-04-12（README.md:242）
- /workspace/erpapi/沃尔玛问题商品清理/cache/submitted_skus.json（489 KB，64 个店铺键）：{店铺名: {SKU: ISO时间戳}}，DELETE 防重（daily_cleanup.py:58,65-103）
- /workspace/erpapi/沃尔玛问题商品清理/cache/revived_skus.json（1.0 MB，3937 个 SKU）：{"sku_attempts": {SKU: {store, attempts, first, last, feed_ids[]}}}，反补计数（relisting.py:52,59-96）
- /workspace/erpapi/沃尔玛问题商品清理/cache/brand_cache.json（46 KB）：{"pending_batches": [{batch_id, batch_name, submitted_at, category, source, asins[]}], "processed_asins": [2544 个]}（brand_collector.py:43-45,76-98）
- /workspace/erpapi/沃尔玛问题商品清理/cache/seen_sku_categories.json（4.2 MB，201195 对）：{"seen": [[SKU, 归类代码], ...]}，错误统计累计去重集合，每轮全量读+全量重写（feishu_sync.py:463-486）
- 历史 Excel 归档 ~/Downloads/walmart_error_items_%Y%m%d_%H%M.xlsx（228 个文件，2026-06-11 起不再产出；仅回补脚本仍依赖：scripts/backfill_db_from_excel.py:29、scripts/init_error_stats.py:34 硬编码 /Users/nextderboy/Downloads）
- 运行期临时 Excel：tempfile.gettempdir()/walmart_error_items_{ts}.xlsx，仅作邮件附件，发送后立即 os.remove（daily_cleanup.py:476, 645-652）
- 店铺凭证与代理来源：../店铺API.xlsx Sheet1，经 walmart_client.load_stores() 读取（walmart_client.py:143-183），ClientId 为 0 或代理任一字段为 0 的店铺直接跳过

### 飞书使用

- 主表 spreadsheet_token = YlA1…dd(token已脱敏,见旧仓库代码)（feishu_sync.py:18-19，电子表格非多维表格，全部按列位置索引）
- sheet d4593b「店铺汇总」：A=店铺 B=状态 C=累计总量 D=当日量；新的一天在 D 列（index 3）insert_dimension_range 插新列并写日期（格式 2026.4.13 无前导零，feishu_sync.py:192-195）；同日再跑则累加 C/D；非 ACTIVE 店铺 D 列写 seller_status 字符串而非数字；最后整表按店铺名排序重写（feishu_sync.py:307-395）
- sheet aCz4c「错误统计」：双区。左区 A1:E14 = 代码/名称/匹配关键词/总数/占比%（13 归类）；右区 G1:W —— G=状态 H=ProductType I=总数 J=占比% K..W=13 个归类列，第 2 行是「全部合计」行，第 3 行起按总数降序（feishu_sync.py:519-654）
- sheet WvPTz2「禁止品牌收集」：A=品牌名 B=来源 C=入库日期(YYYY-MM-DD) D=SKU(ASIN)。来源列取值固定二选一：'沃尔玛-品牌限制'(C 归类) / '沃尔玛-侵权'(E 归类)，兜底 '沃尔玛后台'（brand_collector.py:50-53、feishu_sync.py:789）。写入不用 +append 而是先读全表定位最后非空行再 _write，注释说 +append 遇日期富文本行会误判空行覆盖已有数据（feishu_sync.py:756-758）
- sheet eGjQRX「监管合规删除」：A=店铺 B=SKU C=商品名称 D=ProductType E=错误原因 F=完成(是/空) G=删除时间 H=del_feedId I=ret_feedId。F 列≠'是' 即待处理（feishu_sync.py:661-688），处理后整段回写 C:I 并把 F 置 '是'（feishu_sync.py:691-719）
- 动态 sheet「{日期}问题商品」：13 列 DAILY_COLS = 店铺/SKU/WPID/商品名称/ProductType/状态/生命周期/价格/库存/上次更新/错误原因/删除时间/feedId（feishu_sync.py:25-30）。新建时插在「监管合规删除」的 index+1 位置（feishu_sync.py:254-255），同日重跑按 (店铺,SKU) 去重追加，还带表头缺失自愈逻辑（feishu_sync.py:242-247）
- Amazon 选品黑名单（另一个 workbook，wiki 宿主）：token QNIp…Bb(token已脱敏,见旧仓库代码) / sheet 8280e8 / A=黑名单卖家店铺ID(留空) B=黑名单ASIN(写入目标，表头'黑名单ASIN') C=黑名单类目(留空)（blacklist_sync.py:46-52）
- 调用方式：feishu_sync 走 lark-cli 子进程或 lark_io shim（环境变量 LARK_IO_SHIM，默认 '1' 走 shim，设 '0' 回落 subprocess lark-cli），identity 固定 'bot'（feishu_sync.py:50-92）；blacklist_sync 直接用 lark_io.workbook_info / read_range / append_rows（blacklist_sync.py:100-114,170-177）
- 所有字段名/列位置都是代码里的字符串字面量与列索引硬编码，表头改名或插列会静默错位——这是新仓库 registry 字段常量铁律的直接对象

### 沃尔玛端点

- GET /v3/report/payment/statement — 只用来读 payload.sellerInfo.sellerStatus 判断店铺是否 ACTIVE（daily_cleanup.py:225-241）。⚠️ 走 walmart_client.safe_get（合规）
- GET /v3/items?publishedStatus=UNPUBLISHED|SYSTEM_PROBLEM&limit=200&offset=N[&nextCursor=...] — 主数据源，走 walmart_client.safe_get（合规）。分页语义特殊：nextCursor 是会话 ID，全程不变，真正翻页靠客户端自增 offset；终止条件为本页数 < limit 或 offset >= totalItems（daily_cleanup.py:244-337）。限速 60/min
- POST /v3/feeds?feedType=DELETE_ITEM — payload {ItemFeedHeader{locale:en, version:DELETE_ITEM_VER, businessUnit:WALMART_US}, Item:[{Deletable:{sku}}]}。⚠️ 绕过 walmart_client：直接 httpx.post（daily_cleanup.py:386-395），只复用了 make_headers + store['proxy']，没有连接池/429 退避/重试。限速 10/小时/店铺
- POST /v3/feeds?feedType=RETIRE_ITEM — payload {RetireItemHeader{feedDate, version:'1.0'}, RetireItem:[{sku}]}。⚠️ 同样直接 httpx.post（daily_cleanup.py:414-423）。注意：RETIRE 只在 Step 0 监管合规路径里调用（daily_cleanup.py:185），Step 2 常规删除路径并不调用，README 的『DELETE+RETIRE 兜底』只对合规路径成立
- POST /v3/feeds?feedType=MP_MAINTENANCE — payload {MPItemFeedHeader{businessUnit,locale,version}, MPItem:[{Orderable:{sku, productIdentifiers{productId,productIdType}, endDate}}]}，用于把 endDate 改 2049-12-31 做反补。⚠️ 直接 httpx.post（relisting.py:135-144）。限速 30/min
- 本模块不查 feed 状态：提交后只记 feedId，从不 GET /v3/feeds/{feedId} 回执对账（README 列了 5000/min 配额但代码里没有调用点）
- 非沃尔玛外部依赖：DMIT amazon-scraper http://<SCRAPER_VPS_IP,见旧仓库>:8899 —— POST /api/upload(multipart file+batch_name)、GET /api/export/{batch_name}?format=csv&fields=asin,brand、GET /api/results?batch_id&limit=200&cursor（旧接口兜底）。无鉴权、明文 HTTP、IP 硬编码（brand_collector.py:39,116,146,175）
- SMTP smtp.163.com:465，账号密码明文硬编码在 daily_cleanup.py:48-52，收件人 2622049011@qq.com

### 魔数与踩坑参数

- daily_cleanup.py:56 FETCH_LIMIT=200 — GET /v3/items 每页条数（nextCursor 模式）
- daily_cleanup.py:323 time.sleep(1.1) — GET /v3/items 带 query 参数限速 60/min，注释明说留 9% 余量防 429；只在翻页之间 sleep，不跨 status/店铺
- daily_cleanup.py:355 ThreadPoolExecutor(max_workers=8) — 全店铺并发拉取的并发度
- daily_cleanup.py:57 DELETE_ITEM_VER="5.0.20250919-16_45_47-api" — DELETE_ITEM feed 的 ItemFeedHeader.version，硬编码 spec 版本
- daily_cleanup.py:409 RetireItemHeader.version="1.0" + feedDate 用本地时间强行拼 "...T%H:%M:%S.000Z"（未做 UTC 转换）
- daily_cleanup.py:392,420 httpx.post(timeout=60)；relisting.py:141 timeout=120 —— 但 import walmart_client 时会执行 socket.setdefaulttimeout(90)（walmart_client.py:44），所以 120s 实际被 90s socket 超时截断
- relisting.py:45 SPEC_VERSION="5.0.20260304-22_45_32-api"（与 DELETE 的版本号不同，各自硬编码）
- relisting.py:48 NEW_END_DATE="2049-12-31T00:00:00.000Z" — 必须是 ISO8601 datetime，官方文档写 YYYY-MM-DD 是误导，纯日期会被 Walmart 拒（注释 relisting.py:46-47）
- relisting.py:49 MAX_ATTEMPTS=2 — 同 SKU 反补 2 次仍 UNPUBLISHED 就转 DELETE 兜底，理由是避免 Walmart 标记反复违规
- relisting.py:50 MAX_SKUS_PER_FEED=1000 — MP_MAINTENANCE 单 feed SKU 上限（注明是保守值）
- relisting.py:51 ATTEMPT_RESET_DAYS=30 — 30 天前的反补记录清除，给 SKU 二次机会（等于 attempts 归零）
- relisting.py:161-164 productId 选择优先级 GTIN(zfill 14) > UPC(zfill 12)，长度 <8 视为无效；两者都无则该 SKU 不反补
- feishu_sync.py:31 BATCH_ROWS=384 —— 384 行 × 13 列 = 4992 格，飞书单次写 5000 格上限
- feishu_sync.py:638 STATS_BATCH_ROWS=290 —— 290 × 17 = 4930 格；scripts/init_error_stats.py:35 同样 BATCH=290
- feishu_sync.py:378,718 批量 range 更新每次 100 个 range 一批
- feishu_sync.py:735,766 「禁止品牌收集」读取硬编码 range A1:D20000（超 2 万行就静默截断）；注释说 sheet_id "WvPTz2" 不带 range 后缀会被 lark-cli 误当 A1
- brand_collector.py:40 DMIT_TIMEOUT=60s；:41 PENDING_MAX_HOURS=24（pending batch 超 24h 强制放弃并标记 processed，防无限挂起）；:143 results 分页 limit=200
- brand_collector.py:113 batch_name 用毫秒精度时间戳 `walmart_c_%Y%m%d_%H%M%S_mmm`，注释说明否则同秒重复提交会被 DMIT 合并进旧 batch
- brand_collector.py:290 / blacklist_sync.py:58-60 ASIN 判定口径：len==10 且 isalnum()（沃尔玛 SKU 即 Amazon ASIN）
- blacklist_sync.py:55 _APPEND_BATCH=500 — 黑名单 append 分批行数；scripts/backfill_brands.py:38 BATCH_SIZE=500
- db_store.py:161 execute_values(page_size=1000)
- walmart_client.py:248-251 token 有效期 expires_in（默认 900s）减 60s = 约 840s，daily_cleanup.py:117 的注释就是被这个坑过（原逐 SKU 串行查询 1262 条 × 1.1s 中途 401）

### 防重/幂等语义

多层且各不相同，逐条对照迁移：(1) DELETE 防重 —— 号称 2 日，实际是"同一自然日"：get_today_submitted 只匹配 `datetime.fromisoformat(ts).date() == today`（daily_cleanup.py:81-88），record_submitted 的 2 天 cutoff 只是防缓存无限增长（daily_cleanup.py:92-100）。所以昨天 18:00 提交过的 SKU，今天 00:00 那轮会被再次提交 DELETE_ITEM；README「2 日去重」的说法与代码不符，迁移时必须先确认业务真实意图。另外该清理是按店铺 key 在写入时就地 prune，久未出现的店铺会留下过期条目（缓存里仍有 2026-05-16 的记录）。(2) 缓存只在 record 时更新、失败不回滚：delete_store_items 抛异常时不写缓存（daily_cleanup.py:459 在成功分支内），但 Step 0 合规删除是成败都提前写缓存（daily_cleanup.py:190-192），语义不一致。(3) 反补防重 —— cache 里 attempts >= MAX_ATTEMPTS(2) 的 SKU 转 DELETE；attempts 只在提交成功后 +1；ATTEMPT_RESET_DAYS=30 后记录被清除等于重新给两次机会（relisting.py:81-96,217-226,258-267）。(4) 错误统计去重 —— 全局 (SKU, 归类代码) 对集合，同 SKU 同归类跨天只计 1 次，同 SKU 换归类算新事件（feishu_sync.py:572-598）。(5) 数据库幂等 —— error_items UNIQUE(run_ts, store, sku) + ON CONFLICT DO NOTHING，且入库前先在内存按 (store, sku) 去重；runs 表 ON CONFLICT(run_ts) DO UPDATE。回补脚本用 runs 里已存在的 run_ts 集合做断点续跑（db_store.py:100-165、scripts/backfill_db_from_excel.py:36-40,99-101）。(6) 飞书当日明细 —— 读现有 sheet 的 (店铺,SKU) 集合过滤，同日多次运行不重复写行（feishu_sync.py:235-274）。(7) 品牌采集 —— 排除集 = 飞书「禁止品牌收集」D 列已登记 ∪ pending_batches 里的 asins ∪ processed_asins（brand_collector.py:270-276）；DMIT 返回 "待采集"/"处理中" 不算处理完，"失败"+"无产品库数据，本次失败" 也不标记 processed（会重试），其余无论 brand 有效与否一律标 processed 不再重试（brand_collector.py:220-243）。(8) 黑名单 —— 每次实时读黑名单 B 列全量做差集，无本地缓存，纯幂等（blacklist_sync.py:100-114,148-149）。(9) API 层去重 —— Walmart 分页会返回重复 SKU，fetch_store_items 按 SKU 保留首次出现并打印重复数（daily_cleanup.py:325-335）。

### 危险操作

- POST /v3/feeds?feedType=DELETE_ITEM 批量删除商品 —— 全店铺、全量、无人工确认。唯一保护是 --dry-run 参数（daily_cleanup.py:585,604-606）和当日 submitted_skus 缓存；无 SKU 数量上限、无单店阈值熔断、无「本轮删除数突增就中止」的保护
- POST /v3/feeds?feedType=RETIRE_ITEM 停用商品（Step 0 合规路径，daily_cleanup.py:185）
- POST /v3/feeds?feedType=MP_MAINTENANCE 改 endDate（写操作，会让已下架商品重新上架售卖，方向相反但同样是不可撤销的线上变更）
- ⚠️ dry-run 覆盖不全：Step 0 监管合规删除在 dry-run 下被整体跳过（daily_cleanup.py:593，不会预览也不会执行），而 dry_run_summary 只统计 Step 1 的原始 SKU，不扣除反补 SKU 和 Stage SKU，预览数量必然大于真实删除数（daily_cleanup.py:538-577）。dry-run 输出不能直接当作 --execute 前的人眼确认依据
- ⚠️ 店铺状态判定 fail-open：结算接口无 payload 时默认视为 ACTIVE（daily_cleanup.py:236-239），意味着接口异常的店铺照样会被提交 DELETE
- ⚠️ Step 2 复用 Step 1 阶段取得的 token（daily_cleanup.py:453,457），中间隔了整个反补流程，token 有效期约 840s，长跑时可能已过期导致 401；合规路径反而显式重新 get_token（daily_cleanup.py:174-178）——两条路径行为不一致
- ⚠️ 提交完 feed 后从不查回执，也没有「先写 pending 再提交」的落库保护；feedId 只落在 error_items.feed_id / 飞书 M 列 / revived_skus.json，程序中途崩溃时无法判断 feed 是否真的提交成功
- 飞书「错误统计」/「店铺汇总」用整表重写模式（_rewrite_sorted 先写空白再分批写回，feishu_sync.py:198-215），中途失败会留下被清空的表
- 邮件 SMTP 明文密码硬编码 daily_cleanup.py:48-52（README.md:229 已标注迁移前必须移除）

### 事故教训与必须保留的行为

- pandas NaN 是 truthy，`str(v) or ""` 拦不住，会产出字面量 "nan" 污染统计维度——曾累积 29 次禁售错误挂在 ProductType="nan" 上，因此有专门的 _clean()（feishu_sync.py:38-47，db_store.py:95-98 同款）
- Walmart 拒绝纯日期 "2049-12-31"，官方文档写 YYYY-MM-DD 是误导，实际 endDate 字段是 date-time，必须 ISO8601 带时间（relisting.py:46-48，README 变更记录 2026-05-14 有此条）
- 逐 SKU 串行 GET /v3/items?sku= 会在 token 840s 有效期内跑不完（1262 条 × 1.1s）触发 401；改为全量分页拉取 + 内存 join（daily_cleanup.py:110-130）
- Walmart 的 nextCursor 不是游标而是会话 ID，全程值不变；真正翻页靠客户端自增 offset。误当游标用会死循环或只拿到首页（daily_cleanup.py:246-254，注释引用 memory/walmart_api_fetch_experience.md，实战验证 30 店 99197 件）
- Walmart 分页会返回重复 SKU，必须去重（daily_cleanup.py:325-335）
- 飞书 +append 遇到日期富文本行会把它误判成空行从而覆盖已有数据，所以「禁止品牌收集」改成先读全表定位最后非空行再精确 _write（feishu_sync.py:756-758）
- lark-cli 的 --sheet-id 参数如果不带 range 后缀（如裸 "WvPTz2"）会被误当成 A1 range，必须显式写 `WvPTz2!A1:D20000`（feishu_sync.py:729-731）
- 飞书单次写入 5000 格硬上限，所有批量写都按列数反算行数（384×13、290×17）
- 飞书 insert_dimension_range 是独立端点，sheets_batch_update 不支持 insertDimension（feishu_sync.py:154-157）
- DMIT 同秒内重复提交会被合并进旧 batch，batch_name 必须带毫秒（brand_collector.py:108-113）
- DMIT 会返回 "#"、"N/A" 之类占位符品牌，两道防线过滤：brand_collector._is_valid_brand（:60-69）和 feishu_sync.write_brand_collection_rows 里的 _INVALID 复查（:780-794），规则是 空/单字符/占位符集合/纯标点一律丢弃
- 「Stage status until you go live」是卖家暂存待发布的中间态，不是错误，删了等于误杀（feishu_sync.py:502-512、daily_cleanup.py:620-628）。注意该关键词同时被归入 J 类（feishu_sync.py:445），所以 J 类里混了正常商品
- categorize 采用严重性顺序而非字母顺序：C,D,E,F,G,H,I,J,K,L,B,A —— 具体归类优先，通用禁售 B 次之，过期 A 最后；多条 reason 用 ' | ' 拼接后整体匹配，任一命中即采用（feishu_sync.py:456,488-499）
- 改 categorize 规则后必须跑 scripts/init_error_stats.py 重建累计表并重置 seen 缓存，否则历史 (SKU,归类) 对与新规则不一致（README.md:231）；但该脚本依赖已停产的 ~/Downloads Excel，实际上已经不可重跑——这是个已经埋下的死结
- 黑名单只收 B/C/E/F/G/K（永久产品级禁止），明确排除 A/D/H/I/J/L/Z（可修复/临时/平台类），进了会误杀（blacklist_sync.py:18-21）
- daily_cleanup.py:577 有一行 `SPREADSHEET_URL if False else '...'`，SPREADSHEET_URL 在本文件根本没 import，靠 Python 条件表达式的惰性求值才没 NameError——是死代码地雷
- seen_sku_categories.json 每轮全量 json.load + json.dump（当前 20 万对 / 4.2 MB），随时间单调增长，永不清理
- init_error_stats.py:34 与 backfill_brands.py:37 硬编码 /Users/nextderboy/Downloads 绝对路径（backfill_db_from_excel.py:29 用了 expanduser，三者不一致）

### 切换时必须迁移的状态

- cache/submitted_skus.json → ops.dedupe(scope='cleanup:submitted_sku')：64 店铺 × 若干 SKU 的最近提交时间戳，不搬会导致切换当天对同批 SKU 重复提交 DELETE_ITEM feed（feed 配额 10/小时/店铺）
- cache/revived_skus.json → ops.dedupe(scope='cleanup:relist_attempt') 或独立表：3937 个 SKU 的 attempts/first/last/feed_ids，不搬则 attempts 归零，已判死刑的 SKU 会重新进入反补循环（README 明写「可能死循环」）
- cache/seen_sku_categories.json → 一张 (sku, category) 唯一表：20.1 万对，是飞书「错误统计」表累计数字的唯一真值来源，不搬则统计虚胖，只能重跑 scripts/init_error_stats.py 且该脚本依赖已停产的 ~/Downloads Excel
- cache/brand_cache.json → ops.dedupe(scope='cleanup:brand_asin') + pending batch 表：processed_asins 2544 个（不搬会向 DMIT 重复推送）、pending_batches（不搬则在途批次结果永久丢失）
- PostgreSQL walmart_cleanup.error_items 约 41.7 万行 + runs 表全部 run_ts（2026-04-12 起）→ 并入 walmart_data；注意 2026-05-14 之前的历史行 reason/category 为空是正常现象（README.md:153）
- 飞书侧无需搬但必须继承语义：「监管合规删除」表 F 列='是' 的已完成标记（否则新系统会把历史行重删一遍）、「禁止品牌收集」D 列已登记 ASIN、Amazon 选品黑名单 B 列已有 ASIN——这三处是跨系统的幂等锚点，新系统必须读同样的位置

### 迁移建议

照搬（业务知识，重写代价高且容易漏）：13 个归类的关键词匹配表与严重性顺序 feishu_sync.py:404-456、is_stage_pending、GTIN>UPC 的 productId 选择与 zfill 规则、黑名单入选归类集合 {B,C,E,F,G,K} 及其排除理由、DMIT 占位符品牌过滤规则、nextCursor=会话ID+offset 自增的分页语义、NEW_END_DATE 必须是 ISO8601 datetime、_clean 的 NaN→空串处理。这些直接搬进 services/（建议 services/problem_item.py 承载 categorize/is_stage_pending/pick_product_id，services/blacklist.py 承载归类入选判定），归类关键词表本身建议放 refdata/ 做成可版本化的 TSV。

必须重做：
1) 三处 httpx.post 直连（daily_cleanup.py:386,414；relisting.py:135）改走 api/feeds.py 统一提交——它们虽然传了 store['proxy'] 没有违反固定出口 IP 铁律，但绕开了 walmart_client 的连接池、429 退避、x-current-token-count 自适应，且 relisting 的 120s timeout 被 socket 90s 悄悄截断。
2) db_store.py:70 的裸 psycopg2.connect 改走 registry/db.py。
3) 四个 cache JSON 全部迁 ops.dedupe / ops.cursors：scope 建议 'cleanup:submitted_sku'（meta 存 store+ts）、'cleanup:relist_attempt'（meta 存 attempts/feed_ids）、'cleanup:seen_sku_category'、'cleanup:brand_asin'；brand_cache 的 pending_batches 语义是「在途异步作业」，更适合单独一张 ops 表或 ops.feed_log 的同构表。
4) 所有飞书 token / sheet_id / 列位置迁 registry/resources.py 常量；这个模块是全项目列位置硬编码最密集的地方（DAILY_COLS 13 列、错误统计 G-W 双区、合规表 A-I、黑名单 B 列），必须逐个登记。
5) SMTP 明文凭据与 DMIT IP 迁 .env / registry。
6) 提交前落库：按新铁律，DELETE/MP_MAINTENANCE 提交前先写 ops.feed_log(status='pending')，拿到 feedId 改 'submitted'，并新增一条「启动对账」逻辑去 GET /v3/feeds/{feedId} 补状态——旧系统这块完全空白，是本次迁移最大的净增量。
7) dry-run 要覆盖 Step 0，且预览必须扣除反补 SKU 与 Stage SKU（旧 dry_run_summary 报的数字偏大，不可直接作为人眼确认依据）。
8) Step 2 复用旧 token 的问题：新实现每次提交前统一从 api/_client 取 token（有缓存，不额外开销）。

拆分建议（旧 7 个 Step 耦合在一个 main 里，故障隔离靠 try/except，不适合直接搬）：拆成 5 条独立 workflow —— cleanup_compliance_delete（原 Step 0，dangerous）、cleanup_problem_items（原 Step 1/1.5/1.6/2，dangerous，内部顺序不可变：先反补再排 Stage 再 DELETE）、cleanup_report（原 Step 3/4/5，非危险，可独立重跑）、brand_collect（原 Step 6，非危险，两阶段异步，pending 回收与新批提交都幂等）、blacklist_sync（原 Step 7，非危险，纯幂等，最适合第一个迁移做试点）。cli.py 统一负责锁/ops.runs/飞书通知，替代 SKILL.md 里的 notify.py 两次调用。

切换顺序（严禁新旧并跑破坏性任务）：先迁 blacklist_sync 与 cleanup_report 这两条只读/幂等的验证链路 → 再迁 brand_collect（搬 processed_asins + pending_batches）→ 最后一次性停掉旧 cron、搬 submitted_skus + revived_skus、pg_dump 并入 41.7 万行、再起新调度；cleanup_problem_items 与 cleanup_compliance_delete 必须同一时点切换，不能只切一半（两者共用 submitted_skus 防重缓存）。历史 41.7 万行按 (run_ts, store, sku) 唯一键整体导入，source 字段保留 live/backfill 区分，2026-05-14 之前的行 reason/category 为空需在新表允许 NULL 并在文档标注。

### 待确认问题

- DELETE 防重到底该是「同一自然日」还是「滚动 48 小时」？代码实现是前者，README/注释都说后者。迁移前需业务确认——按 6h/轮的节奏，选错会直接影响 DELETE_ITEM 10/小时/店铺 的配额消耗
- 41.7 万行 error_items 的目标落点：是进 listing schema 做「问题商品快照历史」，还是进 ops 做运行明细？现表是 per-run 全量快照（同一 SKU 每轮都重复一行），直接平移会持续膨胀；是否改成状态变更表（只记首次出现/消失）需要决策
- walmart_cleanup 库是 peer 认证免密的本机库，新库 walmart_data 的连接方式与之不同，迁移时是 pg_dump/restore 后 ALTER 归位，还是 postgres_fdw / dblink 增量搬？需确认生产机上两库是否同实例
- 飞书「错误统计」表的累计语义（全历史 (SKU,归类) 去重计数）在新系统里是否还保留？如果改为直接从 PG 聚合出图，seen_sku_categories.json 这 20 万对就不必迁移，但历史口径会跳变
- DMIT amazon-scraper（<SCRAPER_VPS_IP,见旧仓库>:8899，无鉴权明文 HTTP）是否继续沿用？它同时被 brand_collector 与其他模块使用，属于跨模块共享外部服务，registry 里该如何登记
- Step 0 监管合规删除在 dry-run 下完全跳过，新系统的 dangerous=True 强制 dry-run 语义要求「打印将对哪些 SKU 做什么」——这一步需要重写成可预览
- RETIRE_ITEM 目前只在合规路径调用，常规 DELETE 路径不调用。是刻意如此还是历史遗漏？另有独立定时任务 walmart-daily-retire，两者职责边界需确认
- 本模块从不查 feed 回执，所以「哪些 DELETE 实际生效」在数据上是空白。新系统的 ops.feed_log + 启动对账要接管这块，需要决定是否为存量 feedId 做一次历史回查


<a id="catalog_listing"></a>
## auto_listing + match_listing + sync_online_products → listing / catalog_sync (#9/#10)

### 模块职责

旧仓库中最大最复杂的业务域，由两个互不耦合的双轨子系统组成。(1) `auto_listing/`：把 Amazon ASIN 全量建品搬到 Walmart Marketplace 的端到端闭环——飞书上架表领任务 → 店铺状态/日配额闸门 → 风险拦截(禁售PT/品牌黑名单) → DMIT scraper 拉 Amazon 数据 → 库存/定价廉价过滤 → 变体分组与维度重映射 → UPC 池领号 → LLM(DeepSeek) 映射成 MP_ITEM v5 payload → 同店打包成单个 feed 提交 → T+6h 起 reconcile 回写异步审核结果 → SKU_LOCKED 走 RETIRE_ITEM + 24h 冷却重上 → 每日价格/库存同步 → sync_status_track 用 Walmart catalog 反查真实状态并自愈 Unknown 行。(2) `match_listing/`：跟卖(Offer Setup by Match)独立系统，只用 UPC 对已在售商品挂 offer，走 MP_ITEM_MATCH v4.2 feed，5 个字段，不建内容、不分配 UPC、不管库存，与 auto_listing 零耦合。(3) `tools/sync_online_products.py`：每日把 57 店 Walmart catalog + inventory + DMIT 三源 merge 后整块重写飞书「在线产品总表」(20 列，13 万行)，这张表既是 ASIN 全局去重的数据源，也是 sync_status_track 的 Plan B 数据源。

### 入口与触发

- python3 -m auto_listing.main [--dry-run|--asin|--asins|--limit|--workers N|--xlsx path|--live-spec|--no-feishu-mirror|--skip-feishu-dedup|--strict-file-store] — 主上架编排，Phase 0/0.5/0.7/0.8/1/2/2.5/3 全在单进程内 (auto_listing/main.py:635)
- python3 -m auto_listing.scheduler <cmd> — 调度统一入口，COMMANDS = list_new_only / reconcile_due / reconcile_t2 / sync_status / sync_inventory / retire_locked / relist_cooled / health_report / full_morning / daily_noon (auto_listing/scheduler.py:214)。scheduler 用 subprocess 起子模块，不 in-process 调用 (scheduler.py:36)
- python3 -m auto_listing.auto_reconcile [--force|--dry-run] — 扫 pending_feeds.json 到期 feed 并调 reconcile (auto_listing/auto_reconcile.py:39)
- python3 -m auto_listing.reconcile <feedId> <store> | --days-ago 1,2 | --date YYYY-MM-DD --workers 8 (auto_listing/reconcile.py:409)
- python3 -m auto_listing.sync_status_track — 拉 Walmart /v3/items 或走「在线产品总表」Plan B，写飞书 R-W (auto_listing/sync_status_track.py:656)
- python3 -m auto_listing.sync_price_inventory --auto [--price-mode auto|single|batch --inventory-mode ... --batch-threshold 50 --inv-batch-threshold 2000] (auto_listing/sync_price_inventory.py:72)
- python3 -m auto_listing.retire_and_relist --retire | --relist | --auto | --dry-run (auto_listing/retire_and_relist.py:248)
- python3 -m auto_listing.update_listed --fields images,copy,attributes,price,inventory,shipping,all [--field-overrides] — MP_MAINTENANCE 维护已上架字段 (auto_listing/update_listed.py:352)
- python3 -m auto_listing.upc_audit [--limit N] — UPC 池全站冲突审计 (auto_listing/upc_audit.py:246)
- python3 -m auto_listing.quota check|refresh <store>；python3 -m auto_listing.pending_feed_tracker stats|due|gc；python3 -m auto_listing.retry_state [--asin|--purge-days]；python3 -m auto_listing.check_feed <feedId> <store>；python3 -m auto_listing.store_status；python3 -m auto_listing.risk_gate [--force]
- 一次性修复脚本（迁移时不需要照搬，但解释了历史数据形态）：fix_end_date.py / fix_sheet_consistency.py / fix_upc_pool.py / mark_sku_locked_as_fail.py / resubmit_278.py（后者硬编码了绝对路径 auto_listing/resubmit_278.py:18）
- python3 -m match_listing.main --input 跟卖清单.xlsx [--dry-run|--store|--no-spec-precheck|--no-poll|--feishu --uploader X] (match_listing/main.py:190)
- python3 tools/sync_online_products.py [--workers 16|--store|--skip-dmit|--no-dmit-refresh|--skip-inventory|--skip-patch] (tools/sync_online_products.py:798)
- launchd 4 条（macOS LaunchAgent，绝对路径 /Users/nextderboy/Projects/erpAPI，解释器 /opt/homebrew/bin/python3）：com.user.autolisting.morning 06:00 full_morning / reconcile_hourly 每小时 :15 reconcile_due / retire_daily 23:30 retire_locked / health_4x 08,12,16,20:00 health_report
- auto_listing/dedup_sync_to_server.py — 由 Claude Code scheduled-tasks MCP 每天 14:06 触发，推「在线产品总表」B 列 ASIN + 店铺状态到 erp_listing_server /api/dedup/import
- Excel/worker 模式：DMiT Server :9080 长轮询下发 task，Mac 上 20 个 worker 各跑 `main --xlsx`（见 auto_listing/docs/listing_flow_full.md）

### 调度

macOS launchd 4 条（auto_listing/launchd/*.plist，WorkingDirectory=/Users/nextderboy/Projects/erpAPI，解释器 /opt/homebrew/bin/python3，PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin，RunAtLoad=false）：
- com.user.autolisting.morning — 每天 06:00 → scheduler full_morning（顺序：sync_inventory → reconcile_due → relist_cooled → list_new_only）。06:00 是因为 DMIT 凌晨 2:00 全量采完，留 4h buffer。
- com.user.autolisting.reconcile_hourly — 每小时第 15 分 → scheduler reconcile_due。
- com.user.autolisting.retire_daily — 每天 23:30 → scheduler retire_locked。
- com.user.autolisting.health_4x — 每天 08/12/16/20:00 → scheduler health_report（只读，推飞书机器人）。

**没有 plist 的命令**（只能手动或挂在 full_morning 链里）：daily_noon、reconcile_t2、sync_status、relist_cooled 单跑。注意 sync_status_track 是 R-W 列和 Unknown 自愈的唯一来源，却没有独立定时任务——这是已知缺口。

其它调度源：
- auto_listing/dedup_sync_to_server.py 由 Claude Code `scheduled-tasks` MCP 每天 14:06 触发（推「在线产品总表」B 列 + 店铺状态给 erp_listing_server）；店铺状态另有每小时轻量同步。
- tools/sync_online_products.py 描述为"每日"跑，但范围内未见 plist/cron 定义（可能在 定时任务skill/ 目录里，本次读取范围外）。
- Excel/worker 模式由 DMiT Server :9080 长轮询下发 task，Mac 上 20 个 worker 并发跑 main --xlsx，不走 launchd。
- match_listing 无定时任务，纯人工触发（或 worker 拉 task）。

### 数据存储

- auto_listing/state/pending_feeds.json — feedId → {store, submitted_at(UTC ISO), n_items, feed_path, first_check_at, last_status, attempts, done, result, finished_at}。feed_submit 成功即 register，reconcile 后 update_status。仅 threading.Lock 进程内锁，多进程会丢写 (auto_listing/pending_feed_tracker.py:33,57,107)
- auto_listing/state/feed_history.json — 终态/超期归档的 feed (pending_feed_tracker.py:126,132)
- auto_listing/state/retry_state.sqlite — 失败累计状态机，表 retry_state(asin,store,kind,attempts,first_seen,last_seen,terminated)，PK=(asin,store,kind)，WAL+synchronous=NORMAL (auto_listing/retry_state.py:40,74)
- auto_listing/state/llm_cache.sqlite — LLM 输入哈希缓存，表 llm_cache(input_hash PK, model, response, hit_count, created_at, last_hit_at)，key=sha256(model+messages+temperature+max_tokens)[:32]，生产已约 462 MB (auto_listing/llm_cache.py:32,46,62)
- auto_listing/state/retire_log.json — '{store}:{asin}' → {retire_at(UTC ISO), feed_id}，判 24h 冷却 (auto_listing/retire_and_relist.py:44,186)
- auto_listing/state/upc_audit.json — UPC 审计断点续跑进度 (auto_listing/upc_audit.py:48)
- auto_listing/state/upc_claimed_runtime.txt — append-only 行号纯文本，解决飞书 read-after-write 延迟；按 mtime > 3600s 自动清 (auto_listing/upc_pool.py:363,367,370)
- auto_listing/state/upc_pool.lock — fcntl flock 跨进程独占锁，超时 600s (auto_listing/upc_pool.py:354,407)
- auto_listing/state/risk_gate_cache.json — 禁售 PT + 品牌黑名单缓存，TTL 24h，fail-open (auto_listing/risk_gate.py:31,32,107)
- auto_listing/logs/sync_state.json — {store: {sku: {last_price, last_qty, last_synced_at}}}，价格/库存未变跳过 PUT 的唯一依据；注意它落在 logs/ 而不是 state/ (auto_listing/sync_state.py:23)
- auto_listing/logs/feed_{store}_{sku}_and_{n}_more_{ts}.json — 每次提交的完整 MP_ITEM feed 备份，**永久留存**，reconcile 的 SKU→UPC 反查完全依赖它 (feed_submit.py:219；reconcile.build_sku_to_upc_map reconcile.py:77-102)
- auto_listing/logs/ 其它：run_*.log、llm_raw_{asin}_{ts}.json、reconcile_{feedId}_*.log、maintenance_*.json、retire_{store}_{n}skus_{ts}.json、price_feed_*/inventory_feed_*.json、*_decimal_fixes.txt、launchd/{morning,reconcile,retire,health}.{out,err}.log
- walmart_official_specs/MPSetup_by_pt/ — MP_ITEM v5 spec 按 PT 拆分目录 + _pt_index.json + _orderable.json，由 tools/split_mp_item_spec.py 生成（原 451 MB 单文件 json.load 膨胀 1.3 GB 导致 OOM）(auto_listing/pt_spec.py:30,52,63,75)
- walmart_official_specs/MPSetup/5.0.20260304-22_45_32-api_MP_ITEM_0_0_en.json 与 MPMaintenance/...MP_MAINTENANCE...json；pt_templates/pt_templates_full.json（307 MB 单文件，只有 update_listed.py 用）(config.py:71-88, pt_spec.py:41)
- 店铺API.xlsx（项目根，Sheet1）— 多店铺 client_id/client_secret/固定出口代理，load_stores 的唯一来源
- match_listing/logs/match_{store}_{tag}_{ts}.json — MP_ITEM_MATCH feed 备份 (match_listing/feed_submit.py:54)
- 输入/输出 xlsx：main --xlsx 的 DMIT 导出 47 列 + 输出 7 列（auto_listing/excel_io.py）；match_listing 跟卖清单模板.xlsx 与 {输入名}_跟卖结果_{ts}.xlsx

### 飞书使用

- 上架表 app_token=PDsR…Ph(token已脱敏,见旧仓库代码), sheet_id=ea2b2a(title 'Sheet1') — 26 列主流水线。A ASIN / B 店铺 / C amz标题 / D walmart_product_type / E 审核结果(pass|fail) / F 审核理由 / G 审核日期 / H amz价格 / I 库存 / J walmart价格 / K 是否上架(Yes|No|Unknown) / L feedId / M 上架日期 / N 未上架理由 / O 上架结果 / P 上架失败理由 / Q 复核日期 / R 真实标题 / S 真实PT / T 状态跟踪 / U 跟踪日期 / V 真实UPC / W UPC是否一致 / X-Y 价格库存调整(预留) / Z 异常(预留) / AA variantGroupId (config.py:7-35; feishu_io.py:375-403; feishu_io.py:558)
- **列权责严格分工，跨界写就是 bug**：main Phase 0.5 写 C/H/I/J；main Phase 3 写 K/L/M/N(+AA)；reconcile 只写 O/P/Q，绝不碰 D/E/F/G/K/L/M/N/R-W；sync_status_track 写 R/S/T/U/V/W，并可写 K/L/M/N 做 Unknown 自愈；retire_and_relist 写 O(RETIRING) 及清 K/L/M/N/O/P (reconcile.py:6-11; feishu_io.py:661-725)
- **config.py 与实现不一致（迁移必须消歧）**：FEISHU_RANGE_COLUMNS='A:V'(22 列, config.py:10)，read_pending_rows 按此范围读但索引到 25(Z)，所以 W/X/Y/Z 读出来恒为空；且 index 21(V) 的字段名仍叫 'title_similarity'，而 build_status_track_full_update 往 V 写的是真实 UPC、W 写一致性 — 名实不符 (feishu_io.py:334,398-403,669-700)
- UPC 池：同一 app_token PDsR…Ph(token已脱敏,见旧仓库代码)，sheet_id=NxlS1J，A=UPC，B=标记。**标记字符串格式是隐式协议**：'已领 | {ASIN} | {店铺} | {YYYY-MM-DD HH:MM}'（mark_claimed）、'已用 | {ASIN} | {店铺} | {SKU} | {ts}'（mark_used）、'冲突-catalog已存在 | 尝试ASIN={asin} | {店铺} | {ts}'（mark_conflict）、''（未用）。sync_status_track._read_upc_pool_index 按 | 切片解析 parts[1]=ASIN parts[2]=店铺，并强制 ASIN ^B[0-9A-Z]{9}$ + 店铺含中文正则校验 (upc_pool.py:451,596,811; sync_status_track.py:334-402)
- 定价/配额表 app_token=X4vM…bh(token已脱敏,见旧仓库代码), sheet_id=2FJ2Np(子表'定价')，range A1:I500。行1=类型表头 行2=区间表头 行3+=各店。F/G/H/I = FBA上架/FBA下架/FBM上架/FBM下架 日配额；B-E = FBA 0-30、FBA 30-80、FBM 30-100、FBM 100-300 区间倍率。**倍率列是百分比格式，+cells-get 返回格式化值 '275%'**，必须用 _parse_multiplier 解析 (quota.py:56-102; pricing.py:31-55,138-220)
- 店铺状态表 app_token=CRfC…kb(token已脱敏,见旧仓库代码), sheet_id=899f65(子表'总览')，range A1:H200，B 列=店铺名 F 列=状态(ACTIVE/SUSPENDED/TERMINATED)。fail-open：读不到或不在表内都放行 (config.py:48-50; store_status.py)
- 在线产品总表 app_token=MO2e…mI(token已脱敏,见旧仓库代码), sheet_id=e7834a，20 列：A store / B sku / C wpid / D upc / E productName / F shelf / G productType / H price_amount / I availToSellQty / J publishedStatus / K lifecycleStatus / L unpublishedReasons / M 处理后amz标题 / N 相似度 / O amz价格 / P walmart价格 / Q 更新价格 / R 库存 / S 更新库存 / T last_updated。M-S **仅对 PUBLISHED 行计算** (tools/sync_online_products.py:71-84)
- 类目映射「沃尔玛类目」app_token=Gx9H…wc(token已脱敏,见旧仓库代码), sheet_id=0bdc8b，C=Product Type D=准入状态 E=中国卖家可做；拦截条件 D=='禁售' 或 E 以 '否' 开头 (risk_gate.py:34-35,79-91)
- 「禁止品牌收集」app_token=YlA1…dd(token已脱敏,见旧仓库代码), sheet_id=WvPTz2，A=品牌名 B=来源，casefold 精确匹配 (risk_gate.py:36-37,93-100)
- match_listing 结果表：同上架表 spreadsheet，tab title='跟卖结果'（find-or-create by title，不是固定 sheet_id），11 列 A-K：UPC/SKU/售价/重量/店铺/跟卖状态/匹配GTIN/feedId/feed结果/上传人/时间。用 bot 身份写；不用 +append（多行会 90202），改成读 A 列最后数据行 +1 后精确 range +write，还要 +add-dimension 扩 grid (match_listing/feishu_io.py:16-27,99-167)
- 身份：auto_listing/feishu_io FEISHU_IDENTITY='user'（依赖人工 SSO 登录态）；risk_gate / store_status / dedup_sync / match_listing 用 'bot'。LARK_IO_SHIM 默认走 lark_io 包，置 '0' 回退裸 lark-cli subprocess (feishu_io.py:44; risk_gate.py:46; match_listing/feishu_io.py:19)
- 读写方式：+cells-get / values_batch_get（lark-cli 已废弃 +read，但 tools/sync_online_products.py:511 和 match_listing/feishu_io.py:84,101 仍在用 +read）；写用 values_batch_update；append 用 sheets +append

### 沃尔玛端点

- POST /v3/token — 全部经 walmart_client.get_token，单店 900s 缓存复用
- POST /v3/feeds?feedType=MP_ITEM — 主上架，10/小时，同店所有商品打包成**单个** feed 提交 (feed_submit.py:264)
- GET /v3/feeds?limit=20&feedType=MP_ITEM — 提交失败后反查防重复提交，按 itemsReceived==n_items + 时间窗匹配 (feed_submit.py:148)
- GET /v3/feeds/{feedId}?includeDetails=true&offset&limit=50 — reconcile 拉 itemIngestionStatus，分页 step 50 (reconcile.py:129)
- POST /v3/feeds?feedType=MP_MAINTENANCE — update_listed.py:340、fix_end_date.py
- POST /v3/feeds?feedType=RETIRE_ITEM — retire_and_relist.py:125，body 是 RetireItemHeader{feedDate, version:'1.0'} + RetireItem[{sku}]，**与 MP_ITEM 完全不同的 schema**
- POST /v3/feeds?feedType=PRICE_AND_PROMOTION — walmart_price_inventory.submit_price_batch，6/天，极易打爆，只在 --price-mode batch 或 auto 且 >50 个时走
- POST /v3/feeds?feedType=inventory — walmart_price_inventory 批量库存，50/小时，auto 模式阈值 2000
- PUT /v3/price — 单品价格 100/小时（默认路径）；PUT /v3/inventory — 单品库存 200/分 (sync_price_inventory.py)
- GET /v3/items?lifecycleStatus=ACTIVE|RETIRED&publishedStatus=...&limit=1000&offset&nextCursor — sync_status_track 5 轮全量扫店，60/分(带参) (sync_status_track.py:42-49,106)
- GET /v3/items/{sku} 单查 — sync_status_track.fetch_single_sku 补漏，404 才算真 NOT_FOUND (sync_status_track.py:199)
- GET /v3/items/walmart/search?upc=|gtin= — upc_audit 全站冲突审计（200/min，本地配 180/min）；match_listing/spec_check 用 &responseFormat=SPEC 做跟卖预检 (upc_audit.py:45; match_listing/config.py:18)
- POST /v3/items/spec — live_spec 拉实时 PT 模板，3/分 × 最多 20 PT/次 (live_spec.py:44)
- GET /v3/settings/partnerprofile — store_info 拿 Partner ID(Virtual Fulfillment Center ID)，写进 orderable.inventory[].fulfillmentCenterID，lru_cache
- GET /v3/inventories?limit=50&nextCursor= 批量库存 + GET /v3/inventory?sku= 单查补漏 — tools/sync_online_products.py:437,386
- POST /v3/feeds?feedType=MP_ITEM_MATCH（v4.2）+ GET /v3/feeds?limit=50&feedType=MP_ITEM_MATCH 轮询 + GET /v3/feeds/{id}?includeDetails=true — match_listing (match_listing/config.py:19,23; feed_submit.py:88,149,183)
- **未发现绕过 walmart_client 直连的地方** —— auto_listing/match_listing/tools 三处所有 Walmart 调用都 import 自根目录 walmart_client(BASE_URL/get_token/safe_get_ex/safe_post_ex/load_stores)，代理与固定出口 IP 由它统一处理。但非 Walmart 的外部调用是裸 httpx 直连：DMIT scraper http://<SCRAPER_VPS_IP,见旧仓库>:8899（scraper_client / tools/sync_online_products.py:162+）、ERP_SERVER 的 /api/upc/claim、/api/upc/mark-used、/api/upc/release、/api/dedup/asins-existing、/api/dedup/asins-existing-by-store、/api/dedup/anchor-update（main.py:716,768,1249,1872,1875,1893）、DeepSeek/Qwen（llm_client.py）

### 魔数与踩坑参数

- MIN_INVENTORY_THRESHOLD = 5 — 上架时库存<5 直接跳过；同步时库存<5 强制推 0 (config.py:106；main.py:148/219；sync_price_inventory.py:189)
- MAX_DELIVERY_LEAD_DAYS = 12 — 配送时长>12 天，商品**仍上架**但库存写 0（Walmart 显示缺货，不实际出单）(config.py:112)
- PRICE_DIFF_THRESHOLD = 0.01 — 价差<1 分跳过 PUT，节省 100/h 配额 (config.py:109；sync_price_inventory.py:181)
- RECONCILE_DELAY_HOURS = 6 / RECONCILE_MAX_AGE_HOURS = 168(7天) / RETIRE_RELIST_COOLDOWN_HOURS = 24 (config.py:132-134)
- LLM_WORKERS = 20 — 注释明确：macOS Py3.14 下 50 worker 内存峰值触发 OOM Killer，20 worker 实测稳定(76 行约 4min) (config.py:58)
- MP_ITEM_SPEC_VERSION = '5.0.20260304-22_45_32-api' — feed header version 必须完整时间戳，写 '5.0' 被拒(EXT_DATA_ERROR_74597363510508) (config.py:78；feed_submit.py:88-119)
- SITE_END_DATE = '2028-12-31T00:00:00Z' — 必须 ISO DateTime 含时间，纯 yyyy-mm-dd 被拒(EXT_DATA_ERROR_00030257670757)；mapper 还做了兜底补 T00:00:00Z (config.py:97；mapper.py 约 1288)
- FORCE_BRAND='Unbranded' / SKU_FORMAT='asin'(SKU 直接等于 ASIN) / DEFAULT_FULFILLMENT_LAG_DAYS=1 / DEFAULT_MUST_SHIP_ALONE='No' / DEFAULT_COUNTRY_OF_ORIGIN='China' (config.py:92-103)
- SORT_IMAGES_DEFENSIVE=True — DMIT scraper 用 set() 去重导致图片顺序随机的兜底：强制按 URL 字典序排（保 idempotent 但主图未必正确）(config.py:119)
- FEISHU_TIMEZONE_OFFSET_HOURS = 8 — 日配额按北京 0 点重置 (config.py:38；quota.py:33)
- feishu_io.batch_write_ranges BATCH_SIZE = 3000 — 2026-05-19 从 4000 降到 3000，超大批飞书拒收 (feishu_io.py:146)
- feishu_io / upc_pool 分片读参数：CHUNK_ROWS=5000、BATCH_GET_RANGES=4、READ_WORKERS=4 — 池 126k 行单次全量读会撞飞书 90221 (data exceeded 10485760 bytes) (upc_pool.py:140-142；feishu_io.py:222)
- upc_pool 批量写 BATCH = 4000（飞书真实上限 5000 行/次，留 1000 buffer）(upc_pool.py:486,626,744)
- 飞书瞬时错误码退避：90235(data not ready)/90217(TooManyRequest) 一律 1s/2s/4s 三次退避，_TRANSIENT_CODES 定义 (feishu_io.py:64,67；upc_pool.py:201-217,507-518)
- append_listed_rows BATCH = 500 行/次，range 必须写成 A1:Z{len(chunk)}（lark-cli 1.0.14 要求 range 行数≥values 行数）(feishu_io.py:639,645)
- _UPC_RUNTIME_TTL_SEC = 3600 — 本地实时声明簿过期阈值；upc_lock(timeout=600) (upc_pool.py:367,408)
- UPC 首位白名单 _RETAIL_SAFE_PREFIX_CHARS='016789' — 2/3/4/5 开头被 Walmart 拒(EXT_DATA_ERROR_54514906640101 'designated for special applications')，list_unused 直接跳过 (upc_pool.py:129)
- _normalize_upc: 数字<12 位 zfill 补前导 0；find_upc_row 比较时两边 lstrip('0') (upc_pool.py:110,843)
- retry_state.THRESHOLDS = {DMIT_NOT_FOUND:3, STOCK_LOW:30, PT_INVALID:3, PRICE_OUT_OF_BAND:30, LLM_INVALID:3, DMIT_TRANSIENT:-1, LLM_TRANSIENT:-1, OTHER:-1}，-1=不记账 (retry_state.py:51)
- rate_limiter.WALMART_RATE_LIMITS：PUT /v3/price 100/3600、PUT /v3/inventory 200/60、POST feeds?MP_ITEM 10/3600、MP_MAINTENANCE 10/3600、PRICE_AND_PROMOTION 6/86400、feeds?inventory 50/3600、items/spec 3/60、partnerprofile 60/60、GET feeds/{feedId} 60/60(保守，官方其实 5000/min 共享) (rate_limiter.py:123-133)
- **RETIRE_ITEM 端点没有登记限流** — retire_and_relist.py:121 调 limiter.acquire('POST /v3/feeds?RETIRE_ITEM')，但该 key 不在 WALMART_RATE_LIMITS 里，acquire 直接 return 0.0 完全不限流 (rate_limiter.py:50)
- reconcile 分页：offset step 50、limit 50、每页 3 次重试(退避 1s/2s)、offset==0 失败抛异常整 feed 进网络重试队列、中途失败只 break (reconcile.py:123-146)
- reconcile 第二轮串行重试间隔 retry_sleep = 8s（避开飞书 90217 约 10s 窗口）；跨店并发 max_workers 默认 8 (reconcile.py:285,386)
- feed_submit 反查窗口：默认 window_seconds=600，earliest = t_start-30_000ms；反查前 sleep 5s；NOT_FOUND 后 sleep 30s 二次确认并把窗口拉到 900s (feed_submit.py:123,147,159,313,315)
- feed_submit submit_feed max_retries=2，退避 min(10, 2**attempt)，POST timeout=90，_HTTP_RETRY_STATUSES={408,429,500,502,503,504}，4xx(≠408/429) 立刻抛不重试 (feed_submit.py:192,195,264,286,299)
- sanitize_feed_numbers — 提交边界把所有 float round 到 2 位，Walmart 拒收 >2 位小数 (feed_submit.py:64)
- Phase 0.5 增量补配额：batch_size = max(20, need_more * 2)（按 2x 通过率预取）(main.py:1005)
- Phase 1 server 分配 free_snap limit = max(needed * 10, 5000) — 2x 不够会导致 server 过滤后返回 0 个 (main.py:1246)
- dedup cache 健康护栏：cache_size==0 或 stale<0 或 stale>108000s(30h) 就判不健康回退飞书直读 (main.py:735,784)
- 提交并发 ThreadPoolExecutor(max_workers=min(len(by_store), 16)) (main.py:1467)
- mapper 文案硬约束：productName maxLength 199/minLength 10；shortDescription 截断 4000(实际切 3997+'...')；keyFeatures 最多 7 条、每条 500(切 497+'...')、minItems 按 per-PT 读 spec 默认 4；manufacturer maxLength 60 (EXT_DATA_ERROR_01076067496949)；keyFeatures 不足触发 EXT_DATA_ERROR_55506974520167 (mapper.py:545-620,717-752)
- mapper 图片：mainImageUrl=urls[0]，productSecondaryImageURL=urls[1:9]，不足 5 张(schema minItems=5)直接不写 (mapper.py 约 1256-1268)
- LLM：LLM_MAPPING_MAX_TOKENS=4096(映射用) / chat_json 默认 max_tokens=16384、temperature=0.2、timeout=180s、连接超时 10s；AsyncRetrying stop_after_attempt(max_retries+1)、wait_exponential(min=1,max=10)；AIMD _DynamicLimiter(initial=20, min_size=5, max_size=60)，429/timeout 立刻缩，p95<30000ms 且零失败才增 (llm_client.py:30,90,127,340-342,385-387)
- live_spec：一次最多 20 个 PT，限流 3/min；main 按 20 个一 chunk 合并 (live_spec.py:34；main.py:911)
- pt_spec lru_cache：_load_pt_visible maxsize=512(约 50 MB 常驻)、get_variant_attribute_enum maxsize=512 (pt_spec.py:63,133)
- sync_status_track：PAGE_SIZE=1000（官方 GET /v3/items limit 上限，之前误用 200）；5 轮 lifecycle/published 组合；nextCursor 首次 '*' 之后固定 token + offset 翻页，offset ≤ 10000 超出靠单查补漏；单查补漏 max_workers=8 (sync_status_track.py:42-49,74-106,228)
- sync_status_track 三层 NOT_FOUND 防护：① 在线产品总表空索引直接硬中止 (line 728-730) ② 单店 NOT_FOUND 占比>80% 且行数≥20 熔断丢弃该店全部 NOT_FOUND 写入 (line 516-521) ③ 上架 <48h 在审核窗口内不写 NOT_FOUND (line 410,460)
- sync_online_products：WRITE_BLOCK_ROWS=4000（20 列 5000 行会撞飞书 90227，4000 行约 4MB 留 25% 余量）、READ_CHUNK_ROWS=5000/READ_BATCH_RANGES=4/READ_WORKERS=4、写块间 sleep 0.3s、跨店并发默认 16 (tools/sync_online_products.py:86-89,571-612,799)
- sync_online_products inventory：INVENTORY_PAGE_SIZE=50(官方上限)、INVENTORY_PAGE_SLEEP=0.32(200/min + 10% 余量)、INVENTORY_PATCH_PER_STORE_MAX=0(不限制；历史曾为 3000)、INVENTORY_PATCH_SLEEP=0.32 单店内必须串行 (tools/sync_online_products.py:120-124)
- sync_online_products DMIT：DMIT_REFRESH_POLL_SEC=30、DMIT_REFRESH_TIMEOUT_SEC=7200(2h)；ZERO_STOCK_DMIT_ERROR_TYPES={'variant_offset'} (tools/sync_online_products.py:117-119)
- upc_audit：ITEM_SEARCH_RATE=RateLimit(180,60)(官方 200/min 留 10%)、TOKEN_REFRESH_SEC=800(有效期 900 提前 100 刷)、WORKER_QUEUE_TIMEOUT=3、DEFAULT_FLUSH_EVERY=200、list_unused(limit=10_000_000) (upc_audit.py:52-57,259)
- quota：不在定价表的店铺默认 999（等于不限制）；get_store_max_list 取 max(FBA, FBM) 而非分渠道；读飞书配额失败也按 999 处理 (quota.py:113,114；main.py:890)
- match_listing：SPEC_RATE_PER_MIN=180(官方 200)、SPEC_MIN_INTERVAL=60/180、FEED_MAX_ITEMS=1000(Walmart 实际 10000)、POLL_MAX_ATTEMPTS=16 × POLL_INTERVAL_SEC=15 = 240s、DEFAULT_WEIGHT=1 磅 (match_listing/config.py:66,81-88)
- match_listing 提交 max_retries=2 退避 min(10,2**attempt)，POST timeout=90；4xx(≠408/429) 抛 MatchSubmitError 不重试 (match_listing/feed_submit.py:60,106,114)
- risk_gate 拉飞书用 csv-get，每次 1400 行一段(start+1399)，lark 超时 120s (risk_gate.py:59-73)
- 变体巨型组阈值 full_set ≥ 10（env ERP_MAX_VARIANT_GROUP_SIZE，判定在 server 端 api_tasks，不在 auto_listing）(docs/listing_flow_full.md:51)
- 环境变量开关：ERP_SERVER(有值就走 server 集中 UPC 分配+dedup)、ERP_WORKER_NAME、ERP_TASK_ID、ENABLE_VARIANT_LISTING(默认 '1')、LARK_IO_SHIM(默认 '1'，置 '0' 走裸 lark-cli subprocess)、ERP_STORE_STATUS_LARK_AS(默认 bot)、FEISHU_BOT_WEBHOOK/SECRET

### 防重/幂等语义

四层防重，缺一层都出过事故：

【L1 飞书状态位去重】filter_pending：D(审核)必须 == 'pass'，K(是否上架) ∈ {yes, unknown} 就跳过，O == 'SKU_LOCKED' 永久跳过 (feishu_io.py:407-432)。Unknown 也算已上架，因为可能 Walmart 已收到，只是反查不确定。

【L2 全局 ASIN 去重】Excel/worker 模式独有。非 strict-file-store 时调 ERP_SERVER POST /api/dedup/asins-existing，用「在线产品总表」B 列做 **ASIN-only 全局拦截，不区分店铺**（比旧的 (asin,store) 更严格）；strict-file-store 时调 /api/dedup/asins-existing-by-store 做 (asin,store) 同店去重。server 不可达或 cache 不健康(size==0 / stale<0 / stale>108000s) 自动回退飞书直读 K∈{yes,unknown} 集合 (main.py:694-838)。历史 bug：新上传的 xlsx col 52 全空导致 pending 全过、同一 ASIN 重复上架，这层就是为此加的。

【L3 UPC 池强一致】领号即刻标"已领"、**永不释放**（除下面三种明确回收路径）。三重互斥：优先 ERP_SERVER 集中分配（server 端 claimed 簿 2h TTL）；否则 fcntl flock upc_pool.lock(600s) 串行化 list_unused + mark_claimed_batch；同锁内再用 upc_claimed_runtime.txt（1h TTL）剔除飞书还没 propagate 的行号 (upc_pool.py:354-444; main.py:1223-1340)。

【L4 feed 提交幂等】submit_feed 遇 5xx/408/429/超时/连接错，**先反查 GET /v3/feeds?limit=20 再重试**，按 feedType==MP_ITEM && itemsReceived==n_items && feedDate ∈ [t_start-30s, t_start+600s] 匹配；命中即当成功返回，绝不重复 POST。反查是三态：FOUND→成功；NOT_FOUND（GET 200 但 20 条里没匹配）→再 sleep 30s 用 900s 窗口二次确认，仍空才判定 Walmart 高置信未收到；UNKNOWN（GET 自己失败）→保留"已领"、飞书写 K=Unknown 等自愈 (feed_submit.py:122-328)。

【UPC 回收规则】只有三类会 unmark_used_batch 清空 B 列变回"未用"：提交前就失败的行(prep_fails)、RolledBack(NOT_FOUND 二次确认)、提交被 Walmart 4xx 拒绝。**Unknown 绝不回收**，因为可能已被接收 (main.py:1511-1527)。同时向 server POST /api/upc/mark-used（成功行）或 /api/upc/release（其它）。

【自愈补偿】① sync_status_track 扫 catalog，K=Unknown 但商品真在线 → 改写 K=Yes、L 写伪 feedId `healed:catalog_YYYYMMDD`；reconcile_by_listed_date 见到 `healed:` 前缀会跳过（不是真 feedId 没法查）(sync_status_track.py:451,508; reconcile.py:331)。② retry_state 用 (asin,store,kind) 累计失败次数，达阈值写 D=fail 永久淘汰，成功时 record_success 清掉该 (asin,store) 全部 kind。③ reconcile 本身天然幂等：只写 O/P/Q，重复跑只是重写同值。

【match_listing 幂等】完全不同：MP_ITEM_MATCH 是 offer-only 按 sku REPLACE，重复提交安全（同 sku 覆盖），所以只做轻量 5xx 退避重试，**不做反查防重**（feed_submit.py 模块 docstring 明说）。SPEC 预检按 UPC 在进程内 dict 缓存去重。

### 危险操作

- POST /v3/feeds?feedType=MP_ITEM 上架提交 — main.py 有 --dry-run（生成 feed JSON 落盘但不 POST，feed_submit.py:227）。但注意 **dry-run 仍会真实领 UPC 并标'已领'**？不会：main.py:1282/1320 都判了 `not args.dry_run` 才 mark_claimed_batch，但 row_to_upc 已在内存分配、server 端 /api/upc/claim 在 dry-run 下**仍然会被调用**（main.py:1249 没有 dry_run 判断）——这是个真实缺陷，dry-run 会污染 server claimed 簿（靠 2h TTL 自愈）
- POST /v3/feeds?feedType=RETIRE_ITEM 批量下架 — retire_and_relist.py:94 有 dry_run（只落盘 JSON 不 POST）。这是不可逆操作：SKU 退役后 24h 内不能重上
- stage_relist 清空飞书 K/L/M/N/O/P — 让 main 重新领新 UPC 上架，等于对该 SKU 消耗一个新 UPC。有 --dry-run (retire_and_relist.py:209)
- write_audit_result(row, 'fail', ...) 永久淘汰 — retry_state 达阈值时自动写飞书 D=fail，filter_pending 之后**永远跳过**；且 retry_state 里 terminated=1 不再 record。恢复只能人工改飞书 D 列 + 直接 SQL 改 sqlite (main.py:1748-1764)
- PUT /v3/price / PUT /v3/inventory / PRICE_AND_PROMOTION feed / inventory feed — sync_price_inventory 有 --dry-run。**库存<5 强制推 0** 是隐性破坏性操作：会让商品在 Walmart 显示缺货停售 (sync_price_inventory.py:189)
- MP_MAINTENANCE feed 批量改已上架字段 — update_listed.py 支持 --fields 预设集与 --field-overrides，改错字段集会污染全店 listing
- tools/sync_online_products.py **整块重写**飞书「在线产品总表」13 万行，并在旧表更长时把尾部写空 (line 899-905)。防护：total_products==0 直接 return 1 不动飞书 (line 883)。历史事故：_fetch_batch 静默吞异常导致写残 12 万行总表；README 记载已改成抛异常，但当前代码 tools/sync_online_products.py:544-546 仍是 `except Exception: vrs = []` —— **迁移前需确认这个修复是否真的落地**
- sync_status_track 往飞书 T 列批量写 NOT_FOUND — 6-09 出过全表误写事故。三层防护见 pitfall_params（空索引硬中止 / 单店 >80% 熔断 / <48h 审核窗口豁免）
- upc_pool.mark_conflict / mark_used / unmark_used — 直接改 12 万行 UPC 池 B 列，无 dry-run
- match_listing 真实提交会在用户店铺创建**公开 offer**（main.py 模块 docstring 明确标注为副作用操作），有 --dry-run（只预检+组 feed+备份 JSON）
- 飞书 batch_write_ranges 批量写 — 单次可覆盖 3000 个 range，写错 range 计算等于批量污染主表，无 dry-run

### 事故教训与必须保留的行为

- 【幽灵商品事故，6241 行】2026-05-21 起飞书 +append 遇 90235 直接丢行：feed 已提交、UPC 已消耗，但飞书**没有任何记录** → 下次 main 又拉一遍同 ASIN。修法是 feishu_io._run_cli 统一给所有写路径加 90235/90217 退避重试（此前 e4a07ed3 只修了读路径）+ tools/rescue_lost_appends.py 从 run 日志反扫补录。任何新系统的写路径必须先落库再调接口，绝不能'写飞书失败就算了'
- 【全店误淘汰事故，2355 行】pricing.load_pricing_rules 从 +read 迁到 +cells-get 后，倍率列百分比格式读出 '275%' 字符串，float() 抛 ValueError 被静默跳过 → 全店判'没有 FBA/FBM 倍数配置' → 整批误淘汰。修法 _parse_multiplier (pricing.py:31)。教训：飞书 +cells-get 返回的是**格式化显示值**不是原始值
- 【W 列全错事故】sync_status_track 单次全量读 UPC 池 A2:B126000，B 列长 mark 串导致响应 >10MB 撞飞书 90221 → '响应非 JSON' → 返回空索引 → W 列全写'无上架记录'。修法改用 upc_pool.read_pool_rows 分块并发读 (upc_pool.py:299)
- 【跨店覆盖事故，415 行受影响】reconcile 早期用 {asin: row} 单键索引，同一 ASIN 跨多店时后写覆盖前写，reconcile(A107) 的结果写到了 A114 的行，A107 行 N 列永久空白。修法改 (asin, store) 复合 key (reconcile.py:172-179)
- 【500 行漏 N 事故】reconcile 分页首次失败直接 break，A114 一个 500 行 feed 因网络抖动整批漏写。修法：单页重试 3 次 + offset==0 失败抛异常进网络重试队列，中途失败才 break (reconcile.py:118-146)
- 【451 MB OOM】MP_ITEM v5 单文件 json.load 膨胀成 1.3 GB Python 对象，跑 5048 行 xlsx 时 RSS 飙到 12 GB。修法按 PT 拆分目录 + lru_cache(512) (pt_spec.py:9-14)
- 【50 worker OOM Killer】macOS Python 3.14 下 LLM_WORKERS=50 内存峰值触发系统 OOM Killer，实测 20 稳定 (config.py:58)
- 【SKU_LOCKED 不可用新 UPC 重发】ERR_EXT_DATA_0101211 表示 SKU 已绑死旧 UPC，换新 UPC 重发同一 SKU 也会失败，唯一解是 RETIRE_ITEM 退役 + 24h 冷却 (reconcile.py:72; retire_and_relist.py:1-6)
- 【异步审核假错误】EXT_DATA_ERROR_56026862530206 / EXT_DATA_ERROR_66547201695750 是'还在合规审核中'的假错误，几小时到几天内自然变 SUCCESS。**绝不能当失败重发**，否则产生 duplicate listing。写 O=ASYNC_PENDING 等下次 (reconcile.py:59-64)
- 【ingestionStatus=SUCCESS 可以同时带 ingestionErrors】老逻辑只看 ing 把 412/497 个误判 SUCCESS。必须先看 codes 再看 ing，且优先级 SKU_LOCKED > SUCCESS(无码) > INPROGRESS > 全 ASYNC > SUCCESS_WITH_WARNING > DATA_ERROR (reconcile.py:217-264)
- 【错误码末尾带 \t】Walmart 返回的 code 要 .strip() 否则集合匹配全 miss (reconcile.py:191)
- 【Walmart 文档与实际不符】官方 sample US_MP_ITEM_v5.0.json 列 7 个 header 字段，实际只收 3 个：多传 subset 报 EXT_DATA_ERROR_60670554076755，不传 businessUnit 报 EXT_DATA_ERROR_72600149546850，version 写 '5.0' 报 EXT_DATA_ERROR_74597363510508。endDate 文档写 Date 类型 yyyy-mm-dd，实际拒收，必须 ISO DateTime (feed_submit.py:91-100; config.py:97-100)
- 【零认证强制覆盖】搬运场景拿不到任何 CPC/NRTL/Prop65/Warranty 文档，所以 certification_type→'Neither of these applies'、has_nrtl_listing_certification/isProp65WarningRequired/has_written_warranty/isAssemblyRequired→'No'，且必须**同时删掉** warrantyText/warrantyURL/prop65WarningText/*_document_reference_id/nrtl_information/assemblyInstructions 等 LLM 可能瞎填的文档字段（填了会被判'该证书不存在'）。强制值不在该 PT enum 时按 No→Neither of these applies→Skip for now→None→enum[0] 顺序降级 (mapper.py:1204-1256)
- 【UPC 受限前缀】2/3/4/5 开头是 GS1 生鲜/NDC/优惠券专用，Walmart 报 EXT_DATA_ERROR_54514906640101。历史池里有残留，list_unused 直接跳过不分配 (upc_pool.py:126-133)
- 【orderable 格式陷阱】productIdentifiers 必须是**单个对象不是数组**；price 必须是**裸 number 不是 {amount,currency}**；inventory[].fulfillmentCenterID 必填且必须是 Partner ID(Virtual Node) (mapper.py:1178-1183, 1272-1288)
- 【lark-cli 标准库异常整店翻车】upc_pool 早期只 catch UPCPoolError，FileNotFoundError(worker PATH 配错，A109 case) / TimeoutExpired(卡 60s，A116 case) 直接冒泡 → 整店被错判 unexpected → 全部 K=No，UPC 池里'已领'也没升级'已用'。修法 _run_cli 统一转 UPCPoolError (upc_pool.py:28-41)
- 【lark-cli stdout 有前导文字】所有解析都要先 find('\n{') / rfind('\n{') 再 json.loads (upc_pool.py:64; feishu_io.py:308; quota.py:50)
- 【lark-cli +append 多行报 90202】range 必须写成 A1:Z{len(chunk)}，行数≥values 行数，API 仍会 append 到末尾不会真覆盖前 N 行 (feishu_io.py:643-645)；match_listing 干脆放弃 +append 改成算最后数据行后精确 range +write (match_listing/feishu_io.py:158-166)
- 【pending_feed_tracker 是 threading.Lock 不是文件锁】所有 state/*.json 都假定单进程 cron。多机/多进程并发跑同一店铺仍有 race，需要分布式锁 (pending_feed_tracker.py:18,33)
- 【状态机不可回滚】D=fail 是永久淘汰，写后 filter_pending 永远跳过；retry_state.terminated=1 不再 record。手动恢复必须同时改飞书 D 列和 sqlite
- 【RETIRE_ITEM 用完全不同的 schema】RetireItemHeader{feedDate, version:'1.0'} + RetireItem[{sku}]，不是 MPItemFeedHeader/MPItem。之前用错 schema 导致 retire feed 全部被拒 (retire_and_relist.py:96-104)
- 【healed: 伪 feedId】sync_status_track 自愈时往 L 列写 'healed:catalog_YYYYMMDD'，reconcile_by_listed_date 必须识别并跳过，否则会拿它去查 Walmart (sync_status_track.py:451; reconcile.py:331)
- 【dry-run 泄漏】main.py --dry-run 下 server 端 /api/upc/claim 仍会被调用（main.py:1249 无 dry_run 判断），污染 server claimed 簿（靠 2h TTL 自愈）
- 【RETIRE_ITEM 无限流】'POST /v3/feeds?RETIRE_ITEM' 不在 WALMART_RATE_LIMITS 里，limiter.acquire 直接返回 0.0 完全不限流 (rate_limiter.py:50 vs retire_and_relist.py:121)
- 【config.py 与代码脱节】FEISHU_RANGE_COLUMNS 还是 'A:V'(22 列)、FEISHU_COLUMNS 字典仍是旧 22 列语义(R=reconcile写/U-V=价格库存)，而实际实现已扩到 26 列 + AA。read_pending_rows 用 A:V 读却解析到 Z，W-Z 恒空；且 index 21(V) 字段名仍叫 title_similarity 但实际写的是真实 UPC (config.py:10-35 vs feishu_io.py:334,398-403,682)
- 【硬编码绝对路径】fix_upc_pool.py:39 和 resubmit_278.py:18 直接写死 /Users/nextderboy/Projects/erpAPI/...；4 个 plist 也全是绝对路径
- 【硬编码密钥】DEEPSEEK_API_KEY / QWEN_API_KEY 明文在 config.py:62,66；DMIT IP <SCRAPER_VPS_IP,见旧仓库>:8899 硬编码在 config.py:53 和 tools/sync_online_products.py:117
- 【closed_loop.md 已严重过时】它描述的是 9 列时代（C=PT, D=审核结果, F=是否上架, H=上架结果），与现在 26 列 schema 完全对不上。迁移时以 auto_listing/README.md + 代码为准，不要信 closed_loop.md 的列名
- 【sync_price_inventory 不回写飞书】X/Y 列(价格库存调整/日期)有 feishu_io.write_price_inv_adjust 函数但生产从未调用，同步结果只落在 logs/sync_state.json 里，飞书上看不到 (README.md:37,140-141)
- 【bulk inventory 端点会漏数据】Walmart /v3/inventories 实测漏 0-30 个/店（A114 PUBLISHED 漏 9/10、D052 SYSTEM_PROBLEM 漏 7/7），必须单 SKU GET /v3/inventory 补漏，且单店内**必须串行**（共享 200/min）(tools/sync_online_products.py:386-400)
- 【DMIT variation_asins 有 bug】会误抓推荐位 ASIN 造成巨型伪变体组，靠 full_set ≥ 10 阈值过滤 (docs/listing_flow_full.md:164)
- 【anchor 已知局限】30575 个在线 ASIN 从没被 DMIT 采过算不出 full_set；31.5% 的 anchor 落在非 ACTIVE 店，这些组的新变体一律拒绝；存量散装变体(一期前/手工上架，没 variantGroupId)需人工下架重上，无自动回填 (docs/listing_flow_full.md:158-164)
- 【match_listing SPEC 静默失败】沃尔玛 upc(12位)/gtin(13-14位) 是不同参数传错位数查不到；标识被当数值存会丢前导 0（06433465361050 存成 6433465361050）。解法：按位数生成多个候选 + zfill 补位依次尝试，全相同数字的退化码直接判无效不查 (match_listing/spec_check.py:27-60)
- 【match_listing 新 offer 默认 0 库存是正常现象】MP_ITEM_MATCH v4.2 spec 本身没有库存字段，库存由其他模块负责，别当失败处理 (match_listing/README.md:22)

### 切换时必须迁移的状态

- auto_listing/state/pending_feeds.json — 未 done 的 feedId 队列。切换前必须先跑完 reconcile 或整体导入新库 ops/listing 表，否则这些 feed 永远不会被回写，飞书 O/P/Q 永久空白
- auto_listing/state/retry_state.sqlite — terminated=1 的 (asin,store,kind) 是永久淘汰名单，丢了会导致新系统把几万个 DMIT 404 / 长期 OOS 的 ASIN 重新拉一遍，浪费 DMIT/LLM 配额并把已 D=fail 的行重新激活
- auto_listing/state/llm_cache.sqlite（约 462 MB）— 丢了等于全量重跑 LLM，成本最高的一份状态。可整表导入 PG，但注意 key 含 model 名，换模型即全部失效
- auto_listing/state/retire_log.json — 正在 24h 冷却中的 SKU。丢了会导致 O=RETIRING 的行永远不 relist（find_retiring 找不到 retire_log 条目就 continue，retire_and_relist.py:83）
- auto_listing/logs/sync_state.json — 每店每 SKU 上次推的 price/qty。丢了下一轮价格/库存同步会对全部已上架 SKU 发 PUT，直接打爆 PUT /v3/price 100/小时
- auto_listing/logs/feed_*.json 全量历史 — reconcile 的 SKU→UPC 反查唯一来源，UPC 冲突标记完全依赖它。迁移时应把 (store, sku, upc, feed_id, submitted_at) 抽成 listing.feed_items 表，而不是继续扫目录
- 飞书上架表 Sheet1(ea2b2a) 26 列全量数据 — 这是当前唯一事实源(A/B/D-G 人工，其余机器写)，必须整表导入 listing 表；尤其 K(是否上架) / L(feedId) / M(上架日期) / O(上架结果) 决定去重与重试
- 飞书 UPC 池 sheet(NxlS1J) 12 万+ 行 A=UPC / B=标记('已领|已用|冲突-catalog已存在' 三态) — 必须原样搬成 catalog.upc_pool 表，标记语义与 mark_used/mark_claimed 的字符串格式(见下文)不能丢
- 飞书「在线产品总表」(MO2e…mI(token已脱敏,见旧仓库代码)/e7834a，20 列 13 万行) — dedup 与 sync_status_track Plan B 的数据源
- 飞书定价表(X4vM…bh(token已脱敏,见旧仓库代码)/2FJ2Np) 各店 FBA/FBM 区间倍率 + 日配额；店铺状态表(CRfC…kb(token已脱敏,见旧仓库代码)/899f65)；类目映射「沃尔玛类目」(Gx9H…wc(token已脱敏,见旧仓库代码)/0bdc8b)；「禁止品牌收集」(YlA1…dd(token已脱敏,见旧仓库代码)/WvPTz2)
- erp_listing_server 端的 UPC claimed 簿(2h TTL) 与 dedup/anchor cache — 若新系统不接管 server，Phase 1 会自动回退本地文件锁模式；若接管则要迁 anchor(asin→store) 映射
- walmart_official_specs/MPSetup_by_pt/ 拆分目录 + _pt_index.json（如果不重新生成就必须搬），以及 MP_ITEM_SPEC_VERSION 这个具体时间戳字符串

### 迁移建议

照搬（业务生死线，改了就出事）：① 三态提交语义 Yes/No/Unknown/RolledBack + VerifyResult(FOUND/NOT_FOUND/UNKNOWN) 三态反查 + 30s 二次确认 + 只在 NOT_FOUND 才回收 UPC——这是"绝不重复提交、也绝不误判未提交"的核心，逐行照搬 feed_submit.py:122-328 与 main.py:1476-1527。② MP_ITEM v5 header 只能 3 字段且 version 必须完整时间戳。③ endDate 必须 ISO DateTime。④ 八项零认证强制覆盖 NO_CERT_FORCES + DANGEROUS_DOC_FIELDS 清理（mapper.py:1218-1256）——少一项就会触发 CPC/Prop65/NRTL 文档依赖必拒。⑤ UPC 首位 016789 白名单。⑥ RETIRE_ITEM 用 RetireItemHeader/RetireItem 完全不同的 schema、version '1.0'。⑦ reconcile 的错误码分类四张集合（UPC_COLLISION / ASYNC_PENDING / RETRIABLE / SKU_LOCKED）与优先级顺序（SKU_LOCKED > 真 SUCCESS > INPROGRESS > 全 ASYNC > SUCCESS_WITH_WARNING > DATA_ERROR）。⑧ SKU_LOCKED → RETIRE_ITEM → 24h → 清列重上 这条自愈链。⑨ 同店打包成单个 feed（MP_ITEM 10/小时硬限）。⑩ 价格同步默认走 PUT 单品而非 PRICE_AND_PROMOTION feed（6/天）。

重做（新架构应该做得更好）：① 所有 lark-cli subprocess + JSON 文本解析全部换成 api/feishu.py；90235/90217 退避、分片读、batch_write 都下沉到 api 层，registry 里登记 token/sheet_id/字段常量。② state/*.json + *.sqlite 全部搬进 PG：pending_feeds → ops.feed_runs（"先写 pending 再调接口"正好符合新项目铁律）、retry_state → listing.retry_state、llm_cache → catalog.llm_cache（或独立表 + 定期 vacuum）、retire_log → listing.retire_cooldown、sync_state → listing.sync_state、upc_claimed_runtime.txt + upc_pool.lock → PG 事务 + SELECT FOR UPDATE（这才是 UPC 池"强一致"的正确实现，现在的文件锁+飞书 read-after-write 延迟是纯打补丁）。③ 飞书列位置（A-Z 字母）散落在 20 多个 build_*_update 函数里，必须换成 registry 字段常量。④ reconcile 的 SKU→UPC 反查改成查 PG 而不是 glob 扫 logs/feed_*.json。⑤ scheduler.py 的 subprocess 分发 + launchd plist 换成 cli.py + flock + ops.runs。⑥ config.py 里硬编码的 DeepSeek/Qwen API key、DMIT IP <SCRAPER_VPS_IP,见旧仓库>:8899、绝对路径 /Users/nextderboy/... 全部进 registry/.env。

对应新 workflow 建议拆法（不要照抄 main.py 的 1923 行单体）：workflows/list_new.py（Phase 0-3 主链，dangerous=True）、workflows/reconcile_feeds.py、workflows/sync_status.py、workflows/sync_price_inventory.py（dangerous=True）、workflows/retire_locked.py + relist_cooled.py（dangerous=True）、workflows/upc_audit.py、workflows/sync_online_products.py、workflows/match_listing.py（dangerous=True）。services 层抽出：upc_pool（领/标/回收，PG 事务）、pricing、quota、risk_gate、retry_policy、variant_grouping、llm_mapping（含缓存）、spec_loader、feed_builder。api 层：feeds（提交+反查+分页拉 details）、items（catalog 5 轮 + 单查）、prices、inventory、insights。

迁移顺序建议（#9/#10 是最后两条不是没道理）：先迁 sync_online_products（只读+写一张飞书表，风险最低，但它是 dedup 和 status_track 的地基）→ 再迁 sync_status_track（只写飞书 R-W）→ 再迁 reconcile（只写 O/P/Q，且天然幂等）→ 最后才迁 main 上架链和 retire/relist（真正的破坏性写）。切 main 之前必须先停 launchd morning + 搬 UPC 池 + 搬 retry_state，绝不允许新旧同时跑 list_new（会重复领 UPC 重复上架）。

### 待确认问题

- tools/sync_online_products.py:544-546 的 _fetch_batch 仍是 `except Exception: vrs = []` 静默吞异常，但 README 变更记录（2026-06-11 第 ⑤ 条）声称已改成抛异常。到底哪个是生产版本？这直接关系到会不会再次把 12 万行总表写残——迁移前必须核对生产机上的文件
- 同理，README 说 sync_online_products 已把 B:B 探测迁出废弃的 +read，但 line 511 仍在用 `sheets +read`。match_listing/feishu_io.py:84,101 也还在用 +read。lark-cli 的 +read 到底废弃到什么程度？
- tools/sync_online_products.py 的定时调度定义在哪里（launchd？定时任务skill/ 目录？MCP scheduled-tasks？）——本次读取范围内没找到
- FEISHU_COLUMNS / FEISHU_RANGE_COLUMNS 与实际 26+1(AA) 列的映射差异，需要人工确认权威列定义再写进新 registry。特别是 V 列到底是'真实UPC'还是'标题相似度'（代码里两种命名并存），以及 W/X/Y/Z/AA 的最终语义
- erp_listing_server 是否也在迁移范围内？如果不迁，新系统的 Phase 1 就只能走本地锁模式（server 集中 UPC 分配 + anchor 重定向 + dedup cache 全部失效），变体跨店 anchor 逻辑会退化成 worker 兜底那一层
- llm_cache.sqlite 462 MB 是否值得整体迁入 PG？key 里含 model 名，一旦换模型全部失效；是否应改成只缓存近 N 天
- 飞书身份混用（feishu_io 用 'user' 依赖人工 SSO 登录态，risk_gate/store_status/match_listing 用 'bot'）——新系统 api/feishu.py 用哪个？'user' 身份的 SSO 过期会让整条链静默失败
- daily_noon / reconcile_t2 / sync_status / relist_cooled 没有 plist，实际生产靠什么触发？sync_status_track 是 R-W 列和 Unknown 自愈的唯一来源却没有独立定时任务，是不是长期没跑？
- dry-run 时 server /api/upc/claim 仍被调用（main.py:1249）是有意为之还是 bug？新系统的 dangerous=True dry-run 必须做到零副作用
- 变体巨型组阈值 env ERP_MAX_VARIANT_GROUP_SIZE 的判定代码在 erp_listing_server/api_tasks.py（本次范围外），迁移时需要一并读
- match_listing/inventory_push.py 存在但 README 明说库存不在本系统范围——这个文件是死代码还是有调用方？
- state/upc_claimed_runtime.txt 的 1h TTL 与 server claimed 簿的 2h TTL 不一致，两套并存时是否有窗口能重复分配同一 UPC？


<a id="scheduling_tools"></a>
## 调度全景(定时任务skill + launchd)与 tools/ 救场脚本

### 模块职责

这是旧系统的「调度层 + 救场层」。定时任务skill/ 下 14 个平台无关的 SKILL.md，每个是一段给 AI 执行器看的自然语言 runbook（不是代码）：规定跑哪条命令、怎么容错、什么算成功、怎么写完成简报；统一由 notify.py 给运营飞书发「开始/完成」两条生命周期简报，不依赖调度平台自带投递。真正的调度时间不在仓库里——SKILL.md 只写「建议周期」，实际注册在外部平台（README 说与 2026-06-17 hermes 现状对齐），而另一批任务用 macOS launchd plist 跑，两套调度并存且有重叠。tools/ 是 12 个脚本：1 个是每日生产主力（sync_online_products.py，938 行，拉全店 catalog+inventory+DMIT 后整块重写飞书「在线产品总表」20 列），2 个一次性预处理（拆 451MB MP_ITEM spec、按店拆 xlsx），其余 9 个全是事故补救脚本——每一个都对应旧系统的一处已知崩坏点（飞书 append 丢行、feed 提交被误判为失败、写飞书超 ARG_MAX 挂掉、定价解析回归导致全天维护未触发等）。tests/ 只有 test_lark_io.py 一个文件（989 行，纯 mock，覆盖飞书共享层的瞬时错误码/重试/分块/分窗读）。.claude/commands/ 只有 1 个手动 slash command（并发拉全店产品导 Excel）。

### 入口与触发

- 定时任务skill/notify.py — 统一通知器，两个子命令 `start` / `done`，被全部 14 个 skill 的第一步和最后一步调用；notify.py:195-220；永远 exit 0（notify.py:201,220,248），通知失败绝不阻断任务
- 定时任务skill/<name>/SKILL.md — 14 个 runbook，不是可执行入口，是给 AI 执行器的指令文本；调度平台按 name 触发（frontmatter `name:` + `description:`）
- launchd（macOS，独立于 skill 体系）：auto_listing/launchd/com.user.autolisting.{morning,reconcile_hourly,retire_daily,health_4x,store_status_hourly}.plist；沃尔玛订单审核/deploy/com.user.walmart.order_audit_hourly.plist → deploy/run_hourly.sh → 订单同步.py；erp_worker/deploy/install.sh 生成 com.nextderboy.erp_worker.{1..20}.plist（常驻，非定时）
- tools/sync_online_products.py — 生产主力，`python3 tools/sync_online_products.py --workers 16`，由 erp-online-products-track skill 的 Step 2 调用（tools/sync_online_products.py:798-938）
- tools/{rescue_lost_appends,rescue_feed_misjudged,rescue_a116_full,retry_failed_feeds,reconcile_from_row,backfill_amz_title,backfill_walmart_title,backfill_pq_20260611,backfill_daily_sheet_20260611}.py — 全部手动执行的一次性/救场脚本，无调度
- tools/{split_mp_item_spec,split_xlsx_by_store}.py — 一次性预处理，无调度
- tests/test_lark_io.py — `python -m pytest tests/test_lark_io.py -q`，纯 mock 无网络（tests/test_lark_io.py:1-8）
- .claude/commands/fetch-walmart-products.md — 手动 slash command，生成脚本写 /tmp/fetch_walmart_products.py 后前台执行，禁止用 `&` 后台（.claude/commands/fetch-walmart-products.md:25）

### 调度

两套调度并存，仓库里都不是权威来源。

【A. agent skill 建议周期，14 条，本地时区；实际注册在外部平台，README 标注与 2026-06-17 hermes 现状对齐，定时任务skill/README.md:52-71】
| 时间 | cron | skill | 触发的模块/命令 |
|---|---|---|---|
| 05:00 | `0 5 * * *` | tro-daily-scrape | tro-scraper-matrix：5 个源增量采集 + merge_databases → Excel + 飞书 34f9f3 |
| 06:00 | `0 6 * * *` | trademark-daily-update | 商标数据/daily_update.py（USPTO 增量 → PG uspto） |
| 06:02 | `2 6 * * *` | daily-tro-pipeline | 商标数据/run_cron.sh pipeline（lark_pipeline.py 4 步） |
| 周一 06:15 | `15 6 * * 1` | weekly-brand-refresh | 商标数据/run_cron.sh refresh（TRUNCATE + 全量重查，~8min） |
| 07:05 | `5 7 * * *` | sync-blacklist-brands-daily | 新审核系统 sync.sync_blacklist_brands + sync.sync_phase0_blacklist + 重启 10 worker |
| 07:30 | `30 7 * * *` | erp-online-products-track | ①auto_listing.reconcile --days-ago 1,2 --workers 20 ②tools/sync_online_products.py --workers 16（~60-120min）③sleep 15 && auto_listing.sync_status_track |
| 08:00 | `0 8 * * *` | walmart-kpi-daily | 沃尔玛店铺日报 fetch_walmart_performance.py（含影刀 RPA）→ fetch_walmart_problem_orders.py → walmart_daily_summary.py |
| 08:02 | `2 8 * * *` | walmart-returns-daily-sync | 售后订单同步/fetch_walmart_returns.py --all |
| 12:00 | `0 12 * * *` | walmart-maintenance-all-stores | 沃尔玛商品维护 poll_yesterday.py → sync_lark.py → submit.py --execute --confirm-zeroing |
| 13:30 | `30 13 * * *` | walmart-daily-order-sync | 沃尔玛订单审核/订单同步.py |
| 14:00 | `0 14 * * *` | walmart-kpi-afternoon | fetch_walmart_performance.py --no-yingdao --skip-summary |
| 14:02 | `2 14 * * *` | dedup-sync-online-products | auto_listing.dedup_sync_to_server（全量）+ curl /erp/api/dedup/status |
| 15:00 | `0 15 * * *` | walmart-daily-retire | 沃尔玛批量下架/daily_retire_orchestrator.py（DELETE_ITEM） |
| 每 6h 00/06/12/18:04 | `4 0,6,12,18 * * *` | walmart-daily-cleanup | 沃尔玛问题商品清理/daily_cleanup.py（DELETE_ITEM + MP_MAINTENANCE） |

【B. launchd 作业，6 个（+20 个常驻 worker）】
| 时间 | Label | 跑什么 |
|---|---|---|
| 06:00 | com.user.autolisting.morning | scheduler full_morning = sync_price_inventory --auto + auto_reconcile + retire_and_relist --relist + **auto_listing.main（上架，提交 MP_ITEM feed）**（auto_listing/scheduler.py:173-189） |
| 每小时 :05 | com.user.autolisting.store_status_hourly | dedup_sync_to_server --store-status-only |
| 每小时 :15 | com.user.autolisting.reconcile_hourly | scheduler reconcile_due（auto_reconcile，回写 6h+ 老 feed） |
| 每小时 :15 | com.user.walmart.order_audit_hourly | deploy/run_hourly.sh → 订单同步.py（全店） |
| 08/12/16/20:00 | com.user.autolisting.health_4x | scheduler health_report（只读，推飞书） |
| 23:30 | com.user.autolisting.retire_daily | scheduler retire_locked（RETIRE_ITEM） |
| 常驻 | com.nextderboy.erp_worker.{1..20} | 上架 worker 长轮询（erp_worker/README.md:7） |

【C. 重叠 / 双重调度（迁移必须先解掉）】
1. **订单同步双跑**：launchd 每小时 :15 与 skill 13:30 跑同一个 订单同步.py；13:15 刚跑完 15 分钟后又跑一次。靠 fcntl 锁不会并发写，但纯浪费且完成简报会误导。
2. **reconcile 双写 O/P/Q**：launchd reconcile_hourly（auto_reconcile，按 6h+ 老 feed）与 skill 07:30 Step 1（reconcile --days-ago 1,2）写上架表同一批列，无协调。
3. **⚠️ 最危险：自动上架幽灵调度**。erp-online-products-track/SKILL.md:54,164 明确写「上架已从此任务移除，由用户手动触发，本任务不要调 auto_listing.main」，但 launchd com.user.autolisting.morning 06:00 的 full_morning 链**最后一步就是 auto_listing.main**。若该 plist 仍 loaded，每天 06:00 无人值守提交 MP_ITEM feed 并消耗 UPC 池。文档与调度已脱节。
4. **改价改库存三方争抢**：06:00 launchd sync_price_inventory、12:00 skill submit.py、00/06/12/18:04 daily_cleanup 的 MP_MAINTENANCE 延期，都对同一批 SKU 写 Walmart；且 06:00/12:00 与 cleanup 的 06:04/12:04 只差 4 分钟。
5. **两条下架路径**：15:00 DELETE_ITEM（飞书下架表驱动）与 23:30 RETIRE_ITEM（SKU_LOCKED 驱动），加上 cleanup 每 6h 的 DELETE_ITEM，共 3 个下架来源。
6. **dedup 双模式**：launchd :05 每小时 --store-status-only，skill 14:02 全量——文档已声明互不重叠（dedup-sync-online-products/SKILL.md:13-16），是唯一说清楚的一处。
7. **KPI 与 health_4x 同点**：08:00 与 12:00 各撞一次，只是飞书通知噪音。
8. launchd 在 Mac 睡眠时不触发，唤醒后只补跑一次（合并，不补 N 次）——plist 注释 :19-21。

### 数据存储

- notify.py 标记文件：`{tempfile.gettempdir()}/erp_skill_notify/{task}.json`（notify.py:66,99-126），存 {task, desc, t0}；start 写、done 读回算耗时后 unlink。任务崩溃不发 done → 标记残留 → 下次 done 算出错误耗时
- 飞书表本身就是主数据库（见 feishu_usage），旧系统没有统一的业务库
- 沃尔玛商品维护/maintenance.db（SQLite）：表 submissions / feed_items / inputs；backfill_daily_sheet_20260611.py:36-44 直接 sqlite3.connect 读，字段 batch_id/submitted_date/store_name/sku/feed_type/walmart_feed_id/ingestion_status/inventory_force_zero
- PostgreSQL `walmart_cleanup`：runs + error_items（含归类 category），由 daily_cleanup.py 写（walmart-daily-cleanup/SKILL.md:55）；附件 Excel 为临时文件，发完即删
- PostgreSQL `uspto`（/Users/nextderboy/Projects/商标数据），trademark-daily-update 每日增量导入（trademark-daily-update/SKILL.md:46-52），日志 商标数据/daily_update.log
- 商标数据本地 DB：tro_cases / matched_companies / company_brand_details（daily-tro-pipeline/SKILL.md:60-65）；weekly-brand-refresh 每周 TRUNCATE company_brand_details 全量重查（weekly-brand-refresh/SKILL.md:9-13）
- tro-scraper-matrix 5 个 SQLite：123tro.db / ipsebe.db / worldtro.db / 61tro.db / saibeiip.db → merged.db；导出 本地数据备份/YYYY-MM-DD_merged_export.xlsx（tro-daily-scrape/SKILL.md:59-143）
- 新审核系统 PG：blacklist_brands + Phase0 三表 sellers/asins/amazon_cats（sync-blacklist-brands-daily/SKILL.md:9-11，预期量级 sellers~1.2k / asins~12.6k / cats~11.6k / 总 ~25k）
- ERP listing server 内存 dedup cache：http://<SCRAPER_VPS_IP,见旧仓库>/erp/api/dedup/status，字段 cache_size / stale_seconds / store_status_size / store_status_stale_seconds（dedup-sync-online-products/SKILL.md:62-68）
- DMIT 采集服务 http://<SCRAPER_VPS_IP,见旧仓库>:8899（tools/sync_online_products.py:92），/api/upload、/api/batches/{name}/status、/api/export/{batch}?format=csv、/api/export/all、/api/results
- ⚠️ auto_listing/logs/ 是**载荷状态**不是普通日志：feed_*.json 被 backfill_walmart_title.py:32-36 当作 SKU→productName 的唯一真相；run_*.log 被 rescue_lost_appends.py:26-28 当作丢失飞书行的唯一恢复源（日志里原样打印了 --values JSON）。删日志 = 丢数据
- 锁文件 /tmp/walmart_order_sync.lock（fcntl，订单同步.py 单实例；沃尔玛订单审核/README.md:165）
- 缓存/临时目录：/tmp/title_backfill（tools/backfill_amz_title.py:37）、/tmp/a116_rescue（tools/rescue_a116_full.py:29）、/tmp/split_xlsx（tools/split_xlsx_by_store.py:26）、/tmp/fetch_walmart_products.py
- walmart_official_specs/MPSetup_by_pt/ — split_mp_item_spec.py 产物：_pt_index.json / _orderable.json / _header.json / {PT}.json（tools/split_mp_item_spec.py:12-18）
- 日志目录：auto_listing/logs/launchd/{morning,reconcile,retire,health,store_status}.{out,err}.log；沃尔玛订单审核/logs/launchd/order_audit.YYYYMMDD.log（保留 30 天，run_hourly.sh:22）；商标数据/cron_logs/{pipeline,refresh}_*.log（保留 30 天，daily-tro-pipeline/SKILL.md:88）

### 飞书使用

- 飞书 App 凭证固定为项目内置 bot `cli_a9561a4f8dfadcd2`（永不过期）。⚠️ 调度平台会注入错误的 FEISHU_APP_ID / FEISHU_APP_SECRET —— notify.py:69-70 启动即 os.environ.pop 两个变量；walmart-daily-order-sync/SKILL.md:46-51 与 dedup-sync-online-products/SKILL.md:52-57 要求命令前加 `env -u FEISHU_APP_ID -u FEISHU_APP_SECRET`（未注入时是 no-op，保留即可）
- 通知目标：苏里 = **所有者本人**（2026-08-16 澄清，此前记成「运营」）open_id `ou_36c5f91668c42a735e7b9d4ae74eedc1`（notify.py:63；walmart-kpi-daily 的业务日报也发同一人）。⚠ **open_id 是 per-app 的**：这个值只在 AppID `cli_a9561a4f8dfadcd2` 下成立，换应用要重新取。`ou_` 前缀 → --user-id，`oc_` → --chat-id（notify.py:137）
- 主 spreadsheet token `MO2e…mI(token已脱敏,见旧仓库代码)`（一个文档多个 sheet）：sheet `e7834a` = 在线产品总表 / 商品维护主表（tools/sync_online_products.py:71-72；walmart-maintenance-all-stores/SKILL.md:93）；sheet `38df0D` = 下架表（walmart-daily-retire/SKILL.md:54）；「维护记录_YYYY-MM-DD」= 按日期动态建的 sheet，结果回写列 标题 E / 价格 H / 库存 K（walmart-maintenance-all-stores/SKILL.md:56）；另含上架表 sheet 与 UPC 池 sheet（auto_listing.config FEISHU_SHEET_TOKEN / upc_pool.UPC_SHEET_ID）
- 在线产品总表 20 列（tools/sync_online_products.py:74-81，全部按位置索引，无字段名）：A store B sku C wpid D upc E productName(WMT) F shelf G productType H price_amount(WMT) I availToSellQty(WMT) J publishedStatus K lifecycleStatus L unpublishedReasons M 处理后amz标题 N 相似度 O amz价格 P walmart价格 Q 更新价格 R 库存(DMIT) S 更新库存 T last_updated。M-S 仅对 J=PUBLISHED 行计算（2026-05-18 起）
- 上架表 26 列 schema（2026-05-12 起，erp-online-products-track/SKILL.md:56-60）：A ASIN B 店铺 C amz标题 D walmart_product_type E 审核结果 F 理由 G 审核日期 H amz价格 I 库存 J walmart价格 K 是否上架 L 上架feedid M 上架日期 N 未上架理由 O 上架结果 P 上架失败理由 Q 上架复核日期 R 真实walmart标题 S 真实walmart_product_type T 状态跟踪 U 最近跟踪日期 V 标题相似度 W 价格库存调整 X 价格库存调整日期 Y 异常状态报错 Z 处理feed。**列所有权严格划分**：reconcile 只写 O/P/Q；main.py 提交时一次写定 K/L/M/N；sync_status_track 管 R/S/T/U/V；审核流程独占 D/E/F/G（erp-online-products-track/SKILL.md:69-74,136）
- ⚠️ 同一批救场脚本用的是**旧 schema 的列字母**：tools/rescue_feed_misjudged.py:134-138 写 J:M（J=是否上架/K=feedid/L=日期/M=未上架理由），tools/reconcile_from_row.py:12 筛 J=Yes——对应上架表加列前的 schema。新旧列字母混用是这批脚本最大的地雷
- 店铺KPI 表 `CRfC…kb(token已脱敏,见旧仓库代码)`（sheets 类型，总览 + 每店独立 sheet）；绩效问题订单表 `VbVQ…zd(token已脱敏,见旧仓库代码)`（13 列，永久累积，按数据日期降序）——walmart-kpi-daily/SKILL.md:13-14,81-83
- 售后订单表：wiki 表 `Q2LF…f8(token已脱敏,见旧仓库代码)`，sheet `f83a79`，27 列，按「退货创建时间」倒序**整体覆盖写**（walmart-returns-daily-sync/SKILL.md:52-55）
- TRO 表「TRO案件及商标黑名单」token `ZkL0…hg(token已脱敏,见旧仓库代码)`，sheet `34f9f3`(Tro案件)；新增案件绿色高亮（tro-daily-scrape/SKILL.md:143）。pipeline 另读写 sheet `a7FJb3`(其他补充) / `yoM5mr`(其他补充自动查询)，推送 5 个 sheet：company_overview / not_found / tro_brands / other_supplement_auto / merged_blacklist（daily-tro-pipeline/SKILL.md:60-65）
- 跨文档引用：「禁止品牌收集」sheet `WvPTz2` @「错误商品记录」文档（daily-tro-pipeline/SKILL.md:65）；Amazon 选品黑名单 token 记作 `QNIp…enBb` / sheet `8280e8` / 写 B 列（ASIN 维度，A 卖家·C 类目沃尔玛侧无数据留空）（walmart-daily-cleanup/SKILL.md:59）
- 「监管合规删除」表：F 列未标「是」的条目会被 daily_cleanup Step 0 批量删除；C 列品牌 / E 列知产 ASIN 供品牌采集；B/C/E/F/G/K 类 ASIN 进黑名单（walmart-daily-cleanup/SKILL.md:50,58-59）
- 读写模式：sync_online_products 是**整块重写**不是 upsert——读现有全表 → 内存 merge → 分块 values_batch_update 覆盖 → 若旧表更长再写空行清尾部（tools/sync_online_products.py:886-902）。售后表同为整体覆盖。上架表是 append + 定点 range 写。KPI 表是合并模式覆盖写
- LARK_IO_SHIM=1 环境变量前缀：walmart-kpi-daily / walmart-kpi-afternoon / walmart-daily-retire / walmart-returns-daily-sync 的命令都带此前缀，把飞书读写切到 lark_io 共享层（raw v2 + 分窗 + facade 超时自动回退），修的是历史上 +cells-get/+cells-set/+workbook-info 的 1204/50502 经常性超时；「验证期，去掉前缀即回旧路径」（walmart-daily-retire/SKILL.md:49-51）
- lark_io.sheets_registry.PREFER_RAW_V2 白名单目前只含 (ONLINE_TOKEN=MO2e…mI(token已脱敏,见旧仓库代码), e7834a)（tests/test_lark_io.py:671-686）——即只有在线产品总表强制走 raw v2

### 沃尔玛端点

- GET /v3/items —— catalog，经 auto_listing.sync_status_track.fetch_all_items（tools/sync_online_products.py:65,758-763）。必须按 4 种 publishedStatus 分段拉（PUBLISHED/UNPUBLISHED/SYSTEM_PROBLEM/IN_PROGRESS），单状态 offset 上限 10000，5 轮 nextCursor 每页 1000；超大店（单状态 >10000）超出部分抓不到（erp-online-products-track/SKILL.md:84,119）
- GET /v3/inventories?limit=50&nextCursor= —— tools/sync_online_products.py:468-471，走 walmart_client.safe_get_ex(max_retries=3)，200/min 单店限速，单店内严格 sequential
- GET /v3/inventory?sku={sku} —— 单 SKU 补漏，tools/sync_online_products.py:413-416，同 200/min 限速、单店内串行
- GET /v3/feeds/{feedId}?includeDetails=true —— 下架结果查询（walmart-daily-retire/SKILL.md:55）与维护结果回收 poll_yesterday（walmart-maintenance-all-stores/SKILL.md:56，分页 limit≤50）
- DELETE_ITEM feed —— 两处独立提交：沃尔玛批量下架 daily_retire_orchestrator.py（15:00，飞书下架表驱动）与 沃尔玛问题商品清理 daily_cleanup.py（每 6h，Step 2）
- MP_MAINTENANCE feed —— daily_cleanup Step 1.5 延期 endDate 让过期商品 republish（walmart-daily-cleanup/SKILL.md:52）；以及商品维护的标题更新（walmart-maintenance-all-stores/SKILL.md:79）
- PUT /v3/price 与 PUT /v3/inventory —— 商品维护的 sync 小批量路径（≤5 改价 / ≤10 改库存），结果当场已知、不进 poll（walmart-maintenance-all-stores/SKILL.md:57）
- RETIRE_ITEM —— launchd com.user.autolisting.retire_daily 23:30 走 auto_listing.retire_and_relist --retire（auto_listing/docs/closed_loop.md:67），与 15:00 的 DELETE_ITEM 是两条不同的下架路径
- GET /v3/returns —— 全量拉售后，每店独立 token + 代理（walmart-returns-daily-sync/SKILL.md:52）
- 8 个绩效报告 xlsx（OTD/VTR/取消率/退款率/差评率/退货率/未收到/SRR），并发拉取解析成 13 列（walmart-kpi-daily/SKILL.md:81-82）
- 本模块范围内**没有发现绕过 walmart_client 的 Walmart 直连**：tools/sync_online_products.py:68 统一 import walmart_client 的 BASE_URL/get_token/load_stores/safe_get_ex；erp-online-products-track/SKILL.md:161 明确写「不要试着改成直连」。唯一的非 walmart_client HTTP 是打向 DMIT 采集服务（tools/sync_online_products.py:169,204,232,288,319，httpx 直连 <SCRAPER_VPS_IP,见旧仓库>:8899）和 ERP server（tools/backfill_amz_title.py 用 curl 子进程），都不是沃尔玛
- ⚠️ 飞书侧确实存在绕过共享层的直连：tools/sync_online_products.py:133,531,583 直接 subprocess 调 `lark-cli ... --as user`，不走 lark_io，因此**没有 lark_io 的 90235/50502 退避重试**，写失败只打印一行 ✗ 继续（:602-604）；tools/backfill_pq_20260611.py 复用 sop._run_cli 同样绕过。notify.py:140-164 是「先 lark_io、失败回退裸 lark-cli」的双后端写法

### 魔数与踩坑参数

- tools/sync_online_products.py:89 WRITE_BLOCK_ROWS=4000 —— 注释写死教训：20 列 × 5000 行实测撞飞书 [90227]，4000 行约 4MB 留 25% 余量。别调大
- tools/sync_online_products.py:86-88 READ_CHUNK_ROWS=5000 / READ_BATCH_RANGES=4 / READ_WORKERS=4（与 lark_io/_core.py:60-62 的 CHUNK_ROWS/BATCH_GET_RANGES/READ_WORKERS 是同一组数字的两份拷贝）
- tools/sync_online_products.py:604 每写完一块 time.sleep(0.3)
- tools/sync_online_products.py:120-125 INVENTORY_PAGE_SIZE=50（官方上限）/ INVENTORY_PAGE_SLEEP=0.32（200/min 单店限速 = 300ms + 10% 余量）/ INVENTORY_PATCH_PER_STORE_MAX=0（2026-05-18 起取消单店补漏上限，实测单店漏 0-30 个）/ INVENTORY_PATCH_SLEEP=0.32
- tools/sync_online_products.py:444-446,489 ⚠️ /v3/inventories 退出条件**只能看 nextCursor**，不能用 len(invs)<50——曾导致 A156 第一页 5 条就退出漏 156 条
- tools/sync_online_products.py:448-450 2026-05-15 起 Walmart 强制单店内 inventory sequential，不能同店并发；跨店并发 OK（token+session 各自独立）
- tools/sync_online_products.py:387-396 bulk 后单查补漏：/v3/inventories 偶有漏数据（2026-05-18 实测 A107 漏 0 / A114 漏 9 / D052 漏 30；A128 catalog 901 而 bulk 仅 500）
- tools/sync_online_products.py:396,454 safe_get_ex(max_retries=3) 429/5xx 指数退避（2026-05-16 起）
- tools/sync_online_products.py:115-116 DMIT_REFRESH_POLL_SEC=30 / DMIT_REFRESH_TIMEOUT_SEC=7200（2 小时上限）
- tools/sync_online_products.py:117 ZERO_STOCK_DMIT_ERROR_TYPES={'variant_offset'} → 该批失败的 ASIN 强制写 current_price='偏移'、stock_count='0' 覆盖历史缓存，防旧库存回写（:255-275）
- tools/sync_online_products.py:132,534,586,169 超时分层：lark-cli 通用 120s / 读 120s / 写 240s / DMIT export 600s
- tools/sync_online_products.py:464,284 `while pages < 10000` 防御上限（inventory 与 DMIT results 分页各一处）
- tools/sync_online_products.py:800 --workers 默认 16（捆绑每店 catalog+inventory+patch 串行，跨店并发）；erp-online-products-track/SKILL.md:65 reconcile 用 --workers 20
- tools/sync_online_products.py:855-861 只把 len==10 且 B 开头的 SKU 当 ASIN 推给 DMIT
- erp-online-products-track/SKILL.md:128 Step 2 写完后必须 `sleep 15` 再跑 Step 3——飞书大批量写入后偶发 90235 (data not ready)
- erp-online-products-track/SKILL.md:84 catalog 5 轮 nextCursor 分页、每页 1000、offset ≤ 10000；.claude/commands/fetch-walmart-products.md:31-36 因此必须按 PUBLISHED/UNPUBLISHED/SYSTEM_PROBLEM/IN_PROGRESS 四种状态分段拉才能取全；遇 401/403/404 必须 break 否则死循环
- tools/retry_failed_feeds.py:50,63 串行重跑，每个 feed 间隔 8s 避免飞书限流；FAILED 列表是硬编码的 9 个 (店铺, feedId 前缀) 二元组（:14-24），配 row_index>=3562 过滤（:45）
- tools/reconcile_from_row.py:31-32 --from-row 默认 3562（硬编码的历史断点）/ --workers 默认 8；单店内串行共享 token+速率桶，跨店并发（:68-82）；第二轮把网络抖动失败的 feed 串行重试（:96-110）
- tools/rescue_lost_appends.py:120 append BATCH=500 行/次
- tools/backfill_pq_20260611.py:110 回写 BATCH=3000 ranges/请求（对齐 lark_io MAX_RANGES_PER_OP=3000）
- tools/backfill_amz_title.py:36-37,45,68 SERVER=http://<SCRAPER_VPS_IP,见旧仓库>/erp、CACHE_DIR=/tmp/title_backfill、curl --max-time 20/60、tasks?limit=500&status=done、ThreadPoolExecutor(max_workers=8)、xlsx <1000 字节视为下载失败
- tools/split_xlsx_by_store.py:17-18 INPUT_HEADERS_COUNT=47 / COL_STORE=44(AR 列)——与 auto_listing/excel_io.py INPUT_HEADERS 手工对齐，改一边会静默错位
- tools/rescue_a116_full.py:32-45 全部硬编码：TASK_ID / FEED_ID / STORE=A116林世强 / LISTED_DATE / 期望命中 195 行（:78 数量不符只告警不停）；output.xlsx 列号写死 COL_IS_LISTED=51(AY) … COL_NOT_LISTED_REASON=54(BB)
- lark_io/_core.py:29-35 DEFAULT_TIMEOUT=120(可被 LARK_IO_TIMEOUT 覆盖)/BATCH_TIMEOUT=180/LIGHT_TIMEOUT=60/HEAVY_TIMEOUT=600；MAX_ATTEMPTS=4；BACKOFF=(1,2,4,8)
- lark_io/_core.py:48-50 分块三条件取先触发者：MAX_RANGES_PER_OP=3000 / MAX_CELLS_PER_OP=4500 / MAX_BYTES_PER_OP=120000（tests/test_lark_io.py:301-366 逐条断言；:311-322 记录了一个 spec 与实现的偏差：3001 个 op 其实先撞字节上限而不是 ranges 上限）
- lark_io 瞬时错误码集合（tests/test_lark_io.py:187-220 断言）：90235(data not ready) / 90217 / 50502(facade timeout) + 文本匹配 'Too Many Request' / 'TIMEOUT' / 'data not ready'；非瞬时（如 400）立即抛不重试（:227-250）
- lark_io/_http.py tenant token：提前 5 分钟刷新、99991663(invalid token) 清缓存重试一次、99991400 与 50502 视为瞬时（tests/test_lark_io.py:838-900,930-960）
- notify.py:63-66 DEFAULT_TARGET=ou_36c5f91668c42a735e7b9d4ae74eedc1（苏里，可被 SKILL_NOTIFY_TARGET 覆盖）/ LARK_CLI=/opt/homebrew/bin/lark-cli（LARK_IO_BIN 可覆盖）/ CN_TZ=UTC+8 / 发送 timeout=60（:144,157）
- 沃尔玛订单审核/deploy/com.user.walmart.order_audit_hourly.plist:52-56 环境变量 AUDIT_POLL_TIMEOUT=3600 / AUDIT_POLL_INTERVAL=20；增量窗口自带 1h 重叠 + 30d 兜底（plist 注释 :7），偶尔漏跑下轮自动补齐
- SKILL 通用容错口径（14 个文件重复同一段）：失败步骤退避重试 2–3 次，间隔约 5s→15s→30s；walmart_client 有 90s socket 超时 + 429 退避；飞书限流码 429/50502/9020x/9023x/1061045 一律视为瞬时
- walmart-kpi-daily/SKILL.md:60-73 Phase 1 并发 6 线程；影刀 RPA 超时阈值 10 分钟，超时用旧数据降级；**40+/50 店成功即视为通过**；约 5-6 个店铺 ProxyError(Invalid username/password) 属已知正常现象
- walmart-daily-order-sync/SKILL.md:72-76 异常判定阈值：总耗时>180s → warn；临时失败>5 家 → warn；飞书 90204/90227 → fail
- walmart-returns-daily-sync/SKILL.md:60-63 校验基线 ~3800 行；若 N 比基线低 50% 以上**不要重跑**，只汇报等人确认
- walmart-daily-retire/SKILL.md:68-71 仅 FBM 支持 DELETE_ITEM（WFS 会报错）；单 feed 文件上限 0.1MB（约 1000 SKU）；10 feeds/hour；单日上限读「综合数据源」sheet 的 I 列「单日最大下架数量 fbm」；重试 SKU **不**计入单日上限（:60）
- walmart-maintenance-all-stores/SKILL.md:57 sync 路径阈值：≤5 条改价 / ≤10 条改库存走 PUT /v3/price 与 PUT /v3/inventory（当天即知结果、不进 poll），超过才走 feed；poll_yesterday 分页 limit≤50（:92）
- weekly-brand-refresh/SKILL.md:68-74 |Δ品牌数|>500 或 总耗时>900s（正常 ~8min）→ warn
- daily-tro-pipeline/SKILL.md:76-78 总耗时>300s → warn；merged_blacklist 比上次跌 >1000 行 → 疑某源读失败
- sync-blacklist-brands-daily/SKILL.md:95-100 worker 数已从 14 下调到 10 减本机负载；stop→sleep 2→start→sleep 6→查 /api/workers/online 应为 {"online":10}
- tro-daily-scrape/SKILL.md:57 61tro **必须从 page 1 开始**（不能用 start_page），新案件出现在首页；各源 max_pages=10，worldtro 只抓当年（max_pages=1）

### 防重/幂等语义

多层、全靠外部状态位，没有统一幂等键。(1) 任务级单实例：订单同步.py 用 fcntl 锁 /tmp/walmart_order_sync.lock，上一轮没跑完本次自动跳过（沃尔玛订单审核/README.md:165）——这是唯一防并发的机制，其余 skill 都没有锁。(2) 下架去重：飞书下架表 C 列有 feedid 的行只**查询**结果不重复提交，E 列写「是」/「否N」(N=历史失败次数+1)，重试 SKU 不计入单日上限（walmart-daily-retire/SKILL.md:55-61）。(3) 维护去重：SQLite maintenance.db 的 submissions/feed_items 记录提交，次日 poll_yesterday 只 poll walmart_feed_id 非空的 D-1 提交，回写飞书当日「维护记录」sheet（walmart-maintenance-all-stores/SKILL.md:56-57）。(4) reconcile 去重：按 (feedId, store) 去重，自动跳过 `healed:*` 伪 feedId（tools/reconcile_from_row.py:45-47；erp-online-products-track/SKILL.md:69）；sync_status_track 对 K=Unknown 行自愈时写 L=`healed:catalog_<date>` 标记（SKILL.md:135）。(5) 救场脚本各自的去重：rescue_lost_appends 先读飞书现有 (asin,store) 集合再补差集，且只补 K=Yes 行（K=No 是淘汰行，重跑可再生，不补）（tools/rescue_lost_appends.py:85-98）；rescue_feed_misjudged 跳过 feed_id 已等于目标值的行（已救过，:171）、UPC 池只标 marker 为空的（:216-224）；backfill_amz_title 只补 C 列为空的行（:130），而 backfill_walmart_title **故意覆盖所有命中行**不管是否已有值（tools/backfill_walmart_title.py:6）——两个同名同类脚本语义相反，别混。(6) notify.py 用 --task 名做 start/done 配对键，标记文件在 done 后 unlink（notify.py:218）；任务崩溃不发 done → 标记残留 → 下次算出错误耗时。(7) 通用口径写在每个 SKILL.md 的韧性原则里：读取/同步/覆盖写幂等可整条重跑；提交 feed / 删除商品 / 改价改库存**非幂等，绝不盲目整条重跑**，靠脚本内部重试 + 状态回写去重（例：walmart-daily-cleanup/SKILL.md:18-19「不要因个别店报错就整条 daily_cleanup.py 重跑，会重复删除/重复延期，有问题记录后等下一轮（最多 6h）」）。

### 危险操作

- DELETE_ITEM（永久不可恢复）× 2 条独立链路：walmart-daily-retire 15:00 与 walmart-daily-cleanup 每 6h。保护措施全是**软约束**：飞书下架表 C 列 feedid 存在则不重复提交、单日上限读「综合数据源」sheet I 列、仅 FBM 支持（WFS 报错）、单 feed ≤0.1MB(~1000 SKU)、10 feeds/hour；SKILL.md 用文字告诫「提交阶段失败不要重跑，留到下一轮」（walmart-daily-retire/SKILL.md:18-19）。**没有 dry-run 开关，没有 --execute 闸门**
- MP_MAINTENANCE feed 延期 endDate（daily_cleanup Step 1.5）与 Stage 待发布商品识别（Step 1.6 不删）——保护是脚本内部的 SKIPPED_* 计数（SKIPPED_STAGE / SKIPPED_UPC_LOCKED / SKIPPED_9_False / SKIPPED_NOT_DELETE，walmart-daily-cleanup/SKILL.md:64）
- 沃尔玛商品维护 submit.py 改价/改库存/改标题 —— **唯一有真闸门的**：必须同时加 `--execute --confirm-zeroing`，缺一被脚本自身安全闸门拒掉（walmart-maintenance-all-stores/SKILL.md:69-72）。--confirm-zeroing 专门保护「库存清零」
- RETIRE_ITEM：launchd retire_daily 23:30 扫飞书 SKU_LOCKED 行提交，无 dry-run（auto_listing/docs/closed_loop.md:67）
- auto_listing.main 上架提交 MP_ITEM feed + 消耗 UPC 池 —— 非幂等且不可撤销（重复提交会浪费 UPC 并产生重复 listing）。SKILL 说已改手动，launchd 06:00 可能仍在自动跑（见 schedule 第 3 条）
- tools/sync_online_products.py 对 138k+ 行的「在线产品总表」**整块重写 + 清尾部空行**（:886-902）。若某轮 catalog 大面积失败，写进去的就是残缺表；唯一保护是 `if total_products == 0: 退出不动飞书`（:881-883）——只防「全空」，不防「只抓到一半」。且写失败只打印 ✗ 不中止（:602）
- weekly-brand-refresh 每周一 TRUNCATE company_brand_details 全量重查（weekly-brand-refresh/SKILL.md:9-13）；保护是事后阈值 |Δ|>500 报 warn 让人看
- sync-blacklist-brands-daily Step 3 重启 worker —— **会中断正在跑的 audit 任务**。保护是 Step 2 先 curl /api/jobs?limit=20 检查队列，有任务在跑就跳过重启并报 warn（sync-blacklist-brands-daily/SKILL.md:66-85）
- tools/rescue_lost_appends.py —— 默认 dry-run 盘点，`--apply` 才真 append（:16-17,88-91）；这是 tools/ 里少数默认安全的。风险：append 500 行/批若中途失败会产生部分写入，重跑靠对照飞书现有行去重
- tools/rescue_feed_misjudged.py —— 有 --dry-run 与 --skip-upc-pool，但**默认不是 dry-run**；会批量改飞书 J/K/L/M（把 J=No/Unknown 改成 Yes）并把 UPC 标「已用」。标错 = UPC 被永久占用
- tools/rescue_a116_full.py —— **无 dry-run，无参数，全硬编码**，直接 append 195 行到飞书。期望命中 195 行，数量不符只打警告不停（:78-80）。重跑 = 重复 append
- tools/backfill_walmart_title.py / backfill_pq_20260611.py / backfill_daily_sheet_20260611.py / reconcile_from_row.py / retry_failed_feeds.py —— 前三个有 --dry-run；后两个（reconcile_from_row、retry_failed_feeds）**没有 dry-run**，直接跑真 reconcile 写飞书
- daily_cleanup Step 0：读飞书「监管合规删除」表，批量删除 F 列未标「是」的条目 —— 删除动作由飞书表格内容驱动，操作员误改表即触发（walmart-daily-cleanup/SKILL.md:50）

### 切换时必须迁移的状态

- 沃尔玛商品维护/maintenance.db 全库（submissions / feed_items / inputs）—— 切维护工作流时必须搬，poll_yesterday 依赖它判断 D-1 有哪些 feed 待回收
- PostgreSQL walmart_cleanup（runs + error_items）—— 问题商品清理的历史与归类
- PostgreSQL uspto 全库 + 商标数据本地 DB（tro_cases / matched_companies / company_brand_details）
- tro-scraper-matrix 的 5 个源 SQLite + merged.db（增量采集靠 before/after 计数判新增，丢库=全量重爬）
- 新审核系统 PG 的 blacklist_brands 与 Phase0 三表（sellers/asins/amazon_cats）
- ⚠️ auto_listing/logs/feed_*.json 与 run_*.log —— 不是可丢日志：feed JSON 是「我们提交给 Walmart 的标题」唯一记录（tools/backfill_walmart_title.py:32-36），run 日志是飞书 append 失败行的唯一恢复源（tools/rescue_lost_appends.py:11-14）。迁移前必须整包保留或先跑完 rescue/backfill
- 飞书 UPC 池的「已用/已领/冲突」标记状态（tools/rescue_feed_misjudged.py:71-97 依赖它做去重）
- 飞书各业务表当前内容（上架表 26 列 / 在线产品总表 20 列 / 下架表 C-E 列 feedid 与是否删除 / 维护记录_YYYY-MM-DD 系列 sheet / 绩效问题订单永久累积表）
- ERP listing server 的 dedup cache 内容（可由新系统重建，但切换窗口内必须保证非空，否则 worker 去重失效会重复上架）
- notify.py 的 /tmp/erp_skill_notify/*.json 标记（无需迁移，但切换时应清空，避免残留标记算出荒谬耗时）
- 调度注册本身：14 条 skill 的 cron 只存在于外部平台（hermes）配置里、6 个 launchd plist 只存在于 ~/Library/LaunchAgents/，仓库里没有权威清单——切换前必须先在机器上 `launchctl list` + 导出平台任务列表，否则会漏停旧调度

### 迁移建议

【照搬（这些是血换来的常量，直接抄进新仓 refdata/ 或 services 常量）】
- inventory 分页三件套：limit=50、只看 nextCursor 退出（绝不用 len<50）、单店 sequential + 0.32s 间隔（200/min）、bulk 后单查补漏。tools/sync_online_products.py:120-125,440-495 整段逻辑连注释一起搬。
- catalog 四状态分段 + offset≤10000 的分页策略（.claude/commands/fetch-walmart-products.md:31-36）。
- 飞书写块 4000 行（不是 5000，20 列会撞 90227）、lark_io 的 MAX_RANGES=3000 / MAX_CELLS=4500 / MAX_BYTES=120000 / CHUNK_ROWS=5000 / MAX_ATTEMPTS=4 / BACKOFF=(1,2,4,8) / 瞬时码集合 {90235,90217,50502,90204,90227,1061045,'Too Many Request','data not ready'}。这些进 api/feishu.py，别重新试错。
- 大批量写飞书后 sleep 15 再读（90235 data not ready）。
- DELETE_ITEM 的三条硬限制：仅 FBM、单 feed ≤0.1MB(~1000 SKU)、10 feeds/hour。
- 维护的 sync/feed 分流阈值（≤5 改价 / ≤10 改库存走 PUT，超过走 feed）。
- tests/test_lark_io.py 整份可以直接移植成新仓 api/feishu.py 的测试（纯 mock，989 行，覆盖 5 种返回 shape、瞬时/非瞬时判定、三种分块触发、分窗读顺序、富文本扁平化、workbook_info 回退、tenant token 缓存与失效重试）——这是旧仓唯一像样的测试资产。

【重做（不要照搬旧形态）】
- **14 个 SKILL.md 全部作废，一条不留**。它们本质是「用自然语言告诉 AI 怎么重试、什么算成功、通知发给谁」，这些在新架构里应该是 cli.py 的能力：flock 单实例锁、ops.runs 运行记录、成功/失败飞书通知、dangerous=True 强制 dry-run。每个 SKILL.md 对应新仓一个 `workflows/<name>.py` 的 `run(params)`，SKILL.md 里那些阈值（40+/50 通过、耗时>180s warn、|Δ|>500 warn、N 低于基线 50% 不重跑）应该变成 run() 返回的结构化摘要字段 + cli.py 里的告警规则，不是散在 markdown 里的口头约定。
- notify.py 的标记文件计时（/tmp/erp_skill_notify/*.json）→ 换成 ops.runs 表的 started_at/finished_at，天然解决「崩溃不发 done 导致标记残留算错耗时」。notify.py 的两处仍值得保留：启动即 pop FEISHU_APP_ID/FEISHU_APP_SECRET（:69-70）、通知失败永不阻断任务（永远 exit 0）。
- tools/sync_online_products.py 直接 subprocess 调 lark-cli（:133,531,583）绕过共享层、没有瞬时码重试——新仓必须走 api/feishu.py 单一入口。写失败只打印不中止（:602）也要改成累计失败数并让 workflow 决定是否 fail。
- 「整块重写 138k 行 + 清尾部」这个模式在新仓换成 catalog/listing schema 的 upsert；飞书只做展示层的定期投影，不再当主库。这样也就不需要 backfill_pq / backfill_daily_sheet 这类脚本。
- 9 个救场脚本**一个都不要移植**，但每一个都要读一遍当作需求：它们精确指出了旧系统的 9 处崩坏点，新仓必须从设计上消除。对应关系：
  · rescue_lost_appends（飞书 append 遇 90235 无重试 → 行永久丢失，5-09 起零星、6-08 多 worker 并发后恶化；靠日志里打印的 --values JSON 恢复）→ 新仓：写库在前、写飞书在后，飞书写失败可无损重放。
  · rescue_feed_misjudged Case A/B（mark_used 的 lark-cli timeout 被兜底成 'unexpected' 整店错写 No；submit_feed 3 次 POST 全 timeout 且反查未命中，其实 Walmart 已收到）→ 新仓：铁律「防重状态先落库再调接口」+ 重启时 pending 记录先查 Walmart 实际状态，正是为这两个 case 定的。
  · rescue_a116_full（195 行整店误判 + 服务端 output.xlsx 也要手改）→ 同上。
  · backfill_daily_sheet_20260611（7733 行连续区间单次写超 ARG_MAX 挂掉，daily sheet 一行没写上，次日 poll 无从回填）→ 新仓：命令行传参改 stdin/数据库，且写飞书必分块。
  · backfill_pq_20260611（pricing 的 '275%' 倍数列解析回归 → 52027 行 P/Q 全空 → 14:00 维护链读不到 Q=是 → **全天价格维护静默未触发**）→ 新仓：上游解析失败必须 fail loud，下游 workflow 要对「触发数为 0」告警。
  · backfill_amz_title / backfill_walmart_title（标题只存在于 server 的 output.xlsx 和 auto_listing/logs/feed_*.json 里，没进库）→ 新仓：提交给 Walmart 的 payload 必须落 ops 表。
  · reconcile_from_row / retry_failed_feeds（硬编码 row_index>=3562 和 9 个 feedId 前缀，8s 间隔防限流）→ 新仓：feed 状态查询用 listing schema 的 pending 队列 + 统一退避，不需要人肉列 feedId。
  · split_mp_item_spec（451MB MP_ITEM monolith 一次 json.load 涨到 1.3GB，配合 LLM phase 5048 行场景 RSS 飙到 12GB OOM）→ 新仓：spec 按 PT 拆分后进 refdata/ 或对象存储，永不整体 load。
- split_xlsx_by_store.py 的 INPUT_HEADERS_COUNT=47 / COL_STORE=44 与 auto_listing/excel_io.py 手工对齐 —— 新仓所有列定义进 registry，杜绝这种双份魔数。

【迁移顺序建议】
1. 先做调度盘点：`launchctl list | grep -E 'autolisting|order_audit|erp_worker'` + 导出外部平台（hermes）的 14 条任务，形成权威清单——仓库里没有，必须在机器上取。
2. 特别确认 com.user.autolisting.morning 是否仍 loaded；若是，它 06:00 在自动上架，与「上架已改手动」的文档矛盾，是最大的未知破坏源，优先停或确认。
3. 按铁律「停旧调度 → 搬状态 → 起新调度」逐条切；破坏性工作流（DELETE_ITEM / RETIRE_ITEM / 改价改库存 / 上架）必须完成 dry-run 人眼确认后才 --execute，且严禁与旧调度并跑。
4. 建议切换顺序（从只读到破坏性）：returns 同步 → KPI/日报 → 在线产品总表同步 → 订单同步 → reconcile/状态跟踪 → 商品维护 → 批量下架 → 问题商品清理。TRO/商标/黑名单三条链跑在另外两个仓（tro-scraper-matrix、商标数据、新审核系统），本次迁移是否纳入需要先定边界。

### 待确认问题

- 14 条 skill 的真实 cron 到底注册在哪个平台？README 只说「与 2026-06-17 hermes 现状对齐」，仓库里没有任何平台配置文件。必须在机器上导出权威清单，否则切换时一定漏停旧调度。
- com.user.autolisting.morning（06:00 full_morning，链末尾是 auto_listing.main 上架）是否仍 loaded？若是，则每天 06:00 有无人值守的 MP_ITEM feed 提交 + UPC 消耗，与 erp-online-products-track/SKILL.md:54,164「上架已移除、由用户手动触发」直接矛盾。这是本次盘点发现的最大风险点，需实机 launchctl list 确认。
- 沃尔玛问题商品清理 daily_cleanup（每 6h，含 DELETE_ITEM）的调度器在哪？docs/feishu_cutover_checklist.md:54 明确说「不在本仓 launchd/，先 launchctl list | grep 定位」。既不在 skill 的 launchd 列表也不在 auto_listing/launchd/。
- 订单同步的 launchd 每小时 :15 与 skill 13:30 是有意保留的双保险，还是 skill 化之后忘了停 launchd？需要产品侧确认保留哪一条。
- 20 个常驻 com.nextderboy.erp_worker 是否还该运行？erp_worker/README.md:204 自己也在问，说 erp-core 走 Celery 是另一套架构、不共享队列，未确认能否安全停。
- TRO 采集（tro-scraper-matrix）、USPTO 商标（商标数据）、审核黑名单（新审核系统）这三条链跑在 erpAPI 之外的独立仓库，5 条 skill 依赖它们。本次 WalmartAPI-Contral 迁移是否纳入这三条？若不纳入，notify.py 的通知契约要不要保留一份给它们用？
- 影刀 RPA（.mcp.json 的 yingdao MCP server + spawn_yingdao + 轮询 latest.json）是 walmart-kpi-daily Phase 1c 的硬依赖，只能在那台 Mac 上跑（/Applications/影刀.app）。新系统若要脱离这台机器，「卖家名称 / 销售状态」两列数据源需要另找方案。
- DMIT 采集服务（<SCRAPER_VPS_IP,见旧仓库>:8899）与 ERP listing server（<SCRAPER_VPS_IP,见旧仓库>/erp）都是外部服务，new registry 里要不要登记？sync_online_products 对 DMIT 的依赖是硬的（M-S 七列全靠它）。
- auto_listing/logs/ 目前既是日志又是恢复源，迁移窗口内谁负责保证不被轮转清掉？建议切换前先整包归档。
- tools/ 里 reconcile_from_row.py 与 retry_failed_feeds.py 的硬编码（row_index>=3562、9 个 feedId 前缀）说明当时有一批行没修完 —— 需确认这些历史遗留行现在是否已收敛，否则迁移时会带着脏数据过去。


<a id="specs_refdata"></a>
## 官方 specs / PT 模板 / 类目映射产物

### 模块职责

这是旧仓库里唯一的"静态参考数据 + 规范"层,不是运行时业务流程,而是被其它工作流(auto_listing/risk_gate、批量下架、问题商品清理、商品维护)当作只读知识库消费的一堆产物文件。它包含四类东西:①沃尔玛官方接口规范(DELETE_ITEM / Price&Promotion / MP_ITEM_MATCH v4.2 JSON Schema、OrderManagement/Inventory/Price 的 XSD、20 份 OpenAPI yml、PT_Mapping 与 spec diff xlsx);②从 MP_ITEM spec 里抽出来的 PT 上传模板(约 6942~6951 个 Product Type 的字段/必填/枚举清单);③Amazon 叶子类目 → Walmart Product Type 的映射表(v5.5,15770 行,由 40+26+17+26 个 LLM 子代理分批生成并迭代审核而来)与配套的 PT 5 维度风险表、禁售政策知识库;④运维参考文档(限速表、飞书表清单、lark-cli 用法、运营知识手册)。所有产物最终都同步进飞书 6 张 sheet 供人看,并被 auto_listing 的 risk_gate 反向读回来做上架拦截。整个模块没有任何数据库、没有任何调度,全部是人手动跑脚本 + xlsx/json 文件 + 飞书表。

### 入口与触发

- /workspace/erpapi/类目映射/active/extract_pt_templates.py — 手动执行,新 Walmart spec 发布时(约每 1-2 个月)重跑。无 argparse,路径全硬编码。从 MP_ITEM spec JSON 抽 PT 模板,产出 pt_templates_full.json / pt_templates_summary.xlsx / pt_templates_all_fields.xlsx
- /workspace/erpapi/类目映射/active/sync_v5.5_to_feishu.py — 手动执行,v5.x 数据更新后。README(类目映射/README.md:179)明确说这是模板,下次升级要复制改名为 sync_v5.6_to_feishu.py(即版本号写死在文件名和逻辑里)
- /workspace/erpapi/类目映射/pipeline/04_risk_compliance/aggregate_bizcn_full.py → rebuild_risk_dimensions.py → apply_risk_v2_to_walmart_categories.py [--dry-run] — 手动四步链,每月/每季度跑一次,顺序不可乱(类目映射/README.md:277-283)
- /workspace/erpapi/类目映射/pipeline/04_risk_compliance/crawl_prohibited_policies.py → (人工改 walmart_prohibited_detailed.md) → sync_prohibited_to_feishu.py [--dry-run] — 官方政策页更新时手动跑(类目映射/README.md:286-292)
- /workspace/erpapi/类目映射/pipeline/03_v5_iterations/apply_recheck_to_v5.py → apply_fine_recheck_to_v5.1.py → apply_new_pts_to_v5.3.py → apply_remap_to_v5.4.py → apply_audit_to_v5.5.py — 五步严格顺序,跳步结果错乱(类目映射/README.md:110, 332)
- /workspace/erpapi/类目映射/tools/walmart_category_lookup.py — 唯一带 argparse 的一次性工具(--input/--output/--wm/--amz),README 说现在已被映射表 left join 取代
- 无 launchd/cron:本模块所有脚本都不在定时任务里。docs/feishu_cutover_checklist.md:38 列出的真实 launchd 任务(morning/health_4x/store_status_hourly/reconcile_hourly/retire_daily/order_audit_hourly)全部属于其它模块

### 调度

none —— 本模块所有脚本都是人手动触发,没有任何 launchd plist / cron。README 只给了『何时重跑』的自然语言条件:extract_pt_templates.py = 新 Walmart spec 发布时(约每 1-2 个月);03_v5_iterations 五步 = v5 重新生成后或沃尔玛类目结构变更后;04_risk_compliance 风险链 = 每月/每季度或飞书『错误商品记录』更新后;crawl_prohibited_policies = 官方政策页有更新时;01_amazon_taxonomy = Amazon 大改类目时(约每年 1-2 次)。docs/feishu_cutover_checklist.md:38 列的真实 launchd 任务(morning 06:00 / health_4x 08·12·16·20 / store_status_hourly :05 / reconcile_hourly :15 / retire_daily 23:30 / order_audit_hourly :15)全部属于其它模块,与本模块无关。

### 数据存储

- 【最终映射表 · 新系统要导入 catalog 的就是这个】/workspace/erpapi/类目映射/data/mapping_detail_v5.5.xlsx,1,269,385 字节(1.2 MB),xlsx。sheet『映射明细』15,771 行(含表头)× 11 列 = 15,770 条数据;sheet『按Category汇总』28 行 × 7 列。11 列表头逐字为:Walmart Category | Walmart PTG | Walmart Product Type | Amazon 叶子 | Amazon 路径 | browse_node_id | 排名 | 置信度 | 匹配方式 | 备注 | 来源批次
- 映射表回滚版本:/workspace/erpapi/类目映射/archive_data/mapping_detail_v5.4.xlsx(同 11 列 15,771 行,1.2 MB)与 /workspace/erpapi/类目映射/archive_data/mapping_detail_v4.xlsx(8,920 行 × 10 列,是 W→A 反方向的老映射,列结构少一个『来源批次』)
- PT 模板汇总(两份不一致的副本!):/workspace/erpapi/类目映射/data/pt_templates_summary.xlsx = 6,952 行(6,951 个 PT)× 5 列;/workspace/erpapi/pt_templates/pt_templates_summary.xlsx = 6,943 行(6,942 个 PT)× 5 列。列名相同:Walmart Product Type | 字段总数 | 必填字段数 | 必填字段清单 | 核心字段(全部前20)。同一个 PT('3-in-1 Shampoo...')两份的『必填字段清单』内容顺序完全不同 → 来自不同 spec 版本 + 集合无序(见 pitfalls)
- /workspace/erpapi/pt_templates/pt_templates_summary_sorted.xlsx = 6,943 行 × 7 列,比上面多前置两列 Walmart Category | Walmart PTG,是唯一带类目三级归属的 PT 模板表
- 【仓库里没有的大文件】data/pt_templates_full.json(292 MB,程序读取用)与 data/pt_templates_all_fields.xlsx(384,826 行全字段明细)被 /workspace/erpapi/类目映射/.gitignore:2-3 显式忽略,克隆里不存在。add_ts_audit_warnings.py:37 依赖 pt_templates_full.json
- 【仓库里没有的官方 spec】walmart_spec_version_check.md:14-19 记录 walmart_official_specs/MPSetup/(451MB) / MPMaintenance/(424MB) / WFSSetup/(447MB) / WFSConvert/(45MB) 四个 MP_ITEM 系 spec JSON,克隆中 walmart_official_specs/ 下只有 DELETE_ITEM/PricePromotion/openapi/xsd_schemas 四个子目录,合计 18 MB。真正的上架 spec 不在 git 里
- PT 池与类目树 JSON:/workspace/erpapi/walmart_specs/all_product_types.json(2,312,611 字节)结构 {total:6942, unique:6942, product_types:[6942 个 PT 名字符串], category_map:{24 个 Category → [{productTypeGroup, productType, description}]}};/workspace/erpapi/walmart_specs/taxonomy_v5.json(1,743,764 字节)结构 {status:'OK', itemTaxonomy:[24 个 {description, category, productTypeGroup:[{productTypeGroupName, description, productType:[{productTypeName, description}], department:[{departmentName, departmentNumber}]}]}]} —— department 号只在这份里有
- PT 5 维度风险表 v2:/workspace/erpapi/类目映射/data/PT风险5维度_v2.xlsx,6 个 sheet:0_预警清单(78×4: PT|BIZ-CN去重SKU|BIZ-CN原始次数|建议)、1_中国卖家禁售(61×4,含『来源』列)、2_品牌锁定(40×4)、3_禁售高发(50×4)、4_受限需审批(38×4)、5_知产高危(20×4)
- PT 5 维度风险表 v1(列结构与 v2 完全不同,不能混用):/workspace/erpapi/类目映射/data/PT风险5维度.xlsx,5 个 sheet,每 sheet 列数 4~8 不等(如 1_中国卖家禁售 是 56×8: PT|BIZ-CN次数|非过期错误|BIZ-CN占非过期%|C品牌|B禁售|E知产|F限类)。归档副本 archive_data/PT风险5维度_202604.xlsx
- 中间产物:/workspace/erpapi/类目映射/intermediate/biz_cn/bizcn_aggregate_20260611.csv(2,497 字节)、intermediate/risk_v2_apply_report_20260611.tsv(24,282 字节,4 列 PT|列|变更|原因,是最近一次写飞书的变更审计)、intermediate/policy_crawl_20260611/(46 个政策页各一份 .html + .txt,外加 _summary.tsv)。README 里提到的 walmart_cat_full.tsv / err_stats.csv / pt_diff/ / recheck_v54/ / pt_5_dimensions.xlsx 在克隆里都不存在
- 官方规范文件:walmart_official_specs/DELETE_ITEM/5.0.20250919-16_45_47-api_DELETE_ITEM.json(19,763B,draft-07)、PricePromotion/Price&PromotionJSON/{Price&PromotionFeed.json, Price&PromotionHeader.json, Price&Promotion.json, Price&PromotionJsonExample, Price&PromotionCurlExample}(draft-06)、MP_ITEM_MATCH_v4.2.json(19,763B,draft-04,version enum 锁 '4.2',sellingChannel enum 锁 'mpsetupbymatch')、openapi/ 20 个 yml(6.0 MB)、xsd_schemas/{OrderManagementV3(5 个 V3.3 xsd)、InventoryManagement(3)、PriceManagement(11)、PriceJSON(3 json)}、PT_Mapping.xlsx(sheet『PT Mapping』6,681×16 列 Department 1..12 等 + sheet1 101 行 PT 清单)、MPSetup_FeedDiff.xlsx(Cover Page + Snapshot diff,11 列 Change#/Data Model/Change Type/Attribute Name/Property Changed/Old Value/New Value/Impacted System/Breaking Change?)
- 知识库 markdown(根目录,被类目映射 pipeline 引用):walmart_compliance_kb.md(31,022B)、walmart_prohibited_detailed.md(62,156B,build_prohibited_sheet.py 解析它生成飞书禁售表)、Walmart_Marketplace_API_Guide.md(113,595B)、walmart_spec_version_check.md;另有一份 fork:类目映射/active/walmart_compliance_kb.md(33,995B,md5 与根目录那份不同 → 两份已分叉)
- 运维文档:/workspace/erpapi/docs/{walmart_rate_limits.tsv(188 行 5 列,已一字不差搬到新仓 /home/user/WalmartAPI-Contral/refdata/walmart_rate_limits.tsv), feishu_sheets_registry.md(9 个物理 workbook + 18 个 sheet_id 的事实底表), feishu_migration_plan.md(61KB), feishu_cutover_checklist.md, lark-cli-reference.md};/workspace/erpapi/参考资料/walmart_ops_knowledge_v4.docx.md(30,153B,运营知识手册 5 模块)
- 本模块无任何 SQLite / PostgreSQL / JSON 状态文件。全部状态就是 xlsx 产物 + 飞书表本身。

### 飞书使用

- 【CATEGORY workbook · 本模块的主战场】app_token/spreadsheet_token = Gx9H…wc(token已脱敏,见旧仓库代码)(语义名 CATEGORY,docs/feishu_sheets_registry.md:15)。这是电子表格(sheets v2/v3),不是多维表格 bitable。
-   sheet 0bdc8b『沃尔玛类目』7,008 行 —— 列位硬编码:A=Walmart Category, B=Walmart PTG, C=Walmart Product Type, D=准入状态(值『禁售』), E=中国卖家可做(值『是』/『否(上架记录回测,BIZ-CN触发N个SKU)』/『否(Walmart 禁售)』/『需评估…』), F=(未被脚本使用), G=特殊备注(追加式,🚫品牌锁定(N)/🚫禁售高发(N)/🚫受限需审批(N)/🚫知产高危(N)/⚠️BIZ-CN预警(NSKU))。另有 compliance_enrichment.py:292 写的『销售资质要求 / 所需认证/文件 / IP侵权风险 / 合规说明』四列。读用 +csv-get A{s}:G{e},写用 +batch-update(BATCH=80)。
-   sheet 2p5sL6『映射明细』15,770 行 × 11 列(A-K) —— sync_v5.5_to_feishu.py 全量覆盖写,并把旧 v4 遗留的 L-T 列清空。
-   sheet OJSrkV『沃尔玛禁止』—— sync_prohibited_to_feishu.py 用 csv-put 全量覆盖,2026-06-11 起 46 行(README:53 写 38 行是旧数,以 .agent/feature_list.json 第 4 条『45+行』和 README:346『46行重写』为准)。
-   sheet 3b5Gpy『PT上传模板_汇总』6,951 行 —— integrate_templates_to_feishu.py 写,内容是 pt_templates_summary.xlsx 的 5 列。
-   sheet 2NgLNm『待人工复核』—— build_review_queue.py 写(feishu_sheets_registry.md:15)。
- 【ERROR_PRODUCTS workbook · 风险表的数据源】token = YlA1…dd(token已脱敏,见旧仓库代码)。sheet aCz4c『错误统计』4,499 行(PT × 13 个错误码 A-Z,rebuild_risk_dimensions.py:34-35 读);sheet WvPTz2『禁止品牌收集』(risk_gate 读品牌黑名单);约 70 个日表 sheet 名格式 'YYYY.M.D问题商品'(aggregate_bizcn_full.py:34 正则匹配,cells-search 找 K 列含 BIZ-CN,再读该行 B=SKU / E=ProductType)。
- 【读写模式】全部通过 lark-cli 子进程调用(subprocess.run(['lark-cli','api',...]) 或 ['lark-cli','sheets','+csv-get'/'+batch-update'/'+cells-search'/'+workbook-info'])。成功判定是对 stdout 正则搜 '"code"\s*:\s*0'(sync_v5.5_to_feishu.py:60)—— 纯字符串匹配,没有 JSON 解析,任何返回体里出现 code:0 子串都会被当成功。
- 【身份】本模块脚本一律 --as bot。feishu_sheets_registry.md:15 记 CATEGORY workbook 对 bot 是只读用途(risk_gate 读),但 apply_risk_v2/sync_prohibited/apply_cpc/sync_v5.5 都在往它写 —— 即 bot 对该 workbook 事实上有写权。
- 【错误码语义】docs/feishu_cutover_checklist.md:50502=facade 超时(应降级 raw v2)、90235/90217=瞬时可重试(不得误判为空表)、99991663=token 失效需重取(仅 HTTP 后端)、90204=sheets_batch_update 不支持 insertDimension。

### 沃尔玛端点

- POST /v3/feeds?feedType=DELETE_ITEM —— spec 定义在 walmart_official_specs/DELETE_ITEM/5.0.20250919-16_45_47-api_DELETE_ITEM.json。本模块只提供 spec,不调用;调用方是 沃尔玛批量下架/daily_retire_orchestrator.py:621 与 沃尔玛问题商品清理/daily_cleanup.py:388。docs 记该端点配额 10/hour/店铺(沃尔玛问题商品清理/README.md:220)。
- POST /v3/feeds?feedType=PRICE_AND_PROMOTION —— spec 在 walmart_official_specs/PricePromotion/Price&PromotionJSON/。CurlExample 里的 host 是 pre-prod 沙箱,不可照抄。CLAUDE.md 记该 feed 配额 6/天,是全仓最紧的配额之一。
- POST /v3/feeds?feedType=MP_ITEM / MP_MAINTENANCE / MP_WFS_ITEM / OMNI_WFS —— walmart_spec_version_check.md:8 列明这四个 feed type 共用同一份 5.0.20260304-22_45_32-api spec;对应的 spec JSON(451/424/447/45 MB)不在仓库里。
- feedType=mpsetupbymatch(按匹配上架)—— MP_ITEM_MATCH_v4.2.json,version enum 锁 '4.2',processMode enum 只有 'REPLACE'。
- OrderManagement V3.3 的 XSD:ShipConfirmRequestV3.3 / CancelRequestV3.3 / RefundRequestV3.3 / PurchaseOrderV3.3 / CommonComponentsV3.3(walmart_official_specs/xsd_schemas/OrderManagementV3/)。
- InventoryFeed / InventoryHeader / Inventory 的 XSD(xsd_schemas/InventoryManagement/);BulkPriceFeed / ItemPriceResponse / ItemRetireResponse / PartnerFeedResponse / FeedAcknowledgement1 等 11 个 XSD(xsd_schemas/PriceManagement/)。
- openapi/ 下 20 份官方 OpenAPI yml 覆盖 items / prices / inventory / orders / returns / feeds / reports / on-request-reports / insights / promotions / notifications / reviews / rules / settings / fulfillment / lag-time / shipping / recommendations / utilities / authentication —— 这是新仓 api/ 分文件的天然依据。
- 【本模块自身不调用任何沃尔玛 API,因此不存在绕过 walmart_client 直连的问题】唯一的外部网络调用是 pipeline/01_amazon_taxonomy/crawl_amazon_taxonomy.py 爬 amazon.com(退避 2**attempt + random(0,1),命中限流 sleep 30,常规间隔 random(0.2,0.6))和 pipeline/04_risk_compliance/crawl_prohibited_policies.py 爬 marketplacelearn.walmart.com 的 46 个政策页 —— 两者都是爬公开网页,不走 walmart_client,也不需要走店铺出口代理。

### 魔数与踩坑参数

- 类目映射/active/extract_pt_templates.py:103 —— PT 定义在 MP_ITEM spec 里的路径写死为 data['properties']['MPItem']['items']['properties']['Visible']['properties']。这是解析 451MB spec 的唯一入口,新系统必须照抄这个路径。
- 类目映射/active/extract_pt_templates.py:70 + 93 + 124 —— required 是 set(),`list(required)` 无序,再截断前 20 个拼成『必填字段清单』字符串。⇒ 同一份 spec 两次跑出来的必填清单顺序不同,超过 20 个必填时后面的被丢成 '...(+N 个)'。data/ 与 pt_templates/ 两份 summary 内容对不上就是这个 + 版本差异导致的。新系统别用这个字符串,要用结构化字段表。
- 类目映射/active/extract_pt_templates.py:38-40 —— enum 只保留前 20 个,超出只记 enum_total 计数;:58 数组 item_enum 只留前 10;:34 description 截 200 字符;:44 pattern 截 80;:147 描述再截 300;:139 枚举字符串截 200;:149 完整约束 JSON 截 500。⇒ 现有 pt_templates_summary/all_fields 里的枚举值是残缺的,不能拿来做 feed 校验白名单。
- 类目映射/active/extract_pt_templates.py:21 unpack_schema(max_depth=3) —— 嵌套超过 3 层的 allOf 直接不展开;:27-28 遇到 $ref 只回一个 'ref → xxx' 字符串不解引用。
- 类目映射/active/extract_pt_templates.py:16-17 —— SPEC_PATH 写死 /Users/nextderboy/Projects/erpAPI/walmart_official_specs/MPSetup/5.0.20260330-14_47_14-api_MP_ITEM_0_0_en.json,OUTPUT_DIR 写死 /Users/nextderboy/Downloads/pt_templates。产物根本不落在仓库里,要人手拷进 data/。而且这里的版本号 5.0.20260330-14_47_14 与 walmart_spec_version_check.md:5 记录的当前版本 5.0.20260304-22_45_32-api 对不上 —— 文档与脚本已经不同步。
- 类目映射/active/sync_v5.5_to_feishu.py:23 V55 = /Users/nextderboy/Downloads/mapping_detail_v5.5.xlsx(又是 Downloads 不是仓库);:24 TOKEN='Gx9H…wc(token已脱敏,见旧仓库代码)';:26 SHEET='2p5sL6';:27 OLD_ROW_COUNT=15771;:28 OLD_COL_COUNT=20(旧 v4 遗留 20 列,所以每次同步都要清 L-T 列)。
- 类目映射/active/sync_v5.5_to_feishu.py:123 BATCH=300 行/批;:140 每批 sleep 0.12s;:45 lark_put(retries=2) 退避 1.0*(attempt+1) 秒;:135 连续失败 3 次即中止(fail>=3);:154 清列时 CHUNK=500 行,:165 sleep 0.15s。
- 类目映射/active/sync_v5.5_to_feishu.py:34 —— browse_node_id 用 str.replace('.0','') 去 pandas 浮点尾巴,这是字符串级替换,任何含 '.0' 的 id 内部子串都会被误伤;:35『排名』强制 to_numeric 失败填 1。
- 类目映射/active/sync_v5.5_to_feishu.py:40 LAST_COL = chr(ord('A')+NCOL-1) —— 只能算到 Z 列,超过 26 列会算出乱码字符。同样 :147-148 的 EXTRA_COL_START/END 也是。
- 类目映射/pipeline/04_risk_compliance/rebuild_risk_dimensions.py:107-108 —— 维度1 阈值:BIZ-CN 去重 SKU >= 3 才标『中国卖家禁售』(1-2 个只进预警清单)。注意 README:148 写的是旧 v1 阈值 BIZ-CN >= 5,v2 已改成去重 SKU >= 3,两处文档与代码不一致,以代码为准。
- 类目映射/pipeline/04_risk_compliance/rebuild_risk_dimensions.py:134 维度2 品牌锁定:pure_c>=5 且 pure_c/denom>=0.50;:137 维度3 禁售高发:B>=20 且 B/非过期>=0.70;:140 维度4 受限需审批:F>=5 且 F/非过期>=0.30;:143 维度5 知产高危:E>=10 且 E/非过期>=0.50。分母都是『非过期错误数』(排除 A 过期码)。
- 类目映射/pipeline/04_risk_compliance/rebuild_risk_dimensions.py:34-35 ERR_TOKEN='YlA1…dd(token已脱敏,见旧仓库代码)' / ERR_SHEET='aCz4c'(错误统计表)硬编码。
- 类目映射/pipeline/04_risk_compliance/aggregate_bizcn_full.py:19 TOKEN='YlA1…dd(token已脱敏,见旧仓库代码)';:34 日表名正则 r'\d{4}\.\d{1,2}\.\d{1,2}问题商品';:47-48 用 +cells-search 搜 'BIZ-CN';:55-56 只读命中行的 B:E 区间(B=SKU,E=ProductType,K=错误原因)。这些列位是写死的,飞书表插一列就全错。
- 类目映射/pipeline/04_risk_compliance/apply_risk_v2_to_walmart_categories.py:26-27 TOKEN='Gx9H…wc(token已脱敏,见旧仓库代码)' / SHEET='0bdc8b';:164 写飞书 BATCH=80 个 range/批;:50 读取用 +csv-get 按 A{start}:G{end} 分页。
- 类目映射/pipeline/04_risk_compliance/sync_prohibited_to_feishu.py:19-20 TOKEN=Gx9Hs… / SHEET='OJSrkV';:22 BATCH_ROWS=12(注释写明:含长文本列,控制单请求体积)。
- 类目映射/pipeline/04_risk_compliance/apply_cpc_certifications.py:27-28 TOKEN/SHEET 同上;:152 BATCH=100。
- 类目映射/pipeline/04_risk_compliance/apply_risk_to_walmart_categories.py:19-20 TOKEN/SHEET;:125 fail_count>=3 中止;:130 sleep 0.2s;:27 读 /Users/nextderboy/Downloads/PT风险5维度.xlsx。README:284 明确此脚本已被 v2 取代且『依赖的 /tmp 中间文件已失效』。
- 类目映射/tools/walmart_category_lookup.py:113-116 —— 置信度判定魔数:pct>=0.8 且 count>=10 → 高;pct>=0.6 且 count>=5 → 中;count>=3 → 低;否则『参考』。:112 top_n=3。:27-28 两个 /Users/nextderboy/Downloads/ 数据文件路径写死且带日期戳(walmart_all_stores_20260402_1557.xlsx / batch_20260402_081437.csv)。
- 类目映射/pipeline/04_risk_compliance/build_review_queue.py:26 —— 复核优先级阈值 top_pct>=70 高 / >=50 中 / 否则低。
- docs/lark-cli-reference.md:38-44 —— 飞书删列 API 实测是前闭后开 [start,end),官方文档写的闭区间是错的;插列必须用 insert_dimension_range,sheets_batch_update 不支持 insertDimension(返回 90204)。
- docs/lark-cli-reference.md:47 + 文末 —— 单次写入上限 5000 格,超出必须分批;+append 的 range 必须带 sheetId 前缀。
- docs/feishu_cutover_checklist.md:『批量分片三限』ranges<=3000 / cells<=4500 / bytes<=120KB,任一先触发即切块(对照 2026-05-19『包过大』事故)。
- docs/walmart_rate_limits.tsv —— 188 行 5 列(API category/API name/Rate limit/Method/Endpoint),已与新仓 refdata/walmart_rate_limits.tsv 逐字节相同,无需再迁。

### 防重/幂等语义

["BIZ-CN 聚合按 (SKU, ProductType) 二元组去重(aggregate_bizcn_full.py 文件头注释第 6 行明确『按 (SKU, PT) 去重聚合』),跨约 60 个日表扫描。所以 v2 报的是『N 个 SKU』而 v1 报的是『N 次』。","风险维度合并是『并集』语义(rebuild_risk_dimensions.py:『旧窗口 ∪ 新窗口』),并额外写一个『来源』列标 '202604回测' / '202606新窗口' / '202604+202606'。旧窗口的行在写飞书时被显式跳过(apply_risk_v2_to_walmart_categories.py:110-111, 117-118),这就是幂等保障:重复跑不会把旧文案改掉。","飞书 G 列(特殊备注)的写入用 upsert_tag() 做标签级去重合并(apply_risk_v2_to_walmart_categories.py:95-100),同一标签重复跑不会追加第二遍,且 nv != base 时才计入更新 —— 所以整个 apply 脚本对同一份输入是幂等的。","D/E 列写入前先比对当前值(:102 `if cur != val and u.get(col) != val`),值没变就不产生写操作 —— 天然幂等,且减少写请求量。","无跨进程锁、无 pending/done 状态表。同一脚本并发跑两份会互相覆盖飞书单元格,靠『人手动串行跑』保证。","映射表本身无防重语义:sync_v5.5_to_feishu.py 是全量覆盖写(先写表头再分批 PUT A2:K15771),重复执行结果一致但不做增量比对。"]

### 危险操作

- 【本模块不提交任何沃尔玛 feed、不删商品、不清库存】它只产出被危险工作流消费的规范与参考数据。真正的破坏性在下游:daily_retire_orchestrator.py:25 注释『DELETE_ITEM 是永久删除。每日跑前请确保飞书表格已审核』,而它使用的 DELETE_ITEM_VER 常量正来自本模块提供的 spec。⇒ 本模块的间接危险是:PT 风险标注错了会导致本该拦截的商品被上架,或反过来 spec 版本号写错导致整批 feed 被拒。
- sync_prohibited_to_feishu.py —— 对飞书『沃尔玛禁止』sheet(OJSrkV)做全量覆盖写。保护措施:支持 --dry-run,README:290-291 规定必须先 --dry-run 预览再真跑。
- apply_risk_v2_to_walmart_categories.py —— 直接改生产飞书『沃尔玛类目』D/E/G 列(上次跑改了 211 行、含 96 行矛盾修复)。保护措施:①--dry-run 开关(:70, :151-152 『dry-run,不写飞书』直接 return);②每次跑必写变更报告 intermediate/risk_v2_apply_report_<date>.tsv(:146-150,4 列 PT|列|变更|原因),这是唯一的审计痕迹;③写前按值比对跳过无变化项;④G 列 upsert 不覆盖原内容。
- sync_v5.5_to_feishu.py —— 全量覆盖『映射明细』15,770 行,并额外清空 L-T 列(9 列 × 15,771 行)。保护措施:几乎没有 —— 没有 --dry-run、没有备份、没有回读校验,只有连续失败 3 次中止(:135)。这是本模块风险最高的脚本。
- apply_cpc_certifications.py / add_ts_audit_warnings.py —— 同样直接写 0bdc8b。
- auto_listing/risk_gate.py(下游,由本模块数据驱动)—— 813 个禁售 PT + 1,832 个黑名单品牌的上架前拦截,缓存 24h TTL(类目映射/README.md:346, .agent/feature_list.json 第 9-10 条)。拦截判定:准入状态=禁售 或 中国卖家可做=否*;命中则标 fail + 原因、不消耗 UPC、不提交 feed。
- 03_v5_iterations 五个脚本必须严格顺序执行(README:110, 332),跳步会产出错乱的映射表并被同步到飞书。无程序化的顺序校验,纯靠文档约束。

### 事故教训与必须保留的行为

- 【最终映射表就是 类目映射/data/mapping_detail_v5.5.xlsx】15,770 行 × 11 列 xlsx,1.2 MB。它是 40 个 sonnet 子代理初映射 + 5 轮迭代审核(26/17/新PT/权威表对齐/26)的产物,不是算法可复现的。整份表重建成本按 README:216 描述仅 recheck_v54 一步就要 1-2 小时跑 26 个子代理。⇒ 新系统必须把它当『数据』原样导入 catalog,绝不能想着重新生成。
- 置信度分布(类目映射/README.md:33-37):高 12,915(81.9%)/ 中 1,568(10.0%)/ 低 592(3.8%)/ 无 695(4.4%)。『无』的那 695 行 Category/PTG 列是字面量 '-',PT 列是字面量 '无对应Walmart PT' —— 不是 NULL。导入时必须显式处理这三个哨兵值。
- 【PT 池数量三处对不上】飞书『沃尔玛类目』表 7,008 个 PT vs Walmart spec 6,951 vs all_product_types.json 6,942 vs pt_templates/summary 6,942。README:266/333 明确说飞书多 57 个是历史遗留或不同来源,并规定『所有映射的 PT 必须命中飞书表』(v5.4 已对齐)。⇒ 新系统建 catalog 的 PT 主表时要先决定以哪一份为准,否则外键对不上。
- 【BIZ-CN 语义】唯一明确标注『中国卖家专属禁售』的沃尔玛错误码,错误原文见 README:256。它在错误码分类里被关键词匹配误归到 C品牌 列,但实际是独立维度,必须单独处理(README:258, 334)。
- 【v1 → v2 的口径变更 + 矛盾修复】v1 只写 D 列不写 E 列造成 96 行『D=禁售 但 E=中国卖家可做=是』的自相矛盾,v2 在 apply_risk_v2_to_walmart_categories.py:126-133 专门加了矛盾修复逻辑(D=禁售 且 E∈{是, 需评估*} → E='否(Walmart 禁售)')。而且 v1/v2 的『中国卖家禁售』文案不同:v1 是『BIZ-CN触发98次』,v2 是『BIZ-CN触发76个SKU』(证据:intermediate/risk_v2_apply_report_20260611.tsv 第 2 行)。⇒ 新系统解析这列文案要兼容两种格式。
- 【apply_risk_v2 里的『不重写旧标注』规则】:110-111 与 :117-118 —— 来源列 == '202604回测' 的行直接 continue 跳过,保留表上原有文案。这是刻意的、看起来很怪但必须保留的行为:否则会把旧口径的『触发N次』覆盖成新口径,丢失历史。
- 【G 列是追加不是覆盖】apply_risk_v2_to_walmart_categories.py:95-100 用 upsert_tag() 把 🚫标签合并进已有『特殊备注』文本,不清空原内容。同理 apply_risk_to_walmart_categories.py:73 注释『在原内容前追加 🚫前缀』。⇒ 迁移到 PG 时这列是自由文本累积字段,不是枚举。
- 【Price&PromotionCurlExample 里的 host 是 pre-prod】walmart_official_specs/PricePromotion/Price&PromotionJSON/Price&PromotionCurlExample 第 1 行是 https://aurora-api-gateway.pre-prod.walmart.com/v3/feeds?feedType=PRICE_AND_PROMOTION。照抄这个 URL 会打到沙箱。生产必须用 marketplace.walmartapis.com。
- 【Price&Promotion spec 自身有拼写 bug】Price&Promotion.json 的 dependencies 键写成 'promoId'(大 I),而 properties 里的字段名是 'promoid'(小 i);依赖值里的字段名还带前后空格 'promotionPriceStartDateTime ' / ' promotionPriceEndDateTime '。这个 dependencies 块实际是死的,不会生效。
- 【DELETE_ITEM spec 在仓库里是残的】walmart_official_specs/DELETE_ITEM/5.0.20250919-16_45_47-api_DELETE_ITEM.json 的 Item.items.properties.Deletable 指向 $ref '19_09_2025_16_45_47/deletable-mp_api.json',该文件全仓不存在 → schema 无法完整解析,只能拿到外层 header 约束。
- 【DELETE_ITEM 版本号是 enum 硬锁】该 spec 的 ItemFeedHeader.version 是 enum:['5.0.20250919-16_45_47-api'],businessUnit enum:['SAMSCLUB','WALMART_CA','WALMART_US','ASDA_GM'],locale enum:['en']。生产代码里同一个字符串出现在 沃尔玛问题商品清理/daily_cleanup.py:57 与 沃尔玛批量下架/daily_retire_orchestrator.py:70。版本号写错整个 feed 会被拒。
- 【Price&Promotion feed 单批上限 10000】Price&PromotionFeed.json 的 MPItem maxItems=10000 / minItems=1;DELETE_ITEM 的 Item 只有 minItems=1、没有 maxItems(所以批量下架那边是靠字节数 MAX_BYTES_PER_FEED 自己切的)。
- 【Spec_5x_vs_4x_Diff.xlsx 在克隆里是坏的】10,651,167 字节,file 识别为 Excel 2007+ 但 zipfile 报 BadZipFile(中央目录缺失)→ 文件被截断,读不出来。别指望从它拿 4.x→5.x 差异。
- 【MP_ITEM_MATCH_v4.2.json 是另一条上架路径】它的 sellingChannel enum 锁死 'mpsetupbymatch'、version enum 锁死 '4.2'、processMode enum 只有 'REPLACE',required 只有 [locale, sellingChannel, version]。这是『按匹配上架』的 4.2 老 spec,与 5.x MP_ITEM 完全是两套,别混。
- 【根目录与 active/ 的 walmart_compliance_kb.md 已分叉】md5 分别是 82109a93… 和 280a4c80…(31,022B vs 33,995B)。类目映射/README.md:134/328 说 pipeline 用的是 active/ 那份,.agent/feature_list.json 第 3 条也说 2026-06-11 更新的是 active/ 那份。根目录那份是旧的。
- 【飞书超大表不能用 +cells-* 快捷命令】docs/feishu_sheets_registry.md:68-82 —— 在线产品总表 e7834a(148,646 行)上 +cells-get/+cells-set 读写都超时报 50502(服务端硬上限 request_timeout=14000ms),bot 与 user 同样失败。必须走 raw v2 values_batch_get/values_batch_update/values_append。结论是 registry 要给每个 sheet 加 prefer_raw_v2 标志。
- 【飞书 token 有别名】docs/feishu_sheets_registry.md:21-25 —— X4vM…bh(token已脱敏,见旧仓库代码) 与 E1p9…Kh(token已脱敏,见旧仓库代码) 指向同一个物理 workbook(wiki-node-token vs obj-token 两种 locator)。registry 去重时不能按 token 字符串去重。
- 【pipeline 里大量绝对路径指向 macOS 的 ~/Downloads】build_amazon_taxonomy_final.py:24、extract_amazon_taxonomy.py:25-26、build_review_queue.py:13/111、add_ts_audit_warnings.py:35-37、build_prohibited_sheet.py:13/231/238-239、integrate_templates_to_feishu.py:12/22、tools/walmart_category_lookup.py:27-28、active/extract_pt_templates.py:16-17、active/sync_v5.5_to_feishu.py:23。这些脚本在任何非该开发者机器上都跑不起来。
- 【类目映射目录整个不在 git 里】README.md:337 明确写『当前目录未纳入 git 追踪(工作目录),所有数据变更只能通过文件备份 + 飞书版本来回溯』。虽然克隆里能看到文件,但这句话意味着历史版本无法用 git 找回,archive_data/ 就是全部的回滚保险。
- 【2026-06-11 有过 +append 并发丢行事故】docs/feishu_cutover_checklist.md 验收清单里点名『+append 并发安全:问题清理 / UPC append 无丢行(对照 2026-06-11 事故)』。
- 【nan PT bug】类目映射/.agent/feature_list.json 第 8 条:沃尔玛问题商品清理/feishu_sync.py 里空 ProductType 曾被统计成字面量字符串 'nan',已修为 '(未知)'。新系统统计 PT 维度时同样要防 pandas NaN 转字符串。

### 切换时必须迁移的状态

- /workspace/erpapi/类目映射/data/mapping_detail_v5.5.xlsx —— 15,770 行 × 11 列,新系统 catalog schema 的核心导入数据(建议表名 catalog.amazon_to_walmart_pt)。字段:walmart_category / walmart_ptg / walmart_product_type / amazon_leaf / amazon_path / browse_node_id(text,别转数字)/ rank / confidence(高|中|低|无)/ match_method / note / source_batch。695 行的 PT='无对应Walmart PT' 与 Category/PTG='-' 要归一成 NULL 并单独打标。
- /workspace/erpapi/walmart_specs/taxonomy_v5.json —— 24 Category → PTG → PT 三级树 + department(departmentName/departmentNumber)。这是唯一带部门号的来源,建 catalog.walmart_taxonomy 用它。
- /workspace/erpapi/walmart_specs/all_product_types.json —— 6,942 个 PT 的扁平清单 + category_map,可作 PT 主表的校验基准。
- /workspace/erpapi/pt_templates/pt_templates_summary_sorted.xlsx —— 6,942 行 × 7 列,唯一带 Category/PTG 归属的 PT 模板表,建 catalog.pt_template_summary 用它(不要用没有类目列的那两份)。
- /workspace/erpapi/类目映射/data/PT风险5维度_v2.xlsx —— 6 个 sheet 的 PT 风险标注,建 catalog.pt_risk(pt, dimension 1-5 或 warn, metric_value, ratio_pct, source_window)。同时要把飞书 0bdc8b 的 D/E/G 三列现状(7,008 行)拉一份快照进库,因为『不重写 202604回测 旧标注』意味着飞书上那部分文案在本地 xlsx 里找不到。
- 飞书 0bdc8b『沃尔玛类目』全表 7,008 行 —— 这是事实上的权威 PT 池(比 spec 多 57 个)+ 合规富化列(销售资质要求/所需认证/IP侵权风险/合规说明/特殊备注),这些内容只存在于飞书,本地无完整副本。切换前必须整表导出。
- 飞书 OJSrkV『沃尔玛禁止』46 行政策清单 —— 本地源头是 walmart_prohibited_detailed.md,但飞书上是解析后的结构化版本,建议两者都搬。
- /workspace/erpapi/类目映射/active/walmart_compliance_kb.md(33,995B,新版)与 /workspace/erpapi/walmart_prohibited_detailed.md(62,156B)—— 合规知识库,是 risk_gate 与 compliance_enrichment 的判定依据。根目录的 walmart_compliance_kb.md 是旧版,别搬。
- /workspace/erpapi/walmart_official_specs/ 全部 18 MB(DELETE_ITEM json + PricePromotion 5 文件 + MP_ITEM_MATCH_v4.2.json + 20 份 openapi yml + 22 个 xsd + PT_Mapping.xlsx + MPSetup_FeedDiff.xlsx)—— 除 Spec_5x_vs_4x_Diff.xlsx 已损坏外全部有效。
- /workspace/erpapi/参考资料/walmart_ops_knowledge_v4.docx.md —— 运营知识手册(店铺矩阵/选品/定价/履约 5 模块),含『单SKU单店』『同品牌单店』『关联风险防控』等业务铁律,是新系统写业务规则前必读的背景文档。
- /workspace/erpapi/docs/{feishu_sheets_registry.md, feishu_migration_plan.md, feishu_cutover_checklist.md, lark-cli-reference.md} —— 飞书接线的事实底表与踩坑手册,直接对应新仓 registry/resources.py 的内容来源。
- 【不必搬】docs/walmart_rate_limits.tsv 已与 /home/user/WalmartAPI-Contral/refdata/walmart_rate_limits.tsv 逐字节相同。
- 【搬不了】data/pt_templates_full.json(292 MB)与 pt_templates_all_fields.xlsx(384,826 行)被 gitignore,克隆里没有,必须从原开发机取或用新 spec 重新生成;walmart_official_specs/MPSetup 等四个 spec 大目录(合计 ~1.4 GB)同理不在仓库。

### 迁移建议

照搬 vs 重做的分界很清楚:**数据照搬,脚本全部重做**。

**必须原样导入 catalog(禁止重新生成)**:mapping_detail_v5.5.xlsx 的 15,770 行是 100+ 个 LLM 子代理跑了 6 轮(初映射 40 批 + v5.1 复检 26 + v5.2 精检 17 + v5.3 新PT + v5.4 权威表对齐 + v5.5 二次审核 26 批 3,057 条)沉淀下来的人工资产,README:216 说光重建 recheck_v54 就要 1-2 小时子代理时间,整体不可复现。建议 `catalog.amazon_to_walmart_pt` 表 + `catalog.walmart_taxonomy`(从 taxonomy_v5.json,带 department 号)+ `catalog.pt_template_summary`(从 pt_templates_summary_sorted.xlsx)+ `catalog.pt_risk`(从 PT风险5维度_v2.xlsx)。同时必须把飞书 0bdc8b 全表 7,008 行导一份进来,因为合规富化列(销售资质/必需认证/IP风险/合规说明)与 202604 旧口径的风险文案只存在于飞书。

**PT 池数量必须先裁决**:飞书 7,008 / spec 6,951 / all_product_types.json 6,942,三者不一致且旧系统的规则是"映射 PT 必须命中飞书表"。建议新系统以 taxonomy_v5.json 为 PT 主表(它有 department 号且可从官方 spec 重生),把飞书多出的 57 个 PT 单独打 `legacy_only` 标记,并在导入 mapping 表时做外键校验,把落空的行挑出来人工看。

**registry 收编项**:token `Gx9H…wc(token已脱敏,见旧仓库代码)`(CATEGORY)/ `YlA1…dd(token已脱敏,见旧仓库代码)`(ERROR_PRODUCTS),sheet_id `0bdc8b`/`2p5sL6`/`OJSrkV`/`3b5Gpy`/`2NgLNm`/`aCz4c`,以及 0bdc8b 的列语义常量(D=准入状态 / E=中国卖家可做 / G=特殊备注)。注意 feishu_sheets_registry.md:21-25 的 token 别名问题:两个 token 指向同一 workbook,registry 去重不能按字符串。还要给每个 sheet 带 `prefer_raw_v2` 标志(e7834a=True)。

**必须重做的部分**:
1. 所有 `/Users/nextderboy/...` 硬编码路径(至少 9 个文件)→ registry/paths.py 的 DATA_ROOT。extract_pt_templates.py 甚至把产物写到 ~/Downloads 再靠人拷进仓库,这个流程要改成直接落 DATA_ROOT。
2. `subprocess.run(['lark-cli', ...])` + 正则搜 `"code":0` 判成功 → 换成 api/feishu.py 的正经 JSON 解析 + 错误码分类(50502 降级 raw-v2、90235/90217 重试、99991663 重取 token)。
3. `chr(ord('A')+n)` 算列号(只能到 Z)、`str.replace('.0','')` 去浮点尾巴 → 用正规的列号转换与 dtype=str 读表。
4. PT 模板抽取要重写:现有版本把 required 从 set 里无序取出、截前 20 个、枚举只留 20 个、$ref 不解引用、嵌套只展 3 层 —— 产出的模板不能用来做 feed 校验。新系统应该把 MP_ITEM spec 完整展开成 `catalog.pt_field`(pt, field, required, type, enum_values[], pattern, min/max)结构化表,枚举一个不能丢,因为 walmart_spec_version_check.md 明确说 Walmart 按当前 Recommended spec 的枚举白名单做严格校验。

**对应新 workflow 的建议**:
- `workflows/refdata_import_category_mapping.py` —— 一次性/低频,把 xlsx 导进 catalog,幂等 upsert,dangerous=False。
- `workflows/refdata_extract_pt_templates.py` —— 输入新 spec JSON,输出 catalog.pt_field / pt_template_summary。dangerous=False(只写自己库)。
- `workflows/refdata_refresh_pt_risk.py` —— 合并旧 aggregate_bizcn_full + rebuild_risk_dimensions + apply_risk_v2 三步。阈值(BIZ-CN 去重 SKU≥3;品牌锁定 pure_c≥5 且 ≥50%;禁售高发 B≥20 且 ≥70%;受限需审批 F≥5 且 ≥30%;知产高危 E≥10 且 ≥50%)全部提成显式配置常量并写进 docs,别再散在代码里。**必须标 dangerous=True**(它写生产飞书 211 行),保留 --dry-run 语义 + 变更报告 TSV。
- `workflows/refdata_sync_prohibited.py` —— 全量覆盖 OJSrkV,**dangerous=True**。
- 映射表同步到飞书这件事,建议改成"库是唯一真源,飞书是投影":先写 PG 再单向刷飞书,而不是像现在这样飞书 0bdc8b 既是权威 PT 池又是被写目标(现在的循环依赖是 apply_risk 读它又写它,极易出现 D/E 矛盾那种自相打架的状态)。

**风险提示**:①迁移期间旧的 apply_risk_v2 与新 workflow 严禁并跑,它们写同一批飞书单元格且都不是原子的;②DELETE_ITEM 的 version 字符串 `5.0.20250919-16_45_47-api` 是 enum 硬锁,新系统的 api/feeds.py 要把它做成 registry 常量而不是散落在两个 workflow 里(旧仓就是 daily_cleanup.py:57 和 daily_retire_orchestrator.py:70 各写一份);③Price&Promotion 的官方 curl 样例指向 pre-prod 沙箱,抄 spec 时别把 host 一起抄了;④本地 spec 版本(脚本里 5.0.20260330,文档里 5.0.20260304)已落后官方且互相不一致,新系统第一件事应该是拉一份当前 Recommended spec 重建 PT 字段表,并把版本号作为 catalog 里的一等字段记录下来。

### 待确认问题

- mapping_detail_v5.5.xlsx 的『匹配方式』列除了 'a2w_agent' 还有哪些取值?『来源批次』列(如 'Office_Products')的完整取值域是什么?这两列要建枚举的话需要全表扫一遍。
- 飞书 0bdc8b 的 F 列是什么?所有脚本都读 A:G 但只写 D/E/G,F 从未被使用,克隆里也没有该表的本地副本可查表头。
- data/pt_templates_summary.xlsx(6,951 PT)与 pt_templates/pt_templates_summary.xlsx(6,942 PT)分别对应哪个 spec 版本?两者必填字段清单内容不同,新系统该以哪份为基线导入?建议直接用新 spec 重生成而不是二选一。
- walmart_official_specs/Spec_5x_vs_4x_Diff.xlsx(10.6 MB)在克隆里是截断的坏 zip,原始文件是否还在?4.x→5.x 的字段级差异是否还有别处记录?
- pt_templates_full.json(292 MB)与 pt_templates_all_fields.xlsx(384,826 行)被 gitignore,MPSetup 等四个 spec 目录(~1.4 GB)也不在仓库。这些文件目前在哪台机器上?新系统重建 PT 字段表是打算从官方重新下载 spec,还是要先把这些文件捞回来?
- README:53 说『沃尔玛禁止』38 行,变更记录:346 和 .agent/feature_list.json 说 2026-06-11 已重写为 46 行 —— 飞书上现在的实际行数是多少?
- auto_listing/risk_gate.py 的 813 个禁售 PT 与 1,832 个品牌黑名单是从飞书实时算出来的还是有落地缓存文件?24h TTL 缓存写在哪个路径?(risk_gate 不在本次读取范围内,需另一个模块确认)
- apply_risk_v2 跳过『202604回测』来源行的规则意味着飞书上有一批文案本地无副本。切换到新系统时,是保留这批旧文案原样搬过去,还是统一按 v2 口径(去重SKU数)重算一遍全量?后者会丢失历史但口径统一。
- BIZ-CN 聚合依赖 ERROR_PRODUCTS workbook 里约 70 个 'YYYY.M.D问题商品' 日表。新系统如果把错误记录落 PG(orders/ops schema),这个聚合是否应该改成直接查库?那飞书日表还需要继续维护吗?


<a id="critic"></a>
## 完整性审查结论(critic)

## A. 遗漏清单 —— 14 份摸底(我可见 11 份)之外、迁移必须知道的文件/目录

### A1. `定时任务skill/` —— 真正的调度层,plan.md 完全没提
plan.md 只写"launchd 调 `python cli.py`",但实际生产的调度是**两套并行**:

| 调度方式 | 覆盖任务 | 证据 |
|---|---|---|
| launchd(5 个 plist) | auto_listing 全家:`morning` 06:00 / `reconcile_due` 每小时:15 / `store_status_hourly` 每小时:05 / `health_report` 8·12·16·20 / `retire_locked` 23:30 | `auto_listing/launchd/*.plist`(ProgramArguments 全是 `python3 -m auto_listing.scheduler <子命令>`) |
| launchd(1 个) | 订单审核 每小时:15 → `deploy/run_hourly.sh` → `订单同步.py` | `沃尔玛订单审核/deploy/com.user.walmart.order_audit_hourly.plist`、`run_hourly.sh:16` |
| **AI 跑的 SKILL.md**(15 个) | KPI 日报 08:00 / KPI 下午 14:00 / 售后 08:02 / 维护 12:00 / 下架 15:00 / 清理 每 6h / 订单同步 13:30 / dedup-sync 14:02 / erp-online-products-track 07:30 + 6 个跨仓库任务 | `定时任务skill/*/SKILL.md` frontmatter 的 `description: …(建议 HH:MM)` |

要点:
- **SKILL.md 不是文档,是可执行调度单元**,每个都要求 AI"重试→跳过→继续",并统一调 `定时任务skill/notify.py start|done` 发飞书简报(标记文件 `tempfile.gettempdir()/erp_skill_notify/<task>.json`,`notify.py:66`)。新系统 cli.py 的"成败通知"要替代的就是它。
- **6 个 SKILL 指向 erpAPI 之外的仓库/系统**,迁移范围之外但同机竞争资源:`tro-daily-scrape`(`/Users/nextderboy/Projects/tro-scraper-matrix`)、`trademark-daily-update`(USPTO)、`daily-tro-pipeline`、`weekly-brand-refresh`、`sync-blacklist-brands-daily`(同步飞书黑名单到 PG **并重启 worker**)、`erp-online-products-track`。
- 双重调度已确证:订单同步既在 launchd 每小时:15,又在 SKILL 13:30(plan.md 已知,证据在上表)。

### A2. `类目映射/` —— 已上线模块,且是 erp-core 的活依赖
- 产物 `data/mapping_detail_v5.5.xlsx`(15,770 行)由 `active/sync_v5.5_to_feishu.py:22-25` 同步到 `Gx9H…wc(token已脱敏,见旧仓库代码) / 2p5sL6`。
- `active/extract_pt_templates.py` **不是归档脚本**:`erp-core/backend/app/services/audit/sync/sync_pt_specs.py:174` 直接抛错要求"需先运行 erpAPI/extract_pt_templates.py"。plan.md 写"pipeline 代码留在旧仓库归档"会切断这条链。
- `类目映射/.git-archive` 是内嵌 git 仓(`.gitignore` 注明"内含 7 个未推送 commit"),归档旧仓库前必须处理。

### A3. `erp-core/` —— 一个仍在跑的、独立的沃尔玛写入方
plan.md 说"不属于本次迁移,维持现状",但它不是被动的:
- `erp-core/backend/app/tasks/celery_app.py:23-108` 的 beat 计划含 **写操作**:`poll_pending_feeds` 30s、`full_listings_sync` 6h、`sync_inventory_from_source` 6h(往沃尔玛推库存)、`full_orders_sync` 15min、`rescrape_stale_listings` 4h。
- 它有自己的 walmart_client 分叉(`erp-core/backend/app/services/walmart_client.py:281-300` 自带 `POST /v3/feeds` + rate_limiter),凭证来自 PG `stores` 表(`erp-core/scripts/bootstrap_stores.py:1` 从 `店铺API.xlsx` 灌入)。
- **但根 `README.md:3` 说 erp-core 已于 2026-06 整体迁出到 `~/Projects/erp服务/erp-core`** —— 本仓库这份是快照。也就是说:真正在跑的第三方写入者不在本次审计的任何范围内。新 `maintenance` / `listing` 上线前必须确认它的库存/feed 任务是否还在跑,否则是三方并跑。
- 另有硬依赖:`erp-core/backend/app/api/v1/orders.py:89` 只读硬编码 `/Users/nextderboy/Projects/erpAPI/walmart_settlement.db`;`scripts/etl/etl_walmart_categories.py:17` 读 `walmart_specs/all_product_types.json`。旧仓库转只读归档 ≠ 可以移动路径。

### A4. 单文件/小目录级遗漏
| 位置 | 为什么必须知道 |
|---|---|
| `店铺API.xlsx` **Sheet2**(54 行) | 第二张代理清单,列为 `代理类型/IP/端口/账号/密码/设备名称`。含 17 个 Sheet1 里没有的店铺(如 `A132高佳棋`、`D044邱昌建`),Sheet1 也有 20 个不在 Sheet2(含 `谭总1~4`)。两表已漂移,迁 registry 时必须明确以 Sheet1 为准 |
| `沃尔玛UPC生成器/main.py` + `ean13.py` | 与 `cli.py` 流水线**是两套**:main.py 是 tkinter GUI(`main.py:15` "EAN13位生成器"),生成 **13 位 EAN**,逆向自 `生成器.exe`;cli.py 流水线生成 **12 位 UPC-A**。仓库最后一个 commit 就是它 |
| `recover_lark_writeback.py:1-18` | 记录了一次真实事故:2026-05-07 15:04,**本地代理 127.0.0.1:7897** 在 DELETE_ITEM feed 已提交后拒绝连接,导致 14,610 个单元格写回失败。这是"状态只存在飞书"的代价实例,也说明飞书调用走本机 HTTP 代理(故各处有 `LARK_CLI_NO_PROXY=1` / `no_proxy=True`,见 `auto_listing/risk_gate.py:44-49`)。新 api/feishu.py 必须显式绕开本机代理 |
| `沃尔玛问题商品清理/cache/*.json` | **已提交进 git**,共 5.7MB 生产状态:`submitted_skus.json`(64 个店铺键,多于当前 48 家)、`revived_skus.json`(反补计数含 feed_ids)、`seen_sku_categories.json`(4.2MB)、`brand_cache.json`。可直接拿来定 `ops.dedupe` 的结构,但内容截止 ~2026-05,权威副本仍在生产 Mac |
| `data/frontend_scrape/latest.json` | 影刀产物,`scraped_at=2026-07-04T08:07:05+08:00`,仅 17 个店铺 |
| `.mcp.json` | 影刀 RPA 的接入方式是 **MCP server**(`yingdao-mcp-server`,`SHADOWBOT_PATH=/Applications/影刀.app`,`USER_FOLDER=…/Shadowbot/users/899125848592719874`)。日报模块只知道 UUID,不知道它是这么调起来的 |
| `.claude/settings.json` | `{"defaultMode":"bypassPermissions"}` —— 旧仓库 AI 全程免确认执行。新仓库若沿用,危险工作流的 `--execute` 就是唯一闸门 |
| `docs/feishu_cutover_checklist.md:5-13` | **2026-07-04 已 big-bang 把全仓 38 处 `LARK_IO_SHIM` 门翻成默认开**,且明说"生产运行时 parity 对账未做,是当前唯一残余风险"。即:所有飞书路径在近期才切到 lark_io,可能尚未跑满一轮 |
| `.gitignore:57-79` | 明确列出**不在克隆里**的大件:`walmart_official_specs/{MPSetup,MPMaintenance,WFSSetup,WFSConvert}/*.json`(每个 400MB+)、`MPSetup_by_pt/`(458MB)、`pt_templates_full.json`(292MB)、`*.db`、`auto_listing/state/`。plan.md 的"spec 文件先入 `<DATA_ROOT>/specs/<版本>/`"必须从生产 Mac 取,克隆里没有 |
| `tools/sync_online_products.py` | plan.md 第 9 条 `catalog_sync` 的源文件,但同时 plan.md 又写"tools/ 的 10 个救场脚本不迁移" —— 措辞会误导执行 AI 把它一起砍掉 |
| `沃尔玛订单审核/审核服务.py:1-9` | 自己声明"已退出主流程(2026-06-20),保留为手动重审调试工具",部署在 DMIT systemd `0.0.0.0:8901`,且回填逻辑是旧实现。order_audit 的同名 open_question 到此可关闭:它已不在主链路,但服务可能仍监听 |
| `walmart_client.py.bak`、`tests/test_lark_io.py`、`参考资料/`、`pt_templates/*.xlsx`、`walmart_compliance_kb.md`(与 `类目映射/active/` 下同名文件重复)、`lingxing-erp-research.md`、`walmart_spec_version_check.md` | 均无代码引用,可直接归档 |

### A5. git 历史层面的事实
本克隆只有 **1 个 commit**(`d5237fb`,2026-07-10,squash)。后果:(1) 无法用 git 判断任何文件的活跃度/最后修改时间,执行 AI 别去 `git log` 求证;(2) `店铺API.xlsx` 在**当前 HEAD 就是明文**(48 行 ClientSecret + socks5 账号密码),不需要翻历史即已泄露。

---

## B. 文档声称 vs 实际核实

| 文档声称 | 核实结论 |
|---|---|
| legacy_reference"**8 处绕过共享客户端的直连**" | ✅ **精确成立,正好 8 处生产代码 + 1 处测试**:`fetch_walmart_settlement.py:390,402,420`、`沃尔玛问题商品清理/daily_cleanup.py:386,414`、`沃尔玛问题商品清理/relisting.py:134`、`沃尔玛批量下架/daily_retire_orchestrator.py:619`、`沃尔玛批量下架/retire_walmart_items.py:246`、`沃尔玛订单审核/沃尔玛异步.py:54`、`沃尔玛店铺日报/fetch_walmart_problem_orders.py:151`、`auto_listing/check_feed.py:41`(+测试 `沃尔玛问题商品清理/scripts/test_relisting_live.py:67`)。**重要澄清:8 处全部老实传了 `proxy=store["proxy"]`,没有一处裸连**。丢的是 401 自愈、429/`X-Next-Replenishment-Time` 退避、连接池复用,不是店铺关联安全 |
| "遍历店铺并发执行样板 **6 份**" | ⚠️ **实际 ≥12 份**。跨店 ThreadPoolExecutor:`售后订单同步/fetch_walmart_returns.py:237`(8)、`fetch_walmart_settlement.py:549`(8)、`沃尔玛问题商品清理/daily_cleanup.py:355`(8)、`沃尔玛店铺日报/fetch_walmart_performance.py:201,952`、`沃尔玛店铺日报/fetch_walmart_problem_orders.py:417,723`、`沃尔玛批量下架/daily_retire_orchestrator.py:957,1057`、`沃尔玛商品维护/{sync_lark.py:127,submit.py:265,poll_yesterday.py:162}`、`auto_listing/{main.py:1008,1467,sync_status_track.py:243,709,reconcile.py:361,auto_reconcile.py:131,upc_audit.py:318}`、`沃尔玛UPC生成器/walmart_check.py:150`、`tools/sync_online_products.py:548,825`。另有一份 async 版 `沃尔玛订单审核/沃尔玛异步.py:121`(Semaphore) |
| "飞书'查元信息→扩行→分批写'复制 **3 份**" | ⚠️ **实际 5 份**:`沃尔玛订单审核/飞书表.py:98-139`、`沃尔玛店铺日报/fetch_walmart_problem_orders.py:485-503`、`沃尔玛问题商品清理/feishu_sync.py:156-159`、`售后订单同步/fetch_walmart_returns.py:180`、`类目映射/active/sync_v5.5_to_feishu.py:68-93` |
| "`DELETE_ITEM_VER` 被抄 **3 份**" | ✅ 精确:`沃尔玛问题商品清理/daily_cleanup.py:57`、`沃尔玛批量下架/daily_retire_orchestrator.py:70`、`retire_walmart_items.py:61`,取值同为 `5.0.20250919-16_45_47-api`。**但版本常量的分裂比文档说的更严重**:MP_ITEM/MP_MAINTENANCE spec 版本 `5.0.20260304-22_45_32-api` 又抄了 3 份(`auto_listing/config.py:78,88`、`沃尔玛商品维护/walmart_maintenance_common.py:86`、`沃尔玛问题商品清理/relisting.py:45`);**价格 feed 版本两处不一致**:`沃尔玛商品维护/walmart_maintenance_common.py:87 = "1.7"` vs `auto_listing/walmart_price_inventory.py:32 = "2.0.20240126-12_25_52-api"`;`match_listing/config.py:29 MATCH_SPEC_VERSION = "4.2"` |
| "`店铺API.xlsx` … 57 家店铺"(多个 README) | ❌ **当前实际 48 家**。Sheet1 共 59 数据行,`load_stores` 口径(有 ClientId 且代理类型/IP/端口均非 0)命中 48 行,全部 `socks5`;**48 个 ip:port 两两不重复**(每店固定出口成立),但代理登录账号大量复用(`C008` 一个账号覆盖 21 店)。日报 README 的"50-63 家"、订单审核/售后的"57 家"均已过时。`walmart_client.py:143-206` 的过滤是**静默跳过**,少店不报错 |
| "旧凭证已进过 git 历史" | ✅ 且更糟:当前 HEAD 就含明文。`店铺API.xlsx` Sheet1 可直接解出 ClientSecret 与 socks5 密码 |
| "PRICE_AND_PROMOTION 6/天 / PUT /v3/price 100/小时 / GET /v3/items 带 query 60/分钟 / feeds 共享 5000/分钟 / WFS Inventory Reconciliation 1/小时" | ✅ 全部与 `refdata/walmart_rate_limits.tsv` 第 125/126/89/19-22/41 行一致。且新仓库 `refdata/walmart_rate_limits.tsv` 与旧 `docs/walmart_rate_limits.tsv` **逐字节相同**,无需再迁 |
| plan.md"api/_client.py 移植旧 walmart_client.py **498 行**" | ✅ 实际 497 行,一致 |
| plan.md"飞书:多维表格为主" | ⚠️ 旧仓库**零 bitable 代码**(全仓 grep 不到 `/bitable/`),`lark_io` 只做 sheets v2。另注意 `docs/feishu_sheets_registry.md:14` 显示 **`X4vM…bh(token已脱敏,见旧仓库代码)` 下已有 `40383c:店铺API` sheet** —— Phase 0 的"店铺凭证多维表格"可能不必新建,先去看这张表 |
| legacy_reference 飞书瞬时错误码 90235/90217/50502 | ✅ 与 lark_io 一致;补充:`1204` 在 `沃尔玛批量下架/daily_retire_orchestrator.py:125` 被与 50502 并列当超时处理,但**没进** lark_io 的 TRANSIENT_CODES,新实现要么实测纳入要么显式排除 |
| legacy_reference 状态迁移清单 | 基本齐全,**漏了 3 项**:① `沃尔玛问题商品清理` 的本机 PG(`walmart_cleanup`,peer 免密)之外还有飞书 `QNIp…Bb(token已脱敏,见旧仓库代码)/8280e8` 黑名单 ASIN 表;② `auto_listing/state/risk_gate_cache.json`(TTL 24h,`risk_gate.py:31-32`);③ 影刀 `latest.json` |

---

## C. 各模块 open_questions 的代码级查证结论

**已定论(可直接结案)**

1. **daily_report:`walmart-kpi-afternoon` 是否从未跑成功 → 确认必然失败。** `fetch_walmart_performance.py:1173-1185` 只识别 `--no-yingdao`,其余参数原样当店铺名;`walmart_client.py:198` 的过滤是精确匹配 `name not in filter_names`,`'--skip-summary'` 匹配不到任何店 → `load_stores()` 返回空 → 打印"未找到店铺" + `sys.exit(1)`。而 `定时任务skill/walmart-kpi-afternoon/SKILL.md:54` 的命令正是 `… --no-yingdao --skip-summary`。**结论:下午补刷从未产生过写入,`merge_preserve_afternoon` 的 A-H 保护逻辑在生产从未生效,新系统不必兼容。**

2. **returns_sync:`GET /v3/returns` 不支持时间过滤 → 官方规范打脸。** `walmart_official_specs/openapi/walmart-marketplace-returns-openapi-original.yml` 的 `GET /v3/returns` 明确支持 `returnCreationStartDate/EndDate`、`returnLastModifiedStartDate/EndDate`、`limit`(≤200)。**`售后订单同步/README.md:68`"沃尔玛 returns 端点本身不支持时间过滤,所以只能全量"是错的**,旧代码只是没传(`fetch_walmart_returns.py:49` 只发 `limit=200&replacementInfo=true`)。新 returns_sync 可以做真增量。配额 50/min(`refdata` 第 144 行),旧代码翻页间隔 `time.sleep(1.3)`(`fetch_walmart_returns.py:72`)。

3. **returns_sync:行主键用什么 → 有稳定标识。** 同一份 OpenAPI 的 `ReturnOrderLine` schema 第一个字段就是 `returnOrderLineNumber`。新库主键取 `(returnOrderId, returnOrderLineNumber)`。

4. **product_query:Item Search 上限 20 还是 40、能否分页 → 无分页。** 官方 `walmart-marketplace-items-openapi-original.yml` 的 `GET /v3/items/walmart/search` 仅接受 `query/upc/gtin` 三个业务参数,**无 offset/limit/cursor**;代码里的 20(`query_product_detail.py:427`)只是"疑似被截断"的告警阈值,不是参数。`POST /v3/items/catalog/search` 才有 `page/limit/nextCursor`。另注:旧代码用的 `asin=` 与 `responseFormat=SPEC` 均不在官方规范里,属未公开参数,新实现要保留但标注风险。

5. **daily_retire:`--dry-run` 是设计如此还是 bug → 是真实的地雷。** `daily_retire_orchestrator.py` 的 `--dry-run` 只在两处生效:`:1023-1024`(audit/repair 分支不写回)与 `:1084-1085`(主流程"跳过写回"),而提交 DELETE_ITEM 的 `process_store` 并发块在 `:1057-1080` **无条件执行**。即 dry-run = **照删不误 + 不记 feedid**,是所有组合里最坏的一种(删了没痕迹,下轮还会再选同一批行)。新系统必须反过来。

6. **daily_retire / maintenance:DELETE_ITEM、MP_MAINTENANCE、inventory feed 的配额出处 → 表里确实没有专条。** `refdata/walmart_rate_limits.tsv` 里 feedType 级只有 `PRICE_AND_PROMOTION 6/day`(:125)、`WALMART_FUNDED_INCENTIVES_ENROLLMENT 6/hour`(:131)、`INCENTIVE_ENROLLMENT 6/hour`(:133),其余全部落在通用条 `Update bulk prices (Legacy) 10/hour POST /v3/feeds`(:136)。旧代码的"10/小时/店"确实是从这条推的。**表的结构本身是证据:既然特例 feedType 单列一行,通用行就是其余 feedType 共享的桶** —— 支持"DELETE_ITEM 与 MP_ITEM 抢同一个每店 10/hour"的说法,新系统需要跨 workflow 的每店 feed 令牌桶。顺带:`GET /v3/feeds/{feedId}/errorReport` 是 60/hour(:23),比 feeds 本身紧得多;`DELETE /v3/items/{sku}` 有 900/min(:98),是 RETIRE_ITEM feed 之外的单品下架通道。

7. **walmart_client:rate_limiter 与 refdata 是否一致 → 两处不一致,方向相反。** `auto_listing/rate_limiter.py:129` 配 `POST /v3/items/spec = 3/min`,官方表(:82)是 **10/min** —— 代码更保守,安全;`PUT /v3/inventory 200/60`(:124)与官方(:78)一致。其余条目(feeds 10/hour、6/day)见上条。新 api 层以 refdata 为准 + 保留 spec 的保守值即可。

8. **walmart_client:401 自愈会不会重发 POST → 会,但设计者已知并做了防护。** `walmart_client.py:374-388` 的 401 分支 `continue` 确实不分 method。但 `:485-489` 明确注释 `safe_post` 默认 `max_retries=0`,理由就是"POST 非幂等,自动重试可能导致重复提交(feed 重复)"。**真正的风险点不在 401,在调用方**:`沃尔玛商品维护/walmart_maintenance_common.py:374` 的 `post_feed(..., max_retries=3)` **默认给 feed 提交开了 429/5xx/网络异常自动重试**——网络超时发生在沃尔玛已接收之后就会重复提交一份价格/库存/标题 feed,还白烧 10/hour 配额。对照 `auto_listing/feed_submit.py:262` 是不传 max_retries(=0)+ 自己用 pending_feed_tracker 反查。**这是一条应当报给业务方的既存缺陷,也解释了"防重状态先落库再调接口"这条铁律的由来。**

9. **maintenance:stockzero 是 13 家还是 14 家 → 名单差的那家已不是有效店铺。** `A132高佳棋` 只出现在 `店铺API.xlsx` **Sheet2**(代理清单),不在 Sheet1 → `load_stores` 根本不返回它 → 即便飞书配置表里写着它也无法执行。README 的 14 家与设计方案的 13 家因此不冲突。

10. **lark_io:飞书表 token 是否都在 registry → 有一张漏网。** 全仓 14 个不同 workbook token,`lark_io/sheets_registry.py` 只登记 10 个(9 物理 + 1 alias)。缺 `QNIp…Bb(token已脱敏,见旧仓库代码)`(Amazon 选品黑名单 / sheet `8280e8`),由 `沃尔玛问题商品清理/blacklist_sync.py:46-47` 写入。另 3 个(`UKdZwr…`/`UhZJw3…`/`JeSEw8…`)是 erp-core 独有的 wiki token。

11. **daily_cleanup 的黑名单 ASIN"供 auto_listing 选品拦截" → 本仓库找不到读者。** `8280e8` 全仓只有写入方(`blacklist_sync.py`)与两份文档(`沃尔玛问题商品清理/README.md:97,241`)。auto_listing 的品牌拦截读的是另一张表(`risk_gate.py:36-37`,`YlA1sz…/WvPTz2`)。**这条链路要么断了,要么读者在 erp-core/外部仓库**,迁移前需向业务确认,否则会迁一张没人用的表。

**仍需去生产 Mac / 实测才能定的(建议直接列进切换 checklist)**
- `upc_history.db` / `maintenance.db` / `walmart_settlement.db` / `auto_listing/state/` 的真实体量与在途记录 —— `.gitignore:71-74` 保证它们**一定不在任何克隆里**。
- `recon_details` 的 `INSERT OR IGNORE` 静默丢行数、Payment Statement 能否回溯账期。
- 飞书 bot 对 `LISTING_TOKEN`/`NxlS1J` 的**写**探针(读探针已全绿,`docs/feishu_sheets_registry.md:4`,但写未验证)。
- `+batch-update --operations -` 是否真支持 stdin(`lark_io/_core.py:1219` 已这么用)。
- 2026-05-25 那 6,665 个受限前缀 UPC 在飞书池 B 列是否已标记。
- erp-core(在 `~/Projects/erp服务/erp-core`)的 celery beat 当前是否在跑 —— 这是**唯一可能在新系统之外仍在写沃尔玛库存/feed 的进程**,应在 Phase 0 就查清,而不是等到迁 maintenance/listing 时。