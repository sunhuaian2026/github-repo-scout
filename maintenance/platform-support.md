# 跨平台安装与验收

本仓库遵循 Agent Skills 开放规范，唯一 canonical source 位于 `skills/github-repo-scout/`。根目录的 `README.md`、`install.py` 和 `maintenance/` 不复制到各平台运行目录，也不会被 npx 安装。

## 扫描目录

| 平台 | 用户级目录 |
|---|---|
| Hermes Agent | `~/.hermes/skills/research/github-repo-scout/` |
| Codex CLI | `~/.agents/skills/github-repo-scout/` |
| Claude Code | `~/.claude/skills/github-repo-scout/` |
| Cursor | `~/.cursor/skills/github-repo-scout/` |
| Gemini CLI | `~/.agents/skills/github-repo-scout/` |
| GitHub Copilot | `~/.agents/skills/github-repo-scout/` |
| OpenCode | `~/.agents/skills/github-repo-scout/` |
| Windsurf | `~/.codeium/windsurf/skills/github-repo-scout/` |

Codex、Gemini CLI、GitHub Copilot 和 OpenCode 共用官方支持的 `~/.agents/skills/`，避免重复 Skill 和优先级冲突。平台定义来自 `maintenance/agents.json`。`install.py` 负责检测、别名解析、共享目标去重和任意 `--target`；`maintenance/manage_skill.py` 提供事务复制、临时副本校验、带锁切换和回滚。任何目标切换失败时恢复本轮已经替换的目标，运行副本使用内容哈希检测漂移。

## 部署

普通用户使用一键入口：

```bash
python3 install.py
```

维护者对 Hermes、Codex、Claude 三个平台基线使用完整流程：

```bash
python3 maintenance/validate.py
python3 maintenance/validate.py --smoke
python3 maintenance/manage_skill.py plan --platform all
python3 maintenance/manage_skill.py install --platform all
python3 maintenance/manage_skill.py check --platform all
python3 maintenance/manage_skill.py uninstall --platform all
```

管理器状态包括 `ok`、`outdated`、`missing`、`unmanaged` 和 `drift`。`outdated` 表示受管副本未被用户修改、但 canonical 已更新，可以直接升级；`drift` 表示运行副本内容与管理标记不一致，默认停止。检查差异后可显式使用 `--accept-drift` 覆盖或卸载。管理器永不覆盖非托管同名目录，卸载只处理带管理标记的副本。

## 验收矩阵

1. **格式**：`maintenance/validate.py --smoke` 通过。
2. **Hermes**：能发现 `github-repo-scout`；名称和自然语言触发各验证一次。
3. **Codex**：显式 `$github-repo-scout` 和自然语言触发各验证一次。
4. **Claude Code**：显式 `/github-repo-scout` 和自然语言触发各验证一次。
5. **注册表平台**：Cursor、Gemini CLI、GitHub Copilot、OpenCode、Windsurf 的官方用户级目录完成隔离安装、检查和卸载测试。
6. **自定义平台**：任意 `--target` 完成隔离安装、检查和卸载测试。
7. **行为**：已具备运行环境的 Agent 对同一固定请求都先报告约束与查询矩阵，不执行候选安装。
8. **漂移**：受管副本检查返回 `ok`；真实内容改动返回 `drift`，干净旧版返回 `outdated`。
9. **npx 包边界**：远程安装只包含 `SKILL.md`、`LICENSE`、`scripts/`、`references/` 和 `assets/`；不包含仓库 README、安装器和维护测试。
10. **通用目录识别**：Skills CLI 的 `agents: []` 只表示通用目录未归属单一 Agent；最终以目标路径、哈希和客户端实际发现为准。

平台专有 frontmatter 不写入 canonical `SKILL.md`，避免某一平台的权限或调用语法污染其他平台。
