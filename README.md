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

Set `MONGODB_URI` in `.env` to a MongoDB connection string. `MONGODB_DATABASE`
is optional and defaults to `configguard`. Each successful `/api/analyze`
request stores both uploaded configuration snapshots under the `default`
project in `config_versions` and its risk result in `risk_analyses`. The analysis
document contains the IDs of its before and after snapshots.

Retrieval endpoints:

- `GET /api/config-versions?limit=50` and `GET /api/config-versions/{version_id}`
- `GET /api/analyses?limit=50` and `GET /api/analyses/{analysis_id}`

List limits can range from 1 to 200. Stored MongoDB ObjectIds are returned as
string `id` fields.

## ConfigGuard V2.1 project history

V2 configuration history is scoped by the caller-provided project ID. Save a
version with `POST /api/projects/{project_id}/config-versions` using JSON such
as:

```json
{
  "configuration": {"service": {"timeout": 30}},
  "format": "json"
}
```

Retrieve project history with `GET /api/projects/{project_id}/config-versions`
or one version with `GET /api/projects/{project_id}/config-versions/{version_id}`.
Retrieve project analyses with `GET /api/projects/{project_id}/analyses` or
`GET /api/projects/{project_id}/analyses/{analysis_id}`.
Use `POST /api/projects/{project_id}/historical-analysis` with a proposed
configuration and optional `min_observations` (default 3, maximum 100) to
compare numeric parameters with prior versions in that project. The response
stores a separate analysis record linked to the proposed version and baseline
version IDs. Numeric outlier checks use the median and median absolute deviation;
they are advisory review signals, not predictions of deployment failure.
Missing samples are reported as insufficient history, and non-numeric values
are explicitly left unassessed. Tests use mocked collections and route-level
stubs, so they do not require Atlas access.

Run tests:
```bash
pytest
```
