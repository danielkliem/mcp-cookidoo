"""
Unit tests for token-lifecycle handling.

Verify two things, both required to fix the long-running-server 401 bug:

1. **Proactive refresh** in ``server._ensure_connected`` — when the cached
   token is near or past expiry, ``Cookidoo.refresh_token()`` is called via
   the OAuth refresh-grant rather than throwing the cache out and doing a
   full email+password re-login. Full re-login only happens when the refresh
   itself fails (e.g. refresh token also dead).

2. **Reactive retry** in ``CookidooService`` — for the raw-HTTP custom-recipe
   endpoints that bypass the library's auth handling, a 401 response triggers
   ``refresh_token()`` and the request is retried exactly once. The library
   doesn't wrap these endpoints, so we must do the refresh-on-401 ourselves.

Both paths use the library's existing OAuth refresh mechanism — no full
re-login with credentials in the steady-state path.

Run: ``./venv/bin/python -m unittest test_auth_refresh -v``
"""

import json
import unittest
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

from cookidoo_api.exceptions import CookidooAuthException


def _mock_response(status: int, text: Optional[str] = None, json_data=None):
    """Build a mock aiohttp ClientResponse with async context-manager support.

    If ``json_data`` is given but ``text`` isn't, ``text`` is derived by
    serialising ``json_data`` so callers don't have to do it themselves.
    """
    if text is None:
        text = json.dumps(json_data) if json_data is not None else ""
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=text)
    response.json = AsyncMock(return_value=json_data if json_data is not None else {})
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


def _mock_session_with_responses(responses: list):
    """Build a mock aiohttp ClientSession where each .request() yields the next response."""
    session = MagicMock()
    iterator = iter(responses)
    session.request = MagicMock(side_effect=lambda *a, **kw: next(iterator))
    return session


class TestEnsureConnectedProactiveRefresh(unittest.IsolatedAsyncioTestCase):
    """``server._ensure_connected`` should refresh the token when it's near expiry,
    not throw the cache out and re-login."""

    async def asyncSetUp(self):
        import server
        self.server = server
        # Reset module-level globals between tests
        server._cookidoo_service = None
        server._cookidoo_api = None

    async def asyncTearDown(self):
        self.server._cookidoo_service = None
        self.server._cookidoo_api = None

    async def test_no_cache_does_full_login(self):
        """Cold start: no cached service → call CookidooService.login()."""
        with patch("server.load_cookidoo_credentials", return_value=("e", "p")), \
             patch("server.CookidooService") as MockSvc:
            mock_api = MagicMock()
            mock_api.expires_in = 3600
            mock_svc = MockSvc.return_value
            mock_svc.login = AsyncMock(return_value=mock_api)

            err, svc = await self.server._ensure_connected()

        self.assertIsNone(err)
        self.assertIs(svc, mock_svc)
        mock_svc.login.assert_awaited_once()

    async def test_fresh_token_returns_cache_without_refresh(self):
        """Cache + token has plenty of time left → return cache, skip refresh entirely."""
        mock_svc = MagicMock()
        mock_api = MagicMock()
        mock_api.expires_in = 3600
        mock_api.refresh_token = AsyncMock()
        self.server._cookidoo_service = mock_svc
        self.server._cookidoo_api = mock_api

        err, svc = await self.server._ensure_connected()

        self.assertIsNone(err)
        self.assertIs(svc, mock_svc)
        mock_api.refresh_token.assert_not_awaited()

    async def test_near_expiry_triggers_refresh_no_full_login(self):
        """Cache + token within buffer → refresh_token() runs, no full re-login."""
        mock_svc = MagicMock()
        mock_api = MagicMock()
        mock_api.expires_in = 30  # below 60s buffer
        mock_api.refresh_token = AsyncMock()
        self.server._cookidoo_service = mock_svc
        self.server._cookidoo_api = mock_api

        with patch("server.load_cookidoo_credentials") as mock_creds, \
             patch("server.CookidooService") as MockSvc:
            err, svc = await self.server._ensure_connected()

        self.assertIsNone(err)
        self.assertIs(svc, mock_svc)
        mock_api.refresh_token.assert_awaited_once()
        # crucial: no fall-through to full login
        mock_creds.assert_not_called()
        MockSvc.assert_not_called()

    async def test_expired_token_triggers_refresh(self):
        """expires_in == 0 (already past expiry) → refresh path."""
        mock_svc = MagicMock()
        mock_api = MagicMock()
        mock_api.expires_in = 0
        mock_api.refresh_token = AsyncMock()
        self.server._cookidoo_service = mock_svc
        self.server._cookidoo_api = mock_api

        err, svc = await self.server._ensure_connected()

        self.assertIsNone(err)
        mock_api.refresh_token.assert_awaited_once()

    async def test_refresh_failure_falls_back_to_full_login(self):
        """refresh_token() raises (e.g. refresh token also dead) → drop cache, full re-login."""
        mock_svc = MagicMock()
        mock_svc.close = AsyncMock()
        mock_api = MagicMock()
        mock_api.expires_in = 30
        mock_api.refresh_token = AsyncMock(side_effect=CookidooAuthException("refresh token rejected"))
        self.server._cookidoo_service = mock_svc
        self.server._cookidoo_api = mock_api

        with patch("server.load_cookidoo_credentials", return_value=("e", "p")), \
             patch("server.CookidooService") as MockSvc:
            new_api = MagicMock()
            new_api.expires_in = 3600
            new_svc = MockSvc.return_value
            new_svc.login = AsyncMock(return_value=new_api)

            err, svc = await self.server._ensure_connected()

        self.assertIsNone(err)
        # cache was replaced with a fresh service
        self.assertIs(svc, new_svc)
        new_svc.login.assert_awaited_once()


class TestServiceLevelReactiveRetry(unittest.IsolatedAsyncioTestCase):
    """``CookidooService`` raw-HTTP methods should refresh+retry once on 401."""

    def _build_service_with_session(self, responses: list):
        """Build a CookidooService whose api_client has a session yielding the given responses."""
        from cookidoo_service import CookidooService
        svc = CookidooService("e", "p")

        mock_localization = MagicMock()
        mock_localization.url = "https://cookidoo.ch/foundation/de-CH"
        mock_localization.language = "de-CH"

        mock_auth_data = MagicMock()
        mock_auth_data.access_token = "old_token"

        mock_api = MagicMock()
        mock_api.localization = mock_localization
        mock_api.auth_data = mock_auth_data
        mock_api._session = _mock_session_with_responses(responses)
        mock_api.refresh_token = AsyncMock()

        svc._api_client = mock_api
        svc._session = mock_api._session
        return svc, mock_api

    async def test_list_recipes_succeeds_first_try_no_refresh(self):
        """200 on first call → no refresh, no retry."""
        ok = _mock_response(200, json_data={"items": []})
        svc, api = self._build_service_with_session([ok])

        result = await svc.list_custom_recipes()

        self.assertEqual(result, [])
        api.refresh_token.assert_not_awaited()
        self.assertEqual(api._session.request.call_count, 1)

    async def test_list_recipes_refreshes_on_401_and_retries(self):
        """401 → refresh_token() → second call returns 200 with data."""
        unauth = _mock_response(401, text="unauthorized")
        ok = _mock_response(
            200,
            json_data={
                "items": [
                    {
                        "recipeId": "r1",
                        "createdAt": "2026-01-01",
                        "recipeContent": {"name": "Test", "totalTime": 600, "recipeYield": {"value": 4}},
                    }
                ]
            },
        )
        svc, api = self._build_service_with_session([unauth, ok])

        result = await svc.list_custom_recipes()

        api.refresh_token.assert_awaited_once()
        self.assertEqual(api._session.request.call_count, 2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["recipe_id"], "r1")

    async def test_list_recipes_401_after_refresh_raises(self):
        """Two 401s in a row → raise (don't retry forever)."""
        unauth1 = _mock_response(401, text="unauthorized")
        unauth2 = _mock_response(401, text="still unauthorized")
        svc, api = self._build_service_with_session([unauth1, unauth2])

        with self.assertRaises(Exception) as ctx:
            await svc.list_custom_recipes()

        self.assertIn("401", str(ctx.exception))
        api.refresh_token.assert_awaited_once()
        self.assertEqual(api._session.request.call_count, 2)

    async def test_create_recipe_post_401_refreshes_and_retries(self):
        """Even the POST step (recipe creation) refreshes on 401."""
        # Sequence: POST 401, POST retry 200, PATCH 200
        post_unauth = _mock_response(401, text="unauthorized")
        post_ok = _mock_response(200, json_data={"recipeId": "r99"})
        patch_ok = _mock_response(204, text="")
        svc, api = self._build_service_with_session([post_unauth, post_ok, patch_ok])

        # Skip the 5-second wait between POST and PATCH
        with patch("cookidoo_service.asyncio.sleep", new=AsyncMock()):
            recipe_id = await svc.create_custom_recipe(
                name="Test",
                ingredients=["100 g flour"],
                steps=["Mix it"],
                servings=2,
                prep_time=5,
                total_time=10,
            )

        self.assertEqual(recipe_id, "r99")
        # one refresh on the POST 401
        api.refresh_token.assert_awaited_once()
        self.assertEqual(api._session.request.call_count, 3)

    async def test_create_recipe_patch_401_refreshes_and_retries(self):
        """If only the PATCH gets 401 (token expired during 5s sleep), refresh+retry the PATCH."""
        post_ok = _mock_response(200, json_data={"recipeId": "r99"})
        patch_unauth = _mock_response(401, text="unauthorized")
        patch_ok = _mock_response(204, text="")
        svc, api = self._build_service_with_session([post_ok, patch_unauth, patch_ok])

        with patch("cookidoo_service.asyncio.sleep", new=AsyncMock()):
            recipe_id = await svc.create_custom_recipe(
                name="Test",
                ingredients=["100 g flour"],
                steps=["Mix it"],
                servings=2,
                prep_time=5,
                total_time=10,
            )

        self.assertEqual(recipe_id, "r99")
        api.refresh_token.assert_awaited_once()
        self.assertEqual(api._session.request.call_count, 3)


class TestDeleteRecipeRefreshesOnLibraryAuthException(unittest.IsolatedAsyncioTestCase):
    """``delete_custom_recipe`` uses the library's ``remove_custom_recipe`` which raises
    ``CookidooAuthException`` on 401. We should refresh+retry once."""

    async def test_delete_refreshes_on_auth_exception(self):
        from cookidoo_service import CookidooService
        svc = CookidooService("e", "p")

        mock_api = MagicMock()
        mock_api.remove_custom_recipe = AsyncMock(
            side_effect=[CookidooAuthException("token expired"), None]
        )
        mock_api.refresh_token = AsyncMock()
        svc._api_client = mock_api

        await svc.delete_custom_recipe("r1")

        mock_api.refresh_token.assert_awaited_once()
        self.assertEqual(mock_api.remove_custom_recipe.await_count, 2)

    async def test_delete_succeeds_first_try_no_refresh(self):
        from cookidoo_service import CookidooService
        svc = CookidooService("e", "p")

        mock_api = MagicMock()
        mock_api.remove_custom_recipe = AsyncMock(return_value=None)
        mock_api.refresh_token = AsyncMock()
        svc._api_client = mock_api

        await svc.delete_custom_recipe("r1")

        mock_api.refresh_token.assert_not_awaited()
        mock_api.remove_custom_recipe.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
