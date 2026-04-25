from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import motors, propellants, simulations


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    yield


def create_app() -> FastAPI:
    settings = get_settings()

    application = FastAPI(
        title="Machwave API",
        version="0.1.0",
        description="Internal ballistics simulation platform for rocket motors.",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(propellants.router, prefix="/propellants", tags=["propellants"])
    application.include_router(motors.router, prefix="/motors", tags=["motors"])
    application.include_router(simulations.router, prefix="/simulations", tags=["simulations"])

    @application.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
