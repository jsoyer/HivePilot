"""Browser Web Push for the Pollen PWA (HP-63).

Operator-level VAPID keys (not per-user cloud keys) gate the feature:
when both public and private keys are set, `GET /v1/push/config` advertises
the public key, the shell can subscribe, and the `webpush` notifier fans
`send_notification()` out to stored endpoints.

`pywebpush` is lazy-imported (same pattern as tracing / PDF extras) so a
core install never fails to import this module. Sending without the extra
raises `NotConfigured` and the notification fan-out skips the channel.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from hivepilot.config import settings
from hivepilot.services import db, state_service
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_GONE_STATUS = {404, 410}


class WebPushNotConfigured(RuntimeError):
    """VAPID keys missing or pywebpush extra not installed."""


def push_configured() -> bool:
    return bool(settings.web_push_vapid_public_key and settings.web_push_vapid_private_key)


def public_config() -> dict[str, Any]:
    """Safe-for-browser payload — never includes the private key."""
    if not push_configured():
        return {"enabled": False, "vapid_public_key": None}
    return {
        "enabled": True,
        "vapid_public_key": settings.web_push_vapid_public_key,
    }


def _validate_endpoint(endpoint: str) -> str:
    raw = (endpoint or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("endpoint must be an https URL")
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local"):
        raise ValueError("endpoint host is not a push service")
    return raw


def upsert_subscription(
    *,
    tenant: str,
    endpoint: str,
    p256dh: str,
    auth: str,
) -> None:
    endpoint = _validate_endpoint(endpoint)
    p256dh = (p256dh or "").strip()
    auth = (auth or "").strip()
    if not p256dh or not auth:
        raise ValueError("subscription keys are required")
    state_service.init_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                INSERT INTO web_push_subscriptions (endpoint, p256dh, auth, tenant, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(endpoint) DO UPDATE SET
                    p256dh=excluded.p256dh,
                    auth=excluded.auth,
                    tenant=excluded.tenant
                """
            ),
            (endpoint, p256dh, auth, tenant, now),
        )


def delete_subscription(*, tenant: str, endpoint: str, admin: bool = False) -> None:
    endpoint = (endpoint or "").strip()
    if not endpoint:
        raise ValueError("endpoint is required")
    state_service.init_db()
    with db.connect() as conn:
        if admin:
            conn.execute(
                db.ph("DELETE FROM web_push_subscriptions WHERE endpoint = ?"),
                (endpoint,),
            )
        else:
            conn.execute(
                db.ph("DELETE FROM web_push_subscriptions WHERE endpoint = ? AND tenant = ?"),
                (endpoint, tenant),
            )


def list_subscriptions(tenant: str | None = None) -> list[dict[str, str]]:
    state_service.init_db()
    with db.connect() as conn:
        if tenant is None:
            rows = conn.execute(
                "SELECT endpoint, p256dh, auth, tenant FROM web_push_subscriptions"
            ).fetchall()
        else:
            rows = conn.execute(
                db.ph(
                    "SELECT endpoint, p256dh, auth, tenant FROM web_push_subscriptions "
                    "WHERE tenant = ?"
                ),
                (tenant,),
            ).fetchall()
    return [
        {
            "endpoint": row["endpoint"],
            "p256dh": row["p256dh"],
            "auth": row["auth"],
            "tenant": row["tenant"],
        }
        for row in rows
    ]


def send_web_push_notification(message: str) -> None:
    """NotifierRegistry target — one message to every stored subscription."""
    if not push_configured():
        raise WebPushNotConfigured("web push VAPID keys are not set")
    try:
        from pywebpush import WebPushException, webpush
    except ImportError as exc:
        raise WebPushNotConfigured("pywebpush is not installed") from exc

    subs = list_subscriptions()
    if not subs:
        return
    payload = json.dumps({"title": "Pollen", "body": message[:1800], "url": "/ui/"})
    vapid_claims = {"sub": settings.web_push_vapid_subject}
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=payload,
                vapid_private_key=settings.web_push_vapid_private_key,
                vapid_claims=vapid_claims,
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in _GONE_STATUS:
                delete_subscription(tenant=sub["tenant"], endpoint=sub["endpoint"], admin=True)
                logger.info("web_push.dropped_gone_endpoint", tenant=sub["tenant"])
            else:
                logger.warning("web_push.send_failed", tenant=sub["tenant"], error=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("web_push.send_failed", tenant=sub["tenant"], error=str(exc))
