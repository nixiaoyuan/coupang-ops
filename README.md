# Coupang Ops

把授权导出的销售和库存数据转成商品排名、销量变化、退货观察和补货提示。当前演示使用**完全虚构的 5 个 SKU、14 天数据**，不需要账号，也不会连接 Coupang 后台。

## 作为 Codex 插件使用

本仓库同时提供独立 skill 和 `Coupang Ops` 插件包。插件版本为 `0.1.0`，包含现有 skill、分析脚本、字段说明和模拟数据；需要 Python 3.10+。插件描述位于根目录 `plugin.json`，并提供 `.codex-plugin/plugin.json` 兼容描述。本地来源目录位于 `.agents/plugins/marketplace.json`。

在本机安装这个仓库中的插件：

```text
codex plugin marketplace add .
codex plugin add coupang-ops@coupang-ops-marketplace
```

在 Codex 的“插件”页面选择 `Coupang Ops Plugins` 来源，可查看插件及安装状态。如果当前界面尚未刷新，完成手头任务后重启 Codex。开始新的聊天，选择 Coupang Ops 插件下的技能，再输入：

> 用自带模拟数据生成中文运营分析报告，保存到当前项目的 outputs/demo。

如果本机同时安装了独立的 `$coupang-ops`，请在选择器中确认此次选中的是插件内的技能。独立 skill 的调用方式仍然有效。

将插件描述和 marketplace 文件提交到 GitHub 后，其他人在支持这些插件命令的 Codex 中可以执行：

```text
codex plugin marketplace add nixiaoyuan/coupang-ops --ref main
codex plugin add coupang-ops@coupang-ops-marketplace
```

上述 GitHub 安装要求远程仓库已经包含新插件文件。本地编辑不会自动上传。此来源用于本地试用和仓库分发；公开插件目录上架另需提交和审核。格式与来源规则见 [OpenAI 插件打包文档](https://developers.openai.com/plugins/build/plugins)。

## 先看效果

在仓库目录运行（Python 3.10+，无需安装额外依赖）：

```text
python skills/coupang-ops/scripts/analyze.py --demo --output-dir outputs/demo
```

打开 `outputs/demo/report.md`。已经跑出的模拟报告也保存在 [examples/demo-report.md](examples/demo-report.md)。

也可以在 Codex 中直接说：

> 读取 skills/coupang-ops/SKILL.md，使用自带模拟数据运行第一版，告诉我哪些商品卖得好、哪些销量下滑、哪些需要关注补货。把报告保存到 outputs/demo。

这条提示通过路径明确读取 skill，可以马上试用。正式技能文件是 `skills/coupang-ops/SKILL.md`；根目录的 `coupang.md` 保留你的初稿。

## 在本项目自动发现 skill

根据 [OpenAI 的技能文档](https://learn.chatgpt.com/docs/build-skills)，Codex 会发现 `.agents/skills` 下的技能目录，也支持目录链接。在 Windows PowerShell 中，可以运行以下命令，让本项目直接使用仓库中的 skill：

```powershell
New-Item -ItemType Directory -Force -Path .agents/skills
New-Item -ItemType Junction -Path .agents/skills/coupang-ops -Target (Resolve-Path skills/coupang-ops).Path
```

链接已经存在时无需重复创建。在 Codex 中可用 `$coupang-ops` 提及该技能；若界面没有显示新技能，重新打开项目后再试。链接仅供本机使用，Git 会忽略 `.agents/skills/`，但保留插件来源目录文件。

## 换成真实数据

先依据 [CSV 字段口径](skills/coupang-ops/references/csv-schema.md)映射导出列，并明确订单日期、取消退款的扣除方式和观察截止时间。此脚本只接收标准化后的每日 SKU 汇总，不能把任意后台导出文件直接当成兼容输入。

把真实数据放在 `private/`，结果放在 `outputs/`；这些目录已被 Git 忽略。本地计算脚本不请求网络。若让 Codex 阅读私人数据，仍需要遵循你使用的模型和工具的数据处理设置。

```text
python skills/coupang-ops/scripts/analyze.py --sales private/sales.csv --metadata private/metadata.json --inventory private/inventory.csv --output-dir outputs/my-store
```

没有库存文件时省略 `--inventory`。结果包含中文 `report.md` 和可供后续工具读取的 `summary.json`。

金额按 Decimal 计算。缺失日期、重复键、混用店铺或币种等错误会中止计算；零基数增长和无销量库存会显示无法计算的原因。销售额不等于利润，库存覆盖是基于近期履约销量的估计。

## 验证

```text
python -m unittest discover -s tests -v
```

测试覆盖两期合计、不同排名、零基数、无销量、库存覆盖、退款只扣一次、错误输入中止，以及源文件保持不变。仅在临时目录中变造模拟数据。
