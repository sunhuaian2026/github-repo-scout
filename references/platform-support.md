# 跨平台安装与验收

本包遵循 Agent Skills 开放规范，canonical 内容只有一份。

## 扫描目录

| 平台 | 用户级目录 |
|---|---|
| Hermes Agent | `~/.hermes/skills/research/find-github-repos/` |
| Codex CLI | `~/.agents/skills/find-github-repos/` |
| Claude Code | `~/.claude/skills/find-github-repos/` |

`scripts/install.py` 先完成三平台预检和临时副本校验，再带锁切换目录；任何平台切换失败时会恢复本轮已经替换的目标。修改只发生在 canonical 目录；重新部署后用内容哈希检测漂移。

## 部署

```bash
python3 scripts/validate.py
python3 scripts/validate.py --smoke
python3 scripts/install.py plan --platform all
python3 scripts/install.py install --platform all
python3 scripts/install.py check --platform all
python3 scripts/install.py uninstall --platform all
```

安装器永不覆盖非本安装器管理的同名目录。受管副本发生漂移时默认停止；检查差异后可显式使用 `--accept-drift` 覆盖或卸载。卸载只处理带管理标记的副本，三平台采用先改名、全部成功后再删除的方式，失败时恢复本轮已移动目录。

## 验收矩阵

1. **格式**：`scripts/validate.py` 通过。
2. **Hermes**：`hermes skills list` 能发现；新会话可通过名称或自然语言触发。
3. **Codex**：从用户级目录发现；显式 `$find-github-repos` 和自然语言触发各验证一次。
4. **Claude Code**：`/find-github-repos` 可见；自然语言触发验证一次。
5. **行为**：三平台对同一个固定查询都先输出硬门槛和查询矩阵，不直接安装候选。
6. **漂移**：`install.py --check` 三个平台均返回 `ok`。

平台专有 frontmatter 不写入 canonical `SKILL.md`，避免某一平台的调用、权限或 shell 语法污染其他平台。
