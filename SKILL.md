---
name: find-github-repos
description: 系统发现、筛选并审查 GitHub 仓库，给出有证据的选型建议。Use when 用户要找开源项目、比较 GitHub 仓库、寻找替代方案、评估能否采用某个仓库，或要求推荐可安装的项目。
license: MIT
compatibility: Hermes Agent, Codex CLI, Claude Code；需要 Python 3.9+、GitHub CLI gh 和 GitHub 网络访问。
metadata:
  author: Sun Hongjun (sunerpang)
  version: "1.1.0"
---

# 查找并评估 GitHub 仓库

目标不是列出热门仓库，而是从用户的真实约束出发，形成可追溯的候选漏斗，并把“发现”“推荐”“安装”分成独立 Gate。

## 输入与默认解释

从用户请求提取：

- **任务目标**：仓库要解决什么问题。
- **硬门槛**：平台、许可证、部署方式、成本、离线、本地优先、数据边界等。
- **偏好**：语言、技术栈、成熟度、集成成本。
- **排除项**：明确不能接受的服务、依赖或许可证。
- **时效口径**：动态指标的核验日期。

默认解释明确时直接推进并列出假设；只有错误假设会显著改变候选池或带来风险时才提问。

## 工作流

### 1. 建立查询矩阵

生成两层查询，而不是把所有限制塞进一条搜索：

1. 最多 3 条**核心查询**：使用项目类别、通用技术名和同义词，扩大召回。
2. 最多 3 条**约束查询**：加入真正影响选型的特色条件，如 local-first、offline、self-hosted、no API key。

查询应互补，不为凑数制造同义重复。先运行：

```bash
python3 scripts/github_repos.py doctor
python3 scripts/github_repos.py search \
  --query "<core query 1>" \
  --query "<core query 2>" \
  --query "<constraint query>" \
  --limit-per-query 20 \
  --max-candidates 60 \
  --output /tmp/github-repo-candidates.json
```

`partial: true` 或非零退出码意味着证据不完整。必须在报告中暴露失败查询和影响，不能静默跳过。

### 2. 形成候选漏斗

按 `fullName` 去重，保留每个仓库命中的查询。先应用硬门槛，再排序：

- 默认排除 archived、disabled、明显无关的 fork 或镜像。
- 需要复制、修改、分发时，无许可证不是低分项，而是法律状态未知的 Gate。
- 候选截断按各查询原始排名轮询合并，不能按 Stars 全局截断；Stars 只用于衡量关注度，并且在总分中最多贡献 5 分。
- `updatedAt`、`pushedAt`、最近 commit 和 release 分开报告。

展示漏斗数量：原始命中 → 去重 → 通过硬门槛 → 深度审查 → 推荐。

### 3. 深度审查入围仓库

对通过硬门槛的候选逐个运行：

```bash
python3 scripts/github_repos.py inspect OWNER/REPO \
  --output /tmp/OWNER-REPO-evidence.json
```

至少核对：

- GitHub 仓库元数据和默认分支。
- README、LICENSE、源码树、依赖清单与安装入口。
- `pushed_at`、最近提交、最新 release。
- Issues / PR 是否有人维护，安全政策是否存在。
- 安装脚本、package lifecycle hooks、外部下载、遥测、凭据和高权限操作。

`inspect` 只负责标准元数据、README、manifest、近期活动和安全政策的确定性采集，不等于完成源码安全审查。涉及安装脚本、依赖生命周期、遥测、凭据或高权限操作时，agent 必须继续读取对应源码；不能把一次 `inspect` 成功当成“项目安全”。

README 是维护者主张，不是独立事实。涉及安装、安全、成本和能力边界时，要继续读取对应源码或官方文档。需要更完整的安全审查时，读取 [安全审查清单](references/security-review.md)。

### 4. 先 Gate，后评分

先判定：

- **通过**：满足所有硬门槛。
- **有条件通过**：存在可明确缓解的未知或风险。
- **淘汰**：违反硬门槛。

只有通过或有条件通过的仓库进入评分。使用 [评分模型](references/scoring.md)，总分 100；同时给出置信度，禁止用分数掩盖未知事实。

### 5. 形成建议

优先输出 1–3 个真正合适的候选，不强行凑 5 个。每个候选必须区分：

- **已证实事实**：由仓库/API/源码直接支持。
- **维护者主张**：README 或项目方声明。
- **推断**：基于证据的判断。
- **未知**：没有核验到。

使用 [报告模板](assets/report-template.md)。报告必须包含证据 URL、核验日期、候选漏斗、淘汰原因、风险、集成成本和建议边界。

### 6. 安装是独立 Gate

仓库推荐完成后默认停止，不 clone、不安装、不执行第三方脚本。

只有用户明确批准准确仓库、版本和安装范围后，才进入安装阶段。安装时：

1. 固定 tag 或 commit；不用可变 `main` 作为可复现版本。
2. 先审查将执行的脚本、依赖和写入范围。
3. 给出命令、影响、回滚和验收。
4. 执行后验证真实版本和最小功能。

## 完成标准

仅当以下条件全部满足，任务才算完成：

- 查询矩阵和假设已公开。
- 所有搜索失败与证据缺口已公开。
- 候选漏斗数量可复算。
- 推荐项全部通过硬门槛，或明确标为有条件通过。
- 动态指标有核验日期；结论有直接来源。
- 推荐、安装和执行没有越过用户授权范围。
- 找不到合适仓库时明确说“没有足够证据推荐”，不硬凑答案。

## 支持文件

- [评分模型](references/scoring.md)
- [安全审查清单](references/security-review.md)
- [跨平台安装与验收](references/platform-support.md)
- [报告模板](assets/report-template.md)
- `scripts/github_repos.py`：确定性收集 GitHub 搜索与仓库证据。
- `scripts/install.py`：包维护工具；把 canonical skill 安装、检查或卸载到三平台。执行仓库选型任务时无需调用。
- `scripts/validate.py`：包维护工具；校验规范、引用、Python 语法及安装闭环。执行仓库选型任务时无需调用。
