from fastapi import FastAPI

app = FastAPI(title="Bancaemdia API")


@app.get("/health", response_model=dict[str, str])  # type: ignore[misc]
async def health() -> dict[str, str]:
    return {"status": "healthy"}
