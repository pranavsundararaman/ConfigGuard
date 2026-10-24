# ConfigGuard Backend — Review 1 MVP

Core implementation:
- YAML / JSON / `.env` parsing
- Semantic configuration diff
- Change magnitude analysis
- Rule-based risk analysis
- Explainable risk classification
- FastAPI REST API
- Initial automated tests

## Run

```bash
python -m venv .venv
```

Windows:
```bash
.venv\Scripts\activate
```

macOS/Linux:
```bash
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Swagger UI:
`http://127.0.0.1:8000/docs`

## MongoDB persistence

Set `MONGODB_URI` in `.env` to use a database-backed endpoint. `MONGODB_DATABASE`
is optional and defaults to `configguard`. The application can be imported and
unit tested without `MONGODB_URI`; the first database-backed operation returns a
configuration error if it is missing. Each successful `/api/analyze` request
stores its old snapshot as deployed, its new snapshot as proposed, and the risk
result under the authenticated user's selected repository project. The analysis
references both versions. User data routes require a GitHub session; browser
writes also require the session's CSRF token.

Retrieval endpoints:

- `GET /api/config-versions?project_id={project_id}&limit=50`
- `GET /api/config-versions/{version_id}?project_id={project_id}`
- `GET /api/analyses?project_id={project_id}&limit=50`
- `GET /api/analyses/{analysis_id}?project_id={project_id}`

List limits can range from 1 to 200. Stored MongoDB ObjectIds are returned as
string `id` fields.

The legacy `/api/config-versions` and `/api/analyses` endpoints also require a
GitHub session and the requested project must belong to that signed-in user.
The V1 `/api/analyze` upload endpoint preserves its response shape but now
requires a signed-in session, CSRF header, and an active selected project.
`/api/health` and `/api/parse-config` remain public; the parser endpoint does
not persist data.

## ConfigGuard V2.1 project history

V2 configuration history is scoped by project ID. A saved configuration is a
`proposed` version by default. Save one with
`POST /api/projects/{project_id}/config-versions` using JSON such as:

```json
{
  "configuration": {"service": {"timeout": 30}},
  "format": "json"
}
```

Retrieve project history with `GET /api/projects/{project_id}/config-versions`
or one version with `GET /api/projects/{project_id}/config-versions/{version_id}`.
Mark an accepted deployment with
`POST /api/projects/{project_id}/config-versions/{version_id}/deploy`.
Retrieve project analyses with `GET /api/projects/{project_id}/analyses` or
`GET /api/projects/{project_id}/analyses/{analysis_id}`.
The frontend parser endpoint `POST /api/parse-config` accepts `{ "format": "env", "content": "KEY=value" }` and returns the normalized configuration object using the backend parser.
Use `POST /api/projects/{project_id}/historical-analysis` with a proposed
configuration and optional `min_observations` (default 3, allowed range 3–100)
to compare numeric parameters with deployed versions in that project. The
proposal is saved but remains proposed, so it does not enter later baselines
until explicitly marked deployed. Numeric outlier checks use the median and
median absolute deviation; they are advisory review signals, not predictions of
deployment failure.
Across JSON/YAML and ENV, underscore-separated field components normalize to
the same dotted historical parameter name (for example, nested `service.timeout_seconds`
and `SERVICE_TIMEOUT_SECONDS` normalize to `service.timeout.seconds`). Colliding
paths are rejected. Historical analysis assesses numeric scalar values; other
types are reported as not assessed.

The V2.3 browser editor converts structured paths to ENV by joining path
components with underscores and uppercasing them. Converting ENV back splits
every underscore into a path component and lowercases it; this is a deterministic
inference, not recovery of the original nesting, because literal underscores in
keys are ambiguous. Collisions such as `service_timeout.seconds` and
`service.timeout_seconds` are rejected. ENV values are text; the parser
normalizes numeric and boolean looking values to numbers and booleans, so such
strings cannot be safely round-tripped as strings. JSON/YAML conversions retain
numeric and boolean types.
Missing samples are reported as insufficient history, and non-numeric values
are explicitly left unassessed. Three is the minimum supported observation
count, including for direct calls to the analysis function.

Multi-document writes use MongoDB transactions on deployments that support
them. Standalone MongoDB uses a `write_operations` journal and compensating
deletes. Failed rollbacks remain marked for recovery; pending records are
excluded from API reads, and stale operations are retried on later database
reads or bundle writes. Automated tests use a fake database and do not connect
to Atlas.

Run tests:
```bash
pytest
```

## ConfigGuard V2.4 GitHub Actions (Milestone 1)

The `ConfigGuard PR analysis` workflow runs the existing parser and pairwise
risk engine directly inside the GitHub Actions runner. The `pull_request`
workflow uses the event's `pull_request.base.sha` and `pull_request.head.sha`.
Checkout uses `fetch-depth: 0`, so both commit objects and their merge base are
available for the three-dot comparison, including newly opened and updated
pull requests. It does not call the FastAPI service, MongoDB, or the
historical-analysis endpoint. Supported files are JSON, YAML/YML, `.env`,
`*.env`, and dotenv names such as `.env.production` and `.env.staging`. Common
unsupported configuration suffixes are reported explicitly. ENV files use
strict syntax validation in this workflow without changing the V1 or browser
parser API behavior.

Results are written to the GitHub Actions step summary. It reports analyzed,
unchanged, unavailable, and unsupported file counts separately. A successful
analysis with no rule findings says so explicitly; if nothing could be
analyzed, the summary says that instead. File names, changed parameter names,
classifications, risk scores, rule findings, and counts are shown; old/new
values, source lines, and parser exception text are omitted. Parse errors,
unsupported formats, and risk findings are advisory and do not fail the
analysis step. Setup failures and Git comparison/blob-read failures fail the
workflow. The workflow grants `contents: read` only and disables persisted
checkout credentials.

This is pairwise change-risk analysis of the base and pull request versions,
not historical analysis. It can report semantic configuration changes and
apply the existing magnitude and rule logic. It cannot assess deployed history,
runtime environment, referenced secret values, cross-file interactions, or
deployment success. Renames recognized by Git's similarity-based rename
detection are analyzed as one before/after pair; changes Git classifies as a
separate deletion and addition are assessed separately.

`requirements-ci.txt` installs only the Python packages needed to parse files
and run the shared analysis engine. From the repository root, the focused
tests and equivalent local analysis can be run with:

```powershell
python -m pip install -r requirements-ci.txt
python -m pytest tests/test_ci_analysis.py -q
git fetch origin main
$base = git merge-base origin/main HEAD
$head = git rev-parse HEAD
$env:GITHUB_STEP_SUMMARY = Join-Path $env:TEMP 'configguard-summary.md'
python -m app.ci_analysis --base $base --head $head --repo .
Get-Content $env:GITHUB_STEP_SUMMARY
Remove-Item Env:GITHUB_STEP_SUMMARY
```

The CLI writes the same summary to standard output when `GITHUB_STEP_SUMMARY`
is not set. To run the entire backend suite, use `python -m pytest -q`.

## ConfigGuard V2.5 local CI report history

The dashboard can display sanitised GitHub Actions reports ingested by the
backend. This remains a local/manual integration: the V2.4 pull-request
workflow still writes only its Actions step summary and does not send reports
to an API or require a public backend URL.

Set `CONFIGGUARD_CI_TOKEN` in the backend's ignored local `.env` file for
machine report ingestion. V2.6 replaces the dashboard's shared read token with
GitHub user sessions. `CONFIGGUARD_CI_READ_TOKEN` is no longer accepted by
user-facing read routes.

Authenticated endpoints:

- `POST /api/projects/{project_id}/ci-analyses` ingests a report.
- `GET /api/projects/{project_id}/ci-analyses?limit=50` lists reports.
- `GET /api/projects/{project_id}/ci-analyses/{analysis_id}` retrieves one report.

Project IDs are generated per-user IDs and every user read is owner-scoped. Repository
names and pull-request URLs must identify the same `owner/repository` and PR on
`https://github.com`. The backend bounds the JSON body and all lists/strings,
accepts only risk classifications and known rule names, and rejects unknown
fields. A compound unique index on project, repository, pull-request number,
and head SHA makes retries idempotent. The stored record contains repository
and pull-request metadata, commit SHAs, server-side analysis time, risk score,
classification, sanitised parameter-level findings, and aggregate counts.
Configuration contents, old/new values, parser errors, source excerpts, and
free-form finding text are not accepted. Sensitive-labelled path components
and parameter names are replaced before persistence.

To test ingestion locally, set the machine token in `.env` and call the POST
route with a selected project's ID. The dashboard's **GitHub PR checks** page
loads reports through the authenticated user session. A request body should contain
only the documented metadata, `findings` with `file_path`, `parameter`,
`change_type`, and whitelisted `rule_names`, plus the six integer fields in
`change_counts`; it must not contain configuration values. CI history is
stored separately from historical configuration analyses. No workflow-side
submission is enabled until the backend has a reachable HTTPS deployment and
an explicit workflow configuration is reviewed.

## ConfigGuard V2.6 GitHub sign-in and repository ownership

Copy `.env.example` to `.env`, fill in MongoDB and GitHub OAuth App settings,
and generate a unique `CONFIGGUARD_SESSION_SECRET`. In the GitHub OAuth App,
set the callback URL to the exact value of `GITHUB_REDIRECT_URI`; for local
development the example is `http://localhost:8000/api/auth/github/callback`.
The app requests `read:user` and lists public repositories only. See GitHub's
[OAuth scope reference](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)
and [OAuth App permissions comparison](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/differences-between-github-apps-and-oauth-apps).
GitHub OAuth
Apps do not provide narrow read-only access to private repositories: the
classic `repo` scope grants broad private-repository read/write permissions.
Private repository support should use a GitHub App with granular read-only
permissions in a later phase.

The browser signs in at `/api/auth/github/login`. The backend uses a signed,
eight-hour, HttpOnly, SameSite=Lax session cookie; production mode sets Secure.
Authlib validates OAuth state and uses PKCE S256 for the authorization code flow.
After sign-in, the cookie contains a signed random session ID and active project
ID; a hashed server-side session record stores the user and CSRF token and
supports revocation on sign-out. During OAuth, Authlib's signed temporary state
is also in the cookie. No GitHub access token is stored in the cookie or
database. The token is used only in the callback to retrieve the user's profile
and public repository list, then discarded. Unsafe browser requests require a session CSRF token. Select a
repository from the dashboard to create an owner-scoped ConfigGuard project.
Every project history and CI report read verifies project ownership on each
request. CI ingestion continues to use `CONFIGGUARD_CI_TOKEN`, and accepts
reports only when both the selected project and repository match.

The OAuth implementation uses [Authlib's Starlette client](https://docs.authlib.org/en/latest/oauth2/client/web/)
and the flow follows GitHub's [OAuth authorization guidance](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps).

Existing records without a verified owner remain unavailable through the new
dashboard APIs. They are not automatically assigned to the first user who
signs in. The V2.4 workflow remains local and advisory; it does not submit to a
hosted backend.
