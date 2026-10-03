from fastapi import APIRouter

from app.api.routes import (
    items,
    login,
    marketplace_admin,
    marketplace_billing,
    marketplace_payments,
    marketplace_projects,
    paypal_webhook,
    private,
    users,
    utils,
)
from app.core.config import settings

api_router = APIRouter()
api_router.include_router(login.router)
api_router.include_router(users.router)
api_router.include_router(utils.router)
api_router.include_router(items.router)
api_router.include_router(marketplace_billing.router)
api_router.include_router(marketplace_projects.router)
api_router.include_router(marketplace_payments.router)
api_router.include_router(marketplace_admin.router)
api_router.include_router(paypal_webhook.router)


if settings.FASTAPI_ENV == "development":
    api_router.include_router(private.router)
