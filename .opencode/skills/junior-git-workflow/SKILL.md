---
name: junior-git-workflow
description: Use when the user is about to commit, push, branch, or open a PR in this repo. Enforces the per-issue-branch rule, Socratic-loop commits, end-of-session push, PR body shape, and the repo scaffolding checklist. Do NOT use for code authoring (load `junior-socratic-coder`) or peer review (load `closed-issue-peer-review`).
---

# Junior Git Workflow

A complete workflow for working in this repo. Five rules. Modifying existing branches breaks these rules; new branches are scaffolded from `main`.

This skill is **mechanical** — no Socratic questioning, no peer review. It is the contract for how code moves from local to remote.

## Rule 1 — Per-issue branch, never `main`

At session start, if there isn't already a dedicated branch for the issue, the **first action is creating one**. Naming pattern:

```
<scope>/issue-<NN>-<short-slug>
```

Scope is one of: `feature/`, `fix/`, `refactor/`, `rebuild/`, `docs/`, `chore/`, `test/`. Slug is kebab-case English (3 words or fewer). Examples:

- `rebuild/issue-03-models`
- `feature/issue-04-alembic-initial-schema`
- `fix/issue-12-quant-confidence-floor`

We never commit to `main` directly. The only path from branch → `main` is a Pull Request. If a teammate merges, that's a chain break and the previous branch must be reverted before merging a fix.

Branch creation is one bash action, before any code:

```bash
git checkout -b feature/issue-04-alembic
```

## Rule 2 — Socratic-loop commits

A "Socratic loop" is *one chunk code authored + three Socratic questions answered + verification run green*. Every closed loop ends with a commit. The commit message describes **the chunk, not the issue as a whole**.

Conventional Commits style. Allowed types:

| Type       | Use                                                 |
|------------|----------------------------------------------------|
| `feat`     | New feature chunk (new endpoint, new table, etc.)  |
| `fix`      | Bug fix, not a feature change                       |
| `refactor` | Restructuring without behavior change               |
| `test`     | Tests, including invariant tests                    |
| `docs`     | Documentation only                                  |
| `chore`    | Tooling, scaffolding, dependencies                  |
| `build`    | Build/CI changes                                    |

Scope goes in parens. Format: `<type>(<scope>): <imperative summary>`. Summary ≤72 chars.

**Final commit of the issue** uses the project's existing close-style, e.g. `feat(issue-04): Issue #N — Schema migration with RLS`.

**Smell rule:** if a single issue has more than ~10 commits, that's lots. Less than ~3 and we may have committed too coarsely. Track this and surface it in code review.

## Rule 3 — Push at session end

Before declaring the session over, push the branch. End-of-session push is non-negotiable.

```bash
git push -u origin <branch-name>   # first time
git push origin <branch-name>      # subsequent
```

Mid-session push is fine if collaboration or CI needs it, but is not required.

## Rule 4 — PR shape

Every merge uses a PR. We don't merge except via PR review. PR body has four sections, in this order:

1. **What this PR does** — bulleted, concrete, names the artifacts.
2. **What this PR does NOT do** — explicit non-goals and their future-issue homes.
3. **How to verify** — copy-pasteable bash commands that prove the change works.
4. **Decisions worth flagging** — non-obvious choices a reviewer would ask about.

Length budget: ≤200 lines. Past that, the structure is wrong (chunk the verbosity, not the imperatives).

**Linking GitHub issues**: only link issues that exist. If unsure, prefer `Refs #N` over `Closes #N`. Never link to a PR when you meant an issue, and never invent issue numbers. If a related issue doesn't exist yet, file it first.

**Title**: `<type>(<scope>): Closes #N — <short description>`. `<short description>` ≤ 70 chars.

We never auto-merge. PRs sit open until a human reviews and merges.

## Rule 5 — Repo scaffolding for best-practice GitHub repo

Five files maintain the repo's professional appearance for recruiters / future collaborators / CI:

| Path                                          | Purpose                                                                | Status today                                         |
|-----------------------------------------------|------------------------------------------------------------------------|------------------------------------------------------|
| `README.md`                                   | Project overview, quickstart                                           | **exists** (`/README.md`)                            |
| `.gitignore`                                  | Excludes venv, caches, env files, runtime dirs                          | **exists** (workspace root, partial — see note)       |
| `.github/workflows/ci.yml`                    | Run mypy + pytest on PRs to `main`                                      | **missing** — referenced in CURRENT_STATE.md:175      |
| `.github/PULL_REQUEST_TEMPLATE.md`            | PR body structure scaffold that matches Rule 4                         | **missing**                                          |
| `.github/ISSUE_TEMPLATE/feature.md`           | Structured "I'd like this feature" template                             | **missing**                                          |
| `.github/ISSUE_TEMPLATE/bug.md`               | Structured "this is broken" template                                   | **missing**                                          |
| `LICENSE`                                     | Repo licensing (MIT or Apache-2.0)                                     | **missing**                                          |
| `CODEOWNERS`                                  | Default owner per directory (`app/db/`, `app/agents/`, etc.)            | **missing**                                          |

These missing files get their own planned sessions. The workflow still enforces them: every PR opened includes CI passing, so the workflow file matters; every PR filed uses the PR template, so that file matters; recruiters will look at LICENSE; etc.

> **Note on `.gitignore`:** the existing one ignores `docs/`, `__pycache__/`, `*.pyc`, `.env`, and local volume-mount dirs. It currently lacks `.venv/`, `.pytest_cache/`, and IDE-specific patterns. Add those when we next touch `.gitignore`.
