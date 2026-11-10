---
name: web-gateway-development
description: Work inside this repository (Vite/TypeScript three.js editor plus the Python local_gateway.py) following its round workflow - revise the spec in place, implement, add tests with negative controls, write the docs/works task card plus INDEX and 坑清单 rows, and finish with exactly one local commit per task. Use for any code, gateway, protocol, config or documentation change here, for deciding what belongs in a commit, for amend-vs-new-commit decisions, and whenever 规格/决议号/Q 号/坑清单/任务卡/提交拆分 is mentioned.
---

# web-gateway-development

## Overview

Every round in this repo is a spec-driven loop: the requirement doc is the authority, code follows it, tests pin the judgement, and the round ends with one local commit. Nothing is pushed; the user pushes manually.

Read `docs/works/README.md` first - it holds the task-record 制度 and the cold-start order. This skill adds the runnable commands, the interface-surface invariants, and the commit rules.

## Cold start (in order)

1. `docs/Web前端需求规格v3.md` - the only current requirement authority (search the §-number the task names).
2. `docs/场景文件描述.md` + `docs/ur5e_table_empty.json` - world-file format and real numbers.
3. Top rows of `docs/works/INDEX.md` + `git log --oneline -10` - where the last rounds landed.
4. `docs/works/坑清单.md` - live pitfall list; grep the topic before designing a judgement.
5. `git status --short` - confirm no other session's in-flight edits are in the tree.

## Repo map

| Path | Role |
|---|---|
| `editor/src/main.ts` | whole-page DOM template + event wiring (one giant module, no importable surface) |
| `editor/src/renderer.ts` | three.js scene, `worldAabb`, wireframes, capture passes |
| `editor/src/logic.ts` | pure judgement functions (config parsing, feature gates) - the place unit tests live cheaply |
| `local_gateway.py`, `gateway_state.py` | the browser<->services gateway on 8099 and its persisted state |
| `types.ts`, `contracts/types.ts` | **byte-identical copies** - every edit touches both, same text |
| `config/constant_config.json` | constants surfaced to the browser through `RuntimeConfig` |
| `features.json` | feature keys (`viewport.*`, `agent.*`, ...) - separate surface from constants |
| `tests/` | Python `unittest` suite for the gateway |
| `docs/works/` | one folder per round + `INDEX.md` + `坑清单.md` |
| `tools/` | `audit_parity.py`, `browser_smoke.cjs`, `serve_verification.py`, `parity_inventory.py` |
| `var/`, `dist/`, `node_modules/`, `__pycache__/` | gitignored - scratch belongs here, never in the repo |

## The round loop

1. **Spec first.** Locate the §-number the task belongs to and revise that text *in place*, keeping the Q-number traceable. Do not write spec language into the task card; contradictions of an older prohibition are fixed by rewriting that sentence, with history in the card.
2. **Code.** Scope strictly to the named defect or feature. "只修复 X" means the smallest change on that path; everything else found along the way becomes the card's 不做 list, never extra behaviour in one commit. No abstraction "for later", no `any` / `@ts-ignore` / `as unknown as` to dodge `contracts/types.ts`.
3. **Tests.** Every new judgement needs a case, and every case needs a negative control: break the one line the judgement lives on, confirm exactly the intended tests go red, then restore and verify by checksum. Pin geometric/structural relations rather than asset-dependent constants (assets change; a constant assertion becomes a permanently red fake failure).
4. **Docs.** `docs/works/<YYYY-MM-DD>-<主题>/README.md` with 原因 / 做了什么 / 验证 / 进度; a new top row in `docs/works/INDEX.md`; a new entry in `docs/works/坑清单.md` when anything surprised you.
5. **Commit** (see below), then stop every service this round started.

## Verification commands

```bash
npx tsc -p editor/tsconfig.json --noEmit          # type check (no root tsconfig)
npx vitest run --config editor/vitest.config.ts    # whole JS suite, per-file counts
npx vitest run --config editor/vitest.config.ts editor/src/renderer.test.ts   # one file
npx vite build --config editor/vite.config.ts      # bundle + sizes (report the delta)
python -X utf8 -u -m unittest discover -s tests -p test_gateway_routes.py     # one module
```

Python: run **module by module** with `-p <module>.py`. A bare `discover -s tests` stalls on Windows inside `tests/test_gateway_state.py` (documented in 坑清单); always A/B against `git show HEAD:<file>` before blaming the round for a red that looks pre-existing.

Real-browser evidence: `tools/browser_smoke.cjs` is blocked by the §12.4 identity gate (坑 90), so a round needing live behaviour starts a throwaway stack - `python tools/serve_verification.py --port 8098 --library-url <OLS>` against `dist/`. Never restart, kill or wipe the services on 8000/8080/8099/8500; they belong to the user.

## Interface surface

When a round changes any of these, say so in the card and in the spec 落点 line:

- **New route or root-level endpoint** -> register it in the vite proxy table in `editor/vite.config.ts` or `npm run dev` answers 404 while the built app works (坑 3).
- **New protocol/config field** -> `types.ts` and `contracts/types.ts` are byte copies and must change together.
- **Feature vs constant**: a remotely switchable UI capability is a `features.json` key plus a row in `docs/feature-inventory.csv`; a tuned value is a `config/constant_config.json` key plus `DEFAULT_CONSTANT_CONFIG` in `local_gateway.py` plus a `RuntimeConfig` field plus a fail-open judgement in `logic.ts`. Never add a second switch when one exit already covers the layer.
- **Anything parity-visible** -> `python tools/audit_parity.py`. Its derived output `docs/parity-probes.json` stays **uncommitted**; it is a snapshot that drifts and the user has not ruled on regenerating it.
- Gateway changes need the Python suite; browser-only changes must not touch the gateway.

## Commit rules

One task = one commit, created locally. Never `git push` unless the user explicitly asks.

**Before committing**: stage by name, then read the staged diff.

```bash
git status --short --untracked-files=normal
git add <the files of this task>
git diff --cached --stat && git diff --cached | grep -n "<关键字>"   # nothing that belongs to another round
```

Refuse to stage: `docs/parity-probes.json` (uncommitted by ruling), `var/`, `dist/`, throwaway probe scripts (delete them once they have reported), and any file holding a machine-absolute path - committed files must stay path-free. If another session's edits sit in the same file, split by hunk (`git diff -U3`, apply only your hunks) rather than committing their work.

**Follow-up fix to the same task** - after committing, before pushing: fold it into that commit instead of adding a second one.

```bash
git branch -r --contains HEAD     # empty output => unpushed, amending is safe
git add <same task files>
git commit --amend                # rewrite the message if the fix changes the story
```

If `git branch -r --contains HEAD` names a remote branch, the commit is already public - make a new commit instead, never amend. If a pre-commit hook fails, fix the cause, re-stage, and create a **new** commit; do not amend (the failed commit never happened, so amending would destroy the previous one).

**Two unrelated tasks in one conversation** = two commits, each with its own card, INDEX row and spec edits. Do not let task A's docs ride into task B's commit - stage, verify the diff contains only that task's files, commit, then start task B's staging from a clean index.

Message style, following `git log`: `fix(主题)：一句话判据（Q<number>，§x.y）` or `feat(...)`, body explaining *why* and naming the measured numbers.

## Windows notes

The console is GBK: Chinese output through a pipe gets mangled or throws. Write findings to a file under `var/` and read the file back, and when rewriting UTF-8 docs from Python open with `newline=''`. Heredoc-produced lines carry CRLF, so strip `\r` before feeding a loop. Git will report LF/CRLF warnings on the Chinese docs - harmless.

## Resources

- `docs/works/README.md` - task-record 制度, cold-start order, prohibitions (do not edit user-maintained inputs: `docs/ur5e_table_empty.json`, `tests/fixtures/scene_example.json`, `docs/OLS接口.md`, `docs/轨迹文件布局描述.md`, `docs/Web前端最新要求.md`; do not rewrite frozen text under `docs/works/历史档案/`).
- `docs/works/坑清单.md` - the live pitfall list; the numbered 坑 entries are referenced by every card and are the fastest way to avoid re-deriving a known trap.
- `docs/服务占用端口清单.md`, `docs/各服务启动命令.md` - port assignments and how the user's stack runs.
