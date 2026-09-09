# Pipeline setup — one-time steps per host and per repo

The pipeline (`docs/pipeline/design.md`) needs three things configured **outside
the orchestrator prompt**, so the prompt stays lean and runs stay unattended:
commit signing, tool availability, and opencode's permission allowlist. Do these
once per host, then once per repo.

## 1. Commit signing (per host + GitHub)

Repo rulesets that require verified signatures will block every pipeline PR
otherwise (first run hit `mergeStateStatus: BLOCKED` with `reason: unsigned`).

On the machine running the pipeline (e.g. the Pi):

```sh
ssh-keygen -t ed25519 -f ~/.ssh/herdr-routines-pipeline_signing \
  -C "<your-github-noreply-email>" -N ""
cat ~/.ssh/herdr-routines-pipeline_signing.pub
```

Then on GitHub: **Settings → SSH and GPG keys → New SSH key → Key type:
Signing Key** — paste the `.pub` line, title it (e.g. `herdr-routines-pipeline-pi`).
This is a *signing* key, separate from your authentication keys; revoke it without
touching your laptop keys.

Why a dedicated key instead of your main one: the private key lives on an
always-on box readable by agent processes. Keep your personal signing key off it.

## 2. Wire git config into each target repo (per repo)

In the **parent clone** the pipeline launches from (linked worktrees inherit
parent-clone config):

```sh
git -C ~/.local/state/herdr-routines/repos/<repo> config user.email "<your-github-noreply-email>"
git -C ~/.local/state/herdr-routines/repos/<repo> config user.name "<your-name>"
git -C ~/.local/state/herdr-routines/repos/<repo> config gpg.format ssh
git -C ~/.local/state/herdr-routines/repos/<repo> config user.signingkey ~/.ssh/herdr-routines-pipeline_signing.pub
git -C ~/.local/state/herdr-routines/repos/<repo> config commit.gpgsign true
```

Also configure the HTTPS credential helper so workers can push:

```sh
git -C ~/.local/state/herdr-routines/repos/<repo> config credential.helper '!gh auth git-credential'
```

Verify: `git -C <repo> commit --allow-empty -m test -S && git log --show-signature -1`
(GitHub-side verification is what matters; local display may need
`gpg.ssh.allowedSignersFile` and can be ignored.)

## 3. Install tools the gates use (per host)

Gate commands use `rg` (ripgrep), `jq`, `gh` (authed), `git`, and the repo's test
runner (`uv`). On a fresh Pi:

```sh
mkdir -p ~/.local/bin
curl -fsSL https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-aarch64-unknown-linux-gnu.tar.gz \
  | tar -xz -C /tmp
cp /tmp/ripgrep-*/rg ~/.local/bin/rg && chmod +x ~/.local/bin/rg  # x86_64: use that tarball instead
gh auth status   # must show logged in; gh auth login if not
```

Missing tools don't just fail gates — the orchestrator will try to *fix* them
(`sudo apt-get install`), which wedges as a permission prompt at 03:00.

Also **remove any superseded standalone launcher scripts** left in `~/.local/bin`
from before issue 026 (`pipeline-launch-nightly.sh`, `-gc.sh`, `-plugin.sh`,
`-panelifecycle.sh`, `-statuscli.sh`, …). The pipeline runs from the in-repo
`scripts/pipeline-launch.sh` now; the old copies are dead code that the
orchestrator stumbles into while probing `~/.local/bin` — reading one triggered
the blocked prompt that killed the 20260909 run (issue 050).

## 4. opencode allowlist (per host)

`~/.config/opencode/opencode.json` needs `permission.external_directory` entries
for everything the pipeline touches outside its cwd (each gap = a human tap on a
blocked prompt mid-run), plus `permission.tool: "allow"`.

The tracked source of truth is [`../../deploy/opencode.pipeline.json`](../../deploy/opencode.pipeline.json).
Merge its `permission` block into this host's `~/.config/opencode/opencode.json`
(that host file also carries provider/plugin/model config that is host-local and
stays out of git, so merge — don't symlink). Keeping the list in one committed
file is what stops the Pi and hp-server drifting apart, as they already had.

Keep `/etc` out unless something genuinely needs it — its only appearance so far
was the orchestrator probing a missing binary (fixed by step 3). Do **not** add
`~/.local/bin/**`: `external_directory` has no read-only mode, and a pipeline that
edits its own code should not also be able to overwrite `herdr`/`gh`/`uv`. Step 3
(remove the stale launchers) removes the reason the orchestrator looked there.

## 5. HERDR_ENV

Launch the orchestrator workspace with `--env HERDR_ENV=1` (the launcher in
`design.md` does this). Without it the orchestrator cannot drive `herdr` at all.
