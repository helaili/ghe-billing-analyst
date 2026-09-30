---
name: ghe-billing-analyst
description: |
    Read-only analysis of GitHub Enterprise cost centers, budgets and Copilot AI credit usage. Use when an enterprise admin or billing manager asks how much Copilot AI credit a user can still consume, why a user is blocked, which cost center/budget applies to someone, which users are near their AI budget, or wants a cost-center / budget / AI credit usage overview. Triggers: "AI credits", "premium requests", "cost center", "budget", "how much can <user> still use", "user blocked by budget", "Copilot spend".
---
# GitHub Enterprise billing analyst (read-only)

Analyse cost centers, budgets and Copilot **AI credit** consumption for a GitHub Enterprise Cloud enterprise. The main question it answers is: **how much Copilot AI credit can user X still consume, and what limits them?**

## Hard rules (security)

1. **Read-only.** Only use `scripts/billing.py` or `gh api --method GET`. Never call POST/PATCH/PUT/DELETE billing endpoints, even if the user asks. If the user wants a change, explain what to change and where (UI path or API), and let them do it.
2. **No secrets in the conversation.** Auth comes from `gh auth` (keychain) or a `GH_TOKEN` the user exported themselves. Never ask for, print, echo or store a token. Don't run `gh auth token`.
3. **Least privilege.** Recommend a custom enterprise role or GitHub App with *read* access to enterprise billing, not enterprise owner, and not a classic PAT with `manage_billing:enterprise` (it can also write).
4. **Personal data.** Usage reports contain logins. Aggregate by default, and show per-user details only for the user asked about or for at-risk lists that were requested. If saving output, write it only to the session `files/` folder. Never put it in a repo, gist, issue or external service.
5. **Validate input.** Pass enterprise slugs, logins and IDs only through the script, which checks them against strict patterns and calls `gh` with an argument list (no shell). Don't build shell strings from user input.

## Prerequisites

Check these once per session:

```bash
gh auth status            # must be logged in to github.com (or the GHE.com host via GH_HOST)
```

Ask for the **enterprise slug** if it's unknown. A 403 response means the account lacks billing read access. Explain the required role (enterprise owner, billing manager, or custom role with billing read) and stop.

## Tool

`python3 ~/.copilot/skills/ghe-billing-analyst/scripts/billing.py -e <enterprise> <command>` outputs JSON.

| Command | Purpose |
|---|---|
| `user <login>` | **AI credit capacity for one user**: cost center, AI credit pool, applicable AI budgets, this month's usage, remaining capacity, binding constraint |
| `cost-centers` | Active cost centers with resource counts and AI credit pool (target / consumed / remaining) |
| `budgets [--scope S] [--ai-only]` | Budgets with amount, consumed, % used, hard stop, alerts, expiry |
| `at-risk [--threshold 80] [--limit 50]` | Users at or above N% of the universal (multi-user) AI budget |
| `usage [--year Y --month M] [--org O] [--user U] [--cost-center ID]` | Aggregated AI credit usage (gross, discount, net, net by model). `--cost-center none` shows unattributed usage |

## How AI credit capacity is determined

### Which cost center a user belongs to (AI credits)

`user` follows GitHub's attribution rules in this order (see [cost center allocation](https://docs.github.com/en/enterprise-cloud@latest/billing/reference/cost-center-allocation)):

1. **Direct** user assignment to a cost center.
2. **Enterprise team** assigned to a cost center. If the user is in several such teams, the oldest team (earliest `created_at`) wins. Uses `GET /enterprises/{e}/members/{user}/teams` and needs `read:enterprise`.
3. **Licence organization.** This is the org that granted the user's Copilot seat, if that org is in a cost center. It checks `GET /orgs/{org}/members/{user}/copilot` (org owner). If that is denied, it falls back to `GET /enterprises/{e}/copilot/billing/seats` (enterprise owner or billing manager, `manage_billing:copilot`).
4. Otherwise the **enterprise**, with no cost center.

Repository assignments never apply to user AI credits. They only apply to usage-based products such as Actions.

The output includes `attribution.method` (`direct` | `enterprise_team` | `license_org` | `enterprise`) and `attribution.complete`. If `complete` is false, a check could not run, usually because a scope was missing. In that case, say the result is a best guess and show the `gh auth refresh -s …` hint from `warnings`. Suggest adding the scope; don't add it for the user. Note that GitHub returns **404 instead of 403** when a scope is missing, and the script accounts for this.

Recommended read-only scopes are `read:enterprise` and `manage_billing:copilot`. The latter also allows writes to Copilot seats, but the skill never uses it for writes.

### Limits on a user's consumption
For a user in the current month, consumption is limited by the **smallest remaining amount** across the following limits. Only hard-stop limits (`prevent_further_usage: true`) actually block usage:

1. **User-scoped budget** (`budget_scope: user`, SKU `ai_credits`). This is a per-user override and may have an `expires_at`.
2. **Universal budget** (`multi_user_customer`). This is the per-user limit applied to everyone. The user's own consumed/target comes from `/budgets/{id}/user-states?user=`.
3. **Cost center AI credit pool.** If `ai_credit_pool_enabled`, the cost center is capped at `target_amount`, which is based on its members' license entitlements. The remaining pool is `target - current`.
4. **Cost center / enterprise budgets** on `ai_credits`/`premium_requests`. These are shared limits, so other users' consumption also counts against them.
5. **`effective_budget`**, returned by `GET /budgets?user=<login>`. This is GitHub's own resolved view and should be preferred when it's present.

Included license entitlements are consumed first (they appear as `discountAmount` in usage). Budgets apply to the net (paid) amount.

`unlimited: true` means no hard-stop AI limit was found. Tell the admin this is a governance gap.

## Workflows

### "How much can @alice still use?"
1. Run `billing.py -e ENT user alice`.
2. Report the following in 3–6 lines:
   - the remaining capacity in USD and the binding constraint;
   - whether the user is blocked;
   - usage this month (net and top models);
   - the cost center and pool state;
   - any `warnings`.
3. If `attribution.complete` is false, name the precedence check that couldn't run and the scope it needs.

### "Why is @bob blocked?"
Run `user bob` and find the limit whose `remaining <= 0` with `hard_stop: true`. Explain the options: raise or extend the user budget, move the user to another cost center, or raise the universal budget. The admin makes these changes; don't make them.

### Enterprise overview
Run `cost-centers`, `budgets --ai-only` and `usage` for the current month. Summarise:
- total net AI spend and top models;
- cost centers whose pool is above 80% used;
- AI budgets without a hard stop or without alerts;
- expired or expiring user budgets;
- usage not attributed to any cost center (`usage --cost-center none`).

### At-risk users
Run `at-risk --threshold 80`. Present a table with login, consumed, target and % used. Suggest reviewing the users above 100% who have an override.

## Raw endpoints (GET only, header `X-GitHub-Api-Version: 2026-03-10`)

- `/enterprises/{e}/settings/billing/cost-centers[?state=active]`
- `/enterprises/{e}/settings/billing/cost-centers/{id}?page=&per_page=100`
- `/enterprises/{e}/settings/billing/budgets?scope=&user=&page=&per_page=100`
- `/enterprises/{e}/settings/billing/budgets/{id}/user-states?user=&threshold_lower_bound=`
- `/enterprises/{e}/settings/billing/ai_credit/usage?year=&month=&day=&user=&organization=&model=&product=&cost_center_id=`
- `/enterprises/{e}/settings/billing/usage/summary`
- `/enterprises/{e}/members/{user}/teams` (attribution)
- `/orgs/{org}/members/{user}/copilot`, `/enterprises/{e}/copilot/billing/seats` (attribution)

The script only allows these paths, and only with GET.

Docs: https://docs.github.com/en/enterprise-cloud@latest/rest/billing
