# ConfigGuard V2.2 frontend

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

The Analyze config editor currently accepts JSON objects. JSON is parsed in the browser and checked for an object at the root before a request is sent. YAML, YML, and `.env` are disabled because this frontend does not yet include their parsers. The backend still supports those formats through its file based V1 analysis endpoint and accepts structured configurations for V2 historical analysis.

Historical analysis uses the backend's deployed version baseline and defaults to three observations. New analysis candidates remain proposals and are not included in later baselines until explicitly marked deployed. Historical outliers are review signals, not predictions that a deployment will fail.
