# GitHub Repo Scout（GitHub 开源项目侦察员）

> GitHub 仓库发现、审查与选型

一份符合 Agent Skills 规范、可跨多种 Agent 使用的 GitHub 仓库发现与选型 Skill。

它不会只按 Stars 罗列热门项目，而是从任务约束出发建立查询矩阵、候选漏斗和证据链，先做 Gate，再给推荐；推荐完成后默认停止，不会擅自安装第三方仓库。

## 能做什么

- 为一个技术目标寻找 GitHub 开源项目。
- 比较多个仓库的适配度、维护状态、安全边界和集成成本。
- 寻找 local-first、offline、self-hosted、no API key 等约束下的替代方案。
- 区分事实、维护者主张、推断和未知。
- 在用户明确批准后，为候选安装准备固定版本、影响、回滚和验收方案。

## 前置条件

- Python 3.10+
- [GitHub CLI `gh`](https://cli.github.com/)
- 已完成 `gh auth login`
- 可访问 GitHub

检查环境：

```bash
python3 scripts/github_repos.py doctor
```

## 快速安装

在本仓库根目录执行：

```bash
python3 install.py
```

安装器从 [`maintenance/agents.json`](maintenance/agents.json) 读取平台定义，自动检测本机已有 Agent，只安装到已检测目标；不存在的目标显示 `skipped`，不会创建无效目录。

查看已知 Agent、别名、检测状态和目标路径：

```bash
python3 install.py --list-agents
```

指定单个 Agent：

```bash
python3 install.py --agent hermes
python3 install.py --agent cursor
python3 install.py --agent gemini-cli
python3 install.py --agent cursor --agent opencode
```

显式指定的 Agent 不存在时，安装器会报错。确实需要提前部署目录时，使用：

```bash
python3 install.py --agent hermes --allow-missing-agent
```

对于未收录、但支持标准 `SKILL.md` 的 Agent，直接指定其用户级 skills 根目录；安装器会自动追加 `github-repo-scout/`：

```bash
python3 install.py --target ~/.some-agent/skills
```

`--agent` 和 `--target` 都可以重复使用或组合使用。

检查和卸载：

```bash
python3 install.py --check
python3 install.py --uninstall --agent hermes
python3 install.py --check --target ~/.some-agent/skills
python3 install.py --uninstall --target ~/.some-agent/skills
```

默认路径：

| Agent | 路径 |
|---|---|
| Hermes Agent | `~/.hermes/skills/research/github-repo-scout/` |
| Codex CLI | `~/.agents/skills/github-repo-scout/` |
| Claude Code | `~/.claude/skills/github-repo-scout/` |
| Cursor | `~/.cursor/skills/github-repo-scout/` |
| Gemini CLI | `~/.agents/skills/github-repo-scout/` |
| GitHub Copilot | `~/.agents/skills/github-repo-scout/` |
| OpenCode | `~/.agents/skills/github-repo-scout/` |
| Windsurf | `~/.codeium/windsurf/skills/github-repo-scout/` |

Codex、Gemini CLI、GitHub Copilot 和 OpenCode 官方均支持 `~/.agents/skills/`；安装器优先复用这份标准副本，避免重复 Skill 和优先级冲突。

也可以直接从公开 GitHub 仓库通过 Skills CLI 安装：

```bash
npx skills add sunhuaian2026/github-repo-scout
```

全局安装到指定 Agent，例如 Codex：

```bash
npx skills add sunhuaian2026/github-repo-scout --skill github-repo-scout --agent codex --global --yes --copy
```

不想预装 CLI 时无需先执行 `npm install`；`npx` 会按需运行当前 Skills CLI。Hermes Agent 建议继续使用上面的 `python3 install.py --agent hermes`，以保留本项目的事务安装、漂移检查和回滚能力。

## 维护者验证与高级管理

发布或排错时再运行完整验证、预览和受管副本检查：

```bash
python3 maintenance/validate.py --smoke
python3 maintenance/manage_skill.py plan --platform all
python3 maintenance/manage_skill.py install --platform all
python3 maintenance/manage_skill.py check --platform all
```

管理器拒绝覆盖非托管同名目录，并用内容哈希检查副本漂移。详细机制见 [`maintenance/platform-support.md`](maintenance/platform-support.md)。

## 怎么用

### 直接用自然语言

兼容 Agent 都可以直接描述需求，例如：

#### 查找项目

```text
帮我找一个可以本地部署、不开外部 API、支持中文 OCR 的开源项目，比较许可证、维护状态和部署成本。
```

#### 比较项目

```text
比较 Continue、Cline 和 Aider，重点看隐私边界、模型提供方、最近维护状态和退出成本，不要安装。
```

#### 带硬门槛选型

```text
给这个商业项目找一个允许修改和分发的开源向量数据库，先列硬门槛和候选漏斗。
```

#### 找替代品

```text
找几个 PostHog 的轻量自托管替代品，不能依赖外部 SaaS，并核验是否使用 Cookie。
```

#### 单仓库安全审查

```text
审查 OWNER/REPO 是否值得安装，重点检查安装脚本、依赖 hooks、遥测、凭据和数据外传；只评估，不要安装。
```

#### 先选型，再安装

```text
先选出最合适的两个项目并给出证据；我确认仓库、固定版本和写入范围后再安装。
```

### 显式调用

**Codex CLI**

```text
$github-repo-scout 帮我找一个离线可用的 PDF OCR 项目，不要安装。
```

**Claude Code**

```text
/github-repo-scout 帮我比较三个本地代码检索仓库，只做评估。
```

**Hermes Agent**

直接说“使用 `github-repo-scout` 查找……”，或使用上面的自然语言请求。

## 它会怎样工作

1. 提取任务、硬门槛、偏好和排除项。
2. 生成 2–3 条基础查询；发现缺口时再安排 0–2 条高信息扩展查询。
3. 自动拒绝低收益扩展，并在缺口关闭后停止下一条查询。
4. 使用质量加权合并；基础查询权重大于扩展，多查询命中加分，不按 Stars 全局截断。
5. 先执行许可证、平台、安全和数据边界 Gate。
6. 深读 README、LICENSE、manifest、安装入口、活动记录和相关源码。
7. 用分维度评级和置信度比较候选，不生成虚假的总分。
8. 输出 1–3 个有证据的建议；没有合适结果时明确说明。
9. 默认停在推荐 Gate，等待用户决定是否进入安装阶段。

报告结构见 [`assets/report-template.md`](assets/report-template.md)。

## 只运行证据采集脚本

所有路径都相对本仓库根目录：

```bash
python3 scripts/github_repos.py adaptive-search \
  --plan assets/query-plan.example.json \
  --limit-per-query 20 \
  --max-candidates 40 \
  --output /tmp/github-repo-scout-candidates.json

python3 scripts/github_repos.py inspect owner/repository \
  --output /tmp/owner-repository-evidence.json
```

查询计划包含 `relevance_terms`、`constraint_terms` 和带 `base` / `expansion` 阶段的查询。脚本只通过 `gh` 访问 GitHub，输出结构化 JSON；`query_decisions` 记录扩展是否采用或提前停止。旧的 `search --query` 保留为兼容模式，不是 Skill 默认路径。

`partial: true` 或非零退出码表示证据不完整，不能当成完整评估。

## 安全边界

- 搜索和推荐不是安装授权。
- 安装前固定 tag 或 commit，不使用浮动 `main` 作为可复现版本。
- README 作为维护者主张，安全和能力边界继续向源码或官方资料核验。
- License 是兼容性 Gate，不是质量加分项。
- Stars 只是社区信号，不能补偿适配、安全或维护问题。
- 所有失败查询和未知证据都必须显式报告。

## 更新、检查和卸载

修改 canonical source 后重新部署：

```bash
python3 maintenance/validate.py --smoke
python3 maintenance/manage_skill.py install --platform all
python3 maintenance/manage_skill.py check --platform all
```

安全卸载三个受管副本：

```bash
python3 maintenance/manage_skill.py uninstall --platform all
```

发生受管副本漂移时，管理器默认停止。先检查差异；确认接受后才使用 `--accept-drift`。

## 目录结构

```text
github-repo-scout/
├── SKILL.md                       # Agent 运行流程
├── README.md                      # 人类使用说明
├── LICENSE
├── install.py                      # 通用一键安装入口
├── scripts/
│   └── github_repos.py            # GitHub 证据采集
├── references/
│   ├── scoring.md                 # 分维度评级矩阵
│   └── security-review.md         # 安全审查清单
├── assets/
│   ├── query-plan.example.json    # V2.2 查询计划示例
│   └── report-template.md         # 输出模板
└── maintenance/
    ├── agents.json                # 已知 Agent 声明式注册表
    ├── manage_skill.py            # 事务复制、检查、卸载和回滚
    ├── validate.py                # 静态与 smoke 校验
    └── platform-support.md        # 平台路径与验收矩阵
```

`maintenance/` 只服务 canonical 仓库维护，不复制到 Agent 的运行目录。
