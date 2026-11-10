# 2026-11-06 项目级 skill：web-gateway-development

## 原因

用户原话：

> 在docs/skills里编写一个名为web-gateway-development的项目级skill，描述如何在本项目中工作。此skill中不应该有绝对路径。以及一个额外要求：在每一轮中，如果有一个任务，但是提交（但还没推送）后发现刚才的提交有问题，又补充了一轮修复，要使用追加提交的方式将两次提交合并成一个。如果某一次对话中用户提出了两个不相关的任务，那么这两次提交要分开。不要推送到远程除非用户主动说明要求推送。

本轮之前的口头纪律散在三处（`docs/works/README.md` 的任务记录制度、`docs/works/坑清单.md` 的 121 条、若干条只在对话里说过的提交口径）。提交拆分与追加提交这条**从来没写进任何文件**，每一轮靠会话记忆执行 —— 换个会话就会漂。

## 做了什么

新增 [docs/skills/web-gateway-development/SKILL.md](../../skills/web-gateway-development/SKILL.md)，一份 YAML frontmatter（`name` + 一行 `description`）加正文 106 行：

- **冷启动五读**（规格 → 场景文件描述 + 真实样例 → INDEX 顶行 + `git log` → 坑清单 → `git status`），与 `docs/works/README.md` 那一份对齐但不重复它。
- **仓库地图**：`editor/src/{main,renderer,logic}.ts`、`local_gateway.py`、两份逐字节相同的 `types.ts`、`config/constant_config.json` vs `features.json`（**常量面**与**键面**是两件事）、`tests/`、`docs/works/`、`tools/`、`var/`。
- **一轮五步**：规格就地修订 → 代码（"只修复 X" = 最小改动，其余进卡片的"不做"）→ 测试（每条判据配一次负控制，判据写几何/结构关系不写常数）→ 文档 → 提交并停掉本轮起的服务。
- **可跑命令**：`npx tsc -p editor/tsconfig.json --noEmit`、`npx vitest run --config editor/vitest.config.ts`、`npx vite build --config editor/vite.config.ts`、`python -X utf8 -u -m unittest discover -s tests -p <module>.py`（**逐模块**，裸 `discover -s tests` 在这台 Windows 上会停在 `test_gateway_state`）。真机走查走 `tools/serve_verification.py --port 8098`；8000/8080/8099/8500 是用户的服务，不动。
- **接口面四条**：新根路径要同批补 `editor/vite.config.ts` 的代理表；协议字段要两份 `types.ts` 同改；feature 键 vs 常量各走各自的面；`tools/audit_parity.py` 的派生产物 `docs/parity-probes.json` **不入提交**。
- **提交规则**（用户这条新增要求落在这里）：一任务一提交、**只在本地**、**绝不 push 除非用户主动要求**；本轮提交后发现问题、补一轮修复 ⇒ 先 `git branch -r --contains HEAD` 证明**未推送**再 `git commit --amend`，已公开则新提交不许 amend，pre-commit hook 失败则修根因后**新建**提交；一次对话两个不相关任务 ⇒ 两次提交，各带各的卡片／INDEX 行／规格改动，暂存后要复核暂存 diff 里没有别人的关键字；他人同文件的在途改动按 hunk 拆。
- **Windows 注意**：控制台 GBK，中文输出落文件再读回；改 UTF-8 文档要 `newline=''`；heredoc 出来的行带 CRLF。

**位置与三份副本**：正文的**权威在 `docs/skills/web-gateway-development/SKILL.md`**（入版本库）。另两份是各工具的发现路径，逐机器重新生成、不入版本库：

- `.qoder/skills/web-gateway-development/SKILL.md` —— Qoder 的项目级发现目录（放好后本会话的 skill 列表当场出现 `web-gateway-development`，可 `/web-gateway-development` 调用）。
- `.codex/skills/web-gateway-development/SKILL.md` —— Codex 侧副本，**多出一节「Codex 侧附加口径」**（只写在这一份里，见下）。
- `.gitignore` 因此加一行 `.qoder/`（`.codex/` 早就整目录忽略），与"提交件里不留机器路径 / 派生物不入库"同一条纪律。

**Codex 侧三条裁决**（用户原话，只落在 `.codex` 那一份里，优先级高于 harness 默认）：**① xhigh review 禁止主动使用** —— 不调 `agents/xhigh-review-runner.toml`、不走 `harness/review/` 那套评审闸，除非用户点名；一轮的自检手段就是本 skill 列的负控制 + `tsc`/`vitest`/`build`/逐模块 Python + 暂存 diff 复核，评审类子代理同理只在被点名时用。**② `openspec/` 已过时不再使用** —— 不创建、不更新、不读 `openspec/changes`／`upstream`，规格唯一权威是 `docs/Web前端需求规格v3.md`，改动就地修订并保留 Q 决议号，提案走任务卡。**③ Codex Memory 组件不再使用** —— 不读写 `.codex/memories/`（`memory.db`／`events.jsonl`／`distilled`／`summaries`）也不读写 `.codex/shared/`（`decisions`／`facts`／`routes`／`workflows`／`index.json`），依赖它的 `harness-release-gate` 那类工作流不主动跑；项目事实的来源只有仓库内文档与 git（坑清单、INDEX＋卡片、规格 v3、`git log`），会话记忆与这些文件冲突时以文件为准。

**不做**：不新增脚本（`scripts/`）、不改 `docs/works/README.md` 与规格 v3 的任何口径（skill 是入口不是第二权威）、不把坑清单的内容复制进 skill（只指路 —— 复制就是第二个事实源）。

## 验证

| 项 | 值 |
|---|---|
| 绝对路径 | `grep -nE "([A-Za-z]:[\\\\/]\|/f/\|/home/\|/Users/)"` ⇒ **0 命中** |
| frontmatter | `---` 首尾齐全；`name: web-gateway-development` 与目录名一致；`description` 单行 **554** 字符（≤1024） |
| 体量 | 正文 **106 行**（< 500 行上限）；无 `TODO:`；无多余文件（无 README/CHANGELOG） |
| 事实核对 | 代理表确在 `editor/vite.config.ts:23`；`DEFAULT_CONSTANT_CONFIG` 在 `local_gateway.py`；`.gitignore` 确含 `var/`、`dist/`；用户维护件清单与 `docs/works/README.md` 禁令一节逐条一致 |
| 三份副本 | `git check-ignore -v` 证 `.qoder/skills/...`（命中 `.gitignore:10:.qoder/`）与 `.codex/skills/...`（命中 `.gitignore:11:.codex/`）**均不入库**；`docs/skills/...` 是唯一的跟踪件。`.codex` 那份与权威那份的差集只有一节「Codex 侧附加口径」＋描述里的三个触发词 |
| 发现验证 | 放进 `.qoder/skills/` 后本会话 skill 列表当场出现 `web-gateway-development`（描述按 frontmatter 单行截断显示），即 `/web-gateway-development` 可调用 |
| 回归 | 本轮未碰代码 ⇒ `tsc`／`vitest`／`build` 不需重跑（Q149 那轮提交 `2c20448` 时全绿） |

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-06 |
| 落地 | **一个本地提交**：本卡所属的这一轮补了"三份副本 + `.gitignore` + Codex 侧三条裁决"，与先前那次 skill 提交是**同一个任务**且尚未推送 ⇒ 按本 skill 自己那条纪律走 `git branch -r --contains HEAD`（空＝未推送）后 `git commit --amend` **并成一个**，不留"提交坏了再补一刀"的第二条。同会话里 Q149 那条 bug 修复仍是**独立的** `2c20448`（两个不相关任务 ⇒ 两次提交，这条也照做了） |
| 改动 | `docs/skills/web-gateway-development/SKILL.md`（新增，权威正文）、`.gitignore`（+ `.qoder/` 一行）、`docs/works/INDEX.md` 顶行、本卡。机器本地的两份副本 `.qoder/skills/web-gateway-development/SKILL.md`、`.codex/skills/web-gateway-development/SKILL.md`（多一节 Codex 侧口径）**不入版本库** |
| 未提交 | `docs/parity-probes.json`（按既有裁决留在工作区，不入任何提交）；两份发现目录里的副本（`.gitignore` 命中，`git status` 里不出现） |
