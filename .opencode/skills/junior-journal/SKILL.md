---
name: junior-journal
description: Use when the user wants to write or scaffold a journal entry for a closed issue, typically at end-of-session after PR creation. Collaborative scaffolding: the agent proposes structure in the user's voice, the user supplies content and corrections, the agent edits the file. Distinct from code authoring (load `junior-socratic-coder`) — the "don't write the user's words" rule does NOT apply to journal entries. The journal is reflective writing, not code.
---

# Junior Journal Authoring

This skill exists because the journal is **reflective writing**, not code. The rules from `junior-socratic-coder` — "user types, agent sharpens" — apply to code chunks. They do **not** apply here. The journal's value is that it's *yours*. But getting the structure right is half the work, and structure is something the agent does well.

This skill splits the work:

- **Agent proposes the structure** (sections, headings, what each section contains). Writes it to the file. This is scaffolding, not authorship.
- **User supplies the content** (what happened, what was learned, what would change). May be in chat, in the file directly, or a mix.
- **Agent edits the file** to incorporate user's content into the proposed structure. Reads back the result for the user to confirm.
- **User confirms or pushes back.** Final word is always the user's.

The user can also reject the structure and ask for a different shape. The agent adapts.

## When to activate

Activate when the user says any of:
- "write my journal entry for #N"
- "create a new entry on my journal"
- "log this issue"
- "journal for today"

Or after PR creation in an end-of-session wrap-up.

## Where to write

Read the existing journal directory first to learn the user's convention:
- `docs/journal/new/1_week/NN.md` — daily entries, numbered
- `docs/journal/<other>/...` — other layouts exist; don't assume

If no journal directory exists, ask the user where they want it. Do not invent a path.

## Standard structure (adapt to user's convention)

Read the user's prior entry first. The most recent entry defines the structure the user prefers. If the user has no prior entries, propose this shape:

```markdown
# Issue #NN — <short title>

## Where we stopped

- Branch `<branch-name>` with PR #N open/merged at <commit-hash>.
- One-line status of the issue.

## What happened today

- Numbered list of concrete steps.
- Each step names files / commits / decisions.

## Decisions made

- **<decision>** — <rationale, 1–2 sentences>.

## Decisions worth flagging

- Items the user wants to remember for next session / next issue.
- Open follow-ups, deferred work, things to re-surface.

## Resume point for tomorrow

- Branch state.
- What's next.
- Anything blocking.

## Mood

- Optional. One paragraph. Reflective, not performative.
```

## Operating contract

1. **Read prior entries first.** Match the user's existing structure. Don't impose a new shape on a project with 10+ entries.
2. **Read the issue spec, the plan, the PR.** You have full context — the journal entry should reference commit hashes, PR numbers, file paths the user actually shipped.
3. **Write the structure first.** Sections with bullet stubs. User fills in or corrects.
4. **Iterate.** User adds / removes / rewrites sections. Agent edits the file.
5. **Don't fabricate content the user didn't provide.** If a section needs content the user hasn't given, leave it as a stub `[TODO]` or ask the user one question.
6. **Tone is the user's, not yours.** If the user's prior entries are terse and direct, mirror that. If they're detailed and reflective, mirror that. Don't impose a verbose model-summary voice on a project that's been kept short.

## Hard rules

1. **The user has final say on content.** If the user says "actually, drop that section" or "rewrite this paragraph," do it without argument.
2. **No fact invention.** Commit hashes, PR numbers, file paths must come from the actual git/PR state, not the model's memory. Verify before writing.
3. **No paraphrase into the user's voice for PR-comment posting.** That rule is in `closed-issue-peer-review`. This skill is for *journal* files only.
4. **Mirror the user's typos if they're consistent.** If their prior entries have "minst" instead of "mint" or "caus" instead of "because," don't silently correct — flag it once and let them decide.

## Failure modes to avoid

- **Don't write a model summary and call it a journal.** The journal should sound like the user. If a future-you reading the entry can't tell whether a human or a model wrote it, the agent overwrote voice.
- **Don't impose structure the user didn't ask for.** If their prior entries are two paragraphs, two paragraphs is enough. Sections are scaffolding, not mandatory furniture.
- **Don't refuse to write structure because "rules say user types."** That rule is for code. This skill is the explicit exception. If you're unsure whether the user wants you to write, ask: "want me to scaffold the structure and you fill it in, or do you want to write the whole thing from scratch?"
