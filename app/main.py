import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.services.registry import mcp_status, start_mcp_server, stop_mcp_server

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    start_mcp_server(get_settings().terraform_mcp_url)
    logger.info("InfraAgent started; MCP status: %s", mcp_status())
    try:
        yield
    finally:
        stop_mcp_server()


app = FastAPI(title="InfraAgent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173", "http://127.0.0.1:4173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "mcp": mcp_status()}
