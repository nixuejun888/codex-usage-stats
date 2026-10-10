# Changelog

## 1.2.1

- 费用单位改为人民币：`scripts/deepseek_pricing.py` 的双货币价目表，默认 `CNY`，取自 DeepSeek 中文定价页（不是汇率折算）；`--usd` 切美元。
- 挂件锁定：去掉标题栏 ✕，右键菜单的退出项灰掉，`WM_DELETE_WINDOW`（含 Alt+F4）被拒绝；新增停用出口 `hooks/ensure_widget.py --stop`，调试用 `--allow-close`。
- CLI 列名与 JSON 的货币字段跟随所选价目，删掉 `--fx`。

## 1.1.0

- 挂件改为客户端窗口的 owned window：随客户端置顶、最小化隐藏、位置夹在客户端内容区内，拖不出应用窗口。
- 单次来回费用（`--by turn`）、缓存命中率与进度条、7 天柱状图。

## 1.0.0

- 首个版本：按天 / 线程 / 模型汇总本地 rollout 日志，SessionStart 钩子拉起挂件。
