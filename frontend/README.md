# ConfigGuard V2.3 frontend

The Vite React/TypeScript interface for the ConfigGuard FastAPI backend. It provides a project overview, historical configuration analysis, configuration version lifecycle management, and saved analysis history.

## Run locally

From the repository root, prepare and start the backend in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Configure MONGODB_URI locally in your environment or root .env file.
uvicorn app.main:app --reload
```

Then, in a second terminal, run the frontend:

```powershell
cd frontend
npm install
npm run dev
```

Vite proxies `/api` requests to `http://localhost:8000`. The frontend does not read or contain MongoDB credentials; database configuration remains on the backend. Database backed project operations require the backend's `MONGODB_URI`; the API process can start without it, but returns a configuration error when a database operation is attempted. Set a project identifier in the sidebar to scope the displayed history. It defaults to `configguard-demo` and is kept in browser local storage.

## Supported input

The Analyze config editor accepts JSON, YAML, YML, and ENV text. JSON is parsed with the browser's JSON parser. YAML and YML use the maintained `yaml` package and must have a mapping/object at the root; nested mappings and arrays are preserved. ENV text is sent to the backend's `POST /api/parse-config` endpoint and parsed by the same maintained `python-dotenv` parser used by V1 uploads. The endpoint rejects JSON/YAML masquerading as ENV, malformed assignments, and duplicate keys. The browser does not bundle a Node-only dotenv package. ENV supports `KEY=value`, `export`, quoted values, comments, and boolean/numeric normalization. Multiline quoted values and `${VAR}` expansion are not supported; interpolation syntax remains a literal string. Parsed non-numeric values remain available but are not assessed by historical numeric analysis.

Changing formats keeps the selected format and editor content in sync. JSON/YAML conversions preserve numeric and boolean types. Conversion to or from ENV opens an accessible choice dialog; **Convert using convention** applies the rules below, **Use format example** replaces the editor only after that explicit choice, and **Cancel** retains the exact content and format.

ENV names are flat strings and cannot encode the original nested structure unambiguously. Conversion from ENV deterministically splits every underscore into a path separator and lowercases each component: `SERVICE_TIMEOUT_SECONDS=30` becomes `{ service: { timeout: { seconds: 30 } } }`. This hierarchy is inferred by convention and is not a claim about the original key structure. Literal underscores in a key cannot be represented distinctly from path boundaries. Conversion to ENV joins nested key components with `_` and uppercases the result. It rejects invalid ENV key components, values that would change type, unsupported arrays/null/empty objects, and collisions. For example, `service_timeout.seconds` and `service.timeout_seconds` both flatten to `SERVICE_TIMEOUT_SECONDS`, so conversion is blocked instead of silently overwriting a value. The backend uses the same underscore-as-separator normalization for historical parameter names.

ENV values are text. On parsing, the backend normalizes numeric and boolean looking values to numbers and booleans for analysis; strings that look numeric or boolean therefore cannot be safely round-tripped through ENV and conversion is rejected. JSON/YAML numeric and boolean values retain their types when converting between those structured formats. Default examples are provided for each format.

Historical analysis uses deployed configuration versions and defaults to three observations. It compares numeric scalar values; strings, booleans, and arrays are not assessed as numeric parameters. New analysis candidates remain proposals and are not included in later baselines until explicitly marked deployed. Historical outliers are review signals, not predictions that a deployment will fail.

Run frontend parser tests with `npm run test` (Node.js 22 or later is required for Node's built-in TypeScript stripping in the test command). Run the production checks with `npm run build` and `npm run lint`.
