#!/usr/bin/env python3
"""Read-only GitHub Enterprise billing analyst (cost centers, budgets, AI credits).

Security model:
  * Only HTTP GET is ever issued (enforced in `_get`).
  * Authentication is delegated to `gh` (token stays in the OS keychain / GH_TOKEN).
  * All user-supplied identifiers are validated before use and passed as argv
    items (never through a shell).
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

API_VERSION = "2026-03-10"
SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,98}[A-Za-z0-9])?$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:_[A-Za-z0-9]+)?$")
ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
AI_SKUS = {"ai_credits", "premium_requests", "copilot_premium_request"}


ALLOWED_PATHS = (
    re.compile(r"^/enterprises/[A-Za-z0-9-]+/(settings/billing/|members/[A-Za-z0-9_-]+/teams$|copilot/billing/seats$)"),
    re.compile(r"^/orgs/[A-Za-z0-9_.-]+/members/[A-Za-z0-9_-]+/copilot$"),
)


class ApiError(RuntimeError):
    def __init__(self, msg: str, status: Optional[int] = None):
        super().__init__(msg)
        self.status = status


def _check(value: str, pattern: "re.Pattern[str]", what: str) -> str:
    if not value or not pattern.match(value):
        raise SystemExit(f"error: invalid {what}: {value!r}")
    return value


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Issue a GET through `gh api`. No other HTTP method is possible."""
    if not any(p.match(path) for p in ALLOWED_PATHS):
        raise ApiError(f"refusing path outside allowlist: {path}")
    query = "&".join(f"{k}={v}" for k, v in (params or {}).items() if v is not None)
    url = f"{path}?{query}" if query else path
    cmd = [
        "gh", "api", "--method", "GET",
        "-H", "Accept: application/vnd.github+json",
        "-H", f"X-GitHub-Api-Version: {API_VERSION}",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip()
        m = re.search(r"HTTP (\d{3})", msg)
        status = int(m.group(1)) if m else None
        # GitHub hides missing-scope errors behind 404; gh reveals them via a hint.
        if "needs the" in msg and "scope" in msg:
            status = 403
        raise ApiError(f"GET {url} failed: {msg}", status)
    return json.loads(proc.stdout or "null")


# ---------------------------------------------------------------- fetchers --

def list_cost_centers(ent: str) -> List[Dict[str, Any]]:
    data = _get(f"/enterprises/{ent}/settings/billing/cost-centers", {"state": "active"})
    return data.get("costCenters", [])


def get_cost_center(ent: str, cc_id: str) -> Dict[str, Any]:
    """Fetch a cost center with all resource pages merged."""
    page, merged = 1, None
    while True:
        d = _get(f"/enterprises/{ent}/settings/billing/cost-centers/{cc_id}",
                 {"page": page, "per_page": 100})
        if merged is None:
            merged = d
        else:
            merged["resources"].extend(d.get("resources", []))
        if not d.get("has_next_page"):
            return merged
        page += 1


def list_budgets(ent: str, scope: Optional[str] = None, user: Optional[str] = None) -> Dict[str, Any]:
    page, budgets, extra = 1, [], {}
    while True:
        d = _get(f"/enterprises/{ent}/settings/billing/budgets",
                 {"page": page, "per_page": 100, "scope": scope, "user": user})
        budgets.extend(d.get("budgets", []))
        extra = {k: v for k, v in d.items() if k not in ("budgets", "has_next_page")}
        if not d.get("has_next_page"):
            break
        page += 1
    extra["budgets"] = budgets
    return extra


def budget_user_states(ent: str, budget_id: str, user: Optional[str] = None,
                       lower: Optional[int] = None) -> List[Dict[str, Any]]:
    page, out = 1, []
    while True:
        d = _get(f"/enterprises/{ent}/settings/billing/budgets/{budget_id}/user-states",
                 {"page": page, "per_page": 100, "sort_order": 0, "user": user,
                  "threshold_lower_bound": lower})
        out.extend(d.get("user_states", []))
        if not d.get("has_next_page"):
            return out
        page += 1


def ai_usage(ent: str, year: int, month: int, **filters: Optional[str]) -> Dict[str, Any]:
    return _get(f"/enterprises/{ent}/settings/billing/ai_credit/usage",
                {"year": year, "month": month, **filters})


# ---------------------------------------------------------------- helpers ---

def _is_ai(b: Dict[str, Any]) -> bool:
    return b.get("budget_product_sku") in AI_SKUS or b.get("budget_type") == "BundlePricing"


def _usage_totals(report: Dict[str, Any]) -> Dict[str, Any]:
    items = report.get("usageItems", [])
    by_model: Dict[str, float] = {}
    for i in items:
        by_model[i.get("model") or i.get("sku")] = by_model.get(i.get("model") or i.get("sku"), 0) + i.get("netAmount", 0)
    return {
        "gross_quantity": round(sum(i.get("grossQuantity", 0) for i in items), 2),
        "gross_amount": round(sum(i.get("grossAmount", 0) for i in items), 2),
        "discount_amount": round(sum(i.get("discountAmount", 0) for i in items), 2),
        "net_amount": round(sum(i.get("netAmount", 0) for i in items), 2),
        "net_amount_by_model": {k: round(v, 2) for k, v in sorted(by_model.items(), key=lambda x: -x[1])},
    }


def _now() -> dt.date:
    return dt.date.today()


# ---------------------------------------------------------------- commands --

def cmd_cost_centers(a: argparse.Namespace) -> Dict[str, Any]:
    out = []
    for cc in list_cost_centers(a.enterprise):
        types: Dict[str, int] = {}
        for r in cc.get("resources", []):
            types[r["type"]] = types.get(r["type"], 0) + 1
        pool = cc.get("ai_credit_pool_state") or {}
        tgt, cur = pool.get("target_amount"), pool.get("current_amount")
        out.append({
            "id": cc["id"], "name": cc["name"],
            "resource_counts": types,
            "ai_credit_pool_enabled": cc.get("ai_credit_pool_enabled", False),
            "ai_pool_target": tgt, "ai_pool_consumed": cur,
            "ai_pool_remaining": None if tgt is None or cur is None else round(tgt - cur, 2),
        })
    return {"cost_centers": out}


def cmd_budgets(a: argparse.Namespace) -> Dict[str, Any]:
    data = list_budgets(a.enterprise, scope=a.scope)
    rows = []
    for b in data["budgets"]:
        if a.ai_only and not _is_ai(b):
            continue
        amt, used = b.get("budget_amount"), b.get("consumed_amount")
        rows.append({
            "id": b["id"], "scope": b["budget_scope"],
            "entity": b.get("budget_entity_name") or b.get("user") or "",
            "sku": b.get("budget_product_sku"), "type": b.get("budget_type"),
            "amount": amt, "consumed": used,
            "pct_used": None if not amt or used is None else round(100 * used / amt, 1),
            "hard_stop": b.get("prevent_further_usage"),
            "alerts": (b.get("budget_alerting") or {}).get("will_alert"),
            "expires_at": b.get("expires_at"),
        })
    return {"total": len(rows), "budgets": rows}


def cmd_at_risk(a: argparse.Namespace) -> Dict[str, Any]:
    """Users close to their universal (multi-user) AI budget."""
    universal = [b for b in list_budgets(a.enterprise, scope="multi_user_customer")["budgets"]
                 if _is_ai(b) and b.get("budget_scope") == "multi_user_customer"]
    out = []
    for b in universal:
        states = budget_user_states(a.enterprise, b["id"], lower=a.threshold)
        out.append({"budget_id": b["id"], "universal_amount": b["budget_amount"],
                    "users": [dict(s, pct_used=round(100 * s["consumed_amount"] / s["target_amount"], 1)
                                   if s.get("target_amount") else None) for s in states[: a.limit]],
                    "count": len(states)})
    return {"threshold_pct": a.threshold, "universal_budgets": out}


def cmd_usage(a: argparse.Namespace) -> Dict[str, Any]:
    today = _now()
    year, month = a.year or today.year, a.month or today.month
    rep = ai_usage(a.enterprise, year, month, organization=a.org, user=a.user,
                   cost_center_id=a.cost_center)
    return {"period": f"{year}-{month:02d}", "filters": {"org": a.org, "user": a.user,
            "cost_center_id": a.cost_center}, **_usage_totals(rep)}


def _norm_team(name: str) -> str:
    n = name.lower()
    return n[4:] if n.startswith("ent:") else n


def _all_resources(ent: str, cc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The list endpoint returns full resource lists (verified with >1500 users)."""
    return cc.get("resources") or []


def user_teams(ent: str, login: str) -> List[Dict[str, Any]]:
    page, out = 1, []
    while True:
        d = _get(f"/enterprises/{ent}/members/{login}/teams", {"page": page, "per_page": 100}) or []
        out.extend(d)
        if len(d) < 100:
            return out
        page += 1


def copilot_seat_orgs(ent: str, login: str, candidate_orgs: List[str],
                      warnings: List[str]) -> Optional[List[str]]:
    """Orgs that grant the user a Copilot seat, restricted to candidate orgs.

    Tries the per-org seat endpoint (needs org owner); falls back to the enterprise seat
    list (needs enterprise owner / billing manager). Returns None if undeterminable.
    """
    granted, unknown = [], []
    for i, org in enumerate(candidate_orgs):
        try:
            seat = _get(f"/orgs/{org}/members/{login}/copilot")
            granted.append((seat.get("organization") or {}).get("login") or org)
        except ApiError as e:
            if e.status in (404, 422):
                continue
            if "scope" in str(e):  # token-wide problem: every remaining org would fail too
                unknown.extend(candidate_orgs[i:])
                break
            unknown.append(org)
    if not unknown:
        return granted
    try:
        page = 1
        while True:
            d = _get(f"/enterprises/{ent}/copilot/billing/seats", {"page": page, "per_page": 100})
            for s in d.get("seats", []):
                if (s.get("assignee") or {}).get("login", "").lower() == login.lower():
                    org = (s.get("organization") or {}).get("login")
                    if org and org.lower() in {o.lower() for o in candidate_orgs}:
                        granted.append(org)
            if len(d.get("seats", [])) < 100:
                return sorted(set(granted))
            page += 1
    except ApiError as e:
        warnings.append(f"Could not read Copilot seat assignments for {len(unknown)} cost-center org(s) "
                        f"(HTTP {e.status}); org-based attribution not evaluated. Needs org owner "
                        "with read:org, or enterprise owner/billing manager with "
                        "manage_billing:copilot (run: gh auth refresh -s manage_billing:copilot).")
        return granted or None


def resolve_cost_center(ent: str, login: str, warnings: List[str]):
    """Apply GitHub's AI-credit attribution precedence. Returns (cost_center|None, attribution)."""
    n_warn = len(warnings)
    cc, attr = _resolve_cost_center(ent, login, warnings)
    attr["complete"] = len(warnings) == n_warn
    if not attr["complete"] and attr["method"] in ("enterprise", "license_org"):
        warnings.append("Attribution is a best guess: some precedence checks could not run "
                        "(see warnings above).")
    return cc, attr


def _resolve_cost_center(ent: str, login: str, warnings: List[str]):
    ccs = list_cost_centers(ent)
    res_by_cc = {cc["id"]: _all_resources(ent, cc) for cc in ccs}
    by_id = {cc["id"]: cc for cc in ccs}

    def find(rtype: str, match) -> List[Dict[str, Any]]:
        return [by_id[i] for i, rs in res_by_cc.items()
                if any(r.get("type", "").lower() == rtype and match(r.get("name", "")) for r in rs)]

    # 1. direct user assignment
    hit = find("user", lambda n: n.lower() == login.lower())
    if hit:
        return hit[0], {"method": "direct"}

    # 2. enterprise team membership (oldest team wins)
    if any(r.get("type", "").lower() == "team" for rs in res_by_cc.values() for r in rs):
        try:
            teams = sorted(user_teams(ent, login), key=lambda t: t.get("created_at", ""))
            for t in teams:
                keys = {_norm_team(t.get("slug", "")), _norm_team(t.get("name", ""))}
                hit = find("team", lambda n: _norm_team(n) in keys)
                if hit:
                    if len([x for x in teams if find("team", lambda n, x=x: _norm_team(n) in
                            {_norm_team(x.get("slug", "")), _norm_team(x.get("name", ""))})]) > 1:
                        warnings.append("User is in several enterprise teams assigned to cost "
                                        "centers; the oldest team was applied.")
                    return hit[0], {"method": "enterprise_team", "team": t.get("slug"),
                                    "team_created_at": t.get("created_at")}
        except ApiError as e:
            warnings.append(f"Could not list the user's enterprise teams (HTTP {e.status}); "
                            "team-based attribution not evaluated. Token needs read:enterprise "
                            "(run: gh auth refresh -s read:enterprise).")

    # 3. organization that granted the Copilot license
    org_ccs = {r["name"]: by_id[i] for i, rs in res_by_cc.items()
               for r in rs if r.get("type", "").lower() in ("org", "organization")}
    if org_ccs:
        orgs = copilot_seat_orgs(ent, login, sorted(org_ccs), warnings)
        if orgs:
            distinct = {org_ccs[o]["id"] for o in orgs if o in org_ccs}
            if len(distinct) > 1:
                warnings.append(f"User has Copilot seats from several orgs in different cost "
                                f"centers ({', '.join(orgs)}); GitHub allocates to one of them — "
                                "confirm with a detailed usage report.")
            return org_ccs[orgs[0]], {"method": "license_org", "org": orgs[0], "seat_orgs": orgs}

    # 4. enterprise (no cost center)
    return None, {"method": "enterprise"}


def cmd_user(a: argparse.Namespace) -> Dict[str, Any]:
    """Explain how much AI-credit capacity a given user has left this month."""
    ent, login = a.enterprise, a.login
    today = _now()
    warnings: List[str] = []

    # 1. cost center attribution (direct > enterprise team > license-granting org > enterprise)
    cc_hit, attribution = resolve_cost_center(ent, login, warnings)

    # 2. budgets that apply to this user
    bdata = list_budgets(ent, user=login)
    ai_budgets = [b for b in bdata["budgets"] if _is_ai(b)]
    applicable = []
    for b in ai_budgets:
        scope = b["budget_scope"]
        relevant = (
            scope in ("enterprise", "multi_user_customer")
            or (scope == "user" and (b.get("user") or "").lower() == login.lower())
            or (scope in ("cost_center", "multi_user_cost_center") and cc_hit is not None
                and b.get("budget_entity_name") in (cc_hit["id"], cc_hit["name"]))
        )
        if scope == "organization":
            warnings.append(f"Org-scoped AI budget on '{b.get('budget_entity_name')}' "
                            f"(${b.get('budget_amount')}) may also apply if the user belongs to that org.")
        if not relevant:
            continue
        if b.get("consumed_amount") is None and scope != "multi_user_customer":
            warnings.append(f"{scope} budget {b['id']} (${b.get('budget_amount')}, hard_stop="
                            f"{b.get('prevent_further_usage')}) has no consumption data from the API; "
                            "remaining amount unknown — excluded from capacity calculation.")
        amt, used = b.get("budget_amount"), b.get("consumed_amount")
        if scope == "multi_user_customer":
            st = budget_user_states(ent, b["id"], user=login)
            if st:
                amt, used = st[0].get("target_amount", amt), st[0].get("consumed_amount", used)
        applicable.append({
            "id": b["id"], "scope": scope, "entity": b.get("budget_entity_name") or b.get("user"),
            "sku": b.get("budget_product_sku"), "amount": amt, "consumed": used,
            "remaining": None if amt is None or used is None else round(amt - used, 2),
            "hard_stop": b.get("prevent_further_usage"), "expires_at": b.get("expires_at"),
        })

    # 3. AI credit pool of the cost center
    pool = None
    if cc_hit and cc_hit.get("ai_credit_pool_enabled"):
        st = cc_hit.get("ai_credit_pool_state") or {}
        if st.get("target_amount") is not None and st.get("current_amount") is not None:
            pool = {"target": st["target_amount"], "consumed": st["current_amount"],
                    "remaining": round(st["target_amount"] - st["current_amount"], 2)}

    # 4. actual usage this month
    usage = _usage_totals(ai_usage(ent, today.year, today.month, user=login))

    # 5. binding constraint = smallest remaining among hard-stop limits
    limits = [(f"{b['scope']} budget {b['id']}", b["remaining"]) for b in applicable
              if b["hard_stop"] and b["remaining"] is not None]
    if pool:
        limits.append((f"cost center AI credit pool '{cc_hit['name']}'", pool["remaining"]))
    eff = bdata.get("effective_budget")
    if eff and eff.get("budget_amount") is not None:
        limits.append((f"effective budget {eff.get('id')}",
                       round(eff["budget_amount"] - eff.get("consumed_amount", 0), 2)))
    binding = min(limits, key=lambda x: x[1]) if limits else None

    return {
        "user": login, "period": f"{today.year}-{today.month:02d}",
        "cost_center": None if not cc_hit else {"id": cc_hit["id"], "name": cc_hit["name"],
                                                "ai_credit_pool_enabled": cc_hit.get("ai_credit_pool_enabled", False)},
        "attribution": attribution,
        "ai_credit_pool": pool,
        "applicable_ai_budgets": applicable,
        "effective_budget": eff,
        "usage_this_month": usage,
        "remaining_capacity_usd": None if not binding else max(binding[1], 0),
        "binding_constraint": None if not binding else binding[0],
        "blocked": bool(binding and binding[1] <= 0),
        "unlimited": binding is None,
        "warnings": warnings,
    }


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--enterprise", "-e", required=True, help="enterprise slug")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("cost-centers", help="list active cost centers + AI credit pool state")

    b = sub.add_parser("budgets", help="list budgets")
    b.add_argument("--scope", choices=["enterprise", "organization", "repository", "cost_center",
                                       "multi_user_customer", "user"])
    b.add_argument("--ai-only", action="store_true")

    r = sub.add_parser("at-risk", help="users near their universal AI budget")
    r.add_argument("--threshold", type=int, default=80)
    r.add_argument("--limit", type=int, default=50)

    u = sub.add_parser("usage", help="AI credit usage report (aggregated)")
    u.add_argument("--year", type=int)
    u.add_argument("--month", type=int, choices=range(1, 13))
    u.add_argument("--org")
    u.add_argument("--user")
    u.add_argument("--cost-center")

    us = sub.add_parser("user", help="AI credit capacity for one user")
    us.add_argument("login")

    a = p.parse_args(argv)
    _check(a.enterprise, SLUG_RE, "enterprise slug")
    if getattr(a, "login", None):
        _check(a.login, LOGIN_RE, "user login")
    if getattr(a, "user", None):
        _check(a.user, LOGIN_RE, "user login")
    if getattr(a, "org", None):
        _check(a.org, SLUG_RE, "organization")
    if getattr(a, "cost_center", None):
        _check(a.cost_center, ID_RE, "cost center id")
    if getattr(a, "threshold", None) is not None and not 0 <= a.threshold <= 1000:
        raise SystemExit("error: threshold must be 0-1000")

    handlers = {"cost-centers": cmd_cost_centers, "budgets": cmd_budgets, "at-risk": cmd_at_risk,
                "usage": cmd_usage, "user": cmd_user}
    try:
        print(json.dumps(handlers[a.cmd](a), indent=2))
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
