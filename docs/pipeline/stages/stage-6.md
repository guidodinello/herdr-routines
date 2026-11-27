# Stage 6 — Code review

Run the `code-review` skill against PR `$PR_NUMBER`: read its `SKILL.md` and follow it to the end. Use `code-review` itself, not `code-review-claude-no-autopost` or any other variant. No one is present to confirm, so proceed without waiting for approval.

The pipeline opened this PR on purpose for you to review. Do not skip it as an automated or obviously-fine PR (the skill's step 1).

## Done means the review is on GitHub

The pipeline checks the PR, not your reply. A review written only in this chat counts as no review, and the run fails.

Finish with exactly one PR review posted through `gh api repos/{owner}/{repo}/pulls/$PR_NUMBER/reviews` (the skill's step 8), not a top-level issue comment:

- Its body contains the skill's summary line, even when a count is zero: `Found N blocking issue(s) and M non-blocking note(s).`
- Each finding is an inline comment that starts with `**[blocking]**` or `**[non-blocking]**`.
- With no verified findings, still post a review whose body is `Found 0 blocking issue(s) and 0 non-blocking note(s).`, followed by the skill's "No issues found" text.

Then swap the PR's labels as the skill says, and confirm the review landed: `gh api repos/{owner}/{repo}/pulls/$PR_NUMBER/reviews --jq '.[-1].body'` must print your summary line.
