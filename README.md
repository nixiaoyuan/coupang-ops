# Coupang Ops

`0.2.0` 提供四店 WING 只读采集和五项运营分析：最近30天销量 / 销售额排名、7天销量下滑、库存覆盖、在库零成交、同发货批次退货比例。主要持久化为用户授权的 ERP MySQL 新表 `coupang_ops_sales_snapshot`；中文报告、CSV / JSON 和 Excel 保留为本机查看与可选备份。

WING 已实现的接口直接提供销量、GMV 和流量字段，净口径仍未确认。库存覆盖、滞销和退货分析仍需完整的产品、可售库存、实际发货、非取消交易和完成退货导出；缺少来源时会显示缺少数据。写入 MySQL 不会补出这些缺失来源。完整字段、覆盖声明和命令见 [v2 接入说明](skills/coupang-ops/references/api-integration.md)。

## 安装 skill 或插件

在 Codex 中使用安装技能：

```text
$skill-installer 从 https://github.com/nixiaoyuan/coupang-ops 的 main 分支安装 skills/coupang-ops。
```

安装后在新的聊天中调用 `$coupang-ops`。远程安装取得 GitHub 当时已提交的版本；本机修改不会自动上传。

本仓库也提供 `Coupang Ops` 插件包，描述位于 `plugin.json` 与 `.codex-plugin/plugin.json`，本地来源位于 `.agents/plugins/marketplace.json`。在支持这些插件命令的 Codex 中可运行：

```text
codex plugin marketplace add nixiaoyuan/coupang-ops --ref main
codex plugin add coupang-ops@coupang-ops-marketplace
```

本地仓库试用可把第一行换为 `codex plugin marketplace add .`。本地来源和 GitHub 分发不等于公开插件目录上架。参见 [OpenAI 插件打包文档](https://developers.openai.com/plugins/build/plugins)。

## 先运行合成演示

在仓库目录运行 Python 3.10+：

```text
python skills/coupang-ops/scripts/sync.py demo --output-dir outputs/v2-demo --render
```

四个 `DemoStore1` 至 `DemoStore4` 都是本地生成的合成店，每店五个 SKU，覆盖完整30天和两段7天窗口。演示包含低库存、销量下降、上架已满30天且近30天确实零成交的在库商品，以及部分退货比例超过10%的发货批次。它不会访问店铺、写 ERP 数据库或发送通知。打开生成的 `report.md` 和可选备份 `coupang-data.xlsx`；未找到 Codex Excel 运行时时，CSV / JSON / 报告仍可使用，工作簿状态会说明原因。

旧版五个 SKU、14天 CSV 演示保持可用：

```text
python skills/coupang-ops/scripts/analyze.py --demo --output-dir outputs/demo
```

旧版示例报告见 [examples/demo-report.md](examples/demo-report.md)。真实每日 CSV 仍按 [CSV 字段口径](skills/coupang-ops/references/csv-schema.md) 映射后使用 `analyze.py`。

## 接入授权店铺并更新

把已授权的请求抓包放在本机 `private/`，不要粘贴 Cookie、数据库密码或应用密钥到聊天。将示例 ERP 路径替换为已授权的本机目录：

```text
python skills/coupang-ops/scripts/sync.py init --capture private/wing-capture.txt --config private/coupang-config.json --erp-config "C:\path\to\authorized-erp"
python skills/coupang-ops/scripts/sync.py resolve-recipient --config private/coupang-config.json
python skills/coupang-ops/scripts/sync.py run --config private/coupang-config.json --output-dir outputs/live --save-db --notify --skip-workbook
```

`init` 从各请求自己的 Cookie / XSRF 头提取店铺，不连接 WING。`resolve-recipient` 在已授权的 ERP 配置基础上，只读精确解析接收人；它不发送消息。`run` 查询三个完整销售期间、校验全部分页，生成五区报告。`--save-db` 或私有配置中的 `mysql_enabled=true` 启用 ERP MySQL 持久化：保存三窗口的 SKU 指标、允许业务字段的返回 JSON 以及店铺刷新状态，只写专用新表。连接凭据每次从 `dingtalk_erp_path` 指定的同一 ERP 静态配置读取，不复制到公开仓库。

补充导出默认读取 `private/extras/<店铺ID>.json`，可用 `--extras-dir` 指定目录。认证或数据校验失败时会记录安全状态；数据库保留旧指标并标为 `stale`，本机保留的上次成功数据也不能代表本次成功。Excel 是可选本机备份，`--skip-workbook` 可省略；需要工作簿预览时去掉该参数并添加 `--render`。

`--notify` 通过用户已有 ERP 企业内部应用发送个人工作通知，使用明确绑定的单个 userId，随后查询异步投递结果；受理不等于送达。它不使用群机器人，也不自动查找其他接收人。接口定义及配置边界见 [钉钉个人工作通知说明](skills/coupang-ops/references/dingtalk-setup.md)。不需要通知时省略 `--notify`。

用户要求每30分钟更新时，由 Codex automation 工具实际创建或更新当前聊天的 heartbeat，并核验返回状态；每次执行 `sync.py run --save-db --notify`，可同时使用 `--skip-workbook` 省略备份。安装或调用技能本身不会自动创建任务；仓库没有 Windows cron 替代调度。WING 抓包接口依赖有效授权和当前会话，不能保证持续可用。

## 运行环境和私有数据

采集、分析与通知核心仅使用 Python 3.10+ 标准库，金额使用 Decimal。ERP MySQL 持久化与数据库 userId 查询使用本机已有 `pymysql`，不会自动安装。可选 Excel 备份使用 Codex bundled Artifact Tool 和现有 Node.js 运行时，无需 npm 下载；可通过 Codex `load_workspace_dependencies` 找到依赖路径。

`private/` 和 `outputs/` 已被 Git 忽略。GitHub 仅分发代码与合成演示，不提交真实 Cookie、导出数据、数据库配置或 AppKey / AppSecret。原始源文件保持不变；在 Codex 中读取私人数据仍受所用模型和工具的数据处理设置约束。

## 验证

```text
python -m unittest discover -s tests -v
```

测试使用合成数据和 fake HTTP，覆盖完整期间、分页、隔离店铺、精确金额、缺失值、阈值、发货批次匹配及通知状态，不依赖真实店铺或账号。
