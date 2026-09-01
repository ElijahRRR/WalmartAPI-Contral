---
name: policy-refresh
description: 沃尔玛官方禁售政策全量刷新流程:总览页核对 → 逐页子代理转录 → 独立校验 → 汇总审阅 → 中文翻译 → 入库 → 飞书投影。官方政策页更新、新增/删除类别,或所有者要求重新同步时使用。
---

# 沃尔玛官方禁售政策刷新(policy-refresh)

> 流程定稿依据 2026-09-01 首轮全量转录实战(42 类,含登录门禁页处置);
> 入库口径与飞书设计见 `docs/policy_sync.md`。产物三处:
> `refdata/policy_pages/en/`(英文逐字转录 = **权威判据源**,喂 LLM 的唯一来源)、
> `refdata/policy_pages/zh/`(人读中文译本,永不进提示词)、
> PG `audit.walmart_prohibited_policy`(入库后 L3 实际读取处)。
> 重跑本流程后 refdata 的 git diff 就是政策变更审计记录。

## 合规边界(先读)

- 来源唯一 = 美国站 `marketplacelearn.walmart.com`(公开帮助站):普通 httpx/curl **直连即可**,
  不涉店铺关联封号红线,不走店铺代理;`/ca/` 加拿大页不收。
- 沃尔玛 Marketplace **卖家 API** 仍必须走 `api/_client.py` 每店固定代理 —— 两者严禁混淆。
- **浏览器机翻页面不可作核对依据**:实测 Chrome 自动翻译会吞掉 "are prohibited" 谓语,
  把官方自带矛盾"翻没了"(2026-09-01 Animals 一节实证)。

## 第 0 步 · 总览页核对(变更侦测)

1. 抓总览页 `guides/Prohibited-products-policy:-overview`,**类别清单以页面实际链接解析为准,
   不信任何计数**(教训:曾数出 43,是把无正文页的 Weapons 父级和导语里的 WFS 链接错算进去,实为 42);
   Weapons 嵌套子项摊平收录。
2. 与 `refdata/policy_pages/en/` 现有清单 diff:新增/消失/改名类别、各页 Last Updated 变化。
3. 全部无变化 → 到此为止;有变化 → 只对变化页走第 1-4 步(全量重跑仅在所有者要求时)。

## 第 1 步 · 逐页转录(workflow 派子代理,每代理 ≤3 页)

逐字转录纪律(verbatim):

- 官方英文原文逐段转 markdown,**不翻译/不概括/不增删/不修正** ——
  官方自带的矛盾、笔误、归列错误照录(如 Animals「Allowed with restriction」列内出现
  "…are prohibited" 条目、源码混入 PDF 页眉残句),并逐条记入汇总须知;
- 结构镜像:标题层级、列表条数与嵌套、表格行列、单元格内 `<br>`/`&nbsp;` 原样;
- 只收正文:chrome(Reading time / Bookmark / Tell us what you think / Related guides / 导航页脚)不收;
- 每文件头部固定四行:`# 类别名` / `> 来源: URL` / `> 官方 Last Updated: …` / `> 抓取(UTC): 日期`;
- **拿不到正文不编造不占位**(登录门禁页,如 General-Use Products):记录证据链
  (无 UA / 带 UA 逐字节对比、WebFetch 对照、同域兄弟页对照、URL 从总览页反查),
  请所有者在已登录卖家会话复制正文,按同一纪律补录并在头注写明"所有者粘贴"来源。

## 第 2 步 · 独立校验(另派校验代理,不由转录代理自查)

- 逐节查:漏段/串页/截断/表格破损/chrome 残留/编造痕迹;
- 跨文件一致性(标题层级、Notes 呈现形式、In-this-guide 有无、日期格式)**只记录不强改**,
  归一化统一放到入库清洗层做,转录件保持对官方页的忠实。

## 第 3 步 · 汇总英文权威版,交所有者审阅

头部(总览页 Last Updated、类别数定性)+ 审阅须知(官方自带矛盾/门禁页/词形差)
+ 目录(逐页 Last Updated)+ 全部节 + 附录(解析记录/校验报告/逐节转录说明)。
**所有者审完才进第 4/5 步。**

## 第 4 步 · 中文翻译(人读版;LLM 永远只读英文)

- 纪律:逐段直译不增删不换序;结构镜像;URL/日期/品牌/型号/标准编号不译;
  机构与法规名首现附英文;输出文件只含译文本身;
- 固定栏目统一译名:概述 / 政策是什么? / 补充信息 / 政策要点 / 注:;
  表头 禁止 / 限制性允许 / 允许;
- 术语定稿(修订需过所有者):whitening=美白、lightening=增白、brightening=提亮(法律后果分界词);
  pistol=手枪、handgun=短枪;swastika=万字符;**elk=马鹿**(词典常见错译"麋鹿",勿踩)、
  moose=驼鹿、reindeer=驯鹿;hookah=阿拉伯水烟壶(与 bong/water pipe=水烟筒/水烟斗区分);
  gel caps=胶丸;Shark Liver Oil=鲨鱼肝油(非"鱼肝油");
- 另派校验代理逐节中英对照(漏译/增删/结构/译名统一/残留英文),问题修完再交付。

## 第 5 步 · 入库(先 --dry-run,人眼确认再真跑)

```bash
python cli.py policy_sync --dry-run     # 先跑这个,报告落 <DATA_ROOT>/reports/policy_sync.txt
python cli.py policy_sync               # 人眼确认后才跑真的(缺省即真跑)
```

- `refdata/policy_pages/en/*.md` → `audit.walmart_prohibited_policy`,
  upsert 口径见 `docs/policy_sync.md` §二:存量行不改名、人工中文列一律不碰、
  缺席页不刷新、官方已不含的行不删只报告;
- **首跑 dry-run 必须人眼核对两处**(报告打了标记,但没人看 = 白打):
  ①「未对上」清单——生产表存量 7 行用旧缩写名(Drugs & Paraphernalia、
  Electronics & RF 一族),按「不猜」口径对不上官方全称,**逐条裁决改名/新增/忽略**,
  直接真跑会写出**同概念双行**并污染 S2 候选(报告在「未对上」与「官方已不含」
  之间给出「疑似改名对」提示,顺着它判);
  ②「对上」清单里带 `←官方名` 标记的行 —— 表内名与官方拼写不同,本轮**不改名**
  (改名随 L3 提版批做),确认这些行确实指的是同一个类别;
- **真跑同批手动递增 `AUDIT_RULES_VERSION`**(政策表内容变化 = L3 判定输入变化);
- 真跑摘要还会点名另两条连带后果:新增行人工中文列全 NULL(S4 现渲染为空壳标题,
  待运营补中文)、`services/audit_l3.py` S1/S3 提示词硬写「37 条」将与实际行数不符
  (是否修改随第三步 L3 批由所有者定);
- 喂 LLM 的"机器喂入版"不落库,由渲染层从 full_policy 派生(剥 URL/导览/免责声明/头注/chrome,
  详见 `docs/policy_sync.md` §十)。

## 第 6 步 · 飞书投影 + 归类对照复跑

- 飞书两区表(机器投影区/人工区)见 `docs/policy_sync.md` §八.2;
- 政策表重塑完成后,所有者重跑 `python cli.py error_reclass_report` 看政策名 join 收敛。
