"""Unit tests must never activate sessions in the user's real AgentService."""
import pytest


@pytest.fixture(autouse=True)
def isolate_real_agent_service(monkeypatch):
    import local_gateway
    original = local_gateway.AgentRelay.request
    async def isolated(self, method, path, **kwargs):
        if self.base_url in {"http://127.0.0.1:8500", "http://localhost:8500"}:
            if path == "/sessions" and method == "POST":
                body = kwargs.get("json_body") or {}
                return 200, {"sessionID": body.get("sessionID") or "isolated-session", "camera_id": None,
                             "session_phase": "setup", "browserID": body.get("browserID")}
            raise local_gateway.BrowserError(502, "UPSTREAM_ERROR", "Unit test: real AgentService is isolated")
        return await original(self, method, path, **kwargs)
    monkeypatch.setattr(local_gateway.AgentRelay, "request", isolated)
