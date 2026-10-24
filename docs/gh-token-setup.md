# GitHub token setup (per user)

Any user of this repo needs their **own** GitHub Personal Access Token (PAT).
There is no shared secret and no trick — each person creates one, scoped to
their own work, and keeps it out of git.

## 1. Create the token

1. GitHub → Settings → Developer settings → **Fine-grained personal access tokens** → *Generate new token*.
2. **Repository access** → select *Only select repositories* and pick your fork/clone target of this repo (or *All repositories* if you want it account-wide).
3. **Permissions** — the minimum set this repo's flows need:
   | Permission | Level | Why |
   |---|---|---|
   | Contents | Read and write | push branches, open PRs |
   | Administration | Read and write *(only if you own the repo)* | repo settings, Actions admin |
   | Metadata | (mandatory, auto-selected) | — |
4. Generate, then copy the value once (it is shown a single time).

## 2. Wire it up locally

Pick whichever path matches how you use git:

- **`gh` CLI:** `export GH_TOKEN=<token>` in your shell profile (or `gh auth login` and let it manage the credential).
- **Plain git push/pull (HTTPS):** store it with the credential helper of your choice — e.g. `git config credential.helper store` and enter the token as the password on the first push, or use `gh auth setup-git` after `gh auth login`.
- **Never** paste the token into any tracked file of this repo. The CI and the docs checker have no need for one.

## 3. Rules this repo expects

- One token per human; scopes no wider than the table above.
- Fine-grained tokens are also **host-scoped per repo set** — a token you use for one project (e.g. another org's repos) stays separate from this one. Do not re-use a project-scoped token across projects; that is how accidental cross-project write access happens.
- If a token can no longer see the repo (404 on `gh repo view`), check both dimensions before re-generating: (a) *permissions*, and (b) *repository access* — narrowing either is enough to 404, and editing permissions alone does not widen the repo selection set.
