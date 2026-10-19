from fastapi import APIRouter

from . import admin, base, graph, pages

router = APIRouter()
for module in (base, pages, admin, graph):
    router.include_router(module.router)
