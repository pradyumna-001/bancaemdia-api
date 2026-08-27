from fastapi import FastAPI

app = FastAPI(title="Bancaemdia API")


@app.get("/health", response_model=dict[str, str])
async def health() -> dict[str, str]:  # type: ignore[return-value]
    return {"status": "healthy"}
