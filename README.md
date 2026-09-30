# ghe-billing-analyst

A read-only [Copilot agent skill](https://docs.github.com/en/copilot/concepts/agents/about-agent-skills) that lets GitHub Enterprise Cloud admins analyse **cost centers, budgets and Copilot AI credit usage**, and in particular answer:

> How much Copilot AI credit can user X still consume, and what limits them?

## What it does

`scripts/billing.py` (Python 3 standard library only, uses `gh` for auth) outputs JSON:

| Command | Purpose |
|---|---|
| `user <login>` | Cost center attribution (direct → enterprise team → licence org → enterprise), AI credit pool, applicable AI budgets, this month's usage, remaining capacity and binding constraint |
| `cost-centers` | Active cost centers with resource counts and AI credit pool state |
| `budgets [--scope S] [--ai-only]` | Budgets with amount, consumed, % used, hard stop, alerts, expiry |
| `at-risk [--threshold 80]` | Users at or above N% of the universal AI budget |
| `usage [--year --month --org --user --cost-center]` | Aggregated AI credit usage (`--cost-center none` = unattributed) |

```bash
python3 scripts/billing.py -e <enterprise-slug> user <login>
```

For GHE.com (data residency), set `GH_HOST=<subdomain>.ghe.com`.

## Security model

- **Read-only**: only HTTP `GET`, restricted to an allowlist of billing / attribution endpoints.
- **No token handling**: authentication is delegated to `gh auth` (keychain or `GH_TOKEN`).
- **Input validation**: enterprise slugs, logins and IDs are validated; `gh` is invoked with an argument list, never a shell.
- **Least privilege**: use an enterprise billing manager or a custom role / GitHub App with *read* access to enterprise billing. Full attribution also needs `read:enterprise` (enterprise teams) and `manage_billing:copilot` (Copilot seats).

## Install

```bash
git clone https://github.com/helaili/ghe-billing-analyst ~/.copilot/skills/ghe-billing-analyst
```

See [SKILL.md](SKILL.md) for the agent instructions and attribution / budget rules.
