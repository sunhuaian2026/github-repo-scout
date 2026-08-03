# 跨平台安装与验收

本仓库遵循 Agent Skills 开放规范，运行内容只有一份 canonical source。`maintenance/` 是仓库维护分支，不复制到各平台运行目录。

## 扫描目录

| 平台 | 用户级目录 |
|---|---|
| Hermes Agent | `~/.hermes/skills/research/github-repo-scout/` |
| Codex CLI | `~/.agents/skills/github-repo-scout/` |
| Claude Code | `~/.claude/skills/github-repo-scout/` |

`maintenance/manage_skill.py` 先完成三平台预检和临时副本校验，再带锁切换目录；任何平台切换失败时恢复本轮已经替换的目标。运行副本使用内容哈希检测漂移。

## 部署

```bash
python3 maintenance/validate.py
python3 maintenance/validate.py --smoke
python3 maintenance/manage_skill.py plan --platform all
python3 maintenance/manage_skill.py install --platform all
python3 maintenance/manage_skill.py check --platform all
python3 maintenance/manage_skill.py uninstall --platform all
```

管理器永不覆盖非托管同名目录。受管副本发生漂移时默认停止；检查差异后可显式使用 `--accept-drift` 覆盖或卸载。卸载只处理带管理标记的副本。

## 验收矩阵

1. **格式**：`maintenance/validate.py --smoke` 通过。
2. **Hermes**：能发现 `github-repo-scout`；名称和自然语言触发各验证一次。
3. **Codex**：显式 `$github-repo-scout` 和自然语言触发各验证一次。
4. **Claude Code**：显式 `/github-repo-scout` 和自然语言触发各验证一次。
5. **行为**：三平台对同一固定请求都先报告约束与查询矩阵，不执行候选安装。
6. **漂移**：`manage_skill.py check --platform all` 均返回 `ok`。

平台专有 frontmatter 不写入 canonical `SKILL.md`，避免某一平台的权限或调用语法污染其他平台。
