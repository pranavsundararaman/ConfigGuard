from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse

from .parser import parse_config
from .risk_engine import assess_risk

app = FastAPI(
    title="ConfigGuard API",
    version="0.1.0",
    description="Configuration Change Risk Analysis for CI/CD Pipelines",
)

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "configguard"}

@app.post("/api/analyze")
async def analyze(
    old_config: UploadFile = File(...),
    new_config: UploadFile = File(...),
):
    try:
        old_text = (await old_config.read()).decode("utf-8")
        new_text = (await new_config.read()).decode("utf-8")
        old_data = parse_config(old_config.filename or "", old_text)
        new_data = parse_config(new_config.filename or "", new_text)
        return assess_risk(old_data, new_data).model_dump()
    except UnicodeDecodeError:
        return JSONResponse(
            status_code=400,
            content={"detail": "Configuration files must be UTF-8 text files."},
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
