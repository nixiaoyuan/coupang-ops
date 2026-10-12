# v2 只读接入与数据契约

接口与规则的维护来源是 [wing_client.py](../scripts/wing_client.py)、[operations.py](../scripts/operations.py)、[mysql_sink.py](../scripts/mysql_sink.py) 和 [sync.py](../scripts/sync.py)。主要持久化为已授权的 ERP MySQL 专用新表，本机报告和表格用于查看或备份。完整合成示例由 [demo_operations.py](../scripts/demo_operations.py) 的 `build_demo_bundle(analysis_date)` 生成，避免维护另一份重复 JSON。每个 bundle 含 `datasets` 列表和按店铺 ID 索引的 `extras`。

## 命令与输出

从仓库目录运行；安装到 skill 后把脚本前缀换成该 skill 的绝对路径：

```text
python skills/coupang-ops/scripts/sync.py init --capture private/wing-capture.txt --config private/coupang-config.json --erp-config "C:\path\to\authorized-erp"
python skills/coupang-ops/scripts/sync.py resolve-recipient --config private/coupang-config.json
python skills/coupang-ops/scripts/sync.py run --config private/coupang-config.json --output-dir outputs/live --save-db --notify --skip-workbook
python skills/coupang-ops/scripts/sync.py demo --output-dir outputs/v2-demo --render
```

`init` 只读本机抓包，逐个 Request URL 分组提取本组的 Cookie、X-XSRF-TOKEN 和 User-Agent，并以 Cookie 中 `sc_vid` 去重。已有配置需显式 `--replace` 才会覆盖。配置及抓包必须留在忽略的 `private/` 中，不能打印凭据。`resolve-recipient` 是独立的 ERP 只读查询，不发送通知；可选依赖为本机已有 `pymysql`。补充导出默认位于 `private/extras/<店铺ID>.json`，`--extras-dir` 可改目录。分析日默认使用运行当日的 Asia/Seoul 日期，可用 `--analysis-date YYYY-MM-DD` 指定已完整结束的期间。

本机输出包括 `coupang-data.csv`、`datasets.json`、`summary.json`、`report.md`、`status.json` 和刷新历史。Excel `coupang-data.xlsx` 是可选备份，`--skip-workbook` 可省略；去掉该参数并使用 `--render` 请求预览。Excel 使用现有 Codex bundled Artifact Tool / Node.js，不下载 npm 包。仅在用户已授权发送的范围内使用 `--notify`；demo 不写 ERP 数据库或发送通知。

## 私有配置 schema_version 2

| 字段 | 类型 / 约束 |
| --- | --- |
| `schema_version` | 整数 `2` |
| `stores` | 独立店铺配置列表；每项含 `store`、`cookie`、`xsrf_token`、`user_agent`；可选 `currency` 仅为 `KRW` |
| `dingtalk_erp_path` | 用户授权读取的本机 ERP 目录或配置路径 |
| `mysql_enabled` | 明确布尔；`true` 启用专用表持久化，`--save-db` 也可为单次运行启用；init 默认 false |
| `recipient_name` | 明确的个人接收人；初始配置使用倪笑缘 |
| `dingtalk_user_ids` | 可选，已核验的单个 userId 列表；接收人解析成功后写入 |
| `request_interval` | 请求间隔秒数，默认 `0.5`，有限值 `0` 至 `60` |
| `network_retries` | 仅连接超时或连接中断的同页只读重试，默认 `2`，整数 `0` 至 `3`；认证、限流、HTTP 和结构错误不重试 |
| `max_pages` | 每窗口最大分页数，默认 `200`，整数 `1` 至 `1000` |
| `thresholds` | 可选阈值覆盖，键与默认值见下方 |
| `node_path` / `node_modules_path` | 可选的现有 Codex bundled 运行时路径；通常自动定位 |

Cookie 的 `sc_vid` 必须与配置店铺和每行 `vendorId` 一致。XSRF 头必须等于 Cookie `XSRF-TOKEN` 的百分号解码值；不能把 `+` 当空格。凭据仅发往已验证的固定 WING HTTPS 目标，禁用重定向，使用正常 TLS 校验。未知目标、401 / 403、登录 HTML、429、结构错误或缺页均停止该次采集。仅连接超时或中断可以按上限重新读取同一页，耗尽后停止；不伪造完整结果。当前已捕获接口为 WING business-insight 的 `vi-detail-search`；它是会话相关接口，不是持续可用承诺。不要猜测其他库存、退货或成本端点。

## ERP MySQL 主要持久化

`mysql_enabled=true` 或 `run --save-db` 启用 [mysql_sink.py](../scripts/mysql_sink.py)。使用已有 `pymysql`，静态读取 `dingtalk_erp_path` 指定的同一 ERP 数据库配置，不执行 ERP 代码，不复制或打印数据库凭据。用户授权的写入范围仅为 `coupang_ops_sales_snapshot`；表不存在时创建该新表，已存在时核验专用表归属和结构，不改已有 ERP 业务表。

每个店铺、窗口和 SKU 保存完整三窗口中的允许业务字段：销量 / GMV、流量 JSON、商品详情与指标返回 JSON、期间和采集时间；`response_json` 不含 Cookie、XSRF 或授权头。另存每店一条刷新状态记录，包含尝试时间、刷新状态与完整性。准确列、主键及金额精度以模块中的 `CREATE_SQL` / `SCHEMA` 为准，避免文档复制一套 SQL。

只有完整且新鲜的三窗口数据写入 SKU 指标。取数失败店铺保留旧指标及原采集时间，标为 stale 并更新该次状态；不会把旧数据冒充本次成功。每店独立事务，SQL 执行失败尝试回滚；提交确认丢失时标为 `unknown / commit_outcome_unknown`，不声称已回滚或立即重发。若前面的店已确认提交，结果明确报告该范围。表按店铺、窗口、SKU 保存最新快照；已不在本次结果的旧 SKU 保留并设 `is_current=0`，它不是跨采集时间的全量历史事件表。检查 `status.json` 的 `database.status` 和安全原因核验写入，不能因为报告或通知生成就宣称保存成功。WING 目前仅直接提供销量 / GMV / 流量，净口径未确认；数据库不会补齐另外三项分析所缺的导出源。

后台汇总销量、订单数和 GMV 允许负数，并原样保存；这不是实际发货件数。取消、退款及日期归属的具体扣减口径未确认，不再次扣减，也不能推断负数的具体原因。访客、浏览、点击、搜索以及补充来源的实际库存、发货、退货件数仍要求非负。销售额分母不大于零时占比未知；正分母中的负调整行可以出现负占比。

## 标准化销售 dataset

| 字段 | 定义 |
| --- | --- |
| `store`, `timezone`, `currency` | 单店铺 ID、固定 `Asia/Seoul`、固定 `KRW`；不混店、不混币种 |
| `analysis_date` | `YYYY-MM-DD`；运行的韩国分析日 |
| `observed_at` | 带 UTC 偏移的 ISO 秒级时间，不早于分析日 |
| `sales_basis` | `platform_reported`；只有提供可核验净口径时才能用 `net_confirmed` |
| `sales_basis_note` | 非空字符串，说明取消退款、归属日期等已知或未确认口径 |
| `sales_windows` | `last_30d`、`current_7d`、`previous_7d` 三个完整期间 |
| `raw_windows` | 可选，同名窗口对应仅含业务字段的原始行列表，不含请求头或凭据 |

若分析日为 D：30天窗口为 D-30 至 D-1，本期7天为 D-7 至 D-1，上期7天为 D-14 至 D-8。每窗含 `start_date`、`end_date`、明确布尔 `complete`、`rows`，采集结果另含 `captured_at`。`complete=true` 表示整个店铺该窗口及所有分页已完整校验。`zero_row_omission_confirmed` 可选，默认 false；仅在完整来源确证遗漏行代表零时才启用，不能用它弥补缺页。

销售行必含 `sku`、`name`、非负整数 `units`、有限非负十进制字符串 `amount`。可选 `traffic` 使用 `totalOrders`、`totalUniqueVisitor`、`totalPageViews`、`srpClick`、`searchVolume`、`pvToOrder`，只作描述。采集行另含 `registration_type`、布尔 `is_oos`、`inventory_id`。`is_oos` 或 inventory ID 不等于可售库存数量；平台销量不等于实际发货数。

采集器核对稳定的 `pageSize` / `pageNumber` / `totalResults` / `totalPages`、逐页行数、最终总行数、重复 SKU、同店身份和数值。缺字段不补零。业务行不能按商品名合并，金额不能先变成浮点数。

## extras：另外三项分析的来源

顶层可含 `store`、`timezone`、`currency`；提供时必须匹配 dataset。列表中的记录也可含这些上下文字段且必须一致。按 SKU 或源行 ID 唯一，不把重复行自动聚合。

| 列表 | 必需行字段 | 可选行字段 |
| --- | --- | --- |
| `products` | `sku`, `name`, `first_listed_at`（日期） | `last_deal_at`（日期或 null）, `unit_cost`（金额字符串或 null） |
| `inventory` | `sku`, `available_units`（非负整数）, `snapshot_at`（带偏移 ISO 时间） | 来源说明 |
| `shipments` | `shipment_line_id`, `order_id`, `sku`, `shipped_at`（带偏移 ISO 时间）, `shipped_units`（实际非负整数件数） | 来源费用金额字符串 |
| `transactions` | `transaction_line_id`, `sku`, `occurred_at`（带偏移 ISO 时间）, `units`（非取消成交件数） | 来源费用金额字符串 |
| `returns` | `return_line_id`, `shipment_line_id`, `completed_at`（带偏移 ISO 时间）, `returned_units` | `reason`（字符串或 null）；退款金额字符串 |

`coverage` 含明确布尔 `products_complete`、`inventory_complete`、`shipments_complete`、`transactions_complete`、`returns_complete`。发货和成交另需 `shipments_start_date` / `shipments_end_date`、`transactions_start_date` / `transactions_end_date`。完整发货须覆盖最近30天批次与最近14天需求；完整非取消成交须覆盖最近30天。`returns_complete` 表示在观察截止前的完成退货来源完整，退货必须逐行匹配发货，且累计件数不超过该发货行。

库存快照不得晚于 `observed_at`；上架和末次成交日期必须在合理先后范围内。缺失列表或覆盖声明时，相关区为 `missing_data` / `incomplete`；已完整匹配的退货区仍为 `provisional`，因为近期批次观察期较短。未知退货原因保留为未知，不按销售窗口强行归属。

默认阈值：上期销量至少20件且降幅严格大于30%；库存覆盖严格小于7天；在库、上架至少30天且完整30天非取消成交为零；同30天发货批次至少20件且完成退货比例严格大于10%。对应配置键为 `previous_min_units=20`、`drop_fraction=".30"`、`stock_days="7"`、`listed_days=30`、`return_min_shipped=20`、`return_fraction=".10"`。

## 通知、调度与发布

通知复用用户已授权的 ERP 企业内部应用，绑定明确个人，不用群机器人。接口依据与送达核验见 [钉钉配置说明](dingtalk-setup.md) 和其官方文档链接。只发送概览、更新时间及安全状态，不发送完整导出或凭据。异步 `accepted` 需要单独核验送达。

只有用户请求定时运行时才用 Codex automation 工具实际创建或更新当前聊天的每30分钟 heartbeat，并核验返回状态。每次执行 `sync.py run --save-db --notify`，可添加 `--skip-workbook` 省略可选备份；技能触发不会自行创建调度，不使用 Windows cron 替代。真实抓包和导出放在 `private/`，本机结果放在 `outputs/`；数据库配置与 AppKey / AppSecret 留在现有私有 ERP 配置，不上传 Git。GitHub 只提交代码和合成示例；发布、实际 API 访问或数据库保存成功必须有对应核验。
