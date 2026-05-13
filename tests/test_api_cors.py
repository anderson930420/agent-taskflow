"""Tests for FastAPI CORS middleware on the Mission Control API."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app


class CorsMiddlewareTests(unittest.TestCase):
    """Verify CORS headers are returned for allowed origins."""

    def setUp(self) -> None:
        self.app = create_app()
        self.client = TestClient(self.app)

    def test_options_health_returns_cors_for_127_0_0_1_3001(self) -> None:
        resp = self.client.options(
            "/health",
            headers={
                "Origin": "http://127.0.0.1:3001",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertIn("access-control-allow-origin", resp.headers)
        self.assertEqual(
            resp.headers["access-control-allow-origin"],
            "http://127.0.0.1:3001",
        )

    def test_options_health_returns_cors_for_localhost_3001(self) -> None:
        resp = self.client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3001",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(
            resp.headers["access-control-allow-origin"],
            "http://localhost:3001",
        )

    def test_options_api_tasks_returns_cors_for_127_0_0_1_3001(self) -> None:
        resp = self.client.options(
            "/api/tasks",
            headers={
                "Origin": "http://127.0.0.1:3001",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(
            resp.headers["access-control-allow-origin"],
            "http://127.0.0.1:3001",
        )

    def test_get_health_returns_cors_for_127_0_0_1_3001(self) -> None:
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://127.0.0.1:3001"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers["access-control-allow-origin"],
            "http://127.0.0.1:3001",
        )

    def test_get_health_returns_cors_for_localhost_3001(self) -> None:
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://localhost:3001"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers["access-control-allow-origin"],
            "http://localhost:3001",
        )

    def test_get_api_tasks_returns_cors_for_127_0_0_1_3001(self) -> None:
        resp = self.client.get(
            "/api/tasks",
            headers={"Origin": "http://127.0.0.1:3001"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers["access-control-allow-origin"],
            "http://127.0.0.1:3001",
        )

    def test_get_api_tasks_returns_cors_for_localhost_3001(self) -> None:
        resp = self.client.get(
            "/api/tasks",
            headers={"Origin": "http://localhost:3001"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers["access-control-allow-origin"],
            "http://localhost:3001",
        )

    def test_get_health_returns_cors_for_127_0_0_1_3000(self) -> None:
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://127.0.0.1:3000"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers["access-control-allow-origin"],
            "http://127.0.0.1:3000",
        )

    def test_get_health_returns_cors_for_localhost_3000(self) -> None:
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers["access-control-allow-origin"],
            "http://localhost:3000",
        )

    def test_no_credentials_header_for_safe_cors(self) -> None:
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://127.0.0.1:3001"},
        )
        # allow_credentials=False → no access-control-allow-credentials header
        self.assertNotIn("access-control-allow-credentials", resp.headers)

    def test_disallowed_origin_does_not_receive_cors_header(self) -> None:
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://example.com"},
        )
        self.assertNotIn("access-control-allow-origin", resp.headers)