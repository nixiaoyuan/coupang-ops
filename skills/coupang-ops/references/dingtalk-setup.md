# 钉钉个人工作通知配置

本项目通过企业内部应用发送个人工作通知。接收人固定为明确绑定的倪笑缘 userId；不使用群机器人、部门列表或全员通知。

## 已有 ERP 配置

用户授权静态复用 `C:\fangzhou1\fangzhou-erp-backend` 的原配置。`config/config.py` 中已有 `APPKEY`、`APPSECRET`、`AGENT_ID`。加载器通过 Python AST 读取字面量，不导入或执行 ERP 源码，也不修改 ERP 源码或配置。另行授权的数据库持久化只写专用新表 `coupang_ops_sales_snapshot`，详见 [v2 接入说明](api-integration.md)。

`database_db/ozon_sales_notification.py` 的 `RECIPIENT_NAME` 为倪笑缘，支持 `OZON_SALES_DINGTALK_USER_ID` 环境变量。ERP 的数据库回退按 `nick_name` 或 `user_name` 精确查询有效用户，并要求恰好一行。配置加载器不自行查询数据库；已授权的配置步骤可显式调用 `resolve_erp_user_id(path, recipient_name="倪笑缘")`，它静态读取 ERP 数据库配置，用现有 `pymysql` 执行带参数的只读查询 `LIMIT 2`，连接超时 5 秒、读取超时 10 秒。依赖缺失时返回 `missing_config`，不自动安装。成功返回 `status=resolved`、`recipient_name`、`user_id`；失败仅返回安全固定原因。不得使用其他通知的接收人列表来猜测。

`load_erp_config(path, recipient_name="倪笑缘", recipient_user_ids=None)` 支持 ERP 目录中的 Python 字面量配置、JSON、普通 `.env` 和 `.properties`。`sync.py resolve-recipient` 将只读核验得到的单个 userId 写入 `private/coupang-config.json` 的 `dingtalk_user_ids`，随后传入通知加载器；应用凭证仍每次从 `dingtalk_erp_path` 指向的 ERP 读取，无需复制 AppSecret。应用或接收人缺失、存在多个不同匹配、同一名字绑定多个 userId 时，返回 `missing_config`。它不会选择第一个人，也不会降级为群推送。

成功结果是含 `app_key`、`app_secret`、`agent_id`、`user_ids` 和 `recipient_name` 的进程内字典。不要打印该字典。应用和数据库凭据留在原有私有 ERP 配置；Coupang 私有配置只保存 ERP 路径、认证头与已核验接收人等必要设置，并留在已被 Git 忽略的 `private/` 中。提交到仓库的配置只能含示例占位符。

私有 JSON 的结构如下，实际凭证不能填写在公开文档中：

```json
{
  "app_key": "<本机读取的应用标识>",
  "app_secret": "<本机读取的应用密钥>",
  "agent_id": 123,
  "recipient_name": "倪笑缘",
  "user_ids": ["<经确认的单个 userId>"]
}
```

## 发送与核验

使用 [企业内部应用 access_token](https://open.dingtalk.com/document/orgapp/obtain-orgapp-token) 换取应用凭证，然后调用 [发送工作通知](https://open.dingtalk.com/document/orgapp/asynchronous-sending-of-enterprise-session-messages)。固定请求地址分别为 `api.dingtalk.com/v1.0/oauth2/accessToken` 与 `oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2`。官方 [OAuth SDK 源码](https://github.com/alibabacloud-go/dingtalk/blob/master/oauth2_1_0/client.go) 确认 POST 路径、`appKey`、`appSecret` 请求字段和 `accessToken` 响应字段；[阿里开放平台的工作通知接口](https://developer.alibaba.com/docs/api.htm?apiId=37692) 展示 `agent_id`、`userid_list`、`to_all_user` 和 `task_id` 字段。

`send_work_notice(config, title, text, transport=None)` 成功仅返回 `{"status":"accepted","task_id":...}`。异步接口受理不代表送达。发送请求只提交一次；超时或缺少任务 ID 返回 `unknown`，不得自动重发同一条消息。

`verify_delivery(config, task_id, transport=None)` 先查询 [发送进度](https://open.dingtalk.com/document/orgapp/obtain-the-sending-progress-of-asynchronous-sending-of-enterprise-session)，进度完成后查询 [发送结果](https://open.dingtalk.com/document/orgapp/gets-the-result-of-sending-messages-asynchronously-to-the-enterprise)。只有确切 userId 出现在已读或未读接收列表、且没有出现在失败或禁止列表，才返回 `delivered`。`read=false` 表示钉钉报告已发送但未读，不表示对方已阅读。

该模块只负责通知传输与核验；每30分钟调度由用户请求创建的 Codex heartbeat 负责，每次运行 `sync.py run --save-db --notify`。数据库 `coupang_ops_sales_snapshot` 是主要持久化，Excel 仅为可选本机备份，可添加 `--skip-workbook`。通知可包含销量概览、查询时间、认证及数据库保存状态；通知送达不证明数据库保存成功。当前 API 直接返回销量、GMV 和流量，净口径未确认，库存覆盖 / 滞销 / 退货仍受缺失补充导出限制。

403 或验证页面不单独证明 Cookie 过期。不得包含 Cookie 内容、授权头、AppSecret 或 access_token。HTTP 目标固定为钉钉官方域名和路径，禁用重定向及环境代理，响应错误仅返回安全状态和数字错误码，不记录请求 URL、正文或原始错误文本。技能调用本身不创建 heartbeat，也不使用群机器人或 Windows cron 替代。

仅检查配置不会发送测试通知。离线测试全部使用 fake HTTP：

```text
python -m unittest discover -s tests -p test_dingtalk_notify.py
```
