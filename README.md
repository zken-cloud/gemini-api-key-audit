# gemini-api-key-audit

Quickly find **unrestricted GCP API keys** across an entire organization, folder, or
project — and flag the ones that can be abused to run up **Gemini / Vertex AI bills**
(including **Google AI Studio** keys).

## What it flags

| Severity   | Condition |
|------------|-----------|
| **CRITICAL** | No API restriction **and** Gemini/Vertex AI is enabled in the key's project — an old, embedded "Maps" key that can now call paid Gemini endpoints. |
| **HIGH**     | No API restriction at all (can call *any* enabled, billable API); **or** an AI Studio / Gemini key with no application lock. |
| **MEDIUM**   | API-restricted, but **no application restriction** — still abusable if the key leaks. |
| **OK**       | Both an API restriction and an application restriction are set. Hidden unless you pass `--all`. |

AI Studio / Gemini keys are detected (via the `generative-language` annotation or a
`generativelanguage.googleapis.com` API target) and labeled explicitly. An empty
restriction object (e.g. a Firebase key with `browserKeyRestrictions: {}` and no
allowed referrers) is correctly treated as **no effective restriction**.

> **It never reads your key secrets.** The tool only reads key *metadata and
> restrictions* — it never requests, sees, logs, or handles the actual key strings.

## How it works (two modes)

| | **asset** (default) | **project** (`--mode project`) |
|---|---|---|
| Mechanism | Cloud Asset Inventory — one call per asset type | Iterates projects: `api-keys list` + `services list` |
| Needs Cloud Asset API | Yes (auto-enabled if off) | **No** |
| Speed at org scale | Seconds | Slower (2 calls per project) |
| Coverage | Whole org/folder, incl. projects you can't otherwise list | Only projects your account can list — but **catches AI Studio keys in standalone/other orgs** |
| Brand-new key lag | A few minutes (Asset Inventory propagation) | None (reads live) |

`--mode auto` (the default) uses asset mode and **automatically falls back to project
mode** if the Asset API is unavailable, so it works either way.

## Requirements

- `gcloud` CLI, authenticated: `gcloud auth login`
- Python 3.8+ (standard library only — nothing to `pip install`)

### Permissions

Grant roles at the **scope you audit** (organization / folder / project) so they
inherit downward. The tool never needs permission to read key *secrets*.

**Asset mode (default):**

| Role | Where | Why |
|------|-------|-----|
| `roles/cloudasset.viewer` | audited org / folder / project | List API keys, enabled services, and projects via Asset Inventory |
| `roles/serviceusage.serviceUsageConsumer` | billing/quota project | Use the Asset API with that quota project (`serviceusage.services.use`) |
| `roles/serviceusage.serviceUsageAdmin` | billing/quota project | *Only if the Asset API is off and you want the script to enable it* (`serviceusage.services.enable`). Skip with `--no-enable`. |
| `roles/browser` | org | *Only for scope auto-detect* — lets the script list your organization when `--scope` is omitted |

**Project mode (`--mode project`, or the fallback):**

| Role | Where | Why |
|------|-------|-----|
| `roles/serviceusage.apiKeysViewer` | each audited project (or org/folder) | List API keys and their restrictions — **metadata only, not secrets** |
| `roles/serviceusage.serviceUsageViewer` | each audited project (or org/folder) | Check whether Gemini/Vertex APIs are enabled |
| `roles/browser` | org / folder | List projects to scan (`resourcemanager.projects.list`) when not using `--projects` |

A single broad option that covers everything for an org-wide audit:
`roles/cloudasset.viewer` + `roles/serviceusage.serviceUsageConsumer` on the org, plus
`serviceusage.services.use` on a quota project you own.

## Step-by-step guide

Once the roles above are granted, go from a fresh shell to a finished audit.

**1. Get the tool and make it executable** (needs Python 3.8+, already on most systems):

```bash
git clone https://github.com/zken-cloud/gemini-api-key-audit.git
```

```bash
cd gemini-api-key-audit
```

```bash
chmod +x gemini_api_key_audit.py
```

**2. Authenticate gcloud** as the identity that has the roles:

```bash
gcloud auth login
```

Or, for a service account:

```bash
gcloud auth activate-service-account --key-file=key.json
```

**3. Find the organization** you want to audit (note its numeric ID):

```bash
gcloud organizations list
```

**4. Pick a quota/billing project** you own and can use, and make it your default (or
skip this and pass `--billing-project` on every run instead):

```bash
gcloud config set project MY_QUOTA_PROJECT
```

**5. (Optional) Enable the Cloud Asset API** yourself. You can skip this — the script
auto-enables it on the billing project unless you pass `--no-enable`:

```bash
gcloud services enable cloudasset.googleapis.com --project=MY_QUOTA_PROJECT
```

**6. Run the audit** against your org:

```bash
./gemini_api_key_audit.py --scope organizations/123456789 --billing-project MY_QUOTA_PROJECT
```

**7. (Optional) Save a CSV** for review or CI:

```bash
./gemini_api_key_audit.py --scope organizations/123456789 --billing-project MY_QUOTA_PROJECT --csv report.csv
```

**No Cloud Asset API access?** Skip steps 4–5 and scan per-project instead — this needs
only the project-mode roles and no quota project:

```bash
./gemini_api_key_audit.py --mode project --scope organizations/123456789
```

Or target specific projects directly:

```bash
./gemini_api_key_audit.py --projects prod-app,maps-frontend,gen-lang-client-0123456789
```

Then act on the results: review the table (CRITICAL/HIGH first) and follow
[Remediating a finding](#remediating-a-finding) for each flagged key. Exit code `2`
means at least one CRITICAL was found — handy as a CI gate.

## Usage examples

Audit a whole organization (asset mode; auto-enables the Asset API if needed):

```bash
./gemini_api_key_audit.py --scope organizations/123456789 --billing-project my-quota-proj
```

Auto-detect the org (works when your account sees exactly one org):

```bash
./gemini_api_key_audit.py --billing-project my-quota-proj
```

Audit a single folder:

```bash
./gemini_api_key_audit.py --scope folders/987654321 --billing-project my-quota-proj
```

Audit a single project:

```bash
./gemini_api_key_audit.py --scope projects/my-project --billing-project my-project
```

No Cloud Asset API available? Scan per-project instead (also catches AI Studio keys
living in auto-created `gen-lang-client-*` projects outside your org):

```bash
./gemini_api_key_audit.py --mode project --scope organizations/123456789
```

Scan a specific set of projects directly (implies project mode):

```bash
./gemini_api_key_audit.py --projects prod-app,gen-lang-client-0123456789,maps-frontend
```

Don't auto-enable the Asset API; error with instructions if it's off:

```bash
./gemini_api_key_audit.py --scope organizations/123456789 --billing-project my-quota-proj --no-enable
```

Write a spreadsheet-friendly report for CI / dashboards, and include OK keys:

```bash
./gemini_api_key_audit.py --scope organizations/123456789 --billing-project my-quota-proj --csv report.csv --all
```

Example output:

```
SEVERITY   PROJECT                       KEY NAME                          RISK
-------------------------------------------------------------------------------
CRITICAL   demo-123456              Browser key (auto created by Fir  no API restriction (can call ANY enabled API); no application restriction (usable from anywhere if leaked); can reach billable Gemini/Vertex AI APIs
HIGH       gen-lang-client-123456    Default Gemini API Key            AI Studio / Gemini API key; no application restriction (usable from anywhere if leaked)

Summary: 10 keys scanned — 1 CRITICAL, 1 HIGH
```

### Options

No flag is *syntactically* required — every one has a default, so the command always
runs. But two of them are **required in practice** depending on your environment, as
the *Required?* column explains. (Running with **no arguments** only works if you have
exactly one visible organization **and** your `gcloud` default project can use the
Asset API; otherwise it stops and tells you which flag to add.)

| Flag | Required? | Description |
|------|-----------|-------------|
| `--scope` | **Required unless you have exactly one org** | `organizations/<id>`, `folders/<id>`, or `projects/<id>`. Auto-detect can't choose between multiple orgs, so it errors and asks for this. |
| `--billing-project` | **Required for asset mode unless your default project can use the Asset API** | Quota/billing project. Defaults to your `gcloud config` project; if that lacks `serviceusage.services.use`, asset mode fails (and `--mode auto` falls back to project mode). Not used in `--mode project`. |
| `--mode` | Optional | `auto` (default), `asset`, or `project`. |
| `--projects` | Optional | Comma-separated project IDs to scan directly. Implies `--mode project`. |
| `--no-enable` | Optional | Don't auto-enable the Cloud Asset API; error instead. |
| `--csv FILE` | Optional | Write full findings (all fields, incl. OK keys) as CSV. |
| `--all` | Optional | Include well-restricted (OK) keys in the on-screen table. |
| `--no-color` | Optional | Disable colored output. |

### Exit codes (CI-friendly)

- `0` — no CRITICAL findings
- `2` — at least one CRITICAL finding
- `1` — error (auth / permission / API not enabled with `--no-enable`)

## Remediating a finding

For each flagged key (Console → **APIs & Services → Credentials** → the key):

1. **Add API restrictions** — limit the key to only the APIs it actually needs
   (e.g. just the Maps APIs). This alone stops the Gemini-abuse path.
2. **Add application restrictions** — HTTP referrers, IP addresses, Android/iOS app.
3. If the key may already be public, **rotate** it.

## License

MIT — see [LICENSE](LICENSE).
