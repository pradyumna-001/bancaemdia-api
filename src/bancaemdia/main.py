from fastapi import FastAPI

app = FastAPI(title="Bancaemdia API")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}
