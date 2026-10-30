"""Aggregates all API routers under a single versioned prefix (/api/v1)."""

from fastapi import APIRouter

from app.api import athletes, context, devices, health, recordings, samples, teams

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(athletes.router)
api_router.include_router(samples.router)
api_router.include_router(devices.router)
api_router.include_router(teams.router)
api_router.include_router(recordings.router)
api_router.include_router(context.router)
