from fastapi import FastAPI
from fastapi.responses import JSONResponse

from invoice_parser.runtime import build_runtime_report

app = FastAPI(title="Agentic AI - Invoice Parser")

@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.get("/ready")
def readiness_check():
    report = build_runtime_report(check_writes=True)
    return JSONResponse(content=report, status_code=200 if report["status"] == "ready" else 503)

@app.get("/")
def root():
    return {"message": "Agentic AI Invoice Parser API"}
