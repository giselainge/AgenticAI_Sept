from fastapi import FastAPI

app = FastAPI(title="Agentic AI - Invoice Parser")

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/")
def root():
    return {"message": "Agentic AI Invoice Parser API"}
