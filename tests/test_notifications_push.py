"""Tests for the ntfy / webhook notification channel (addon/app/notifications.py).

Run from the repo root (needs aiohttp, which the add-on image has):

    python3 -m unittest discover -s tests

The channel is exercised against a throwaway HTTP server on localhost, so no
Home Assistant, ntfy or network is needed.
"""

import json
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "addon", "app"))

try:
    from aiohttp import web
    import notifications
    import storage
except ImportError:  # aiohttp is not installed here
    web = None


def setUpModule():
    logging.disable(logging.CRITICAL)  # the failure paths log on purpose


def tearDownModule():
    logging.disable(logging.NOTSET)


@unittest.skipIf(web is None, "aiohttp is not installed")
class PushTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.status = 200
        self.location = None

        async def handler(request):
            self.requests.append({
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "headers": dict(request.headers),
                "body": await request.read(),
            })
            headers = {"Location": self.location} if self.location else {}
            return web.Response(status=self.status, headers=headers)

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        await self.runner.cleanup()

    def settings(self, **extra):
        return {"notify_push_url": f"{self.base}/secret-topic", **extra}

    async def test_ntfy_sends_message_as_body_and_title_in_query(self):
        sent, reason = await notifications.send_push(
            "Petlibro Local: Comedero niño - Offline", "Device is offline.\nSecond line", self.settings())
        self.assertEqual((sent, reason), (True, ""))
        req = self.requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], "/secret-topic")
        self.assertEqual(req["query"], {"title": "Petlibro Local: Comedero niño - Offline"})
        self.assertEqual(req["body"].decode("utf-8"), "Device is offline.\nSecond line")
        self.assertNotIn("Authorization", req["headers"])

    async def test_ntfy_keeps_the_query_already_in_the_url(self):
        settings = {"notify_push_url": f"{self.base}/topic?priority=high"}
        await notifications.send_push("T", "M", settings)
        self.assertEqual(self.requests[0]["query"], {"priority": "high", "title": "T"})

    async def test_token_is_bearer_and_user_password_is_basic(self):
        await notifications.send_push("T", "M", self.settings(notify_push_token="tk_abc"))
        self.assertEqual(self.requests[0]["headers"]["Authorization"], "Bearer tk_abc")
        await notifications.send_push("T", "M", self.settings(notify_push_token="me:p4ss"))
        self.assertEqual(self.requests[1]["headers"]["Authorization"], "Basic bWU6cDRzcw==")

    async def test_webhook_posts_json(self):
        settings = self.settings(notify_push_format="webhook")
        sent, _ = await notifications.send_push("Title ñ", "Msg", settings, "petlibro_local_AF04_offline")
        self.assertTrue(sent)
        req = self.requests[0]
        self.assertEqual(req["query"], {})
        self.assertEqual(json.loads(req["body"]), {
            "title": "Title ñ", "message": "Msg",
            "id": "petlibro_local_AF04_offline", "source": "petlibro-local"})

    async def test_http_error_is_reported_without_the_url(self):
        self.status = 401
        sent, reason = await notifications.send_push("T", "M", self.settings())
        self.assertEqual((sent, reason), (False, "HTTP 401"))

    async def test_redirect_is_not_followed(self):
        self.status = 301
        self.location = f"{self.base}/elsewhere"
        sent, reason = await notifications.send_push("T", "M", self.settings())
        self.assertFalse(sent)
        self.assertIn("redirect", reason)
        self.assertEqual(len(self.requests), 1)  # no GET to the new location

    async def test_connection_failure_does_not_leak_the_topic(self):
        await self.runner.cleanup()  # nothing listens any more
        sent, reason = await notifications.send_push("T", "M", self.settings())
        self.assertFalse(sent)
        self.assertNotIn("secret-topic", reason)
        await self.runner.setup()  # let asyncTearDown clean up

    async def test_url_must_be_http_or_https(self):
        for url in ("", "ntfy.sh/topic", "file:///etc/passwd", "ftp://host/x", "http://"):
            sent, _ = await notifications.send_push("T", "M", {"notify_push_url": url})
            self.assertFalse(sent, url)
            self.assertFalse(notifications.push_configured({"notify_push_url": url}), url)
        self.assertEqual(self.requests, [])

    async def test_fire_notification_without_home_assistant(self):
        # No HA: only the push channel runs, and the HA ones are skipped, not attempted.
        with mock.patch.object(notifications, "ha_configured", return_value=False), \
             mock.patch.object(notifications, "_fire_persistent") as bell, \
             mock.patch.object(notifications, "_fire_notify_service") as service:
            settings = self.settings(notify_email_service="smtp", notify_email_to="a@b.c")
            await notifications.fire_notification("T", "M", settings, {"notify_mobile": True})
            await notifications.dismiss_notification("id", settings, {"notify_mobile": True})
        bell.assert_not_called()
        service.assert_not_called()
        self.assertEqual(len(self.requests), 1)

    async def test_device_can_turn_the_push_channel_off(self):
        with mock.patch.object(notifications, "ha_configured", return_value=False):
            await notifications.fire_notification("T", "M", self.settings(), {"notify_push": False})
            self.assertEqual(self.requests, [])
            await notifications.fire_notification("T", "M", self.settings(), {})  # on by default
            self.assertEqual(len(self.requests), 1)

    async def test_nothing_is_sent_when_not_configured(self):
        with mock.patch.object(notifications, "ha_configured", return_value=False):
            await notifications.fire_notification("T", "M", {}, {})
        self.assertEqual(self.requests, [])


@unittest.skipIf(web is None, "aiohttp is not installed")
class EnvFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(storage, "DATA_FILE", os.path.join(self.tmp.name, "data.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "NOTIFY_PUSH_URL": "https://ntfy.example/from-env",
            "NOTIFY_PUSH_FORMAT": "webhook",
            "NOTIFY_PUSH_TOKEN": "tk_env",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_environment_fills_in_blank_settings(self):
        settings = storage.get_settings()
        self.assertEqual(settings["notify_push_url"], "https://ntfy.example/from-env")
        self.assertEqual(settings["notify_push_format"], "webhook")
        self.assertEqual(settings["notify_push_token"], "tk_env")
        self.assertEqual(sorted(storage.env_fallback_settings()),
                         ["notify_push_format", "notify_push_token", "notify_push_url"])

    def test_settings_page_value_wins_over_the_environment(self):
        storage.save_settings({"notify_push_url": "https://ntfy.example/from-ui"})
        self.assertEqual(storage.get_settings()["notify_push_url"], "https://ntfy.example/from-ui")
        self.assertNotIn("notify_push_url", storage.env_fallback_settings())

    def test_the_settings_page_view_never_carries_environment_values(self):
        # Otherwise saving the page would copy them into the data file.
        view = storage.get_settings(include_env_fallback=False)
        self.assertEqual(view["notify_push_url"], "")
        self.assertEqual(view["notify_push_token"], "")
        storage.save_settings(view)
        with open(storage.DATA_FILE) as f:
            saved = json.load(f)["settings"]
        self.assertEqual(saved["notify_push_url"], "")

    def test_no_environment_means_off(self):
        with mock.patch.dict(os.environ, clear=False):
            for var in ("NOTIFY_PUSH_URL", "NOTIFY_PUSH_FORMAT", "NOTIFY_PUSH_TOKEN"):
                os.environ.pop(var)
            self.assertEqual(storage.get_settings()["notify_push_url"], "")
            self.assertEqual(storage.env_fallback_settings(), [])


if __name__ == "__main__":
    unittest.main()
