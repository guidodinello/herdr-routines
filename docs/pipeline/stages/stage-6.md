# Stage 6 — Address comments

Run the `address-pr-comments` skill against PR `$PR_NUMBER`. The skill fetches all unresolved inline review threads, assesses their validity, fixes valid ones, commits and pushes, then replies to every thread with the outcome.
