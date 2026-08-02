"""End-to-end tests of the REST API through an in-process ASGI transport."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from mediabot.domain.enums import AdminRole, SubscriptionTier

pytestmark = pytest.mark.integration

httpx = pytest.importorskip("httpx")


@pytest.fixture
async def api_container(settings, uow_factory, cache, dispatcher):
    """A container whose infrastructure is swapped for the test doubles."""
    from mediabot.core.container import Container

    container = Container(settings, dispatcher=dispatcher)
    # Reuse the fixtures' SQLite session factory and fakeredis cache instead of
    # opening real connections; everything above these two seams is production
    # code, so the test still exercises the real wiring.
    container.__dict__["uow_factory"] = uow_factory
    container.__dict__["cache"] = cache
    return container


@pytest.fixture
async def client(api_container) -> AsyncIterator[object]:
    from mediabot.presentation.api.app import create_app

    app = create_app(api_container)
    await api_container.admin.ensure_bootstrap_admin()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def token(client, api_container) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "username": api_container.settings.api.admin_username,
            "password": api_container.settings.api.admin_password.get_secret_value(),
        },
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestPublicEndpoints:
    async def test_root(self, client) -> None:
        response = await client.get("/")
        assert response.status_code == 200
        assert response.json()["service"] == "mediabot"

    async def test_health_reports_dependencies(self, client) -> None:
        response = await client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        assert body["database"] is True
        assert body["redis"] is True
        assert body["status"] in {"ok", "degraded"}

    async def test_metrics_are_exposed(self, client) -> None:
        response = await client.get("/api/v1/metrics")
        assert response.status_code == 200
        assert b"mediabot_downloads_total" in response.content

    async def test_security_headers_are_present(self, client) -> None:
        response = await client.get("/")
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


class TestAuthentication:
    async def test_login_returns_a_token_pair(self, client, api_container) -> None:
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "username": api_container.settings.api.admin_username,
                "password": api_container.settings.api.admin_password.get_secret_value(),
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0

    async def test_wrong_password_is_rejected(self, client) -> None:
        response = await client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "wrong-password"}
        )
        assert response.status_code == 403
        assert response.json()["error"] == "permission_denied"

    async def test_protected_endpoint_requires_a_token(self, client) -> None:
        response = await client.get("/api/v1/users")
        assert response.status_code == 401
        assert response.json()["error"] == "unauthenticated"

    async def test_refresh_rotates_the_access_token(self, client, api_container) -> None:
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "username": api_container.settings.api.admin_username,
                "password": api_container.settings.api.admin_password.get_secret_value(),
            },
        )
        refreshed = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["access_token"]

    async def test_access_token_cannot_be_used_as_a_refresh_token(self, client, token) -> None:
        response = await client.post("/api/v1/auth/refresh", json={"refresh_token": token})
        assert response.status_code == 401


class TestUserEndpoints:
    @pytest.fixture
    async def user(self, api_container):
        return await api_container.users.get_or_create(
            user_id=777, username="apitester", first_name="API"
        )

    async def test_list_and_search_users(self, client, token, user) -> None:
        response = await client.get("/api/v1/users", headers=auth(token))
        assert response.status_code == 200
        assert response.json()["total"] >= 1

        found = await client.get("/api/v1/users?query=apitester", headers=auth(token))
        assert [item["id"] for item in found.json()["items"]] == [777]

    async def test_get_single_user(self, client, token, user) -> None:
        response = await client.get("/api/v1/users/777", headers=auth(token))
        assert response.status_code == 200
        assert response.json()["username"] == "apitester"

    async def test_missing_user_returns_404(self, client, token) -> None:
        response = await client.get("/api/v1/users/424242", headers=auth(token))
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    async def test_limits_reflect_the_free_tier(self, client, token, user) -> None:
        response = await client.get("/api/v1/users/777/limits", headers=auth(token))
        body = response.json()
        assert body["tier"] == SubscriptionTier.FREE.value
        assert body["daily_downloads"] == 10
        assert body["remaining"] == 10

    async def test_grant_subscription_changes_the_tier(self, client, token, user) -> None:
        response = await client.post(
            "/api/v1/users/777/subscription",
            headers=auth(token),
            json={"user_id": 777, "tier": "premium", "days": 30},
        )
        assert response.status_code == 200
        assert response.json()["tier"] == "premium"

        check = await client.get("/api/v1/users/777/subscription", headers=auth(token))
        assert check.json()["active"] is True

    async def test_statistics_endpoint(self, client, token, user) -> None:
        response = await client.get("/api/v1/users/777/statistics", headers=auth(token))
        assert response.status_code == 200
        assert response.json()["user_id"] == 777

    async def test_dashboard(self, client, token) -> None:
        response = await client.get("/api/v1/statistics/dashboard", headers=auth(token))
        assert response.status_code == 200
        assert "downloads_today" in response.json()


class TestPromoEndpoints:
    async def test_create_list_and_redeem(self, client, token, api_container) -> None:
        await api_container.users.get_or_create(user_id=888, username="promo-user")

        created = await client.post(
            "/api/v1/promo",
            headers=auth(token),
            json={"promo_type": "coins", "value": 150, "code": "APITEST", "max_activations": 3},
        )
        assert created.status_code == 200
        assert created.json()["code"] == "APITEST"

        listed = await client.get("/api/v1/promo", headers=auth(token))
        assert "APITEST" in [item["code"] for item in listed.json()]

        redeemed = await client.post(
            "/api/v1/promo/redeem",
            headers=auth(token),
            json={"user_id": 888, "code": "APITEST"},
        )
        assert redeemed.status_code == 200
        assert redeemed.json()["coins"] == 150

        again = await client.post(
            "/api/v1/promo/redeem",
            headers=auth(token),
            json={"user_id": 888, "code": "APITEST"},
        )
        assert again.status_code == 400
        assert again.json()["error"] == "promo_already_used"

    async def test_disable_promo(self, client, token) -> None:
        await client.post(
            "/api/v1/promo",
            headers=auth(token),
            json={"promo_type": "coins", "value": 10, "code": "TODISABLE"},
        )
        response = await client.delete("/api/v1/promo/TODISABLE", headers=auth(token))
        assert response.status_code == 200
        assert response.json()["status"] == "disabled"

    async def test_validation_rejects_a_negative_value(self, client, token) -> None:
        response = await client.post(
            "/api/v1/promo",
            headers=auth(token),
            json={"promo_type": "coins", "value": -5},
        )
        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"


class TestDownloadEndpoints:
    async def test_history_is_empty_for_a_new_user(self, client, token, api_container) -> None:
        await api_container.users.get_or_create(user_id=999)
        response = await client.get("/api/v1/users/999/history", headers=auth(token))
        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0, "page": 1, "page_size": 20}

    async def test_unknown_download_returns_404(self, client, token) -> None:
        response = await client.get("/api/v1/downloads/12345", headers=auth(token))
        assert response.status_code == 404

    async def test_invalid_url_is_rejected_before_any_network_call(self, client, token) -> None:
        response = await client.post(
            "/api/v1/downloads",
            headers=auth(token),
            json={"user_id": 1, "url": "ftp://example.com/file", "kind": "video"},
        )
        assert response.status_code == 422


class TestRoles:
    async def test_support_role_cannot_create_promo_codes(self, client, api_container) -> None:
        support_token = api_container.jwt.create_token_pair(
            "2", role=AdminRole.SUPPORT.value, username="support"
        ).access_token
        response = await client.post(
            "/api/v1/promo",
            headers=auth(support_token),
            json={"promo_type": "coins", "value": 10},
        )
        assert response.status_code == 403
        assert response.json()["error"] == "permission_denied"
