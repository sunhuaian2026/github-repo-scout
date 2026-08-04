# 安全审查清单

在推荐用户安装、执行或集成前检查：

## 来源与身份

- 仓库 owner、组织身份、官方主页和发布渠道能否对应。
- 默认分支、tag、release artifact 是否可追溯。
- 是否存在仿冒、被接管、废弃 fork 或域名漂移迹象。

## 代码与依赖

- 阅读依赖清单和 lockfile；识别来源不明、固定不足或生命周期脚本。
- 检查 `install`、`postinstall`、hooks、Makefile、容器入口和下载脚本。
- 搜索 `curl|wget|eval|exec|sudo|base64` 时结合上下文判断，不机械定罪。
- 检查是否读取 SSH、云凭据、浏览器数据、agent memory 或环境变量。

## 网络与数据

- 列出联网域名、上传数据、遥测、模型/API 提供方和默认开启状态。
- 明确数据是否离开本机，是否需要 API key，凭据如何存储。
- 把公开 API 的正常编码与隐藏 payload 区分开。

## 安装与回滚

- 固定 tag 或 commit，并在可用时验证 checksum/signature。
- 先给写入路径、服务影响、权限、回滚和最小验收。
- 不从 README 推导“一键安装安全”；执行是独立审批 Gate。

## 报告

每个风险标为：`blocking`、`mitigable`、`informational` 或 `unknown`，并给出证据位置。
