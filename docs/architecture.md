# FreshKeep AI —— 架构设计（V1）

## 1. 文档目的

本文档定义 FreshKeep AI V1 的分层结构、模块职责、依赖方向、数据模型、SQLite 结构演进方式、AI 集成与校验、错误处理与测试策略。业务规则编号（`R-xx`）与 [product_spec.md](product_spec.md) 一致。

## 2. 技术选型

Python 3.11+、Streamlit、OpenAI 兼容 API、SQLite（标准库 `sqlite3`）、pytest。目标读者为初学者，因此优先选择可直接阅读与调试的实现方式。

## 3. 系统概览

```
pages/ + app.py     UI        输入与展示，零业务规则
services/           Services  协调业务流程与事务
core/               Core      纯业务规则、校验、计算
storage/            Storage   SQLite 持久化（db / schema / *_repo）
ai/                 AI        模型调用、提示词、结构化输出解析
```

## 4. 依赖方向硬约束

| 约束 | 说明 |
|---|---|
| 单向无环 | `UI → Services → {Core, Storage, AI}` |
| Core 纯净 | `core` 不 import `storage`，不 import `ai`，是纯函数与纯计算的集合 |
| AI 不碰数据库 | `ai` 只做"结构化输入 → 结构化输出"，不读写库 |
| UI 无业务逻辑 | 页面不做数值计算、日期运算、过敏原判定，只调用 Services 并渲染结果与错误信息 |
| 可测试性 | Services 通过构造参数接收 AI client 与仓储对象，测试注入 FakeClient 与临时数据库 |

禁止事项：抽象基类、接口协议层、插件注册表、依赖注入框架、多智能体架构、事件总线、后台任务调度。

## 5. 模块职责

| 模块 | 文件 | 职责 |
|---|---|---|
| UI | `app.py`、`pages/1_数字冰箱.py`、`pages/2_食谱规划.py` | 表单、列表、错误提示、会话状态 |
| Services | `services/inventory_service.py` | 批次入库 / 编辑 / 消耗 / 丢弃 / 按批次分配 / 应用规划结果的编排与事务边界（R-12、R-15、R-16、R-22） |
| Services | `services/ai_service.py` | AI 配置与客户端的唯一入口：读取配置、按需加载 `.env`、调用客户端并向上返回文本；错误统一为 `AIError`（S4） |
| Services | `services/recipe_service.py` | 菜谱规划编排（S5，已实现）：校验请求 → 读库存/过敏原 → 组提示词 → 调 `ai_service` → `ai/schemas` 结构校验 → `core/allergens` 确定性分流 → `core/recipe_math` 算缺口 → 返回分区结果。**不写库存、不写 SQL、不创建 SDK 客户端** |
| Core | `core/models.py` | 数据结构定义 |
| Core | `core/validation.py` | 名称、数量、单位、类别、日期等输入校验（R-10） |
| Core | `core/dates.py` | 保质期判定顺序（用户日期原样采用）、冲突处理（R-04）、购买日期留空（R-25、R-26）、通用参考区间（R-03）、只用于展示的 AI 估算（R-30）、新鲜度分级 |
| Core | `core/allergens.py` | 归一化、内置别名表、三态判定（R-05 ~ R-08、R-14）；逐批次判定，不跨批次继承声明 |
| Core | `core/allocation.py` | 批次消耗分配顺序（先到期先消耗、未知期限后置）、把一次扣减量分摊到多个批次的逐批次拆分、汇总与批次一致性校验（R-16、R-29） |
| Core | `core/recipe_math.py` | 缺口计算（S5，已实现）：按"归一化名称 + 单位"匹配库存汇总，输出 matched / unit_mismatch / unmatched 与现有量、缺口。未实现 R-27 的份数缩放（阶段 6 范围） |
| Storage | `storage/db.py` | 连接管理、事务上下文、结构初始化入口 |
| Storage | `storage/schema.py` | 结构版本、迁移步骤、DDL |
| Storage | `storage/inventory_repo.py`、`storage/allergen_repo.py`、`storage/recipe_repo.py` | 各实体 SQL |
| AI | `ai/client.py` | OpenAI 兼容客户端：`generate(messages) -> str`，唯一网络出口，一次调用不自动重试；错误映射为 `AIError` 子类（S4） |
| AI | `ai/prompts.py` | 菜谱提示词与 `PROMPT_VERSION`；库存与过敏原由服务层动态注入，提示词里不写死数据（S5） |
| AI | `ai/schemas.py` | pydantic 菜谱契约（`RecipeIngredient` / `Recipe` / `RecipeResponse`，`extra="forbid"`）与 `parse_recipe_response()`（S5） |
| 配置 | `config.py` | 数据库路径、`DUE_SOON_DAYS = 2`、AI 配置（`get_ai_settings()` / `load_env_file()`）；**不保存参考期限数据**（该数据归 `core/dates.py`） |

## 6. 数据模型

### 6.1 inventory_item

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `name` | TEXT NOT NULL | 展示用名称 |
| `name_normalized` | TEXT NOT NULL | 归一化名称，用于与 AI 输出匹配 |
| `category` | TEXT NOT NULL | 类别白名单；参考区间的输入之一 |
| `quantity` | REAL NOT NULL | 剩余数量 |
| `unit` | TEXT NOT NULL | 单位白名单 |
| `purchased_on` | TEXT NULL | ISO 日期；NULL 表示"购买日期未知"（R-25），与"今天购买"是两种不同状态 |
| `purchase_date_defaulted` | INTEGER NOT NULL | 1 表示该日期是系统默认填入当天并由用户确认过的，0 表示用户明确指定 |
| `storage` | TEXT NOT NULL | 冷藏 / 冷冻 / 常温 / 其他 |
| `expiry_date` | TEXT NULL | 仅存放用户提供的日期（或由用户天数换算）。无用户数据时为 NULL |
| `shelf_life_days` | INTEGER NULL | 用户提供的天数原值，用于追溯冲突（R-04） |
| `expiry_source` | TEXT NOT NULL | 正式有效期日期的出处：`user_date`（用户填写的日期）/ `user_days_unresolved`（用户只给了天数、没有正式日期，此时 `expiry_date` 必须为 NULL）/ `user_days`（历史数据：旧版本按天数换算过日期）/ `unknown`（没有任何用户期限数据）。**AI 估算不落库**，因此没有对应取值。展示时以本列为准，不重新推算 |
| `allergen_status` | TEXT NOT NULL | `declared` / `unknown` |
| `declared_allergens` | TEXT NULL | JSON 数组；未声明时保持 NULL 而非空数组 |
| `notes` | TEXT NULL | 备注 |
| `status` | TEXT NOT NULL | `active` / `consumed` / `discarded` |
| `created_at`、`updated_at` | TEXT NOT NULL | ISO 时间戳 |

批次语义：**`inventory_item` 的一行就是一条独立的购买批次**，不再引入额外的批次实体。同一名称多次购买产生多行，互不覆盖（R-15）。汇总库存是查询时的聚合结果：按 `name_normalized` 与 `unit` 分组，对 `status='active'` 的 `quantity` 求和；汇总值必须始终等于其批次之和。

设计理由：`expiry_date` 与参考区间彻底分离，使 R-02 与 R-03 在数据结构层面即不可能被违反；`declared_allergens` 区分 NULL 与空数组，使 R-14 的"未声明"可被区分；`purchased_on` 允许 NULL 使 R-25 的"未知"可被表达；`category` 是参考区间的必要输入。过敏原字段属于单个批次，同名食材的不同批次各自独立声明，判定时逐批次进行，不存在跨批次继承。

### 6.2 inventory_event

字段：`id`、`item_id`、`action`、`qty_delta`、`qty_after`、`source`（manual / recipe_plan）、`plan_id`（可空）、`at`、`note`。

`action` 取值与写入时机（R-23）：

| action | 触发操作 | `qty_delta` 符号 |
|---|---|---|
| `initial_add` | 首次初始化库存 | 正 |
| `purchase_add` | 后续购买入库 | 正 |
| `edit` | 数量编辑 | 正或负（差值） |
| `consume` | 消耗（含确认菜谱用量） | 负 |
| `discard` | 丢弃 | 负 |

`qty_delta` 语义固定为"变更后数量 − 变更前数量"。查询、规划、查看批次不写事件。

产品规格中的六类操作对应下表的 5 个 `action` 值：手动消耗与"确认菜谱用量"都记为 `consume`，通过 `source`（manual / recipe_plan）区分。

审计范围：**所有库存数量变化都必须有事件记录**；上述五类操作只要改变了数量就必须写事件，`qty_delta` 为 0 的操作不写事件。`item_id` 指向具体批次，因此一次跨多批次的扣减会产生多条事件（R-12、R-29）。

用途：库存一致性的审计依据，支撑 R-12，并使"事件变更量之和等于库存变化量"可被测试断言。

### 6.3 recipe_plan 与 recipe_plan_ingredient

> **状态（阶段 5）：这两张表尚未创建**，数据库版本保持 3。S5 只生成并展示临时结果，
> 不做菜谱持久化；下面的字段定义保留为将来需要保存菜谱/确认消耗时的设计依据
> （`quantity_state`、`allergen_state`、`confirmed_qty` 等语义已由 S5 的
> `core/recipe_math.IngredientGap.match_state` 与 `core/allergens` 的三态在内存中实现）。

- `recipe_plan`：`id`、`title`、`servings`、`rationale`、`model`、`prompt_version`、`status`（`draft` / `applied`）、`created_at`、`applied_at`。
- `recipe_plan_ingredient`：`id`、`plan_id`、`item_id`（可空）、`raw_name`、`required_qty`、`available_qty`（可空）、`missing_qty`（可空）、`unit`、`match_state`（`matched` / `unmatched` / `unit_mismatch`）、`quantity_state`（`known` / `unknown`）、`allergen_state`（`conflict` / `unverifiable` / `none_known`）、`allergen_reason`（可空）、`confirmed_qty`（可空）、`user_confirmed`（0 / 1）。

`required_qty` 是**菜谱需求量**（按目标份数缩放后的理论值），`confirmed_qty` 是**用户确认的实际消耗量**，两者是不同的量，允许不相等（R-28）。用户只确认部分食材时，未确认行的 `confirmed_qty` 为 NULL 且 `user_confirmed=0`，其批次不做任何变动。

批次扣减不新增表：每个批次的扣减量由 `core/allocation.py` 计算后直接作用于对应批次行，并通过 `inventory_event`（`item_id` = 该批次、`plan_id` = 本次规划）逐批次留痕（R-29）。

未匹配食材的 `available_qty` 与 `missing_qty` 必须为 **NULL**，不得写 0（R-19）；`quantity_state=unknown` 与 0 是两种不同语义，只有用户确认后才通过 `user_confirmed=1` 参与重新计算。

`quantity_state` 的判定依据是"是否存在可确认该食材的库存记录"，不是"当前是否存在活动批次"：`matched` 且该食材的批次已全部消耗或丢弃 → `quantity_state='known'`、`available_qty=0`、`missing_qty` 按 0 正常计算（已知为零）；`unmatched`（从未匹配到记录）或记录不足以确认 → `quantity_state='unknown'`、两个数量字段为 NULL。若用户已确认实际库存，则按确认结果重算（R-19）。

设计理由：`raw_name` 保留 AI 原文、`item_id` 保留匹配结果，两者并存才能解释"为何算作缺失"；`match_state`、`quantity_state` 与 `allergen_state` 把 R-06、R-07、R-13、R-19 的判定结果固化为可测试的数据；`allergen_reason` 支撑待核实区域的"成分 + 风险原因"展示（R-07）；`status` 是重复应用防护的载体。

### 6.4 user_allergen

字段：`id`、`display_name`、`name_normalized`、`created_at`。是 R-05 硬约束的数据源。

### 6.5 不落库的数据

参考区间与 AI 估算都是纯函数输出：参考区间由 `(category, storage)` 查表得到；AI 估算在此之上
用 `purchased_on` 换算成估算日期区间（缺少购买日期则不生成）。两者都**不写入数据库**。
理由：一旦落库，就有被误读为用户数据或确定到期日的风险（R-03、R-30）。

参考期限数据本身（`(category, storage) → (天数下限, 天数上限, 依据文案)`）作为模块级常量保存在 `core/dates.py`，属于日期领域逻辑；`config.py` 不保存同一份数据的副本，只保存 `DUE_SOON_DAYS = 2` 等固定业务常量与运行参数。

### 6.6 强制规则的落地位置

| 规则 | 落地方式 |
|---|---|
| 用户日期与用户天数冲突（R-04） | `core/dates.py` 在两者结果不一致时采用具体日期作为正式记录（写入 `expiry_date`，`expiry_source=user_date`），保留 `shelf_life_days` 原值，并返回冲突标记供 UI 提示"信息冲突" |
| 参考区间不得伪装（R-03） | 参考区间不写入任何字段，只在渲染时输出**通用**天数区间与依据（不换算成日期）；UI 必须同时渲染"参考信息，非食品安全保证"；无正式期限时新鲜度分级固定为"未知" |
| AI 估算只展示（R-30） | `core/dates.ai_estimate()` 只产出展示文本：没有正式期限且有购买日期时才生成，否则输出"未生成（缺少购买日期…）"；UI 必须与「参考信息」分列并带"不是食品安全保证"；`expiry_date` 永远只存用户填写的日期 |
| 展示以库中来源为准（R-01、R-30） | `services/inventory_service._build_view()` 把 `item.expiry_source` 作为 `stored_source` 传给 `core/dates.build_expiry_view()`，避免把系统换算出来的日期重新标成"用户提供" |
| 成分无法核实不得当作普通推荐（R-07） | `core/allergens.py` 输出 `unverifiable` 时，`services/recipe_service.py` 把该菜谱与普通推荐分流，写入独立区域的展示数据；`recipe_plan_ingredient.allergen_state` 固化该判定 |
| 购买日期留空（R-25、R-26） | `purchased_on` 允许 NULL（任何录入模式都允许留空）；`purchase_date_defaulted` 记录是否采用了界面预填的今天；购买日期为空时不生成 AI 估算，也不做任何推算 |
| 批次独立（R-15） | `inventory_item` 一行即一个批次，入库一律 INSERT；`storage/inventory_repo.py` 提供按 `name_normalized` + `unit` 聚合的汇总查询 |
| 批次分配可解释（R-16） | `core/allocation.py` 输出确定性的分配结果（先到期先消耗、期限未知后置、同序按购买日期与 id）；Services 据此在同一事务内逐批次更新并写事件 |
| 未知不等于 0（R-19） | 未匹配食材的 `available_qty` 与 `missing_qty` 写 NULL 且 `quantity_state=unknown`；所有汇总额外过滤 `quantity_state='known'`。已知为零（记录可确认当前无剩余，含批次全部耗尽）写 0 且 `quantity_state='known'`，照常参与缺口与补购计算，不得因为"没有活动批次"就改写为 unknown |
| 选择后重算（R-17） | `services/recipe_service.py` 在用户选定菜谱时重新读取库存并重算缺口，不使用生成规划时缓存的数量 |
| 事务与一致性（R-22） | `storage/db.py` 暴露单一事务上下文，一次业务操作的所有仓储调用共用同一连接；扣减使用条件更新并校验受影响行数，不引入版本号体系 |
| 用户只填天数（R-26、R-30） | `core/dates.py` 保留 `shelf_life_days`、把 `expiry_date` 置为 NULL、`expiry_source=user_days_unresolved`；有购买日期时另出「AI 估算」文本（不落库），没有购买日期时输出"未生成"；新鲜度分级固定为"未知" |
| 份数缩放精度（R-27） | `core/recipe_math.py` 用单一比例换算全部相关食材，结果四舍五入保留 2 位小数并做有限性与正数校验；计件单位的整数建议由 Python 计算，仅用于展示 |
| 需求量与实际消耗量分离（R-28） | `recipe_plan_ingredient.required_qty` 与 `confirmed_qty` 分别存储；`confirmed_qty` 来自用户输入，Services 只校验不修改 |
| 多批次扣减（R-29） | `core/allocation.py` 把一次扣减量分摊为逐批次的扣减清单；`services/inventory_service.py` 在同一事务内逐批次条件更新并逐条写事件 |
| 过敏原按批次判定（R-14） | `core/allergens.py` 逐批次读取 `allergen_status` 与 `declared_allergens`；任一被引用批次未声明且用户已声明相关过敏原，则该菜谱进入待核实 |

## 7. SQLite 结构演进策略

不引入 ORM，不引入迁移框架，采用"版本游标 + 有序迁移 + 独立的重置操作"：

| 措施 | 说明 |
|---|---|
| 版本游标 | `storage/schema.py` 保存 `SCHEMA_VERSION` 与有序的迁移步骤（每步对应一个版本号）；`storage/db.py` 读取 `PRAGMA user_version`，按版本号顺序只执行高于当前版本的步骤，每步成功后回写版本号 |
| 新增表 | 使用 `CREATE TABLE IF NOT EXISTS` |
| 新增字段 | 先查 `PRAGMA table_info(<表名>)`，缺列才执行 `ALTER TABLE ... ADD COLUMN` |
| 迁移失败 | **报错并停止**：抛出异常、不执行后续步骤、不删除或重建数据库，事务回滚且 `PRAGMA user_version` 保持迁移前的值。迁移失败必须由人工检查后重跑，不得自动降级为"删库重建" |
| 破坏性变更 | SQLite 不支持删列、改列名、改约束，V1 一律不做这类变更；确有必要时改为新增列并保留旧列不使用，而不是删除数据库 |
| 演示数据库重置 | 是**单独的、明确的人工操作**：`python scripts/seed_demo.py --reset` 只清空并重建演示库 `data/freshkeep.db`。它与迁移是两条不同路径，迁移流程中任何位置都不得调用它，迁移失败时也不得自动触发 |
| 演示数据定位 | 演示数据库位于 `data/`，在 `.gitignore` 中排除；其内容声明为可丢弃 |
| 测试隔离 | 测试用 `tmp_path` 创建一次性数据库并建到最新版本；测试绝不读写 `data/freshkeep.db` |
| 升级验证 | 迁移测试**必须验证旧数据得到保留**：用旧版本 DDL 建库、写入若干行数据、升级到最新版本，断言数据行数与内容不变且新列已存在 |
| 失败验证 | 另有一条测试：构造会失败的迁移步骤，断言抛出异常、`user_version` 未推进、原有数据未被删除 |

选择该策略的理由：零额外依赖、行为完全可被 pytest 断言、任何迁移失败都停在报错而不是销毁数据，同时避免迁移框架带来的抽象成本。

## 8. AI 集成与输出校验

1. 调用链固定为 `pages/ → services/ai_service.py → ai/client.py`：界面不接触 SDK，也不读环境变量。
2. `services/ai_service.py` 读取 `config.get_ai_settings()`（按需先加载 `.env`），缺配置时抛 `AIMisconfiguredError`，**不发请求**。
3. `ai/client.py` 的 `generate(messages) -> str`：校验 messages（role 只能是 system/user/assistant、content 非空）→
   用 `openai.OpenAI(api_key, base_url, timeout)` 构造客户端 → 调 `chat.completions.create(model, messages)` →
   只返回文本；空回复抛 `AIEmptyResponseError`；**不自动重试、不打印、不写库**。
4. 错误映射（全部是 `AIError` 子类，且**不回显 `str(exc)`**，避免泄露密钥）：
   `AIMisconfiguredError`（缺 Key/模型/SDK）、`AIInvalidRequestError`（本地输入非法）、`AITimeoutError`（超时）、
   `AIConnectionError`（网络不可达）、`AIAPIError`（服务端错误，带 `status_code`）、`AIEmptyResponseError`（空回复）。
5. 菜谱规划的调用链（S5 已实现）：

   ``` 
   pages/2_食谱规划.py
        → services/recipe_service.plan_recipes()
              → services/inventory_service（读活动批次 / 汇总 / 用户过敏原，服务层不写 SQL）
              → ai/prompts.build_recipe_messages()（本请求的库存与过敏原动态注入）
              → services/ai_service.generate_text() → ai/client.AIClient（唯一网络出口）
              → ai/schemas.parse_recipe_response()（结构校验，失败即整份拒绝）
              → core/allergens（确定性三态判定：conflict / unverifiable / none_known）
              → core/recipe_math.compute_gaps()（现有量与缺口由程序算）
   ```

   分区结果：`conflict` → 已排除；`unverifiable` → 待核实区域（不作为普通推荐）；
   `none_known` → 普通推荐。**S5 不写库存、不写事件、不落库**（菜谱只存在于会话中）。
6. `core` 在发送前清洗名称（长度限制、去除控制字符），降低提示注入风险。
7. 校验分两层（R-21）：
   - 响应级：`ai/schemas.py` 使用 pydantic 严格校验整份响应（字段齐全、禁止多余字段、类型正确）。不符合即拒绝整份响应（R-13）。
   - 菜谱级：通过响应级校验后，由 Services 逐道菜谱做业务校验（数量为有限正数、推荐理由非空、至少含一条有效食材）。不合格的菜谱被丢弃，其余合法菜谱保留并提示。
8. 库存匹配、数量重算、缺失量计算与过敏原分流全部由 Services 配合 `core` 完成；AI 不参与数量计算，也不能决定过敏原结论（R-05、R-10）。
9. 未匹配食材、食材不足、单位不一致属于业务处理，**不得**因此判定整份 AI 响应失败（R-19）；AI 调用本身失败时显示错误并保持库存不变。

## 9. 错误处理

| 层 | 处理方式 |
|---|---|
| Core | 返回明确的结果对象或抛出可读异常，不打印、不静默吞掉 |
| Services | 捕获 AI 与数据库异常，转换为用户可读错误，并保证事务回滚 |
| Storage | 依赖 SQLite 事务；条件更新配合受影响行数校验，行数不符即视为冲突 |
| UI | 只负责渲染错误文本，不自行编造提示 |

## 10. 配置项

`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`、`AI_TIMEOUT_SECONDS`、`DATABASE_PATH`、`DUE_SOON_DAYS`。

AI 配置（阶段 4）由 `config.get_ai_settings()` 统一读取：`OPENAI_API_KEY`、`OPENAI_BASE_URL`（可空，
表示用 SDK 默认地址）、`OPENAI_MODEL`、`AI_TIMEOUT_SECONDS`（默认 30，非法值回退默认）。
缺少 Key 或模型名时**不阻止应用启动**，只在真正调用模型时报 `AIMisconfiguredError`；
`.env` 由 `config.load_env_file()` 用 `python-dotenv` 按需加载（未安装或缺文件都返回 False，不抛异常），
调用入口是 `services/ai_service.load_env_if_present()`。

`DUE_SOON_DAYS = 2` 是固定业务常量，不提供用户配置功能，且只用于基于用户提供日期或天数计算出的期限；参考期限不得用于确定性分级（R-03）。

参考期限数据位于 `core/dates.py`（领域数据），**不在配置文件里重复保存**，也不通过环境变量覆盖。

密钥只从环境变量或 `.env` 读取，不写入代码与仓库。

## 11. 测试策略

| 层次 | 范围 |
|---|---|
| 单元测试 | Core 的校验、日期判定与购买日期留空、参考区间、AI 估算、过敏原三态、批次分配顺序、缺失量与份数缩放计算 |
| 界面测试 | `tests/test_fridge_page.py`：用 Streamlit `AppTest` 驱动真实页面，断言购买日期可留空、AI 估算单独展示、有效期保持未知、冲突提示在刷新后仍可见 |
| AI 接入测试 | `tests/test_ai_client.py`：配置读取（含非法超时回退）、缺配置错误、请求参数透传、文本提取、空回复、超时/连接/API 错误映射、密钥不出现在错误信息与描述里；全部用假 SDK 与注入客户端，不联网、不需要真实 Key |
| 菜谱契约测试 | `tests/test_recipe_schemas.py`：合法 JSON 解析、缺字段/多余字段/非法 JSON、空名称与空列表、非法用量与份数（含布尔与小数）、代码块包裹、可读错误 |
| 缺口计算测试 | `tests/test_recipe_math.py`：充足 / 不足 / 零库存 / 批次耗尽（已知为零）/ 同名不同单位 / 未匹配（未知≠0）/ 非法数量 / 不修改库存 / 计数 |
| 规划服务测试 | `tests/test_recipe_service.py`：成功路径、模型的"库存充足"说法不覆盖程序计算、输出不合法、模型失败、缺配置不调用模型、库存读取失败、空库存不调用模型、三种过敏原分流、规划前后库存与事件完全不变 |
| 规划界面测试 | `tests/test_recipe_page.py`：页面加载、未配置时提示且不生成、成功展示菜谱与缺口、失败不伪装成功、结果跨刷新保留且不重复调用模型 |
| 服务测试 | Services 的事务行为、AI 失败路径、库存不变性 |
| 迁移测试 | 结构版本推进、旧库升级保数据、测试库隔离 |
| 端到端测试 | 录入 → 规划 → 确认消耗 → 库存变化，使用 FakeClient，不调用真实 API |

必须覆盖的关键断言：

- 无用户保质期输入时，正式保质期字段恒为空，且新鲜度分级不返回"新鲜"（R-02）。
- 用户日期与天数冲突时，记录采用日期并产生冲突提示（R-04）。
- 参考区间命中与未命中两条路径；输出不含确定到期日、始终带"非食品安全保证"标注（R-03）。
- 过敏原三态：别名命中、复合调味料、未声明成分（R-06 ~ R-08、R-14）。
- 成分无法核实的菜谱出现在待核实区域，且不在普通推荐列表中（R-07）。
- 规划前后 `inventory_item` 表逐行完全一致（R-09）。
- 批量扣减的原子性、重复应用被拒、事件变更量之和等于库存变化量（R-12）。
- 非法 AI 输出全部被拒绝（R-13）。
- 同名食材多次入库产生多条独立批次，汇总库存恒等于批次之和，且汇总不会把不同单位合并（R-15）。
- 批次分配规则可复现：先到期先消耗、期限未知的批次排在已知期限之后、不伪造到期日期（R-16）。
- 购买日期留空时 `purchased_on` 为 NULL 且 `purchase_date_defaulted = 0`（R-25）。
- 未匹配食材的 `available_qty` 与 `missing_qty` 为 NULL 且不进入缺口与补购汇总（R-19）。
- 同名食材的批次全部消耗或丢弃后：`quantity_state='known'`、现有量为 0、缺口等于需求量，正常给出补购建议；只有从未匹配到记录时才为 unknown（R-19）。
- 用户选择菜谱后按最新库存重算：库存变化后缺口随之变化（R-17）。
- 份数缩放后所有相关食材按比例同步变化，非法数量被拒绝（R-18）。
- 整份响应级校验失败被拒绝；单道菜谱业务错误只丢弃该菜谱且其余保留（R-21）。
- 确认消耗时库存不足则整体回滚，库存与事件表均无变化（R-22、R-24）。
- 只填天数时：保留 `shelf_life_days`、`expiry_date` 为 NULL、`expiry_source='user_days_unresolved'`、新鲜度分级为"未知"，且不出现任何按天数换算出来的正式日期（R-26、R-30）。
- AI 估算不落库：有正式期限时不生成估算；无购买日期时不生成估算日期；估算文本必须带"AI 估算"与免责说明（R-30）。
- 展示来源与库中 `expiry_source` 一致（含历史 `user_days` 行不得被标成"用户提供"）；日期/天数冲突提示在刷新后仍可见（R-01、R-04）。
- 份数缩放：所有相关食材按同一比例换算，结果保留 2 位小数；计件单位另给出整数建议但不改写记录值；非法缩放值被拒绝（R-27）。
- 需求量与实际消耗量分离：用户只确认部分食材时，未确认食材对应的批次数量完全不变（R-28）。
- 多批次扣减：一次扣减跨多个批次时逐批次正确扣减并逐条写事件，事件 `qty_delta` 之和等于扣减总量；任一批次失败则整体回滚（R-29）。
- 过敏原按批次判定：同名食材中只要有一个被引用批次未声明成分且用户已声明相关过敏原，该菜谱即进入待核实区域（R-14）。
- 迁移失败：抛出异常、`user_version` 不推进、原有数据不被删除（第 7 节）。

每个模块必须有对应测试文件；无法被测试断言的抽象不写。

## 12. 阶段文件白名单

| 阶段 | 新增 | 修改 |
|---|---|---|
| 0 | `README.md`、`docs/product_spec.md`、`docs/architecture.md` | 无 |
| 1 | `app.py`、`config.py`、`storage/{__init__,db,schema}.py`、`pages/{1_数字冰箱,2_食谱规划}.py`、`requirements.txt`、`.env.example`、`.gitignore`、`tests/{conftest,test_db,test_schema_migration}.py` | `README.md` |
| 2 | `core/{__init__,models,validation,allocation}.py`、`services/{__init__,inventory_service}.py`、`storage/inventory_repo.py`、`tests/{test_validation,test_inventory_service,test_allocation}.py` | `pages/1_数字冰箱.py`、`storage/schema.py` |
| 3 | `core/{dates,allergens}.py`、`storage/allergen_repo.py`、`tests/{test_dates,test_allergens,test_fridge_page}.py` | `services/inventory_service.py`、`pages/1_数字冰箱.py`、`storage/schema.py`、`storage/inventory_repo.py`（批次入库必须能写 `expiry_date` / `shelf_life_days` / `declared_allergens`，见 6.1）、`config.py` |
| 4 | `ai/{__init__,client}.py`、`services/ai_service.py`、`tests/test_ai_client.py`、`scripts/check_ai_connection.py` | `config.py`、`.env.example`、`requirements.txt` |
| 5 | `core/recipe_math.py`、`ai/{prompts,schemas}.py`、`services/recipe_service.py`、`tests/{test_recipe_schemas,test_recipe_math,test_recipe_service,test_recipe_page}.py` | `pages/2_食谱规划.py`、`requirements.txt`（显式声明 pydantic）、`app.py`（首页文案） |
| 6 | `scripts/seed_demo.py`、`tests/{test_apply_plan,test_e2e_flow}.py` | `services/{inventory_service,recipe_service}.py`、`pages/1_数字冰箱.py`、`pages/2_食谱规划.py`、`README.md`、`requirements.txt` |

S4 说明：`ai/schemas.py`、`ai/prompts.py` 与 `tests/test_ai_schemas.py` 推迟到 S5（S5 才需要结构化输出校验与提示词），
以免留下空模块；`services/ai_service.py` 与 `tests/test_ai_client.py` 取代了原列表中的占位命名。

S5 说明：原列表里的 `storage/recipe_repo.py` 与 `storage/schema.py` **没有创建/修改**——现行产品需求不要求持久化菜谱，
因此本阶段只生成与展示临时结果，数据库版本保持 3，也没有新增迁移。

超出白名单的文件必须先修订计划并获得批准。按上表白名单逐项相加，V1 结束时**非测试 Python 文件不超过 25 个**（含 `__init__.py` 与 `scripts/` 下的脚本），**测试文件不超过 14 个**（含 `conftest.py`）；这两个上限与白名单一致，不额外留余量。

## 13. 依赖

| 依赖 | 必要性 |
|---|---|
| `streamlit` | 唯一 UI 框架，单文件即可产出可演示界面 |
| `openai` | 支持 `base_url` 指向 OpenAI 兼容端点，避免手写 HTTP 封装 |
| `pydantic` | AI 输出强校验是硬规则（R-13），提示词无法替代 |
| `python-dotenv` | 密钥不进入代码与仓库 |
| `pytest`（阶段 6 可选 `pytest-cov`） | 全部业务规则的可验收手段 |
| SQLite（标准库） | 零安装、具备真实事务，匹配 R-12 |

不引入：SQLAlchemy、alembic、langchain、chromadb、pandas、numpy、freezegun、fastapi、APScheduler、plotly。

## 14. 阶段收尾要求

每个阶段结束时必须依次提交：实际新增或修改的文件清单 → 关键差异 → 实际执行的测试命令与结果（含未通过项）→ 未完成项与已知风险 → 声明等待审核。未经批准不得进入下一阶段。
