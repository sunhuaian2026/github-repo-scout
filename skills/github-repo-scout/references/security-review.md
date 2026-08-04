# 安全采用审查

用于评估候选项目的采用风险；审查读取证据，不执行候选代码。

## 来源与身份

- 仓库 owner、组织身份、官方主页和发布渠道能否对应。
- 默认分支、tag、release artifact 是否可追溯。
- 是否存在仿冒、被接管、废弃 fork 或域名漂移迹象。

## 代码与依赖

- 阅读依赖清单和 lockfile；识别来源不明、固定不足或生命周期脚本。
- 检查 `install`、`postinstall`、hooks、Makefile、容器入口和下载脚本。
- 搜索 `curl|wget|eval|exec|sudo|base64` 时结合上下文判断。
- 检查是否读取 SSH、云凭据、浏览器数据、agent memory 或环境变量。

## 网络与数据

- 列出联网域名、上传数据、遥测、模型/API 提供方和默认状态。
- 明确数据是否离开本机，是否需要 API key，凭据如何存储。
- 区分公开 API 的正常编码与隐藏 payload。

## 采用与可逆性

- 记录可复现版本、checksum/signature、写入路径、权限和服务影响。
- 评估配置迁移、数据导出、卸载与回滚条件。
- 把 README 的安装主张与 manifest、脚本和发布物交叉核对。

## 报告

每个风险标为：`blocking`、`mitigable`、`informational` 或 `unknown`，并给出证据位置。
