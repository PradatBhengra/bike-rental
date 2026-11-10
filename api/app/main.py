import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app import cache, db, errors, rides
from app.admin import routes as admin_routes
from app.analytics import routes as analytics_routes
from app.auth import routes as auth_routes
from app.bookings import routes as booking_routes
from app.catalog import routes as catalog_routes
from app.chat import routes as chat_routes
from app.docs import routes as docs_routes
from app.errors import Problem
from app.payments import routes as payment_routes
from app.staff import routes as staff_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    opened_here = db.pool is None
    if opened_here:
        db.open_pool()
    yield
    if opened_here:
        db.close_pool()


app = FastAPI(title="Saal Bikes API", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
errors.install(app)

api = APIRouter(prefix="/api/v1")
for module in (auth_routes, catalog_routes, booking_routes, payment_routes, staff_routes, admin_routes, analytics_routes):
    api.include_router(module.router)
for extra in (analytics_routes.admin, docs_routes.router, docs_routes.admin, rides.router, rides.admin,
              chat_routes.router, chat_routes.admin):
    api.include_router(extra)
app.include_router(api)


@app.get("/healthz", tags=["ops"])
def healthz():
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
def readyz():
    try:
        with db.tx() as conn:
            conn.execute("SELECT 1")
        cache.r.ping()
    except Exception:
        raise Problem(503, "Not ready") from None
    return {"db": "ok", "redis": "ok"}
