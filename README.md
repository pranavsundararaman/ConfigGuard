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
result under the `default` project. The analysis references both versions.

Retrieval endpoints:

- `GET /api/config-versions?project_id={project_id}&limit=50`
- `GET /api/config-versions/{version_id}?project_id={project_id}`
- `GET /api/analyses?project_id={project_id}&limit=50`
- `GET /api/analyses/{analysis_id}?project_id={project_id}`

List limits can range from 1 to 200. Stored MongoDB ObjectIds are returned as
string `id` fields.

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
Use `POST /api/projects/{project_id}/historical-analysis` with a proposed
configuration and optional `min_observations` (default 3, allowed range 3–100)
to compare numeric parameters with deployed versions in that project. The
proposal is saved but remains proposed, so it does not enter later baselines
until explicitly marked deployed. Numeric outlier checks use the median and
median absolute deviation; they are advisory review signals, not predictions of
deployment failure.
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
