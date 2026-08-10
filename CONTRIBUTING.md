# Contributing to SIN-Save-Token

Thanks for improving SIN-Save-Token. Keep changes small, testable, and easy to review. The repository optimizes agent token usage without trading away correctness, security, or reproducibility.

## Before you change anything

1. Read `AGENTS.md` and `CLAUDE.md`. Their RTK rules are mandatory for local shell work in this repository.
2. Inspect the current worktree before editing. Preserve unrelated local changes; never reset, overwrite, stage, or commit them as part of your contribution.
3. Keep one contribution focused on one problem. Avoid drive-by refactors or generated-file churn unless they are required by the change.
4. For code changes, understand the affected execution path before editing. Follow the repository's GitNexus impact/review rules when they apply.

## RTK command policy

Every local shell command must be prefixed with `rtk`, including each command in a chain. Use `rtk git ...`, `rtk pytest ...`, and `rtk curl ...` rather than the raw commands. Prefer absolute repository paths instead of changing directories inside scripted workflows.

Examples:

```bash
rtk git status --short --branch
rtk git diff --check
rtk pytest tests/ -q -p no:cacheprovider
rtk ruff check bin lib tests scripts
rtk mypy lib/sin_orca/ lib/sin_context/ --show-error-codes --pretty
```

Do not bypass RTK simply to obtain more verbose output. If a command is unsupported by a dedicated RTK filter, RTK passes it through.

## Verification

Run the smallest focused test while developing, then run the relevant repository gates before requesting review. For changes that can affect normal Python behavior, the baseline is:

```bash
rtk pytest tests/ -q -p no:cacheprovider
rtk ruff check bin lib tests scripts
rtk mypy lib/sin_orca/ lib/sin_context/ --show-error-codes --pretty
rtk git diff --check
```

For orchestration or integration changes, also run the repository integration verifier when applicable:

```bash
rtk python3 scripts/verify-local-integration.py --allow-dirty
```

GitHub CI performs Python compilation, structural and policy checks, Ruff critical rules, Mypy, the full pytest suite, architecture-contract checks, and whitespace validation. A contribution is not ready to merge while required CI checks are failing.

## Review expectations

Before review:

- inspect `rtk git status --short --branch` and the complete task-owned diff;
- confirm no unrelated pre-existing files were staged;
- check that behavior changes have tests or other reproducible evidence;
- keep generated artifacts reproducible from their canonical source rather than editing generated output by hand;
- call out known limitations, follow-up work, or external blockers explicitly instead of hiding them.

Reviewers should verify the actual diff and evidence rather than relying only on a completion claim. High-risk or security-sensitive changes require extra scrutiny and explicit authorization for any action outside normal repository edits and tests.

## Commits and pushes

Stage only files owned by the contribution. Do not use broad staging when the worktree contains unrelated changes.

```bash
rtk git add -- path/to/owned-file
rtk git diff --cached --check
rtk git diff --cached
rtk git commit -m "docs: describe contribution workflow"
```

Use a short, descriptive commit subject. Push only after the relevant checks pass and only when you are authorized to update the target branch. Never rewrite shared history or discard someone else's work to make a branch appear clean.

## Security and local state

- Never commit API keys, passwords, cookies, callback capabilities, access tokens, private keys, or other credentials.
- Never print or copy secret values into logs, task evidence, issues, commits, or review comments. Redact security-scan output when it could contain sensitive material.
- Keep repository-local runtime/task state such as `.sin-gpt-web/` and `.sin-goal/` out of commits unless a documented repository contract explicitly says otherwise.
- Do not weaken security controls, rotate credentials, change deployment state, or perform destructive operations as part of an ordinary contribution without explicit authorization.
- If you discover a vulnerability, do not publish exploitable details or credentials in a public issue. Preserve evidence safely and use the repository owner's private security-reporting channel when available.

## Pull-request checklist

A reviewable contribution should satisfy all of the following:

- the change is scoped to the stated problem;
- unrelated worktree changes are preserved and unstaged;
- relevant tests and static checks pass;
- `rtk git diff --check` is clean;
- documentation is updated when behavior or operator workflow changes;
- no secrets or local runtime state are included;
- the commit history is understandable and contains no unrelated changes.
