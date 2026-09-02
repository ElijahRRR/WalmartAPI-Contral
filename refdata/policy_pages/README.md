# 沃尔玛官方禁售政策转录库(policy_pages)

> 生成流程:`.claude/skills/policy-refresh/`(总览核对 → 子代理逐页转录 → 独立校验 →
> 所有者审阅 → 翻译)。入库与喂入口径:`docs/policy_sync.md`。
> **重跑 policy-refresh 更新本目录;git diff 即政策变更审计记录。**

- `en/` —— 官方英文逐字转录(44 份 = 42 节 Prohibited Products Policy + 内容族 2 页),**权威判据源**:入库 `audit.walmart_prohibited_policy`
  与 L3 提示词的唯一文本来源。忠实纪律:不增删不修正,官方自带矛盾照录(见下)。
- `zh/` —— 人读中文全译本(44 份),给运营团队看,**永不进 LLM 提示词**(语言原则:
  给 LLM 的 = 官方英文原文;给人的 = 中文)。

首轮转录:2026-09-01(总览页 Last Updated: Jun 5, 2026,官方实解 42 类)。
内容族两页(43 `Content standards: Overview`、44 `Product details policy`):2026-09-02 收录,
它们不在禁售总览页上,是沃尔玛「violates Walmart's content policy」/「unverified authenticity
claims」两类下架原因所指的页面(入表理由 `docs/audit_step3_spec.md` §一 A 批)。

## 已知官方原文自带事项(转录忠实保留,入库/使用时留意)

1. **Animals**「Animal Parts…」表「Allowed with restriction」列前两条逐字为
   "…are prohibited"(列名与内容相悖;2026-09-01 对实时官网 SSR+页内 JSON 双处复核确认;
   浏览器机翻会吞掉该谓语,勿以机翻页核对);
2. **General-Use Products** 页在官方登录墙内,直接抓取不可得 —— 本节正文由所有者
   登录卖家会话复制提供(Last updated May 20, 2026),头注已注明来源;
3. **Restricted/Illegal Products** 源页混入一句官方 PDF 页眉残句(Walmart Confidential…),
   照录未删,入库清洗时处理;
4. 各节标题层级 / Notes 呈现形式 / In-this-guide 有无 / 日期格式在官方各页间本就不一致,
   转录不做跨节统一,归一化在入库清洗层做;
5. **Content standards: Overview**(43)在登录墙内,正文由所有者粘贴;粘贴件不含 H1(按 URL
   路径段写,待核)与 FAQ 段(折叠面板未展开,待补录,不占位);「Additional guidelines」的
   21 个分类名原是两栏链接网格,按粘贴顺序照录;
6. **Product details policy**(44)公开页,由页面结构化数据渲染并与所有者粘贴交叉核对:
   「Product title」表末尾一空数据行、「Description」表条目在源码拆成四个列表、一张无文字
   示意图、"ect." 与 "Black Friday savings" 两处官方笔误 —— 全部照录(头注逐条列明)。
