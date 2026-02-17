import os
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="TASS Calendar Bridge")

@app.get("/")
def root():
    return {"service": "tass-calendar-bridge", "status": "up"}

@app.get("/health")
def health():
    return {"ok": True}

# 👇 NECESARIO PARA RENDER
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
