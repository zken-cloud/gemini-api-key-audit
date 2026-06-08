#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys

# Services reachable via an API key that bill per-call for generative AI.
# An unrestricted key in a project where any of these is enabled is the
# classic "Maps key drains your Gemini budget" scenario.
AI_SERVICES = {
    "generativelanguage.googleapis.com",  # Gemini API (the usual culprit)
    "aiplatform.googleapis.com",           # Vertex AI (express-mode API keys)
}

# The four kinds of *application* restriction a key can carry. Presence of any
# one means the key is locked to a caller (referrer / IP / app / bundle id).
APP_RESTRICTION_FIELDS = (
    "browserKeyRestrictions",  # allowedReferrers   (HTTP referrers)
    "serverKeyRestrictions",   # allowedIps         (IP addresses)
    "androidKeyRestrictions",  # allowedApplications
    "iosKeyRestrictions",      # allowedBundleIds
)

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "OK": 4}

# ANSI colors, disabled automatically when stdout is not a TTY.
_COLORS = {
    "CRITICAL": "\033[1;37;41m",  # white on red
    "HIGH": "\033[1;31m",          # bold red
    "MEDIUM": "\033[1;33m",        # bold yellow
    "LOW": "\033[0;36m",           # cyan
    "OK": "\033[0;32m",            # green
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
}


def color(text: str, key: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{_COLORS.get(key, '')}{text}{_COLORS['reset']}"


def run_gcloud(args: list[str]) -> str:
    """Run a gcloud command, returning stdout. Exits with a helpful message on failure."""
    cmd = ["gcloud"] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("error: 'gcloud' not found on PATH. Install the Google Cloud SDK.")
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        hint = ""
        if "cloudasset.googleapis.com" in stderr and "has not been used" in stderr:
            hint = ("\nhint: enable the Cloud Asset API on your billing project:\n"
                    "      gcloud services enable cloudasset.googleapis.com "
                    "--project=<billing-project>")
        elif "USER_PROJECT_DENIED" in stderr or "serviceUsageConsumer" in stderr:
            hint = ("\nhint: pass --billing-project=<a project you can use>, and ensure you "
                    "have serviceusage.services.use on it.")
        elif "PERMISSION_DENIED" in stderr or "does not have permission" in stderr:
            hint = ("\nhint: you need roles/cloudasset.viewer at the scope you are auditing.")
        sys.exit(f"error: gcloud command failed:\n  $ {' '.join(cmd)}\n{stderr}{hint}")
    return proc.stdout


def asset_list(scope_flag: str, scope_value: str, asset_type: str, billing_project: str) -> list[dict]:
    """One Cloud Asset Inventory call returning full resource data for an asset type."""
    args = [
        "asset", "list",
        scope_flag, scope_value,
        "--asset-types", asset_type,
        "--content-type", "resource",
        "--format", "json",
    ]
    if billing_project:
        args += ["--billing-project", billing_project]
    out = run_gcloud(args)
    return json.loads(out) if out.strip() else []


def ensure_asset_api(billing_project: str, auto_enable: bool, use_color: bool) -> None:
    """Check that the Cloud Asset API is enabled on the billing/quota project, and
    enable it if it isn't (unless auto_enable is False). Best-effort: if the check
    itself can't run (e.g. no serviceusage access), we stay quiet and let the actual
    asset call surface a precise error / trigger the auto fallback."""
    if not billing_project:
        return
    probe = subprocess.run(
        ["gcloud", "services", "list", "--enabled", "--project", billing_project,
         "--filter", "config.name:cloudasset.googleapis.com",
         "--format", "value(config.name)"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        return  # can't tell; defer to the real call's error handling
    if probe.stdout.strip():
        return  # already enabled
    # Not enabled.
    if not auto_enable:
        sys.exit(f"error: Cloud Asset API is not enabled on '{billing_project}'.\n"
                 f"hint: enable it (or re-run without --no-enable):\n"
                 f"      gcloud services enable cloudasset.googleapis.com "
                 f"--project={billing_project}")
    print(color(f"Cloud Asset API not enabled on '{billing_project}' — enabling it now...",
                "MEDIUM", use_color), file=sys.stderr)
    run_gcloud(["services", "enable", "cloudasset.googleapis.com", "--project", billing_project])
    print(color("Enabled. (New API keys created in the last few minutes may take a short "
                "while to appear in Asset Inventory.)", "dim", use_color), file=sys.stderr)


def project_number_from_name(name: str) -> str | None:
    """Extract the project number from a resource name like
    //apikeys.googleapis.com/projects/12345/locations/global/keys/uuid
    or a parent like //cloudresourcemanager.googleapis.com/projects/12345 ."""
    parts = name.split("/")
    if "projects" in parts:
        i = parts.index("projects")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def is_gemini_key(data: dict) -> bool:
    """True if this is an AI Studio / Gemini API key — i.e. it carries the
    generative-language annotation that AI Studio stamps, or is API-restricted to
    the Generative Language service. These are the keys people paste into client
    code and public repos, then get drained."""
    if (data.get("annotations") or {}).get("generative-language"):
        return True
    targets = (data.get("restrictions") or {}).get("apiTargets") or []
    return any(t.get("service") == "generativelanguage.googleapis.com" for t in targets)


def evaluate_key(data: dict, project_has_ai: bool):
    """Classify a key from its restriction data and whether its project has a
    billable AI API enabled. Returns (severity, reasons, has_api_restriction,
    has_app_restriction, reaches_ai)."""
    restrictions = data.get("restrictions", {}) or {}
    api_targets = restrictions.get("apiTargets")  # list or None
    has_api_restriction = bool(api_targets)
    has_app_restriction = any(restrictions.get(f) for f in APP_RESTRICTION_FIELDS)

    # Which AI services can this key actually reach?
    if has_api_restriction:
        allowed = {t.get("service") for t in api_targets if t.get("service")}
        # A "*" or empty service means "all" in some encodings; treat unknown as broad.
        if "*" in allowed or any(not s for s in allowed):
            reaches_ai = project_has_ai
        else:
            reaches_ai = bool(allowed & AI_SERVICES)
    else:
        # No API restriction: key can call any API enabled in the project.
        reaches_ai = project_has_ai

    gemini = is_gemini_key(data)

    reasons: list[str] = []
    if gemini:
        reasons.append("AI Studio / Gemini API key")
    if not has_api_restriction:
        reasons.append("no API restriction (can call ANY enabled API)")
    if not has_app_restriction:
        reasons.append("no application restriction (usable from anywhere if leaked)")
    if reaches_ai and not gemini:
        reasons.append("can reach billable Gemini/Vertex AI APIs")

    # Severity model
    if not has_api_restriction and reaches_ai:
        severity = "CRITICAL"  # the Maps-key-drains-Gemini scenario
    elif not has_api_restriction:
        severity = "HIGH"      # can call any enabled (billable) API
    elif reaches_ai and not has_app_restriction:
        severity = "HIGH"      # AI-capable key with no lock; leak = abuse
    elif not has_app_restriction:
        severity = "MEDIUM"    # API-restricted but portable if leaked
    else:
        severity = "OK"        # both API- and app-restricted

    return severity, reasons, has_api_restriction, has_app_restriction, reaches_ai


class AssetUnavailable(Exception):
    """Raised when Cloud Asset Inventory can't be used (API off or no permission)."""


def gather_asset_mode(scope_flag: str, scope_value: str, billing: str) -> list[dict]:
    """Enumerate keys org/folder/project-wide via Cloud Asset Inventory.
    Returns a list of records: {data, project_id, project_has_ai}.
    Raises AssetUnavailable if the Asset API is off or access is denied."""
    try:
        keys = asset_list(scope_flag, scope_value, "apikeys.googleapis.com/Key", billing)
        services = asset_list(scope_flag, scope_value,
                              "serviceusage.googleapis.com/Service", billing)
        projects = asset_list(scope_flag, scope_value,
                              "cloudresourcemanager.googleapis.com/Project", billing)
    except SystemExit as e:
        # run_gcloud exits on failure; convert the common asset-unavailable cases.
        msg = str(e)
        if any(s in msg for s in ("cloudasset.googleapis.com", "does not have permission",
                                  "PERMISSION_DENIED", "USER_PROJECT_DENIED")):
            raise AssetUnavailable(msg)
        raise

    ai_projects: set[str] = set()
    for s in services:
        data = s.get("resource", {}).get("data", {})
        if data.get("state") == "ENABLED" and data.get("name") in AI_SERVICES:
            pn = project_number_from_name(s.get("name", ""))
            if pn:
                ai_projects.add(pn)

    num_to_id: dict[str, str] = {}
    for p in projects:
        data = p.get("resource", {}).get("data", {})
        pid, pnum = data.get("projectId"), str(data.get("projectNumber", "")) or None
        if pid and pnum:
            num_to_id[pnum] = pid

    records = []
    for k in keys:
        data = k.get("resource", {}).get("data", {})
        pnum = project_number_from_name(k.get("name", ""))
        records.append({
            "data": data,
            "project_id": num_to_id.get(pnum, pnum or "unknown"),
            "project_has_ai": pnum in ai_projects if pnum else False,
        })
    return records


def gather_project_mode(project_ids: list[str]) -> list[dict]:
    """Enumerate keys by iterating projects directly — no Asset API needed.
    Only checks enabled services for projects that actually have keys."""
    records = []
    for pid in project_ids:
        out = run_gcloud(["services", "api-keys", "list", "--project", pid, "--format", "json"])
        keys = json.loads(out) if out.strip() else []
        if not keys:
            continue
        # Does this project have a billable AI API enabled? One call per project-with-keys.
        svc_out = run_gcloud([
            "services", "list", "--enabled", "--project", pid,
            "--filter", "config.name:(generativelanguage.googleapis.com OR aiplatform.googleapis.com)",
            "--format", "value(config.name)",
        ])
        project_has_ai = bool(svc_out.strip())
        for data in keys:
            records.append({"data": data, "project_id": pid, "project_has_ai": project_has_ai})
    return records


def list_visible_projects() -> list[str]:
    out = run_gcloud(["projects", "list", "--format", "value(projectId)"])
    return [p for p in out.split() if p]


def detect_scope(explicit: str | None) -> tuple[str, str]:
    """Return (gcloud_flag, value) for the audit scope."""
    if explicit:
        if explicit.startswith("organizations/"):
            return "--organization", explicit.split("/", 1)[1]
        if explicit.startswith("folders/"):
            return "--folder", explicit.split("/", 1)[1]
        if explicit.startswith("projects/"):
            return "--project", explicit.split("/", 1)[1]
        sys.exit("error: --scope must be organizations/<id>, folders/<id>, or projects/<id>")
    # Auto-detect: use the single org the caller can see.
    out = run_gcloud(["organizations", "list", "--format=value(ID)"])
    orgs = [o for o in out.split() if o]
    if len(orgs) == 1:
        return "--organization", orgs[0]
    if not orgs:
        sys.exit("error: no organization found. Pass --scope projects/<id> or "
                 "--scope organizations/<id>.")
    sys.exit("error: multiple organizations visible: " + ", ".join(orgs) +
             "\n       pass --scope organizations/<id> to choose one.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Audit GCP API keys for missing restrictions across an org/folder/project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--scope", help="organizations/<id>, folders/<id>, or projects/<id>. "
                                    "Defaults to the single org you can see.")
    ap.add_argument("--mode", choices=("auto", "asset", "project"), default="auto",
                    help="auto (default): use Asset Inventory, fall back to per-project scan if "
                         "it's unavailable. asset: Asset Inventory only. project: per-project scan "
                         "(no Asset API needed; also catches AI Studio keys in projects outside "
                         "the audited org).")
    ap.add_argument("--projects", help="Comma-separated project IDs to scan directly. Implies "
                                       "--mode project and overrides --scope enumeration.")
    ap.add_argument("--billing-project", default=None,
                    help="Quota/billing project for the Asset API "
                         "(defaults to your gcloud config project).")
    ap.add_argument("--no-enable", action="store_true",
                    help="Do not auto-enable the Cloud Asset API if it is off; error instead.")
    ap.add_argument("--csv", metavar="FILE", help="Write the full findings as CSV to FILE.")
    ap.add_argument("--all", action="store_true",
                    help="Include well-restricted (OK) keys in the table.")
    ap.add_argument("--no-color", action="store_true", help="Disable colored output.")
    args = ap.parse_args()

    use_color = sys.stdout.isatty() and not args.no_color

    billing = args.billing_project
    if billing is None:
        billing = run_gcloud(["config", "get-value", "project"]).strip()
        if billing in ("", "(unset)"):
            billing = ""

    # --- Decide mode and gather records ------------------------------------
    explicit_projects = [p.strip() for p in args.projects.split(",")] if args.projects else None
    mode = "project" if explicit_projects else args.mode
    records: list[dict] = []

    if mode in ("auto", "asset"):
        scope_flag, scope_value = detect_scope(args.scope)
        scope_label = scope_flag.replace("--", "") + " " + scope_value
        print(color(f"Auditing API keys under {scope_label} via Asset Inventory "
                    f"(billing project: {billing or 'default'})", "bold", use_color),
              file=sys.stderr)
        ensure_asset_api(billing, auto_enable=not args.no_enable, use_color=use_color)
        try:
            records = gather_asset_mode(scope_flag, scope_value, billing)
        except AssetUnavailable as e:
            if mode == "asset":
                sys.exit(f"error: Cloud Asset Inventory is unavailable.\n{e}")
            # auto: fall back to per-project scan.
            print(color("\nNotice: Asset Inventory unavailable — falling back to per-project "
                        "scan.", "MEDIUM", use_color), file=sys.stderr)
            print(color("        (covers only projects your account can list; enable "
                        "cloudasset.googleapis.com for full org coverage.)", "dim", use_color),
                  file=sys.stderr)
            mode = "project"

    if mode == "project":
        project_ids = explicit_projects
        if project_ids is None:
            if args.scope and args.scope.startswith("projects/"):
                project_ids = [args.scope.split("/", 1)[1]]
            else:
                project_ids = list_visible_projects()
        scope_label = (f"{len(project_ids)} project(s)" if not explicit_projects
                       else ", ".join(project_ids))
        print(color(f"Scanning {scope_label} per-project (no Asset API).", "bold", use_color),
              file=sys.stderr)
        records = gather_project_mode(project_ids)

    # --- Evaluate -----------------------------------------------------------
    findings = []
    for rec in records:
        data = rec["data"]
        sev, reasons, has_api, has_app, reaches_ai = evaluate_key(data, rec["project_has_ai"])
        findings.append({
            "severity": sev,
            "project_id": rec["project_id"],
            "display_name": data.get("displayName", "(unnamed)"),
            "key_uid": data.get("uid"),
            "resource_name": data.get("name"),
            "is_gemini_key": is_gemini_key(data),
            "has_api_restriction": has_api,
            "has_app_restriction": has_app,
            "reaches_ai": reaches_ai,
            "ai_enabled_in_project": rec["project_has_ai"],
            "reasons": reasons,
            "create_time": data.get("createTime"),
        })

    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]],
                                 f["project_id"], f["display_name"]))

    # --- Report -------------------------------------------------------------
    print_table(findings, args.all, use_color)
    print_summary(findings, len(records), use_color)

    if args.csv:
        write_csv(args.csv, findings)
        print(f"\nFull CSV report written to {args.csv}", file=sys.stderr)

    if any(f["severity"] == "CRITICAL" for f in findings):
        return 2
    return 0


CSV_COLUMNS = [
    "severity", "project_id", "display_name", "key_uid", "resource_name",
    "is_gemini_key", "has_api_restriction", "has_app_restriction",
    "reaches_ai", "ai_enabled_in_project", "reasons", "create_time",
]


def _csv_safe(value):
    """Defang spreadsheet formula injection: a key's display name is attacker-
    influenceable, so a cell starting with = + - @ (or tab/CR) could execute as a
    formula in Excel/Sheets. Prefix such values with a single quote."""
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def write_csv(path: str, findings: list[dict]) -> None:
    """Write all findings (including OK ones) to a CSV file, one row per key."""
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for f in findings:
            row = {k: _csv_safe(v) for k, v in f.items()}
            row["reasons"] = _csv_safe("; ".join(f.get("reasons", [])))  # flatten list for CSV
            writer.writerow(row)


def print_table(findings: list[dict], show_all: bool, use_color: bool) -> None:
    shown = [f for f in findings if show_all or f["severity"] != "OK"]
    if not shown:
        print(color("\n✓ No keys missing restrictions found in scope.", "OK", use_color))
        return

    print()
    header = f"{'SEVERITY':<9}  {'PROJECT':<28}  {'KEY NAME':<32}  RISK"
    print(color(header, "bold", use_color))
    print(color("-" * len(header), "dim", use_color))
    for f in shown:
        sev = f["severity"]
        sev_cell = color(f"{sev:<9}", sev, use_color)
        proj = f["project_id"][:28]
        keyname = f["display_name"][:32]
        risk = "; ".join(f["reasons"]) if f["reasons"] else "well restricted"
        print(f"{sev_cell}  {proj:<28}  {keyname:<32}  {risk}")


def print_summary(findings: list[dict], total: int, use_color: bool) -> None:
    counts = {s: 0 for s in SEVERITY_ORDER}
    for f in findings:
        counts[f["severity"]] += 1
    print()
    parts = []
    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "OK"):
        if counts[sev]:
            parts.append(color(f"{counts[sev]} {sev}", sev, use_color))
    print(color("Summary: ", "bold", use_color) + f"{total} keys scanned — " +
          (", ".join(parts) if parts else "none"))
    if counts["CRITICAL"]:
        print(color("\n⚠ CRITICAL: unrestricted key(s) in project(s) with Gemini/Vertex enabled. "
                    "These can be abused to run up AI bills.", "CRITICAL", use_color))
        print("  Remediate: add an API restriction (Console → APIs & Services → Credentials → "
              "the key → API restrictions), or restrict/rotate the key.")


if __name__ == "__main__":
    sys.exit(main())
