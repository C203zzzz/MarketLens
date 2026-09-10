# MarketLens（原「竞研台」）· Agent 交接文档

**交接日期**：2026-09-10（**2026-09-11 有两轮更新，第二轮见下**）
**状态**：模型选型、检索范围、联网来源重构均已完成；第二轮按「能否交付」标准做了产品化审视与修复

---

## 2026-09-11 第二轮更新（产品化审视）

以「这个报告能不能直接交付给用户」为标准重新审视整个应用，修复以下问题。

### 1. 报告核心区块失效（最严重）

- **「一页结论」为空**：`ai_key_findings` 要求 ≥3 条可溯源事实才生成，旧数据没有该字段时直接显示「未形成结论」。现区分「早期版本无此区块」与「事实不足」两种情况分别说明；实测重新分析可稳定生成 3-5 条对比性结论
- **分析对象混入竞品列表**：新增 `is_category_like(name, parsed)`，在候选汇总处统一剔除品类词（"增肌粉""乳清蛋白粉"）、用户输入的产品名，以及 AI 标记为 `own_product` 的品牌与产品。修复前，分析"汤臣倍健蛋白粉"时竞品列表里同时出现"汤臣倍健"和"蛋白粉"
- **历史报告回放**：`load_stored_result` 按当前规则重新清理 competitors，旧报告不再显示品类词

### 2. 让报告真正形成横向对比

审视中发现最实质的产品缺陷：报告里的数据几乎全是分析对象自己的，5 个竞品一条数据都没有 —— 检索词围绕分析对象、抽取 prompt 也只「关注」分析对象，报告退化成「自我介绍」。三处修复：

- **竞品补充检索**：候选品牌确认后，针对竞品再跑一轮快速检索（跳过较慢的 qwen），查询形如「维力维 汤臣倍健蛋白粉 参数 价格」
- **来源读取优先级**：`read_external_sources` 增加标题启发式 —— 榜单/对比类文章（含「排行榜/十大/TOP/对比」）优先读，它们天然并列多个品牌；「怎么做竞品调研」这类方法论文章降到最低
- **对象名归一 + 抽取覆盖面**：新增 `align_subject`，把「汤臣倍健蛋白粉」与「汤臣倍健」归一到同一对象（不归一会导致有事实却显示「暂无可比较的数据」）；`extract_web_facts` 的「关注对象」改为全部比较对象，并要求逐个抽取

效果：6 个比较对象中有数据者从 1 个提升到 3 个，结论从「分析对象自述」变为跨品牌对比（例如「维力维以 95.8% 纯乳清蛋白及零糖脂领先，汤臣倍健侧重动植物双蛋白配比，Dymatize 专注水解乳清」）。

### 3. 工程硬伤

- **测试套件已损坏**：search 适配器新增 `market` 参数后测试未同步（2 个 error），且测试会真的对 example.com 发 HEAD 请求导致不稳定。已修签名并 mock `url_is_dead`，10+ 测试从 1.6s 降到 0.03s
- **源码出现 178 处 `\uXXXX` 转义**：一次编辑后中文字面量被写成转义序列，功能正常但可读性受损，已用脚本还原
- **`secrets.example.toml` 与实际配置脱节**：补 `BOCHA_API_KEY`、`QWEN_SEARCH_ENDPOINT`、`FILE_ANALYSIS_MODEL_DEEP`；`SEARCH_PROVIDERS` 默认值更正为 `qwen,bocha,tavily,exa,serpapi,brave`
- **历史记录排序错乱**：时间格式从 ISO 改为 `YYYY-MM-DD HH:MM` 后，字符串排序让新记录排在旧记录之后（`空格` < `T`），最新报告沉底。已改为 `ORDER BY REPLACE(created_at, 'T', ' ') DESC`

### 4. 界面降噪

- 内部工程指标（估算成本、搜索次数、原始结果、缓存命中等 6 项）从报告首屏移入「检索过程与用量」折叠区；首屏只留用户能理解的 4 项
- 横向比较从「按品牌分组」改为**按维度分组**：同一指标下不同对象并排才是真正的横向比较，并显式标注哪些对象暂无数据
- 时间显示统一为 `YYYY-MM-DD HH:MM`（历史 ISO 串经 `format_time` 转换）
- 隐藏 Streamlit 自带的 Deploy/主菜单，避免演示时开发态 UI 穿帮
- 落地页：品牌名与定位语合并进同一色块、hero 高度收敛、主 CTA 加宽以区分主次、三步说明加重
- 报告 / 来源 / 历史三个 tab 去掉与 tab 名重复的 subheader

### 5. 检索预算重分配（横向覆盖 1/6 → 6/6）

第二轮让报告有了横向对比，但覆盖仍然偏窄。根因不在检索能力，而在**预算分配**：

- **多引擎跑同一批查询**：qwen/bocha/tavily 收到的是同一个查询列表，16 次搜索里有 6 次纯重复。`merge_source` 事后能去重，但救不回已经花掉的配额 —— 竞品补充检索只剩 1 次额度。现改为：发现型检索词（竞品发现、价格）所有引擎都跑，补齐型检索词按引擎轮流分片
- **读取名额纯按优先级截断**：来源池常有 30+ 条而读取额度只有 8 条，某些竞品一条来源都进不去。新增 `pick_read_targets`，先从每个比较对象各挑一条最相关的来源，剩余名额再按优先级补满
- **检索限定词是伪随机**：`terms[len(queries) % len(terms)]` 会让「认证与安全性」配到「对比」。新增 `dimension_term`，按维度语义映射限定词（认证→认证资质、参数→参数规格、评价→评测口碑）
- **内部标签混进检索词**：AI 会把「国内电商」写进 query，搜索引擎不认识，已在 `missing_queries` 中剔除

实测（同一输入「汤臣倍健蛋白粉」，国内档，标准模型）：

| 指标 | 初版 | 第二轮 | 第三轮 |
|---|---|---|---|
| 一页结论 | 0 条 | 3 条 | 4 条 |
| 待确认项 | 24 条 | 0 条 | 0 条 |
| 横向覆盖 | 1/6 个对象 | 3/6 个对象 | **6/6 个对象** |
| 可溯源事实 | 16 条 | 17 条 | 25 条 |
| 耗时 | 74s | 77s | 82s |

### 6. 填满维度格子（覆盖 21/30 格，事实 40 条）

第三轮解决了「每个对象有没有数据」，这一轮解决「每个格子里有没有数据」。诊断脚本 `tmp/analyze_gaps.py` 会打印对象 × 维度矩阵。

- **「每克蛋白质单价」整列空白**：原文里价格、规格、蛋白含量往往同时出现，但没有任何环节去算。已在抽取提示词中加入**派生指标**规则：允许计算，但必须能指出原文中计算所需的全部输入，并在 value 里写明算式（如「404元/2罐÷(2罐×119g)=1.70元/克」）；缺任一输入则留空，不得估算
- **读取页数从 8 提到 12，覆盖反而下降**（20→18 格）：一次性把 12 个页面（约 5.4 万字符）塞给抽取模型，注意力被稀释。改为**分批抽取**（每批 6 页、抽完合并）后，事实数 20 → 40，覆盖 18 → 21 格
- **读取名单混入无关页面**：此前只按「标题含『榜』」判断，把别的行业的榜单（如「某工具品牌榜」）也读了。新增 `topic_terms` 相关性过滤，要求标题或摘要出现目标品类词

实测（同一输入，国内档，标准模型）：

| 指标 | 第三轮 | 第四轮 |
|---|---|---|
| 可溯源事实 | 25 条 | 40 条 |
| 覆盖格数 | 20/36 | 21/30（70%） |
| 满格维度 | — | 含量、认证 6/6 全覆盖 |
| 价格条数 | 2 条 | 8 条 |
| 耗时 | 82s | 105s |

### 7. 通用性检验（换品类实测）

用三个差异极大的品类验证系统不是只对单一品类有效：

| 品类 | 入口 | 维度名 | 横向覆盖 |
|---|---|---|---|
| 保健品（蛋白粉） | 输入产品名 | 中文 | 6/6 对象、21/30 格 |
| 实验室仪器（生化培养箱） | 上传 Excel | 中文 | 资料内 4 个品牌各 5/5 格 |
| SaaS（飞书） | 输入产品名 | 中文 | 5/6 对象有数据，抓到订阅制价格 |

过程中发现并修复了 4 个**只在特定品类下才暴露**的缺陷：

1. **维度名语言不确定**：实验室仪器品类下 AI 输出 `unit_price`、`capacity` 这类英文键名，导致所有中文关键词逻辑（价格检索词优先、语义限定词匹配）全部失效。已在三处 prompt 中强制「维度名必须是中文」，并给判断逻辑补了英文关键词作防御
2. **型号被当成品牌**：AI 把 `LRH-150`、`SPX-150BIII` 标成 listed_competitor 后并入品牌列表，比较表变成零件清单。新增 `looks_like_model`（含数字 + 分隔符），在候选汇总处剔除
3. **品牌列提取不干净**：`extract_brands` 原先同时读「品牌」和「产品」列，型号因此混入。改为优先只读品牌/厂商/公司列，没有这类列时才退用产品列
4. **维度名与表格列名对不上（最严重）**：AI 生成的「容积规格」「控温功能」与用户表格的「型号」「单价」「备注」字面完全不同，导致**用户上传的资料一条数据都提取不出来**（4 个资料内品牌覆盖全为 0）。新增 `columns_for_dimension` 做语义映射，并用「备注」列兜底；修复后 4 个品牌各 5/5 格，事实从 6 条升到 27 条

第 4 点尤其值得记录：**上传资料模式是这个产品的核心价值场景**（用户有私有资料、公开搜索补不上），而它此前在非保健品品类下完全失效 —— 只测单一品类不可能发现。

另发现待改进项：SaaS 品类下 AI 会把母公司当竞品（输出「阿里巴巴」而非「钉钉」），已在候选识别 prompt 中明确要求输出产品本身。

### 8. PDF 资料入口修复 + 全品类通用性收口

用 Tecan 酶标仪的两份产品手册（PDF，图文混排）测「上传 PDF」入口，暴露 5 个问题：

1. **PDF 正文完全没被抽取（最严重）**：`extract_web_facts` 只认 `origin == "外部搜索"` 的来源，用户上传的 PDF 恰好被排除在外 —— 28 页资料一条事实都没产出，报告全靠联网结果。改为按来源类型分配配额：PDF 页与联网页各占一半，两类内容都能进抽取
2. **自家产品被当成竞品**：AI 把 Tecan 标为 own_product，但其产品线（Infinite M Nano 等）仍被并入竞品列表，生成「Tecan vs Tecan 各型号」的荒诞比较表。`build_result` 现在剔除全部 own_product 实体，`analysis_subject` 在资料模式下取 own_product 的品牌
3. **检索词主体过宽**：检索主体用 category（「实验室仪器」），把纯水机厂也搜了进来。改为优先用 `document_profile.product_scope`，并在提示词中要求 AI 必填该字段。实测检索词从「实验室仪器 竞品」变为「多功能微孔板检测仪（酶标仪） 竞品」，竞品从梅特勒-托利多/NEPTEC 变为美谷分子/瑞孚迪/奥盛/Thermo Fisher
4. **文档类型被当成品类**：AI 输出 category = 「产品宣传册/技术规格书」，会让报告标题和检索方向全错。新增 `is_document_type` 过滤 + 提示词约束
5. **模板花括号导致崩溃**：在 `AI_USER_TEMPLATE`（用 `.format()` 渲染）里写 `{name, ...}` 触发 KeyError，**所有上传资料的分析都会直接崩掉**。已修并加回归测试

最终实测（Tecan 酶标仪 PDF，国内档）：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 分析对象 | 空 | Tecan |
| 竞品 | Tecan 自家 8 个型号 | 奥盛、瑞孚迪、美谷分子、Thermo Fisher |
| category | 产品宣传册/技术规格书 | 多功能酶标仪 |
| 可溯源事实 | 8 条 | 21 条 |
| 竞品覆盖 | 0 个 | 2 个（美谷分子 4 维度、Thermo 3 维度） |

结论示例（仪器选型级对比）：

> 美谷分子 SpectraMax iD5 温控上限达 66℃，显著优于 Tecan 的 42℃
> Tecan 凭借四光栅技术实现全波长任意选择，相比美谷分子滤光片方案在检测灵活性上更具优势
> 美谷分子 FlexStation 3 集成移液系统并支持 1536 孔板，相较 Tecan 最高 384 孔的支持，在高通量自动化整合上领先

**全品类验证汇总**：保健品（输入产品名）、实验室仪器（上传 Excel）、酶标仪（上传 PDF）、SaaS（输入产品名）四个入口、三种资料形态全部跑通，18 个单测通过。

### 9. 架构级通用化（去掉品类相关的硬编码）

前几轮每遇到新品类的失效，处理方式都是「再补一张关键词表」。这属于打补丁：`DIMENSION_COLUMN_HINTS`（维度→列名）、`DIMENSION_TERM_RULES`（维度→检索词）、`CATEGORY_WORD_SUFFIXES`（品类词后缀）三张表都只覆盖少数行业，换品类就要再写一遍。

本轮改为**让 AI 和数据结构承担语义判断，代码只保留跨品类通用的机制**，删掉三张表：

| 原来的做法 | 现在 | 为什么 |
|---|---|---|
| AI 生成维度 → 用 `DIMENSION_COLUMN_HINTS` 猜表格列 | **表格列名直接作为比较维度** | 列名就是用户定义的比较框架，天然与事实提取对齐，映射表整个不需要 |
| 维度 → 查表换成通用检索词 | **直接用维度名作检索词** | "控温模式"比映射出来的"温度 控温 精度"更精准 |
| 靠后缀表（粉/液/膏/片）判断品类词 | **靠 AI 的 `entity_type`** + 型号几何特征 | 后缀表只覆盖消费品，且是在用关键词模拟语义 |
| 硬编码比价站域名（smzdm/zol/it168…） | **按页面特征判断**（货币符号+价格词=报价页） | 换个品类（仪器、软件）原域名表完全失效 |

保留下来的是**语言层面的通用规律**，它们不涉及行业知识，因此不算过拟合：模型号识别（含数字+分隔符）、方法论文章降权（"怎么/如何/教程"）、对比文提权（"排行/对比/vs"）、文档类型过滤（"宣传册/说明书"）。

`CATEGORY_HINTS` 与 `CANDIDATE_LIBRARY` 保留，但已明确标注：**只在未配置 AI Key 的演示降级路径生效**，有 Key 时品类与维度全部由模型判定，新增品类无需改动它们。

**六个品类、三种资料形态实测，全程未为新品类改一行代码**：

| 品类 | 入口 | 识别出的品类 | 竞品 | 横向覆盖 |
|---|---|---|---|---|
| 蛋白粉 | 输入产品名 | 膳食营养补充剂/蛋白粉 | 康比特、康恩贝、Myprotein… | 6/6 对象 |
| 生化培养箱 | 上传 Excel | 生化培养箱 | 博迅、Eppendorf、BINDER… | 资料内 4 品牌各 4/4 格 |
| 酶标仪 | 上传 PDF | 多功能酶标仪 | 奥盛、瑞孚迪、美谷分子、Thermo | Tecan 6/6 + 美谷分子 6/6 |
| 飞书 | 输入产品名 | 企业协作与办公自动化软件 | 钉钉、企业微信、豆包工作 | 5/6 对象 |
| 冲锋衣 | 输入产品名 | 户外服装/冲锋衣 | 哥伦比亚、凯乐石、骆驼… | 5/5 对象 |
| 扫地机器人 | 输入产品名 | 扫地机器人 | 科沃斯、石头、追觅、云鲸、iRobot | **5/5 对象，2 个满格** |

新增品类实测（扫地机器人）的维度完全贴合品类：清洁能力、导航与避障技术、续航与充电方式、智能功能与 APP 体验、价格与性价比、品牌口碑与售后服务 —— 这些是模型现生成的，没有任何行业词表参与。

### 10. PDF 送进模型的链路复查（终版前检查）

逐个环节量化 PDF 正文的流失，发现两处截断：

| 环节 | 原状 | 问题 | 现在 |
|---|---|---|---|
| `ai_context` 送模型 | `join(blocks)[:26000]` | **整体截断**：内容超 26000 时靠后的文件与页面整批消失（28 页手册只传前 6 页） | 按内容块均分预算；没超预算就一字不删 |
| `extract_file_evidence` 建来源 | `text[:1200]` | 密集页面（规格书）在抽取时丢参数 | `text[:4500]` |
| `parse_files` 存页文本 | `text[:5000]` | 一般够用 | 不变 |

实测两个 Tecan 手册：总正文 21327 字符，`ai_context` 输出 22785 字符、**28/28 页标记齐全**、最长的 2045 字页面完整保留。模拟 30 页 × 3000 字（9 万字）时，上下文控制在 26074 字符且 30 页全部保留代表。

同时修正 `page_count` 的语义：原先把 PDF 页也算进「可用来源」（28 页 PDF 会显示成 35 个来源），现在只统计联网读取的页面；界面指标改为「参考来源」= 联网页 + 资料页。

最终 PDF 端到端（Tecan 手册，国内档）：Tecan 6/6 满格、美谷分子 5/6、**Thermo Fisher 3/6（修复前为 0）**，30 条可溯源事实，结论为双向技术对比。

---

## 2026-09-11 第一轮更新（检索范围贯通 + 联网来源重构）

### 1. 检索范围（market）真正贯通

之前 UI 的「分析市场」（国内电商/海外电商）只被写进 prompt 文字，**没有进入检索链路**：`missing_queries` 不看 market、`qwen_search` 的 system prompt 硬编码"优先京东天猫"、`build_result` 的 region 硬编码"未指定"。现在：

- 新增 `MARKET_PROFILES`（app.py 约 90 行）：定义两档的 region、币种、检索语言、来源侧重、检索词来源词与发现词
- `missing_queries(parsed, facts)` 由 market 驱动：国内用中文来源词（官网/评测/参数/对比），海外用英文（official/review/specifications/comparison）
- `search_system_prompt(market)` 按 market 生成检索要求，替代原硬编码常量
- `build_result` 的 `task.region` / `task.currency` 用 profile 值
- 实测：同一产品在国内外两档产出**完全不重叠**的竞品集（国内：肌肉科技/汤臣倍健/康比特/维力维；海外：Transparent Labs/Legion/Ascent/Dymatize）

### 2. qwen 联网改走原生 API（关键修复）

**实测确认：compatible-mode 不返回 `search_info`** —— 顶层只有 model/id/choices/created/object/usage，message 只含 content/role。模型只能靠"回忆"在正文里写 URL，实测大量编造（`detail.tmall.com/item.htm?id=61234567890`、`item.jd.com/100012345678.html` 这类顺序数字伪 URL）；早先"qwen 联网稳定 6/6 真实"的结论是误判——当时 query 命中模型训练数据里的真 URL，验证又只看"上传文件品牌黑名单零违反"，未校验 URL 可访问性。

**修复**：`qwen_search` 改用原生端点 `https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation`，配 `search_options: {enable_source: true}`，只取 `output.search_info.search_results` 的真实 URL，**完全忽略模型正文**。实测返回如 `item.jd.com/product/AZpp1PsHtg6AAtu0SHpwOQ.html`（真实 hash）、`smzdm.com/p/181754578/`、`mt.sohu.com/a/1072794320_122860275`。

- 原生 API 可用模型实测：qwen-plus（21-40s）、qwen-max、qwen3-max、qwen-turbo；**qwen3.7-plus / qwen3.5-plus 在原生 API 报 400**（模型名不通用）→ 搜索模型与提取模型已解耦，搜索用 `QWEN_SEARCH_MODEL`（默认 qwen-plus），提取仍用 compatible-mode 的 qwen3.7-plus/3.8-max
- `search_results` 字段只有 icon/site_name/title/url/index，**无正文摘要** → 产品详细信息仍必须靠抓正文
- 端点与模型可用环境变量覆盖：`QWEN_SEARCH_ENDPOINT` / `QWEN_SEARCH_MODEL`

### 3. 反编造与来源质量

- `url_is_dead(url)`（functools.lru_cache + ThreadPoolExecutor 并发）：只判定确定的死链（404/410）；403/405/429/超时视为存活，**避免误杀反爬的电商/媒体站**。qwen 来源每条进 sources 前校验
- 检索 prompt 去电商化：电商平台降为"价格与在售佐证之一"，来源要求覆盖品牌官网、行业媒体与专业评测、百科、行业研报
- 正文抓取优先级调整：电商详情页（jd/tmall/taobao/1688/amazon…）排到媒体/评测/官网之后
- 实测国内档来源全为行业报告（观研/新食界/新浪财经）、PDF 研报、界面新闻、知乎、社区论坛，**无一电商详情页**

### 4. 两个新修 bug

- **竞品发现检索词被截断**：`missing_queries` 把发现型检索词（"X 竞品 主流品牌 对比"）放在最后，而 `make_live_result` 只取 `[:6]`，导致它永远被砍 → 结果只盘点单一品牌、没有横向对比。已改为 `insert(0)` 优先
- **占位符污染检索词**：AI 会输出 `[Competitor Brand]` 这类未替换的模板串，以及类目降级时 `待确认产品类别` 被拼进检索词（如"蛋白粉 待确认产品类别 价格与套餐 官方"）。已过滤含 `[]` 的检索词，并用 `query_subject()` 在类别不可用时退化为主体品牌/产品名

### 5. 后续修复（2026-09-11 当日完成）

- **AI 类别识别失败（根因比预想严重）**：`ai_identify_product_name` 的 prompt 只说"返回与 file-intake-v3 相同的结构"却**没列出键名** → AI 自由发挥，返回 `{meta, input_analysis, comparison_dimensions, search_plan}` 而非 `{document_profile, entities, ...}`。normalize 只认 `document_profile.category.name`，于是类别、维度、实体**全部被丢弃**，退回本地兜底的"待确认产品类别"和固定模板维度。修复：抽出 `SCHEMA_SPEC` 常量写死键名（并提示不要用 dimension_name/rationale/primary_category 等别名）；normalize 加 `_recover_category()` 从漂移路径捞回；类别实在不可用时用产品名兜底。修复后类别识别为"运动营养/膳食补充剂"，维度变成 AI 生成的专业维度
- **检索主体的选择**：修复过程中发现，用抽象品类名（"运动营养/膳食补充剂"）作检索词会搜出"竞品调研方法论"文章和行业研报，产品参数提取不到；用具体产品名（"蛋白粉"）才搜得到真实品牌对比 → `query_subject()` 改为优先用用户输入的产品名
- **海外档检索词英文化**：identify prompt 在海外档要求输出 `document_profile.product_name_en`，`query_subject()` 海外档优先用它
- **搜索提速**：qwen 搜索模型换 `qwen-turbo`（实测 6s，qwen-plus 需 20-40s），且 qwen 只跑前 3 条发现型检索词，细节补齐交给 Tavily。完整流程从 83-174s 降到 76s
- **前端改版**：安装并应用 `anti-ui-slop` skill（uizze 出品，被 github/awesome-copilot 收录）重做 CSS——去掉药丸标签与圆角卡片，改用间距/对齐/细线分组，数字用 tabular-nums，控件圆角统一 4px；新增 `.streamlit/config.toml` 把主题色从 Streamlit 默认红改为墨绿。注意 Streamlit 组件样式优先级高于裸标签选择器，覆盖 heading 需 `!important`
- **生成分析加进度条**：`make_live_result(parsed, note, progress=…)` 新增可选进度回调（CLI 不传则无影响），界面用 `st.progress` + 阶段文字显示"检索 xxx → 读取网页正文 → 抽取事实"，不再只有转圈
- **历史记录可回看完整报告**：DB 增列 `result_json` 存整份报告（`_result_payload()` 剔除不可序列化的 DataFrame 与网页/文件原文）；历史 tab 加下拉框选中即可用 `render_result` 重放当时的完整报告。**旧记录没有 result_json，会提示只保留摘要**
- **正文抓取改并发**：`read_external_sources` 原本串行抓 8 页、每页失败还重试镜像站，最坏要几分钟卡住界面；改为 `ThreadPoolExecutor` 并发 + 超时 18s→12s，并把正文缓存从 `st.session_state` 提到模块级 `_PAGE_CACHE`（线程安全且同一 URL 不重复抓）
- **报告可读性（按用户反馈调整）**：① **去掉批量"待核验"**——`make_live_result` 原先给每个"品牌×维度"空组合都插一行"需要从原文核对"，10 品牌×6 维度会产生大量噪音、让报告无法直接交付；现在空单元格显示"—"，只呈现有依据的事实。② 新增**证据明细**表：每条事实一行 + `LinkColumn` "打开"直达原文，解决"要能方便回到原文校对"的诉求。③ 横向比较表把**网页原文里实际找到的品牌**并入比较对象（原先只列候选，导致矩阵表缺行、与"关键差异"对不上）。④ 矩阵表设固定列宽，减少横向拖动
- **界面文案精简**：删除各模块下口语化的解释小字（如"这里只总结当前证据能支持的差异…""六维图用于快速发现资料差异…"），只保留必要的状态与元信息；来源面板统一为 URL + "打开原文"按钮（原先 qwen 来源那句"未提供可访问的原文链接"已过时）
- **时间格式**：`now()` 由 ISO（`2026-09-11T00:27:51+08:00`）改为可读格式（`2026-09-11 00:27`），旧记录在 `load_history` 里兼容转换
- **产品改名 + 落地页（2026-09-11）**：品牌从"竞研台"改为 **MarketLens**（英文名 + 中文功能注解「竞品情报工作台」；注意目录名仍是 `竞研台`）。新增**落地展示页**：进来先看到品牌展示与两个入口（使用说明 / 开始使用），用 `st.session_state["view"]` 在 landing / guide / app 三个视图间切换，不再直接进功能区；落地页有淡入与分隔线展开动效，下方是三步流程说明
- **候选竞品改为品牌级**：候选识别 prompt 原先返回产品/品类名（"增肌配方粉""植物蛋白粉"），已在 prompt 明确要求只返回品牌或厂商名，并过滤与品类名相同的候选
- **评价改为归纳**：`review_summary` 原先照抄用户评价原文（口语化、冗长），prompt 改为要求 15-30 字客观归纳
- **图表在证据稀疏时可读**：覆盖热图原先对 0 值渲染"待补"文字，导致缺数据时整图铺满占位；改为裁掉全空行列 + 0 值不写字，维度不足 3 个时不生成雷达图（写 `-no-radar` 标记避免重复计算）
- **横向比较改版式**：宽表格（品牌×维度）需要横向滚动且信息密集，改为**按品牌分组**的对齐列表（`render_comparison`），一行读一个对象
- **电商数据获取（2026-09-11 实测结论）**：报告长期没有价格，根因有两层。
  - **抓取层**：京东 PC 商品页返回的是 SPA 空壳（实测 2.6KB，无正文），而同一个商品的移动端地址返回完整 HTML（实测 251KB，含 `price`/`jdPrice` 字段）。已实现 `mobile_variant()` 自动重写（`item.jd.com/X.html` → `item.m.jd.com/product/X.html`，天猫同理 `detail.m.tmall.com`），并换用真实浏览器 UA。这是公开页面的另一种地址，不涉及绕过访问控制
  - **检索层（更关键）**：qwen 联网返回的检索结果偏向媒体/研报，几乎不含电商商品页。实测 `query="蛋白粉 价格 多少钱 一罐"` 时返回 10 条全是比价/电商页（smzdm、1688、微博促销，含"康恩贝乳清蛋白粉2罐70.6元"这类直接带价的内容）——**说明带价格意图的检索词才能命中比价页**。已把价格检索词提到前两位（原先排在 `[:6]` 配额之外被截断）
  - 另：qwen 原生 API 的 `search_options.assigned_site_list` 实测可限定检索站点（限定 jd.com/tmall.com/smzdm.com 后结果收敛）
  - **法律边界**：不建议绕过验证码或高频抓取（大众点评案、车来了案已判，《反不正当竞争法》+ 刑法 285 条）。官方开放平台（京东联盟/淘宝开放平台）需企业资质且多为推广用途，个人实习项目不可行；第三方搜索 API（博查/秘塔）是更现实且合规的路径

### 6. 遗留问题

- **Tavily 配额**：本机测试 key 会报 `exceed usage limit`（**项目部署用的是另一套 key，不受影响**）。在本机跑测试时 tavily 会静默失败，可用搜索源只剩 qwen + exa/serpapi/brave
- 独立搜索底座（博查 BochaAI / 秘塔 / 百度千帆）国内可用性好，本次未接入，作为后续备选
- 视觉读图测试、`enable_thinking=True` 对比：仍未做

---

## 项目概况

竞研台 = Streamlit 竞品分析工作台，位于 `d:\Codex\实习项目\竞研台`（Windows 10，bash shell）。
主程序 `app.py`（约 90KB）：用户上传 Excel/CSV/PDF → 后端 PyMuPDF 解析 → 文本拼 prompt → 百炼兼容接口（dashscope compatible-mode v1）调 qwen 模型 → 结构化 JSON 落库展示。

## 已完成（今天）

1. **模型结构化提取测试**：qwen3.7-flash / plus / qwen3.8-max 三模型对比完成，结论：
   - 接入建议：**3.7-plus 默认档 + 3.8-max 深度档**；3.7-flash 不推荐
2. **app.py prompt 升级 v3**（`AI_PROMPT_VERSION = "file-intake-v3"`）：
   - 关系规则：无证据不得标 own_product
   - 逐字符抄写型号
   - schema 键名写死（与 `normalize_ai_payload` 对齐）
3. **联网搜索测试**：3.7-plus 稳定可用；3.8-max 联网模式生成退化（吐 0 串）；Tavily 有效互补
4. **PDF 直读测试**：API 三种传 PDF 方式全部失败（400/忽略/400）→ 本地解析路线是唯一可行
5. 测试脚本均在 `tmp/model-benchmark/`（可复用，见下）

## 待办（未完成）

- [x] 竞研台实际接入联调：app.py 模型换 3.7-plus（默认）/3.8-max（深度档，UI「开始分析」可选），全 payload 加 enable_thinking=False，主分析 timeout 300s；实测标准档通过（71s、20 实体、6 聚合字段、无幻觉、页码溯源准确）；实测中发现并修复 normalize 对 quality.source_coverage 非数值（模型输出文字/'partial'）时抛 ValueError 导致整单降级的 bug（改 safe_float 容错，非数值兜 0.5）
- [x] 联网搜索接入竞研台：主搜 qwen3.7-plus（enable_search）+ Tavily，不足时（<MIN_UNIQUE_SOURCES 或 <MIN_CANDIDATES）按顺序补充 exa → serpapi → brave；默认 SEARCH_PROVIDERS=qwen,bocha,tavily,exa,serpapi,brave；实测补充链路生效（exa 补 6 条 unique），extract_web_facts timeout 45s 不足导致静默 0 产出，已修 120s（实测 50s）
- [x] ~~qwen 联网搜索修复（2026-09-10 晚）~~ **（2026-09-11 修正：该诊断与修复方向均有误 —— 不是"百炼剥离 URL"，而是 compatible-mode 不返回 search_info；从模型正文解析 URL 恰恰是编造来源。正解见上方「2026-09-11 更新」第 2 条）**：原记录为 —— 蛋白粉实测 qwen 6 连败（failed=6）→ 诊断为百炼 enable_search 将搜索结果注入 prompt 但**剥离 URL**（模型自述"知识库中未包含具体的 http 链接地址"），且响应无 search_info 字段；模型被迫编造电商 URL（item.jd.com/100012345678.html 等连续数字）。修复：qwen_search 改 benchmark 验证过的参数（system prompt 强制 JSON 数组带 source_url + temp 0.3 + max_tokens 4000 + timeout 60s），从 content 解析 JSON 数组 + _usable_url 过滤编造 URL；read_external_sources 跳过 provider=qwen 的来源（URL 不可信不 fetch，防风控页/错源）；UI 对 qwen 来源显示"检索摘要、无原文链接"不渲染打开按钮。复测：failed 0、qwen 贡献 16 条国内电商产品（汤臣倍健/康比特/维力维/纽崔莱/美莱健，均为京东天猫真实在售）、facts 高质量（维力维纯度 95.8%、ON 水解率 94.1% 等）
- [ ] 视觉读图测试（未做）：source-page-1~7.png 逐张传 image_url，测模型读图提取信息能力
- [ ] 深度思考轮（未做）：enable_thinking=True 对比无思考质量差异
- [ ] 3.8-max 联网退化问题：若要启用需固定"短输出 + temp 0.3"参数组合，或等百炼修复

## 关键技术事实（踩过的坑）

1. **qwen3.7 是混合推理模型**：`enable_thinking` 不传时默认开启思考，思考 token 计入 max_tokens 导致 JSON 截断。必须显式 `"enable_thinking": False`
2. **API 不支持 PDF 直传**：file content 类型 400；file_ids 被静默忽略；data URL 只认图片格式。网页版控制台的 PDF 解析是黑盒专属管道（曾导致三模型读不同页子集、漏移液器）
3. **3.8-max 联网搜索退化**：长输出时写 URL 写到一半开始吐 "0" 填满 token（两次复现）；短输出（每品类 2 条）+ temp 0.3 可抑制，但引入张冠李戴风险
4. **模型抄写不保真**：三模型都把 SPX-70BIII 抄成 SPX-70BII（prompt v3 已加逐字符规则）
5. **实体关系共错**：三家都把资料第一品牌（FAITHFUL）默认标 own_product（prompt v3 已修）
6. **bash 环境坑**（Windows + 中文路径）：命令行直接输中文目录名会乱码（U+FFFD）；工作目录持久化在命令间保留。可靠写法：
   ```bash
   S=$(find . -maxdepth 5 -path '*model-benchmark/xxx.py' | head -1)
   PYTHONUTF8=1 python "$S"   # 不要 cd（cd 后旧相对路径失效），脚本内用 __file__ 定位
   ```
7. **3.8-max 输出结构漂移**（联调时三次调用：20 实体/6 字段、21/6、22/17）：temperature=0 仍有非确定性；quality.source_coverage 会输出文字而非数值；relation 会全标 unknown（合规但保守，v3 规则允许）。normalize 已容错（safe_float 兜底），但做深度档质量对比时需多次采样
8. **serpapi 国内网络不稳**：首次测试 7 hits 正常，后续直连超时（非限流）。作为第三补充源可接受，失败静默跳过（failed_calls 计数）。exa 稳定且结果质量好
9. **API Key**：已通过 `setx` 配置到本机用户级环境变量（QWEN_API_KEY / DASHSCOPE_API_KEY / TAVILY_API_KEY / EXA_API_KEY / SERPAPI_API_KEY），新开终端生效；安全约束：真实 Key 不写入 app.py、README、SQLite、报告或聊天——脚本一律读环境变量
10. **百炼联网：必须用原生 API，compatible-mode 拿不到真实来源（2026-09-11 修正）**：compatible-mode 响应里没有 search_info，模型只能在正文里凭记忆编 URL（实测编出 `item.jd.com/100012345678.html` 这类顺序数字假链接）。**原生端点 `/api/v1/services/aigc/text-generation/generation` 配 `search_options.enable_source=true` 才会回传真实 `search_results`**。早先"百炼剥离 URL"的判断不准确 —— URL 在原生接口里拿得到。另：原生 API 不接受 qwen3.7-plus/qwen3.5-plus（400），搜索模型与提取模型须解耦

## 可复用资产

| 文件 | 用途 |
|---|---|
| `tmp/model-benchmark/run_api_test.py` | 结构化提取测试（支持 argv 指定模型重跑） |
| `tmp/model-benchmark/quick_check.py` | API 连通性诊断（30s 内出结果） |
| `tmp/model-benchmark/eval_api_results.py` | 三模型输出 8 维度评估 |
| `tmp/model-benchmark/search_benchmark.py` | 双源联网检索 + 去重（支持 `python xxx.py qwen3.8-max` 部分重跑） |
| `tmp/model-benchmark/pdf_direct_test.py` | PDF 直读测试 |
| `tmp/model-benchmark/integration_test.py` | 联调测试：parse_files → ai_analyze_documents 全流程（`python xxx.py 标准|深度`，保存 normalize 后结果） |
| `tmp/model-benchmark/debug_api_call.py` | 联调 debug：直调 API 打印 HTTP 错误并保存模型原始输出（`python xxx.py 标准|深度`） |
| `tmp/model-benchmark/check_search_apis.py` | 单 provider 搜索可用性检查（exa/serpapi/tavily） |
| `tmp/model-benchmark/search_flow_test.py` | 完整搜索流程实测（parse → AI 分析 → make_live_result，可 MIN_UNIQUE_SOURCES=100 强制触发补充搜索） |
| `tmp/model-benchmark/debug_web_facts.py` | extract_web_facts 单独 debug（复用 search-flow-result.json 的 sources） |
| `tmp/model-benchmark/keyword_flow_test.py` | 关键词路线实测：make_name_input → ai_identify_product_name → apply_ai_analysis → make_live_result，打印全链路并保存 result json |
| `tmp/model-benchmark/debug_qwen_web.py` | qwen 联网参数对照诊断（benchmark 参数 vs app.py 参数） |
| `tmp/model-benchmark/debug_qwen_raw.py` | 打印 qwen 联网原始响应完整结构（验证 search_info 是否存在） |
| `tmp/model-benchmark/native_search_test.py` | 验证百炼**原生 API** 的 search_options.enable_source 是否回传真实 URL |
| `tmp/model-benchmark/native_search_fields.py` | 原生 API search_results 完整字段 + 可用模型探测（qwen-plus/qwen-max/qwen3-max/qwen-turbo 可用；qwen3.7-plus 400） |
| `tmp/model-benchmark/debug_qwen_urls.py` | 测 qwen 能否复述检索到的真实 URL（结论：百炼不注入 URL） |
| `tmp/model-benchmark/test_qwen_search_fix.py` | 修复后 qwen_search 单测（3 组 query 验证 hits） |
| `tmp/model-benchmark/results/integration-标准-raw.json` | 3.7-plus 联调原始输出样例 |
| `tmp/model-benchmark/v4-text-input.txt` | benchmark 纯文本输入（7800 字符，7 个 source page 分节） |
| `output/pdf/竞研台-模型对比资料-v4-标准版.pdf` | benchmark PDF（13 页） |
| `docs/迭代记录-2026-09-10.md` | HR 展示用迭代记录 |

## 基准事实（评估对照用）

- benchmark 含 4 品牌：FAITHFUL（SPX 系列）、生元（LRH 系列）、合肥右科（YK-S160）、大龙（TopPette/MicroPette Plus/Hipette）
- 正确关系：资料无"自家产品"证据，全部品牌应标 listed_competitor
- 文本层 20 处〔低〕标记、7 个表格、关键数值：0~65℃ / 0~70℃ / 0~50℃、±0.5/±1℃、0.1℃、2000-20000μL"即将上市"
