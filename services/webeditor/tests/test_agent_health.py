"""§5.3/§7.2 (Q115): the gateway's three health routes, against a fake Brain and a fake OLS.

`GET /health` and `GET /api/agent/health` are passthroughs of the Agent service's own
reading -- the pruning right belongs to Brain -- and `GET /api/health` adds the three
sub-items the service status bar reads. Two rules run through the whole file: a missing
reading is a 502, never a 200 that pretends to be healthy, and no health reading may
touch a job (§14.6 rule 5) -- a failed probe is evidence about a service, not about a task.
"""
import asyncio
import json
import time
import unittest
import uuid
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import local_gateway
from local_gateway import (
    AGENT_HEALTH_CACHE_SECONDS,
    AGENT_JOB_FIELDS,
    DEFAULT_CONSTANT_CONFIG,
    HEALTH_PROBE_TIMEOUT_SECONDS,
    StandaloneGateway,
    agent_service_health,
)

# §5.3: the seven fields Brain grades itself with, all of them well.
HEALTHY = {"service": "ready", "protocol": "brain-web/2", "executed": False,
           "qwen": "ready", "browser": "bound", "browser_id": "browser-7",
           "assets": "ok", "capabilities": {"read_live": True},
           "tasks": {"active_task_id": None, "queued": 0}}
# The core criterion of Q115: only Qwen is down, Brain's process is still up.
QWEN_DOWN = {**HEALTHY, "qwen": "unavailable"}


def config(**overrides):
    value = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
    value.update(overrides)
    return value


class FakeBrainService:
    """`F:\\AgentExample`'s health route: an answer, a status, a delay, or no answer at all."""

    def __init__(self) -> None:
        self.health_body: object = HEALTHY
        self.health_status = 200
        self.health_raw: str | None = None
        self.health_delay = 0.0
        self.health_calls = 0
        self.task_state = "进行中"
        self.state_calls = 0

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/health", self.health)
        app.router.add_post("/startTask", self.start_task)
        app.router.add_get("/queryTaskState", self.query_state)

    async def health(self, request: web.Request) -> web.Response:
        self.health_calls += 1
        if self.health_delay:
            await asyncio.sleep(self.health_delay)
        if self.health_raw is not None:
            return web.Response(text=self.health_raw, status=self.health_status,
                                content_type="text/plain")
        return web.json_response(self.health_body, status=self.health_status)

    async def start_task(self, request: web.Request) -> web.Response:
        return web.json_response({"taskID": 12, "state": self.task_state, "terminal": False})

    async def query_state(self, request: web.Request) -> web.Response:
        self.state_calls += 1
        return web.json_response({"taskID": 12, "state": self.task_state, "terminal": False})


class FakeLibraryService:
    """OLS's `/list` (§7.2: probed through the route the asset panel already uses)."""

    def __init__(self) -> None:
        self.status = 200
        self.calls: list[str] = []

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/list", self.list_models)

    async def list_models(self, request: web.Request) -> web.Response:
        self.calls.append(request.query.get("type", ""))
        return web.json_response({"items": [{"id": 1, "name": "cup"}]}, status=self.status)


# A service that is not there. An unresolvable host rather than a refused port because a
# refused port costs two seconds per attempt on this machine, and this suite asks for one
# ten times over -- the detail line says "unreachable" either way.
DEAD_URL = "http://no-such-agent-service.invalid"

# §5.15: the scene a job is submitted with. Health routes never carry it, and never echo it.
SCENE = {"scene_schema_version": 1, "scene_version": 7, "scene_id": 7,
         "scene_name": "relay", "objects": []}


async def submit(client: TestClient, gateway: StandaloneGateway) -> dict:
    """§5.15 step 2/3: one submission, including the live read it now begins from.

    This suite's subject is the health route, so the read is answered at the `dispatch` seam -- the
    `browser.request` frame itself is another suite's contract -- and a browser is bound so the
    submission has a 现场 to name. A fresh `clientRequestID` per call keeps the idempotency map of
    §7.1 out of the way; these tests are not about retries.
    """
    if gateway.state.browser_id is None:
        gateway.state.bind_browser("browser-7")

    async def answer(action, payload, *, timeout=None, binary=None, browser_id=None):
        if action != "getWorld":
            raise AssertionError(f"a submission asked the browser for {action!r}")
        return {"world": SCENE, "scene_id": SCENE["scene_id"], "scene_version": SCENE["scene_version"],
                "scene_sub_version": 0, "playback_state": "idle"}

    gateway.dispatch = answer  # type: ignore[method-assign]
    response = await client.post("/api/agent/jobs",
                                 json={"command": "pick the cup", "scene": SCENE,
                                       "clientRequestID": str(uuid.uuid4())})
    assert response.status == 200, await response.text()
    return await response.json()


class HealthRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.brain = FakeBrainService()
        brain_app = web.Application()
        self.brain.add_routes(brain_app)
        brain_server = TestServer(brain_app)
        await brain_server.start_server()
        self.addAsyncCleanup(brain_server.close)
        self.brain_url = str(brain_server.make_url("/")).rstrip("/")

        self.library = FakeLibraryService()
        library_app = web.Application()
        self.library.add_routes(library_app)
        library_server = TestServer(library_app)
        await library_server.start_server()
        self.addAsyncCleanup(library_server.close)

        self.gateway = StandaloneGateway(config=config(agent_service_url=self.brain_url,
                                                       library_server_url=str(library_server.make_url("/")).rstrip("/")))
        app = web.Application()
        app.router.add_get("/api/health", self.gateway.health)
        app.router.add_get("/health", self.gateway.agent_health)
        app.router.add_get("/api/agent/health", self.gateway.agent_health_cached)
        app.router.add_post("/api/agent/jobs", self.gateway.jobs)
        app.router.add_get("/api/agent/jobs", self.gateway.jobs)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_health_passes_the_agents_own_body_through(self):
        # §5.3: 不裁剪、不改写、不重排 -- `protocol`, `capabilities` and `tasks` are not
        # fields this gateway reads, and they still arrive.
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), HEALTHY)
        self.assertEqual(self.brain.health_calls, 1)

    async def test_a_service_that_is_not_there_is_502_and_never_200(self):
        # The one mistake this route can make is answering 200 about a service that is down.
        self.gateway.config["agent_service_url"] = DEAD_URL
        response = await self.client.get("/health")
        self.assertEqual(response.status, 502)
        body = await response.json()
        self.assertEqual(body["status"], "unreachable")
        self.assertIn("unreachable", body["detail"])

    async def test_a_service_that_answers_too_slow_is_502(self):
        # The clock itself is pinned by `the_two_health_numbers_are_built_in`; here it is
        # only shorter, so the test proves the timeout path without waiting three seconds.
        with mock.patch.object(local_gateway, "HEALTH_PROBE_TIMEOUT_SECONDS", 0.2):
            self.brain.health_delay = 0.5
            response = await self.client.get("/health")
        self.assertEqual(response.status, 502)
        self.assertIn("did not answer /health", (await response.json())["detail"])

    async def test_a_service_that_answers_without_json_is_502(self):
        self.brain.health_raw = "brain is up but this is not a health document"
        response = await self.client.get("/health")
        self.assertEqual(response.status, 502)
        body = await response.json()
        self.assertEqual(body["status"], "unreachable")
        self.assertIn("not JSON", body["detail"])

    async def test_an_empty_agent_service_url_is_502_not_a_healthy_lie(self):
        # §5.15: the relay is off, so there is no service to ask. That is "unreachable",
        # and it says so instead of inventing a reading.
        self.gateway.config["agent_service_url"] = ""
        response = await self.client.get("/health")
        self.assertEqual(response.status, 502)
        self.assertIn("agent_service_url is empty", (await response.json())["detail"])

    async def test_the_upstreams_own_status_code_is_passed_through(self):
        # A 5xx from Brain is still Brain's reading; the gateway does not soften it.
        self.brain.health_status = 503
        self.brain.health_body = {"error": "服务繁忙，请稍后重试"}
        response = await self.client.get("/health")
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"], "服务繁忙，请稍后重试")

    async def test_the_browser_route_repeats_one_reading_for_five_seconds(self):
        # §5.3: the panel polls every 15 s and this cache is what keeps a restart from
        # turning every poll of every tab into a probe of Brain.
        first = await self.client.get("/api/agent/health")
        self.assertEqual(first.status, 200)
        self.assertNotIn("X-Agent-Health-Age", first.headers)
        second = await self.client.get("/api/agent/health")
        self.assertEqual(await second.json(), HEALTHY)
        self.assertEqual(self.brain.health_calls, 1)
        age = float(second.headers["X-Agent-Health-Age"])
        self.assertGreaterEqual(age, 0.0)
        self.assertLessEqual(age, AGENT_HEALTH_CACHE_SECONDS)

    async def test_the_cache_expires_and_asks_again(self):
        await self.client.get("/api/agent/health")
        # Moving the clock back is the whole of what the route checks; no sleeping for 5 s.
        self.gateway.agent_health_at = time.monotonic() - AGENT_HEALTH_CACHE_SECONDS - 0.001
        response = await self.client.get("/api/agent/health")
        self.assertEqual(self.brain.health_calls, 2)
        self.assertNotIn("X-Agent-Health-Age", response.headers)

    async def test_an_unreachable_service_is_cached_like_any_other_answer(self):
        # A dying service must not be probed once per panel poll per tab.
        self.gateway.config["agent_service_url"] = DEAD_URL
        self.assertEqual((await self.client.get("/api/agent/health")).status, 502)
        self.assertEqual((await self.client.get("/api/agent/health")).status, 502)
        self.assertIn("X-Agent-Health-Age", (await self.client.get("/api/agent/health")).headers)

    def test_the_two_health_numbers_are_built_in(self):
        # §12.18 ⑤: 3 s and 5 s are the shipped values, and they stay out of the constant
        # config on purpose -- every configurable number is one more place to deploy wrong.
        self.assertEqual(HEALTH_PROBE_TIMEOUT_SECONDS, 3.0)
        self.assertEqual(AGENT_HEALTH_CACHE_SECONDS, 5.0)
        for key in ("health", "agent_health"):
            self.assertNotIn(key, DEFAULT_CONSTANT_CONFIG)

    async def test_the_route_is_a_json_body_even_when_it_is_502(self):
        # Registered before the catch-all static route (坑 2), so it must not hand back the SPA shell.
        self.gateway.config["agent_service_url"] = DEAD_URL
        body = await (await self.client.get("/health")).text()
        self.assertNotIn("<html", body)
        self.assertEqual(json.loads(body)["status"], "unreachable")


class ApiHealthTests(unittest.IsolatedAsyncioTestCase):
    """§5.3/§7.2: the three sub-items of the route the panel reads at startup and every 15 s."""

    async def asyncSetUp(self):
        self.brain = FakeBrainService()
        brain_app = web.Application()
        self.brain.add_routes(brain_app)
        self.brain_server = TestServer(brain_app)
        await self.brain_server.start_server()
        self.addAsyncCleanup(self.brain_server.close)

        self.library = FakeLibraryService()
        library_app = web.Application()
        self.library.add_routes(library_app)
        self.library_server = TestServer(library_app)
        await self.library_server.start_server()
        self.addAsyncCleanup(self.library_server.close)

        self.gateway = StandaloneGateway(config=config(
            agent_service_url=str(self.brain_server.make_url("/")).rstrip("/"),
            library_server_url=str(self.library_server.make_url("/")).rstrip("/")))
        app = web.Application()
        app.router.add_get("/api/health", self.gateway.health)
        app.router.add_post("/api/agent/jobs", self.gateway.jobs)
        app.router.add_get("/api/agent/jobs", self.gateway.jobs)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_everything_good_reads_ready(self):
        body = await (await self.client.get("/api/health")).json()
        self.assertEqual((body["status"], body["mode"]), ("ok", "local"))
        self.assertEqual(body["agent_service"], {"status": "ready", "detail": None})
        self.assertEqual(body["browser"], {"connected": False, "browser_id": None})
        self.assertEqual(body["assets"], {"reachable": True})
        # §5.9: the caller demo and the smoke both read the three fields that were here before.
        self.assertIsInstance(body["uptimeSeconds"], float)
        self.assertEqual(self.library.calls, ["model"])

    async def test_only_qwen_down_is_degraded_not_unreachable(self):
        # The判据 Q115 exists for: "this service is not up" and "this service is up and one
        # of its dependencies is broken" must be two different readings.
        self.brain.health_body = QWEN_DOWN
        view = (await (await self.client.get("/api/health")).json())["agent_service"]
        self.assertEqual(view["status"], "degraded")
        self.assertIn("qwen=unavailable", view["detail"])

    async def test_an_unbound_browser_is_degraded(self):
        self.brain.health_body = {**HEALTHY, "browser": "not_bound"}
        view = (await (await self.client.get("/api/health")).json())["agent_service"]
        self.assertEqual(view["status"], "degraded")
        self.assertIn("browser=not_bound", view["detail"])

    async def test_a_service_that_is_down_is_unreachable_and_the_route_still_answers_200(self):
        # The subject of this route is "the gateway is alive" (§7.2).
        self.gateway.config["agent_service_url"] = DEAD_URL
        response = await self.client.get("/api/health")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["agent_service"]["status"], "unreachable")

    async def test_a_library_that_is_down_only_moves_its_own_sub_item(self):
        self.gateway.config["library_server_url"] = DEAD_URL
        body = await (await self.client.get("/api/health")).json()
        self.assertEqual(body["assets"], {"reachable": False})
        self.assertEqual(body["agent_service"]["status"], "ready")

    async def test_two_slow_probes_do_not_add_up(self):
        # §7.2: 并发探、互不阻塞. Summed, the two 0.4 s waits would take 0.8 s; side by
        # side they take one, and the route still answers 200 with both readings.
        self.brain.health_delay = 0.4
        slow_library = self.library

        async def slow_list(request: web.Request) -> web.Response:
            await asyncio.sleep(0.4)
            slow_library.calls.append(request.query.get("type", ""))
            return web.json_response({"items": []})

        probe_app = web.Application()
        probe_app.router.add_get("/list", slow_list)
        probe_server = TestServer(probe_app)
        await probe_server.start_server()
        self.addAsyncCleanup(probe_server.close)
        self.gateway.config["library_server_url"] = str(probe_server.make_url("/")).rstrip("/")

        started = time.monotonic()
        body = await (await self.client.get("/api/health")).json()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.75, "the two probes were not concurrent")
        self.assertEqual(body["agent_service"]["status"], "ready")
        self.assertEqual(body["assets"], {"reachable": True})

    async def test_a_probe_that_times_out_does_not_take_the_others_down(self):
        with mock.patch.object(local_gateway, "HEALTH_PROBE_TIMEOUT_SECONDS", 0.2):
            self.brain.health_delay = 0.5
            response = await self.client.get("/api/health")
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["agent_service"]["status"], "unreachable")
        self.assertEqual(body["assets"], {"reachable": True})

    async def test_the_browser_sub_item_names_the_browser_that_holds_the_session(self):
        socket = object()
        self.gateway.browsers["browser-7"] = socket  # type: ignore[index]
        self.gateway.state.bind_browser("browser-7")
        body = await (await self.client.get("/api/health")).json()
        self.assertEqual(body["browser"], {"connected": True, "browser_id": "browser-7"})
        # §7.2 left this as "null until the §6 binding exists". It exists now, so a second tab does
        # not make the name ambiguous -- it makes a viewer.
        self.gateway.browsers["browser-8"] = object()  # type: ignore[index]
        body = await (await self.client.get("/api/health")).json()
        self.assertEqual(body["browser"], {"connected": True, "browser_id": "browser-7"})
        # A socket nobody has bound names nobody: `connected` and `browser_id` are two questions.
        self.gateway.state.raw["browser_id"] = None
        body = await (await self.client.get("/api/health")).json()
        self.assertEqual(body["browser"], {"connected": True, "browser_id": None})


class HealthCannotTouchJobsTests(unittest.IsolatedAsyncioTestCase):
    """§14.6 rule 5: a health reading is about a service, and never about a task."""

    async def asyncSetUp(self):
        self.brain = FakeBrainService()
        brain_app = web.Application()
        self.brain.add_routes(brain_app)
        self.brain_server = TestServer(brain_app)
        await self.brain_server.start_server()
        self.addAsyncCleanup(self.brain_server.close)

        self.gateway = StandaloneGateway(config=config(
            agent_service_url=str(self.brain_server.make_url("/")).rstrip("/"),
            library_server_url=DEAD_URL))
        app = web.Application()
        app.router.add_get("/api/health", self.gateway.health)
        app.router.add_post("/api/agent/jobs", self.gateway.jobs)
        app.router.add_get("/api/agent/jobs", self.gateway.jobs)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_no_job_field_carries_a_health_reading(self):
        self.assertEqual("health" in json.dumps(AGENT_JOB_FIELDS), False)
        job = await submit(self.client, self.gateway)
        self.assertEqual(sorted(job), sorted(AGENT_JOB_FIELDS))

    async def test_a_failed_probe_leaves_a_running_job_untouched(self):
        job = await submit(self.client, self.gateway)
        # Brain's task routes keep answering while its health route has stopped, which is
        # exactly the shape a restarting service makes.
        self.gateway.config["agent_service_url"] = DEAD_URL
        before = dict(self.gateway.jobs_state[job["id"]])
        health = await self.client.get("/api/health")
        self.assertEqual(health.status, 200)
        self.assertEqual((await health.json())["agent_service"]["status"], "unreachable")
        self.assertEqual(self.gateway.jobs_state[job["id"]], before)

    async def test_a_health_failure_does_not_count_towards_the_poll_limit(self):
        # The two paths are independent (§14.6 rule 5): a probe failure is neither a poll
        # failure nor a verdict, and a task only ends at the five-in-a-row limit of §7.4.
        job = await submit(self.client, self.gateway)
        live = str(self.brain_server.make_url("/")).rstrip("/")
        for _ in range(local_gateway.AGENT_POLL_ERROR_LIMIT - 1):
            self.gateway.config["agent_service_url"] = DEAD_URL
            await self.gateway.poll_agent_tasks_once()
            await self.client.get("/api/health")
            self.gateway.config["agent_service_url"] = live
        current = self.gateway.jobs_state[job["id"]]
        self.assertFalse(current["terminal"])
        self.assertEqual(current["poll_errors"], local_gateway.AGENT_POLL_ERROR_LIMIT - 1)
        self.assertEqual(current["agent_state"], self.brain.task_state)


class AgentServiceHealthHelperTests(unittest.TestCase):
    """The graded sub-item is a pure function of one probe, so the probe shapes test directly."""

    def test_a_missing_reading_is_unreachable(self):
        probe = local_gateway.HealthProbe(status=0, body=b"", payload=None, detail="no answer")
        self.assertEqual(agent_service_health(probe), {"status": "unreachable", "detail": "no answer"})

    def test_a_reading_without_an_object_is_degraded(self):
        probe = local_gateway.HealthProbe(status=200, body=b"[]", payload=[], detail=None)
        self.assertEqual(agent_service_health(probe)["status"], "degraded")

    def test_every_bad_field_is_named_at_once(self):
        probe = local_gateway.HealthProbe(status=200, body=b"", payload={**HEALTHY, "qwen": "model_not_found",
                                                                        "assets": "unconfigured"}, detail=None)
        view = agent_service_health(probe)
        self.assertEqual(view["status"], "degraded")
        self.assertIn("qwen=model_not_found", view["detail"])
        self.assertIn("assets=unconfigured", view["detail"])

    def test_a_field_that_is_absent_is_not_a_good_field(self):
        probe = local_gateway.HealthProbe(status=200, body=b"", payload={"service": "ready"}, detail=None)
        view = agent_service_health(probe)
        self.assertEqual(view["status"], "degraded")
        self.assertIn("qwen=None", view["detail"])

    def test_all_four_good_words_is_ready(self):
        probe = local_gateway.HealthProbe(status=200, body=b"", payload=HEALTHY, detail=None)
        self.assertEqual(agent_service_health(probe), {"status": "ready", "detail": None})


if __name__ == "__main__":
    unittest.main()
