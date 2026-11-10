"""§5.15/§7.1/§7.4: the Agent task relay, driven against a fake Agent service.

docs/gateway与Agent新通信流程.md gives the gateway four upstream routes
(POST /startTask, GET /queryTaskState, GET /queryTaskResult, GET
/queryTaskFailureReason), the artifact whitelist proxy of §14.5 E2, and makes the gateway the only
party that may call them (Q86). **Q116** took the running-state vocabulary out of this build:
`state` is an opaque string the service invents, `terminal` is the only thing that may end a task,
and `outcome` -- the five of §7.3 -- arrives with the result after that. So this suite pins the
pass-through, the terminal criterion, the once-each result and reason reads, the
failure-reason-before-publish ordering of §5.15 step 5, the §5.13 codes the relay raises, and the
rule that the scene the browser sent is never echoed back.

§7.1's submission gates are the other half: one live `getWorld` of the bound browser is what travels
upstream with the command (§14.6 rule 1), a `clientRequestID` is replayed as the task it already
started, the same id over other content is `request_id_reused`, a running task makes a second one
`task_busy`, and a submission with no client id or no bound browser is `session_context_required` --
all four decided before the browser is asked anything.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from digest import scene_digest
from gateway_state import TransactionReceipts
from local_gateway import (
    AGENT_ARTIFACT_NAMES,
    AGENT_JOB_FIELDS,
    AGENT_OUTCOMES,
    AGENT_POLL_ERROR_LIMIT,
    BROWSER_ERROR_STATUS,
    DEFAULT_CONSTANT_CONFIG,
    NO_RECEIPT_REASON,
    AgentRelay,
    BrowserError,
    StandaloneGateway,
    TaskContext,
    agent_artifact_names,
    agent_error_text,
    agent_outcome,
    agent_reply,
    agent_state_detail,
    agent_state_text,
    agent_task_id,
    agent_terminal,
    agent_transaction_ids,
    create_app,
)

# The words the Agent service happens to answer with today. These are samples of upstream
# prose, not a table: §7.4 (Q116) is exactly the promise that a new one needs no change here.
CREATED, QUEUED, RUNNING, SUCCESS, FAILED = "已创建", "已进入队列", "进行中", "任务完成", "任务失败"
FOREIGN = "正在检索知识库"
REASON = "轨迹规划无解：目标点被障碍物遮挡"

ROOT = Path(__file__).resolve().parent.parent

# §1.1: the browser is the scene authority, so the world JSON itself is what step 2 sends.
WORLD = {"scene_schema_version": 1, "scene_version": 7, "scene_id": 7, "scene_name": "relay",
         "objects": [{"object_id_in_scene": 0, "components": []}]}
# §5.15 step 2: the document the live read answers with. It is deliberately **not** the body's
# `scene` above -- the panel may have added an object between the click and the read, and the task
# has to describe the 现场 as it is, not as it was when the browser composed the request.
LIVE = {"scene_schema_version": 1, "scene_id": 7, "scene_version": 8, "scene_sub_version": 0,
        "scene_name": "relay",
        "objects": [{"object_id_in_scene": 0, "components": []},
                    {"object_id_in_scene": 1, "components": []}]}

# One context for the relay-level calls, so the wire names are asserted in one place only.
CONTEXT = TaskContext(scene_digest=scene_digest(LIVE), scene_id=7, scene_version=8,
                      scene_sub_version=0, session_id=None, client_request_id="req-1",
                      browser_id="browser-1")


def state(text=CREATED, **fields):
    """One `/queryTaskState` answer: the state string plus whatever the test adds."""
    return {"state": text, **fields}


def config(**overrides):
    value = json.loads(json.dumps(DEFAULT_CONSTANT_CONFIG))
    value.update(overrides)
    return value


class LiveRead:
    """One gateway's bound browser, answering `getWorld` with a document the test chooses.

    §5.15 step 2 makes a live read a precondition of every submission, and this suite's subject is
    what the relay does with the answer -- so the read is installed at `dispatch`, the seam the
    `browser.request` frame itself is built on (test_ws_protocol.py's contract, not this file's).
    `mismatch` makes one flat field disagree with the document, which is §5.1's broken browser;
    `bind=False` leaves the gateway with no bound browser, so a missing context gate shows up as
    zero calls rather than as a task.
    """

    def __init__(self, gateway: StandaloneGateway, document: dict, *, bind: bool = True) -> None:
        self.document = document
        self.mismatch: str | None = None
        self.calls: list[dict[str, Any]] = []
        if bind:
            gateway.state.bind_browser("browser-1")
        gateway.dispatch = self.answer  # type: ignore[method-assign]

    async def answer(self, action: str, payload: dict, *, timeout: float | None = None,
                     binary: bytes | None = None, browser_id: str | None = None) -> dict:
        self.calls.append({"action": action, "browser_id": browser_id})
        if action != "getWorld":
            raise AssertionError(f"a submission asked the browser for {action!r}")
        return {"world": self.document, "scene_id": self.document["scene_id"],
                "scene_version": self.document["scene_version"],
                "scene_sub_version": self.document.get("scene_sub_version", 0),
                "playback_state": "idle",
                **({self.mismatch: 3} if self.mismatch is not None else {})}


class FakeAgentService:
    """The Agent service of F:\\AgentExample, minus the threads and the random delays."""

    def __init__(self) -> None:
        self.tasks: dict[int, dict] = {}
        # Per-task scripts let a test hand over the next answer, the way a worker thread would.
        self.scripts: dict[int, list[dict]] = {}
        self.reasons: dict[int, str] = {}
        self.results: dict[int, dict] = {}
        self.received: list[dict] = []
        self.state_calls: list[int] = []
        self.result_calls: list[int] = []
        self.trace: list[str] = []
        self.poll_status: int | None = None
        self.poll_raw: object | None = None
        self.reason_status: int | None = None
        self.result_status: int | None = None
        self.start_reply: tuple[int, dict] | None = None
        self.slow_seconds = 0.0
        self.next_task_id = 1
        # §14.5 E2: the artifacts are files, so they are served and asserted as bytes.
        self.artifacts: dict[tuple[int, str], bytes] = {}
        self.artifact_calls: list[tuple[int, str]] = []
        self.artifact_status: int | None = None
        self.artifact_content_type = "application/json"

    def add_routes(self, app: web.Application) -> None:
        app.router.add_post("/startTask", self.start_task)
        app.router.add_get("/queryTaskState", self.query_state)
        app.router.add_get("/queryTaskResult", self.query_result)
        app.router.add_get("/queryTaskFailureReason", self.failure_reason)
        app.router.add_get("/tasks/{taskID}/artifacts/{name}", self.artifact)

    def finish(self, task_id: int, **result) -> None:
        """Give task `task_id` the result a terminal task has to offer (§7.3)."""
        self.results[task_id] = {"outcome": "scene_updated", **result}

    async def start_task(self, request: web.Request) -> web.Response:
        if self.slow_seconds:
            await asyncio.sleep(self.slow_seconds)
        self.received.append(await request.json())
        if self.start_reply is not None:
            status, payload = self.start_reply
            return web.json_response(payload, status=status)
        task_id = self.next_task_id
        self.next_task_id += 1
        self.tasks[task_id] = state(CREATED, terminal=False)
        return web.json_response({"taskID": task_id, "state": CREATED, "terminal": False,
                                  "code": 0, "message": "任务已创建"})

    async def query_state(self, request: web.Request) -> web.Response:
        task_id = int(request.query.get("taskID", "0"))
        self.state_calls.append(task_id)
        if self.poll_status is not None:
            return web.json_response({"error": "服务内部错误"}, status=self.poll_status)
        if self.poll_raw is not None:
            return web.json_response(self.poll_raw)
        if task_id not in self.tasks:
            return web.json_response({"error": f"任务不存在: {task_id}"}, status=404)
        script = self.scripts.get(task_id)
        payload = script.pop(0) if script else self.tasks[task_id]
        self.tasks[task_id] = payload
        return web.json_response({"taskID": task_id, **payload})

    async def query_result(self, request: web.Request) -> web.Response:
        task_id = int(request.query.get("taskID", "0"))
        self.result_calls.append(task_id)
        self.trace.append("query_result")
        if self.result_status is not None:
            return web.json_response({"error": "结果不可读"}, status=self.result_status)
        if task_id not in self.results:
            # §7.3/§A11.7: a task with no result answers 202, never an empty object -- an empty
            # one would read as "finished with nothing", and a 2xx body would read as a result.
            return web.json_response({"error": f"任务未完成: {task_id}"}, status=202)
        return web.json_response({"taskID": task_id, "executed": False, **self.results[task_id]})

    async def failure_reason(self, request: web.Request) -> web.Response:
        task_id = int(request.query.get("taskID", "0"))
        # The ordering assertion of §5.15 step 5: the reason has to be in hand before the
        # terminal frame reaches the browsers, so one frame carries both.
        self.trace.append("failure_reason")
        if self.reason_status is not None:
            return web.json_response({"error": f"任务不存在: {task_id}"}, status=self.reason_status)
        if task_id not in self.tasks:
            return web.json_response({"error": f"任务不存在: {task_id}"}, status=404)
        return web.json_response({"taskID": task_id, "failureReason": self.reasons.get(task_id)})

    async def artifact(self, request: web.Request) -> web.Response:
        task_id = int(request.match_info["taskID"])
        name = request.match_info["name"]
        self.artifact_calls.append((task_id, name))
        if self.artifact_status is not None:
            return web.json_response({"error": f"任务不存在: {task_id}"}, status=self.artifact_status)
        body = self.artifacts.get((task_id, name))
        if body is None:
            return web.json_response({"error": f"产物不存在: {name}"}, status=404)
        return web.Response(body=body, content_type=self.artifact_content_type)


class AgentTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.agent = FakeAgentService()
        agent_app = web.Application()
        self.agent.add_routes(agent_app)
        self.agent_server = TestServer(agent_app)
        await self.agent_server.start_server()
        self.addAsyncCleanup(self.agent_server.close)
        base_url = str(self.agent_server.make_url("/")).rstrip("/")

        self.frames: list[dict] = []
        self.phase_frames: list[dict] = []
        self.gateway = StandaloneGateway(config=config(agent_service_url=base_url))
        self.base_url = base_url
        # §5.15 step 2: every submission in this suite reads a live 现场 first.
        self.live = LiveRead(self.gateway, LIVE)
        self.request_seq = 0
        self.last_request_id = ""
        broadcast = self.gateway.broadcast

        async def record(event: dict) -> None:
            # §4's `session.phase_changed` rides the same channel as the job frames this suite
            # watches, so the two are recorded apart instead of every assertion here learning to
            # skip a frame that has nothing to do with a task.
            if event.get("type") == "agent.job_updated":
                payload = event["payload"]
                self.frames.append(event)
                self.agent.trace.append(f"broadcast:{payload['agent_state']}:{payload['terminal']}")
            else:
                self.phase_frames.append(event)
            await broadcast(event)

        # No browser is connected in this suite, so /events would drop the frames; the
        # relay's contract is what each broadcast would carry.
        self.gateway.broadcast = record  # type: ignore[method-assign]
        self.client = await self.start_gateway_client(self.gateway)

    async def start_gateway_client(self, gateway: StandaloneGateway) -> TestClient:
        app = web.Application()
        app.router.add_get("/api/agent/jobs", gateway.jobs)
        app.router.add_post("/api/agent/jobs", gateway.jobs)
        app.router.add_get("/api/agent/jobs/{jobId}", gateway.job)
        app.router.add_get("/tasks/{taskID}/artifacts/{name}", gateway.task_artifact)
        app.router.add_post("/api/agent/conversations", gateway.conversations_create)
        app.router.add_get("/api/agent/conversations/{conversationId}", gateway.conversation)
        app.router.add_post("/api/agent/conversations/{conversationId}/messages", gateway.conversation_messages)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    def body(self, command: str = "pick the cup", scene: dict | None = None,
             client_id: str | None = None, conversation_id: str | None = None) -> dict:
        """One §7.1 C1 body: the client id is generated per submission and reused on a retry."""
        if client_id is None:
            self.request_seq += 1
            client_id = f"req-{self.request_seq}"
        self.last_request_id = client_id
        body = {"command": command, "scene": LIVE if scene is None else scene,
                "clientRequestID": client_id}
        if conversation_id is not None:
            body["conversationId"] = conversation_id
        return body

    async def submit(self, command: str = "pick the cup", scene: dict | None = None,
                     client_id: str | None = None, conversation_id: str | None = None) -> dict:
        response = await self.client.post("/api/agent/jobs",
                                          json=self.body(command, scene, client_id, conversation_id))
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def post(self, **fields) -> web.Response:
        return await self.client.post("/api/agent/jobs", json=fields)

    async def poll_to(self, task_id: int, payload: dict) -> dict:
        """One round of polling that answers `payload`, returning the frame it published."""
        self.agent.scripts[task_id] = [payload]
        self.assertEqual(await self.gateway.poll_agent_tasks_once(), 1)
        return self.frames[-1]["payload"]

    async def test_start_task_relays_the_command_and_the_scene(self):
        # §5.15 step 2/3: only this round's command and this round's 现场 go upstream, and the
        # taskID the Agent hands back becomes the job id, as text.
        job = await self.submit("  move the arm  ")
        self.assertEqual(self.agent.received, [{
            "command": "move the arm", "scene": LIVE, "sceneDigest": scene_digest(LIVE),
            "scene_id": 7, "scene_version": 8, "scene_sub_version": 0, "sessionID": None,
            "clientRequestID": self.last_request_id, "browserID": "browser-1"}])
        self.assertEqual(job["id"], "1")
        self.assertEqual(job["task_id"], 1)
        self.assertEqual(job["command"], "move the arm")
        self.assertEqual((job["agent_state"], job["terminal"], job["message"]), (CREATED, False, CREATED))
        self.assertIsNone(job["outcome"])
        self.assertEqual(job["artifacts"], [])
        self.assertIsNone(job["failure_reason"])
        self.assertEqual(self.frames, [{"type": "agent.job_updated", "payload": job}])
        # §4: accepting this task is also the edge that hands the scene to the Agent.
        self.assertEqual(self.phase_frames, [{"type": "session.phase_changed",
                                              "payload": {"browser_id": "browser-1",
                                                          "session_phase": "agent_owned"}}])

    async def test_the_live_read_is_what_travels_not_the_body_scene(self):
        # §14.6 rule 1 (C2 before C3): the body's copy is shape-checked and hashed for idempotency,
        # and the document that reaches Brain is the one the bound browser handed over just now.
        stale = {"scene_schema_version": 1, "scene_id": 7, "scene_version": 3, "objects": []}
        job = await self.submit("pick", stale)
        self.assertEqual(self.agent.received[0]["scene"], LIVE)
        self.assertEqual((self.agent.received[0]["scene_version"],
                          self.agent.received[0]["sceneDigest"]), (8, scene_digest(LIVE)))
        self.assertEqual(self.live.calls, [{"action": "getWorld", "browser_id": "browser-1"}])
        # The read is a live one in every sense: it feeds §1.4's cache and the stored four-tuple,
        # so a caller that arrives afterwards sees the same 现场 this task was started over.
        self.assertEqual(self.gateway.world_cache, LIVE)
        self.assertEqual(self.gateway.state.scene_identity["scene_version"], 8)
        self.assertEqual(job["task_id"], 1)

    async def test_a_live_read_that_reports_two_versions_of_one_moment_starts_no_task(self):
        # §5.1: the flat fields beside the document are a cross-check, not a source. A browser that
        # names one version in its headers and another in its body is broken, and neither reading
        # may become a task -- Brain would then hold a lease over a scene nobody ever reported.
        self.live.mismatch = "scene_version"
        response = await self.post(command="pick", scene=LIVE, clientRequestID="req-a")
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())["error"]["code"], "GATEWAY_FRAMING_ERROR")
        self.assertEqual(self.agent.received, [])
        self.assertEqual(self.gateway.jobs_state, {})
        self.assertEqual(self.gateway.agent_requests, {})

    async def test_the_same_client_request_id_answers_the_task_it_started(self):
        # §7.1: the browser generates the id and reuses it across retries, so a resend of the same
        # payload is the same task -- not a second task, and not a second read of the 现场 either.
        job = await self.submit("pick the cup", client_id="req-retry")
        asked = len(self.live.calls)
        response = await self.post(command="pick the cup", scene=LIVE, clientRequestID="req-retry")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), job)
        self.assertEqual(len(self.agent.received), 1)
        self.assertEqual(len(self.live.calls), asked)
        self.assertEqual(list(self.gateway.jobs_state), ["1"])
        self.assertEqual(len(self.frames), 1)

    async def test_the_same_client_request_id_over_other_content_is_reused(self):
        # A second submission wearing the first one's id is a bug on the sender's side, and the
        # §10 code that says so is Brain's own `request_id_reused`. Refusing is the only answer
        # that cannot silently start a task the user never asked for.
        await self.submit("pick the cup", client_id="req-clash")
        asked = len(self.live.calls)
        response = await self.post(command="place it", scene=LIVE, clientRequestID="req-clash")
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "request_id_reused")
        self.assertEqual(len(self.agent.received), 1)
        self.assertEqual(len(self.live.calls), asked)
        self.assertEqual(list(self.gateway.jobs_state), ["1"])

    async def test_a_second_task_while_one_is_running_is_task_busy(self):
        # §7.1: one session holds one running task. The refusal happens before the read, because a
        # busy session has no business asking the browser what its scene is.
        job = await self.submit("pick the cup", client_id="req-a")
        asked = len(self.live.calls)
        response = await self.post(command="place it", scene=LIVE, clientRequestID="req-b")
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "task_busy")
        self.assertEqual(len(self.live.calls), asked)
        self.assertEqual(len(self.agent.received), 1)
        # Terminal is what frees the session -- not a state name (§7.4).
        self.agent.finish(job["task_id"])
        await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        again = await self.submit("place it", client_id="req-b")
        self.assertEqual(again["task_id"], 2)

    async def test_a_submission_without_a_client_id_or_a_bound_browser_is_missing_context(self):
        # §7.1's 缺上下文字段: the id is what makes a retry a retry, and with no bound browser there
        # is no 现场 to read. Both refuse before the browser is asked and before Brain is reached.
        for fields in ({"command": "pick", "scene": LIVE},
                       {"command": "pick", "scene": LIVE, "clientRequestID": "req/../x"},
                       {"command": "pick", "scene": LIVE, "clientRequestID": ""}):
            response = await self.post(**fields)
            self.assertEqual(response.status, 409, fields)
            self.assertEqual((await response.json())["error"]["code"], "session_context_required", fields)
        self.assertEqual(self.live.calls, [])
        self.assertEqual(self.agent.received, [])

        unbound = StandaloneGateway(config=config(agent_service_url=self.base_url))
        live = LiveRead(unbound, LIVE, bind=False)
        client = await self.start_gateway_client(unbound)
        response = await client.post("/api/agent/jobs",
                                     json={"command": "pick", "scene": LIVE, "clientRequestID": "req-a"})
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "session_context_required")
        self.assertEqual(live.calls, [])
        self.assertEqual(unbound.jobs_state, {})

    async def test_the_scene_never_travels_back_downstream(self):
        # §5.15: a world may be 100 MB (§11.1); GET /api/agent/jobs is a list of statuses.
        job = await self.submit()
        self.assertEqual(sorted(job), sorted(AGENT_JOB_FIELDS))
        self.assertNotIn("poll_errors", job)
        listed = await (await self.client.get("/api/agent/jobs")).json()
        self.assertEqual(listed["jobs"], [job])
        self.assertNotIn("scene", json.dumps(listed))
        self.assertNotIn("scene", json.dumps(self.frames + self.phase_frames))

    async def test_no_job_view_field_is_named_status(self):
        # §7.4 (Q116): `status` was the gateway's own token, and a browser that read it was
        # running a second state machine. One criterion survives: `terminal`.
        self.assertNotIn("status", AGENT_JOB_FIELDS)
        self.assertIn("terminal", AGENT_JOB_FIELDS)
        job = await self.submit()
        self.assertNotIn("status", job)

    async def test_jobs_are_scoped_to_conversation_and_broadcast_owner(self):
        self.gateway.state.start_session("s-a", phase="agent_owned")
        first = await (await self.client.post("/api/agent/conversations", json={"sessionID": "s-a"})).json()
        a = await self.submit(conversation_id=first["conversationId"])
        self.gateway.state.start_session("s-b", phase="agent_owned")
        second = await (await self.client.post("/api/agent/conversations", json={"sessionID": "s-b"})).json()
        b = await self.submit(conversation_id=second["conversationId"])
        self.assertEqual(a["conversation_id"], first["conversationId"])
        self.assertEqual(self.frames[-1]["payload"]["conversation_id"], second["conversationId"])
        filtered = await (await self.client.get("/api/agent/jobs", params={"conversationId": first["conversationId"]})).json()
        self.assertEqual(filtered["jobs"], [a])
        self.assertEqual((await (await self.client.get("/api/agent/jobs")).json())["jobs"], [a, b])
        await self.client.post(f"/api/agent/conversations/{first['conversationId']}/messages", json={
            "role": "user", "content": "late message", "turnId": "late",
        })
        self.assertEqual(self.gateway.active_conversation_id, second["conversationId"])

    async def test_task_phase_is_kept_in_its_session_context_until_terminal(self):
        self.gateway.state.start_session("s-a", phase="agent_owned")
        conversation = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "s-a", "title": "A",
        })).json()
        start = self.gateway.agent_relay.start_task

        async def switched_start(command, document, context):
            self.gateway.state.start_session("s-b", phase="setup")
            return await start(command, document, context)

        self.gateway.agent_relay.start_task = switched_start
        job = await self.submit(conversation_id=conversation["conversationId"])
        self.assertEqual(self.gateway.session_contexts["s-a"]["phase"], "agent_owned")
        self.assertEqual(self.gateway.state.session_phase, "setup")
        self.gateway.state.start_session("s-b", phase="setup")
        self.agent.finish(job["task_id"], outcome="plan_ready", reply="done")
        await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        self.assertEqual(self.gateway.session_contexts["s-a"]["phase"], "setup")

    async def test_stale_submission_session_is_refused_before_live_read(self):
        self.gateway.state.start_session("s-b", phase="agent_owned")
        response = await self.client.post("/api/agent/jobs", json={**self.body(), "sessionID": "s-a"})
        self.assertEqual(response.status, 409)
        self.assertEqual(self.live.calls, [])
        self.assertEqual(self.agent.received, [])

    async def test_session_switch_during_live_read_starts_no_task(self):
        self.gateway.state.start_session("s-a", phase="agent_owned")
        read = self.gateway._read_live_world

        async def switched(browser_id):
            world = await read(browser_id)
            self.gateway.state.start_session("s-b")
            return world

        self.gateway._read_live_world = switched
        response = await self.client.post("/api/agent/jobs", json={**self.body(), "sessionID": "s-a"})
        self.assertEqual(response.status, 409)
        self.assertEqual(self.agent.received, [])

    async def test_command_and_scene_are_validated_before_any_task_is_created(self):
        for body, code in (({"command": "   ", "scene": WORLD}, "INVALID_PARAM"),
                           ({"scene": WORLD}, "INVALID_PARAM"),
                           ({"command": "pick"}, "MISSING_PARAM"),
                           ({"command": "pick", "scene": "scene-7"}, "MISSING_PARAM")):
            response = await self.client.post("/api/agent/jobs", json=body)
            self.assertEqual(response.status, 400, body)
            self.assertEqual((await response.json())["error"]["code"], code)
        self.assertEqual(self.agent.received, [])
        self.assertEqual((await (await self.client.get("/api/agent/jobs")).json())["jobs"], [])

    async def test_one_task_is_read_back_by_id(self):
        job = await self.submit()
        listed = await (await self.client.get(f"/api/agent/jobs/{job['id']}")).json()
        self.assertEqual(listed, job)
        missing = await self.client.get("/api/agent/jobs/42")
        self.assertEqual(missing.status, 404)
        self.assertEqual((await missing.json())["error"]["code"], "TASK_NOT_FOUND")

    async def test_an_empty_agent_service_url_disables_the_relay(self):
        relay = AgentRelay(config(agent_service_url=""))
        self.assertEqual(relay.base_url, "")
        # A trailing slash is trimmed so /startTask cannot become //startTask.
        self.assertEqual(AgentRelay(config(agent_service_url="http://127.0.0.1:8500/")).base_url,
                         "http://127.0.0.1:8500")
        for call, arguments in (("start_task", ("pick", WORLD, CONTEXT)), ("task_state", (1,)),
                                ("task_result", (1,)), ("failure_reason", (1,)),
                                ("artifact", (1, "commands"))):
            with self.subTest(call=call):
                with self.assertRaises(BrowserError) as caught:
                    await getattr(relay, call)(*arguments)
                self.assertEqual(caught.exception.status_code, 501)
                self.assertEqual(caught.exception.code, "AGENT_UNAVAILABLE")

    async def test_an_unreachable_agent_service_is_502_and_stores_nothing(self):
        gateway = StandaloneGateway(config=config(agent_service_url="http://127.0.0.1:1"))
        LiveRead(gateway, LIVE)
        client = await self.start_gateway_client(gateway)
        response = await client.post("/api/agent/jobs", json=self.body("pick"))
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())["error"]["code"], "UPSTREAM_ERROR")
        self.assertEqual(gateway.jobs_state, {})
        self.assertEqual(gateway.agent_requests, {})

    async def test_a_slow_agent_service_is_504(self):
        self.agent.slow_seconds = 0.5
        gateway = StandaloneGateway(config=config(agent_service_url=self.base_url,
                                                  agent_request_timeout_seconds=0.1))
        LiveRead(gateway, LIVE)
        client = await self.start_gateway_client(gateway)
        response = await client.post("/api/agent/jobs", json=self.body("pick"))
        self.assertEqual(response.status, 504)
        self.assertEqual((await response.json())["error"]["code"], "AGENT_TIMEOUT")
        self.assertEqual(gateway.jobs_state, {})

    async def test_a_start_task_reply_without_a_task_id_is_502(self):
        # The Agent service answers 200 with {"taskID": ...}; anything else is a broken
        # upstream, and a task without an id could never be polled.
        async def bad_start_task(request: web.Request) -> web.Response:
            return web.json_response({"code": 0, "message": "任务已创建"})

        app = web.Application()
        app.router.add_post("/startTask", bad_start_task)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        gateway = StandaloneGateway(config=config(agent_service_url=str(server.make_url("/")).rstrip("/")))
        LiveRead(gateway, LIVE)
        client = await self.start_gateway_client(gateway)
        response = await client.post("/api/agent/jobs", json=self.body("pick"))
        self.assertEqual(response.status, 502)
        self.assertIn("taskID", (await response.json())["error"]["message"])
        self.assertEqual(gateway.jobs_state, {})

    async def test_an_error_reply_from_start_task_keeps_the_upstream_text(self):
        # §5.15: the relay quotes the Agent service's own line instead of writing a new one.
        self.agent.start_reply = (503, {"error": "服务繁忙，请稍后重试"})
        response = await self.post(**self.body("pick"))
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())["error"]["message"], "服务繁忙，请稍后重试")
        self.assertEqual(self.gateway.jobs_state, {})

    async def test_a_task_id_of_zero_is_not_a_task(self):
        self.agent.start_reply = (200, {"taskID": 0, "state": CREATED, "terminal": False})
        response = await self.post(**self.body("pick"))
        self.assertEqual(response.status, 502)
        self.assertIn("taskID", (await response.json())["error"]["message"])
        self.assertEqual(self.gateway.jobs_state, {})

    async def test_a_start_task_that_claims_terminal_is_ignored(self):
        # §7.4 rule 8: a task that was just created is running. A service that says otherwise
        # is warned about, and the first /queryTaskState is the one that decides.
        self.agent.start_reply = (200, {"taskID": 9, "state": CREATED, "terminal": True})
        self.agent.tasks[9] = state(CREATED, terminal=False)
        self.agent.finish(9)
        with self.assertLogs("web_frontend.local_gateway", level="WARNING") as logged:
            job = await self.submit()
        self.assertFalse(job["terminal"])
        self.assertFalse(self.gateway.jobs_state["9"]["terminal"])
        self.assertTrue(any("terminal:true" in line for line in logged.output), logged.output)
        payload = await self.poll_to(9, state(SUCCESS, terminal=True))
        self.assertTrue(payload["terminal"])
        self.assertEqual(payload["outcome"], "scene_updated")

    async def test_a_poll_reply_without_a_state_field_is_unknown_not_terminal(self):
        # §7.4 rule 1: an empty state string is quoted as it arrived and ends nothing.
        job = await self.submit()
        self.agent.poll_raw = {"taskID": job["task_id"], "terminal": False}
        self.assertEqual(await self.gateway.poll_agent_tasks_once(), 1)
        current = self.gateway.jobs_state[job["id"]]
        self.assertEqual((current["agent_state"], current["terminal"]), ("", False))
        self.assertEqual(current["poll_errors"], 0)
        self.assertEqual(await self.gateway.poll_agent_tasks_once(), 1)

    async def test_a_poll_reply_that_is_not_an_object_is_an_upstream_error(self):
        job = await self.submit()
        self.agent.poll_raw = [CREATED]
        self.assertEqual(await self.gateway.poll_agent_tasks_once(), 1)
        current = self.gateway.jobs_state[job["id"]]
        self.assertEqual(current["agent_state"], CREATED)
        self.assertFalse(current["terminal"])
        self.assertEqual(current["poll_errors"], 1)

    async def test_a_missing_terminal_flag_is_not_terminal(self):
        # §7.4 rule 2: only `true` ends a task, and the gateway says so in the log rather than
        # silently agreeing with a service that forgot the field.
        job = await self.submit()
        with self.assertLogs("web_frontend.local_gateway", level="WARNING") as logged:
            payload = await self.poll_to(job["task_id"], state(RUNNING))
        self.assertFalse(payload["terminal"])
        self.assertTrue(any("`terminal`" in line for line in logged.output), logged.output)

    async def test_a_terminal_flag_that_is_not_a_boolean_is_not_terminal(self):
        # "true", 1 and null each mean the service never said it, and the gateway may not say
        # it on its behalf -- a wrong "yes" would freeze the task halfway forever.
        job = await self.submit()
        for value in ("true", 1, None):
            with self.subTest(value=value):
                payload = await self.poll_to(job["task_id"], state(RUNNING, terminal=value))
                self.assertFalse(payload["terminal"])

    async def test_the_poll_quotes_whatever_upstream_calls_itself(self):
        # §7.4 (Q116): these strings are opaque prose, one of them from a vocabulary no table
        # of ours ever knew. The job view carries them verbatim; nothing is translated.
        job = await self.submit()
        for text in (QUEUED, FOREIGN, RUNNING):
            payload = await self.poll_to(job["task_id"], state(text, terminal=False))
            self.assertEqual(payload["agent_state"], text)
            self.assertFalse(payload["terminal"])
        self.assertEqual([frame["payload"]["agent_state"] for frame in self.frames],
                         [CREATED, QUEUED, FOREIGN, RUNNING])
        self.assertIsNone(self.gateway.jobs_state[job["id"]]["outcome"])

    async def test_an_unchanged_state_rebroadcasts_nothing(self):
        job = await self.submit()
        self.agent.scripts[job["task_id"]] = [state(CREATED, terminal=False)]
        self.assertEqual(await self.gateway.poll_agent_tasks_once(), 1)
        self.assertEqual(len(self.frames), 1)
        self.assertEqual(self.gateway.jobs_state[job["id"]]["agent_state"], CREATED)

    async def test_a_state_detail_change_broadcasts_even_when_the_state_repeats(self):
        # §7.4 rule 3: `state_detail` is upstream's progress line, and on a long run it is the
        # only thing that moves -- a poll that ignored it would look frozen.
        job = await self.submit()
        payload = await self.poll_to(job["task_id"], state(RUNNING, terminal=False, state_detail="第 2/5 段"))
        self.assertEqual(payload["state_detail"], "第 2/5 段")
        self.assertEqual(len(self.frames), 2)
        await self.poll_to(job["task_id"], state(RUNNING, terminal=False, state_detail="第 2/5 段"))
        self.assertEqual(len(self.frames), 2)

    async def test_the_terminal_round_reads_result_and_reason_once_each(self):
        # §7.4: `terminal` flips -> one /queryTaskResult + one /queryTaskFailureReason, and
        # after that this job is never asked again.
        job = await self.submit()
        self.agent.finish(job["task_id"], outcome="plan_ready", reply="计划如下",
                          plan={"commands": ["pick", "place"]}, metrics={"model_calls": 3},
                          artifacts=[{"name": "commands"}])
        self.agent.reasons[job["task_id"]] = REASON
        payload = await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        self.assertTrue(payload["terminal"])
        self.assertEqual(self.agent.result_calls, [job["task_id"]])
        self.assertEqual(self.agent.trace.count("failure_reason"), 1)
        self.assertEqual(self.agent.trace,
                         ["broadcast:已创建:False", "query_result", "failure_reason",
                          "broadcast:任务完成:True"])
        asked = len(self.agent.state_calls)
        self.assertEqual(await self.gateway.poll_agent_tasks_once(), 0)
        self.assertEqual(len(self.agent.state_calls), asked)

    async def test_the_result_lands_in_the_job_view(self):
        # §7.3: the fields the panel branches on, taken from one /queryTaskResult.
        job = await self.submit()
        self.agent.finish(job["task_id"], outcome="plan_ready", reply="计划如下",
                          plan={"commands": ["a", "b"]}, metrics={"model_calls": 2},
                          artifacts=[{"name": "scene_config"}, {"name": "query_result"}])
        payload = await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        self.assertEqual(payload["outcome"], "plan_ready")
        self.assertEqual(payload["reply"], "计划如下")
        self.assertEqual(payload["plan"], {"commands": ["a", "b"]})
        self.assertEqual(payload["metrics"], {"model_calls": 2})
        self.assertEqual(payload["artifacts"], ["scene_config", "query_result"])
        # `executed` is Brain's own field and the panel never reads it (§7.3: 恒 false).
        self.assertNotIn("executed", payload)

    async def test_offline_terminal_reply_is_journaled_to_origin_once(self):
        self.gateway.state.start_session("origin-session", phase="agent_owned")
        origin = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "origin-session", "title": "Origin",
        })).json()
        job = await self.submit()
        other = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "other-session", "title": "Other",
        })).json()
        self.gateway.state.start_session("other-session")
        self.assertFalse(self.gateway.browsers)
        self.agent.finish(job["task_id"], outcome="plan_ready", reply="Offline plan ready")
        await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        path = f"/api/agent/conversations/{origin['conversationId']}"
        detail = await (await self.client.get(path)).json()
        self.assertEqual([(item["role"], item["content"], item["turnId"])
                          for item in detail["messages"]],
                         [("assistant", "Offline plan ready", job["id"])])
        self.assertEqual(self.gateway.active_conversation_id, other["conversationId"])
        self.assertEqual(self.gateway.conversations[other["conversationId"]]["messages"], [])
        self.assertEqual(await self.gateway.poll_agent_tasks_once(), 0)
        # An older browser may still echo the terminal message; the shared task turnId dedupes it.
        await self.client.post(path + "/messages", json={
            "role": "assistant", "content": "Offline plan ready", "turnId": job["id"],
        })
        again = await (await self.client.get(path)).json()
        self.assertEqual(again["messages"], detail["messages"])
        self.assertEqual(self.gateway.active_conversation_id, other["conversationId"])

    async def test_terminal_reply_uses_the_active_row_when_a_session_has_duplicate_history(self):
        self.gateway.state.start_session("origin-session", phase="agent_owned")
        old = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "origin-session", "title": "Old origin",
        })).json()
        current = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "origin-session", "title": "Current origin",
        })).json()
        later = await (await self.client.post("/api/agent/conversations", json={
            "sessionID": "origin-session", "title": "Later active origin",
        })).json()
        job = await self.submit(conversation_id=current["conversationId"])
        self.assertEqual(self.gateway.jobs_state[str(job["task_id"])] ["conversation_id"],
                         current["conversationId"])
        self.agent.finish(job["task_id"], outcome="plan_ready", reply="Current plan")
        await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        old_detail = await (await self.client.get(
            f"/api/agent/conversations/{old['conversationId']}" )).json()
        current_detail = await (await self.client.get(
            f"/api/agent/conversations/{current['conversationId']}" )).json()
        later_detail = await (await self.client.get(
            f"/api/agent/conversations/{later['conversationId']}" )).json()
        self.assertEqual(old_detail["messages"], [])
        self.assertEqual(later_detail["messages"], [])
        self.assertEqual([(item["role"], item["content"], item["turnId"])
                          for item in current_detail["messages"]],
                         [("assistant", "Current plan", job["id"])])

    async def test_artifacts_are_filtered_to_the_four_whitelisted_names(self):
        # §7.3: `debug/` and server paths are not published at all -- a browser that saw such a
        # name could ask for it, and the download route is a whitelist too (§7).
        job = await self.submit()
        self.agent.finish(job["task_id"], outcome="plan_ready",
                          artifacts=[{"name": "scene_config"}, {"name": "debug/trace.log"},
                                     {"name": "/srv/agent/scene.json"}, {"name": "commands"},
                                     "query_result", {"name": "commands"}, {"note": "no name"}])
        payload = await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        self.assertEqual(payload["artifacts"], ["scene_config", "commands", "query_result"])

    # ------------------------------------------------- §7.3 rule 6: commit_receipts, from our disk

    def receipt(self, transaction_id: str = "tx-1", **overrides: Any) -> dict[str, Any]:
        """One §8.1 receipt as this gateway's own ledger would hold it."""
        record: dict[str, Any] = {"transactionID": transaction_id, "browserID": "browser-1",
                                 "status": "committed", "scene_id": 7, "scene_version": 8,
                                 "scene_sub_version": 0, "sceneDigest": scene_digest(LIVE),
                                 "rejected_objects": [], "candidate_digest": scene_digest(LIVE)}
        record.update(overrides)
        return record

    async def test_a_named_transaction_comes_back_as_our_own_receipt(self):
        # §7.3 rule 6: Brain's `commitReceipts` opens the ledger, it does not read it out to us.
        job = await self.submit()
        self.gateway.receipts.save("tx-1", self.receipt())
        self.agent.finish(job["task_id"], outcome="scene_updated", commitReceipts=["tx-1"])
        payload = await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        self.assertEqual(payload["commit_receipts"], [{key: value for key, value in self.receipt().items()
                                                      if key != "candidate_digest"}])

    async def test_a_transaction_this_gateway_never_settled_says_unknown(self):
        # The claim and the ledger are two different things, and a job view publishes the ledger:
        # `unknown` is the reading §8.1 reserves for an absence, and it never ages into `rejected`.
        job = await self.submit()
        self.agent.finish(job["task_id"], outcome="scene_updated",
                          commitReceipts=[{"transactionID": "tx-9", "status": "committed",
                                           "sceneDigest": scene_digest(LIVE)}])
        payload = await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        self.assertEqual(payload["commit_receipts"], [{"transactionID": "tx-9", "status": "unknown",
                                                       "reason": NO_RECEIPT_REASON}])

    async def test_an_unreadable_receipt_leaves_the_task_finished_and_the_receipt_unknown(self):
        # A half-written file is a 500 on `queryWorldCommit`, because that route is asked to settle
        # a transaction. Here it is only a display, and the task did end: §8.3 keeps the one
        # route that may turn an `unknown` into a verdict.
        workspace = tempfile.TemporaryDirectory(prefix="agent-receipts-")
        self.addCleanup(workspace.cleanup)
        directory = Path(workspace.name) / "gateway_transactions"
        directory.mkdir()
        (directory / "tx-7.json").write_text('{"transactionID": "tx-7"', encoding="utf-8")
        self.gateway.receipts = TransactionReceipts(directory)
        job = await self.submit()
        self.agent.finish(job["task_id"], outcome="scene_updated", commitReceipts=["tx-7"])
        payload = await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        self.assertTrue(payload["terminal"])
        self.assertEqual(payload["outcome"], "scene_updated")
        self.assertEqual(payload["commit_receipts"],
                         [{"transactionID": "tx-7", "status": "unknown",
                           "reason": "the stored receipt does not read as a commit record"}])

    async def test_a_rejected_receipt_is_published_as_rejected(self):
        # The ledger's own verdict, whatever it is: a rejected commit is a fact about the task.
        job = await self.submit()
        self.gateway.receipts.save("tx-2", self.receipt("tx-2", status="rejected",
                                                        rejected_objects=[{"index": 1}],
                                                        reason="1 object refused"))
        self.agent.finish(job["task_id"], outcome="scene_updated", commitReceipts=["tx-2"])
        payload = await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        self.assertEqual(payload["commit_receipts"][0]["status"], "rejected")
        self.assertEqual(payload["commit_receipts"][0]["reason"], "1 object refused")

    async def test_a_task_that_named_nothing_publishes_no_receipts(self):
        job = await self.submit()
        self.agent.finish(job["task_id"], outcome="answered", reply="桌上有两个杯子")
        payload = await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        self.assertEqual(payload["commit_receipts"], [])
        # A task that was never accepted names nothing either -- the field is in every view.
        self.assertEqual(job["commit_receipts"], [])
        self.assertIn("commit_receipts", AGENT_JOB_FIELDS)

    def test_only_a_name_is_taken_from_an_upstream_receipt_entry(self):
        # §8.1's file-name whitelist is the whole check, so a `../` entry cannot even be looked up.
        self.assertEqual(
            agent_transaction_ids(["tx-1", {"transactionID": "tx-2", "status": "committed"},
                                   "../etc/passwd", "x" * 129, "", None, 7, {"note": "no id"},
                                   "tx-1", {"transactionID": "../a"}]),
            ["tx-1", "tx-2"])

    async def test_an_artifact_comes_back_as_the_bytes_brain_sent(self):
        # §14.5 E2: the proxy is a pipe. No job was ever submitted here, and that is the point --
        # the job table is process memory (§13 O10) and must not decide what a caller may still
        # download from a service that has the file on disk.
        self.agent.artifacts[(1, "scene_patch")] = b'{"objects": []}\n'
        response = await self.client.get("/tasks/1/artifacts/scene_patch")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b'{"objects": []}\n')
        self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertEqual(self.agent.artifact_calls, [(1, "scene_patch")])

    async def test_a_binary_artifact_keeps_its_own_content_type(self):
        self.agent.artifacts[(2, "commands")] = b"\x00\x01\x02"
        self.agent.artifact_content_type = "application/octet-stream"
        response = await self.client.get("/tasks/2/artifacts/commands")
        self.assertEqual(await response.read(), b"\x00\x01\x02")
        self.assertEqual(response.headers["Content-Type"], "application/octet-stream")

    async def test_every_name_off_the_four_is_refused_without_an_upstream_call(self):
        # The whitelist is the whole path check: a name that carries a separator cannot be one of
        # the four, so the encoded traversal is refused here and never enters the URL we build.
        for name in ("debug", "trace.log", "scene_config.json", "SCENE_CONFIG",
                     "%2e%2e%2fscene_config", "scene_patch%2f..%2fsecret"):
            response = await self.client.get(f"/tasks/1/artifacts/{name}")
            self.assertEqual(response.status, 400, name)
            self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM", name)
        self.assertEqual(self.agent.artifact_calls, [])

    async def test_a_task_id_that_is_not_a_uint32_is_refused_without_an_upstream_call(self):
        for raw in ("abc", "-1", "4294967296", "1.5", "１"):
            response = await self.client.get(f"/tasks/{raw}/artifacts/commands")
            self.assertEqual(response.status, 400, raw)
            self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM", raw)
        self.assertEqual(self.agent.artifact_calls, [])

    async def test_an_artifact_of_a_task_brain_forgot_keeps_brains_404(self):
        # The 404 the spec's route table names is Brain's own answer about its own storage, quoted
        # rather than rewritten -- a caller has to be able to tell "that task is gone" from a break.
        self.agent.artifact_status = 404
        response = await self.client.get("/tasks/99/artifacts/commands")
        self.assertEqual(response.status, 404)
        self.assertEqual((await response.json())["error"], "任务不存在: 99")
        self.assertEqual(self.agent.artifact_calls, [(99, "commands")])

    async def test_an_artifact_that_reaches_no_agent_service_is_502(self):
        gateway = StandaloneGateway(config=config(agent_service_url="http://127.0.0.1:1"))
        client = await self.start_gateway_client(gateway)
        response = await client.get("/tasks/1/artifacts/commands")
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())["error"]["code"], "UPSTREAM_ERROR")

    async def test_the_artifact_route_is_registered_before_the_static_catch_all(self):
        # 坑 2: a route the SPA shell swallows answers 200 with index.html instead of the contract.
        app = create_app(config(agent_service_url=self.base_url))
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        response = await client.get("/tasks/not-a-number/artifacts/commands")
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "INVALID_PARAM")
        self.agent.artifacts[(1, "query_result")] = b'{"rows": []}'
        response = await client.get("/tasks/1/artifacts/query_result")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b'{"rows": []}')

    async def test_an_outcome_outside_the_five_is_published_as_none(self):
        # §7.4: `outcome` is the one enumeration we keep, so an unknown one is a contract
        # break. Guessing a classification would put words in the panel Brain never said.
        job = await self.submit()
        self.agent.finish(job["task_id"], outcome="succeeded")
        with self.assertLogs("web_frontend.local_gateway", level="WARNING") as logged:
            payload = await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        self.assertIsNone(payload["outcome"])
        self.assertTrue(payload["terminal"])
        self.assertTrue(any("outcome" in line for line in logged.output), logged.output)

    async def test_an_unreadable_result_still_settles_the_task(self):
        # §7.4: the task said it ended, which is enough to stop polling. A result we cannot read
        # leaves `outcome` absent instead of failing something we never witnessed.
        job = await self.submit()
        self.agent.result_status = 500
        payload = await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        self.assertTrue(payload["terminal"])
        self.assertIsNone(payload["outcome"])
        self.assertIsNone(payload["reply"])
        self.assertEqual(payload["artifacts"], [])
        self.assertEqual(payload["message"], SUCCESS)

    async def test_a_202_from_the_result_route_is_not_read_as_a_result(self):
        # §7.3: a task with no result answers 202, and a 2xx body is a statement about the task,
        # not its outcome. Reading it would publish "finished with nothing" for a task that
        # merely has not written a result. The job still ends -- `terminal` was upstream's own
        # word for it -- just with no `outcome`.
        job = await self.submit()
        payload = await self.poll_to(job["task_id"], state(SUCCESS, terminal=True))
        self.assertTrue(payload["terminal"])
        self.assertIsNone(payload["outcome"])
        self.assertEqual(payload["message"], SUCCESS)

    async def test_a_failure_reason_route_that_fails_leaves_the_message_on_the_state(self):
        # §5.15: the reason is best-effort; a missing one must not hide the failure itself.
        job = await self.submit()
        self.agent.finish(job["task_id"], outcome="failed")
        self.agent.reasons[job["task_id"]] = REASON
        self.agent.reason_status = 404
        payload = await self.poll_to(job["task_id"], state(FAILED, terminal=True))
        self.assertTrue(payload["terminal"])
        self.assertEqual(payload["outcome"], "failed")
        self.assertIsNone(payload["failure_reason"])
        self.assertEqual(payload["message"], FAILED)

    async def test_the_failure_reason_is_read_before_the_terminal_frame_is_published(self):
        # §5.15 step 5/6: one frame has to carry both the state and the reason.
        job = await self.submit()
        self.agent.finish(job["task_id"], outcome="failed")
        self.agent.reasons[job["task_id"]] = REASON
        payload = await self.poll_to(job["task_id"], state(FAILED, terminal=True))
        self.assertEqual(payload["agent_state"], FAILED)
        self.assertEqual(payload["failure_reason"], REASON)
        self.assertEqual(payload["message"], REASON)
        self.assertLess(self.agent.trace.index("failure_reason"),
                        self.agent.trace.index("broadcast:任务失败:True"))

    async def test_a_failure_without_a_reason_falls_back_to_the_state(self):
        job = await self.submit()
        self.agent.finish(job["task_id"], outcome="failed")
        payload = await self.poll_to(job["task_id"], state(FAILED, terminal=True))
        self.assertIsNone(payload["failure_reason"])
        self.assertEqual(payload["message"], FAILED)

    async def test_a_task_the_agent_forgot_is_failed_with_the_upstream_text(self):
        # §7.4 rule 5: the service restarted, so /queryTaskState answers 404. That is a fact
        # about the service, not one of its state words, and the gateway quotes its line rather
        # than inventing one (§5.6).
        job = await self.submit()
        del self.agent.tasks[job["task_id"]]
        self.assertEqual(await self.gateway.poll_agent_tasks_once(), 1)
        payload = self.frames[-1]["payload"]
        self.assertTrue(payload["terminal"])
        self.assertEqual(payload["outcome"], "failed")
        self.assertIn("任务不存在", payload["agent_state"])
        self.assertIn("任务不存在", payload["message"])
        # Nothing further can be read from a service that has forgotten the task.
        self.assertNotIn("failure_reason", self.agent.trace)
        self.assertNotIn("query_result", self.agent.trace)
        self.assertEqual(self.agent.state_calls, [job["task_id"]])
        self.assertEqual(await self.gateway.poll_agent_tasks_once(), 0)

    async def test_poll_errors_below_the_limit_keep_the_task_running(self):
        # §7.4 rule 6: one HTTP hiccup must not be reported as a dead task.
        job = await self.submit()
        self.agent.poll_status = 500
        for count in range(1, AGENT_POLL_ERROR_LIMIT):
            self.assertEqual(await self.gateway.poll_agent_tasks_once(), 1)
            current = self.gateway.jobs_state[job["id"]]
            self.assertEqual(current["agent_state"], CREATED)
            self.assertFalse(current["terminal"])
            self.assertEqual(current["poll_errors"], count)
        self.assertEqual(len(self.frames), 1)

    async def test_the_fifth_consecutive_poll_error_finalizes_the_task(self):
        job = await self.submit()
        self.agent.poll_status = 500
        for _ in range(AGENT_POLL_ERROR_LIMIT):
            await self.gateway.poll_agent_tasks_once()
        payload = self.frames[-1]["payload"]
        self.assertTrue(payload["terminal"])
        self.assertEqual(payload["outcome"], "failed")
        self.assertEqual(payload["message"], "UPSTREAM_ERROR: 服务内部错误")
        self.assertEqual(payload["failure_reason"], "UPSTREAM_ERROR: 服务内部错误")
        self.assertEqual(self.gateway.jobs_state[job["id"]]["poll_errors"], AGENT_POLL_ERROR_LIMIT)
        self.assertEqual(await self.gateway.poll_agent_tasks_once(), 0)

    async def test_a_transport_error_counts_as_a_poll_error(self):
        job = await self.submit()
        self.gateway.config["agent_service_url"] = "http://127.0.0.1:1"
        self.assertEqual(await self.gateway.poll_agent_tasks_once(), 1)
        current = self.gateway.jobs_state[job["id"]]
        self.assertFalse(current["terminal"])
        self.assertEqual(current["poll_errors"], 1)
        # §5.15: a good round resets the streak, so a recovered service is not punished.
        self.gateway.config["agent_service_url"] = self.base_url
        payload = await self.poll_to(job["task_id"], state(RUNNING, terminal=False))
        self.assertEqual(self.gateway.jobs_state[job["id"]]["poll_errors"], 0)
        self.assertEqual(payload["agent_state"], RUNNING)

    async def test_the_poller_belongs_to_the_app_not_to_a_handler(self):
        # §5.15/Q86: one poller per process, started and stopped with the app (坑 10).
        gateway = StandaloneGateway(config=config(agent_service_url=self.base_url,
                                                  agent_poll_interval_seconds=30))
        app = web.Application()
        app.on_startup.append(gateway.start_agent_poller)
        app.on_cleanup.append(gateway.stop_agent_poller)
        async with TestClient(TestServer(app)):
            self.assertIsNotNone(gateway.agent_poller)
            poller = gateway.agent_poller
            await gateway.start_agent_poller()
            self.assertIs(gateway.agent_poller, poller)
        self.assertIsNone(gateway.agent_poller)
        self.assertTrue(poller.cancelled())

    async def test_the_agent_codes_are_gateway_raised_not_browser_reported(self):
        # §5.13: BROWSER_ERROR_STATUS maps what a browser reports; these three never
        # travel through a browser.response, so a stale entry there would be a lie.
        for code in ("AGENT_UNAVAILABLE", "TASK_NOT_FOUND", "AGENT_TIMEOUT"):
            self.assertNotIn(code, BROWSER_ERROR_STATUS)

    async def test_no_source_file_names_the_old_state_vocabulary(self):
        # §7.1 判据: the five literals and the table that mapped them may survive only in this
        # suite's fake, because such a name anywhere else is a second state machine.
        banned = (CREATED, QUEUED, RUNNING, SUCCESS, FAILED, "AGENT_STATES")
        for relative in ("local_gateway.py", "editor/src/logic.ts", "editor/src/main.ts"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            for word in banned:
                self.assertNotIn(word, text, f"{relative} still speaks {word!r}")


class AgentHelperTests(unittest.TestCase):
    def test_agent_state_text_quotes_the_upstream_string(self):
        self.assertEqual(agent_state_text(FOREIGN), FOREIGN)
        for value in ("", None, 7, True):
            self.assertEqual(agent_state_text(value), "", value)

    def test_agent_terminal_accepts_only_true(self):
        for value, expected in ((True, True), (False, False), (None, False), ("true", False),
                                (1, False), ({}, False)):
            with self.subTest(value=value):
                self.assertEqual(agent_terminal({"state": RUNNING, "terminal": value}), expected)
        self.assertEqual(agent_terminal({"state": RUNNING}), False)

    def test_agent_state_detail_keeps_a_line_and_drops_noise(self):
        for payload, expected in (({"state_detail": "第 2/5 段"}, "第 2/5 段"),
                                  ({"state_detail": "  "}, None), ({"state_detail": None}, None),
                                  ({"state_detail": 3}, None), ({}, None)):
            self.assertEqual(agent_state_detail(payload), expected, payload)

    def test_agent_outcome_keeps_the_five_and_rejects_the_rest(self):
        for value in AGENT_OUTCOMES:
            self.assertEqual(agent_outcome(value), value)
        for value in ("succeeded", "SUCCESS", None, "", 3):
            self.assertEqual(agent_outcome(value), None, value)

    def test_agent_reply_only_passes_a_line_with_content(self):
        self.assertEqual(agent_reply("已摆放"), "已摆放")
        for value in ("   ", None, 3):
            self.assertIsNone(agent_reply(value), value)

    def test_agent_artifact_names_keeps_the_whitelist(self):
        self.assertEqual(agent_artifact_names([{"name": name} for name in AGENT_ARTIFACT_NAMES]),
                         list(AGENT_ARTIFACT_NAMES))
        self.assertEqual(agent_artifact_names(["commands", "debug/log", {"name": "scene_patch"}]),
                         ["commands", "scene_patch"])
        for value in (None, {}, [], "scene_config", [{"other": 1}]):
            self.assertEqual(agent_artifact_names(value), [], value)

    def test_task_context_spells_the_c3_fields(self):
        # §7.1: seven names beside `command` and `scene`, spelled in exactly one place. A field the
        # flow doc renames has to break here rather than quietly stop travelling upstream.
        self.assertEqual(CONTEXT.start_task_fields(),
                         {"sceneDigest": scene_digest(LIVE), "scene_id": 7, "scene_version": 8,
                          "scene_sub_version": 0, "sessionID": None,
                          "clientRequestID": "req-1", "browserID": "browser-1"})

    def test_agent_task_id_rejects_the_ids_a_poll_cannot_use(self):
        for value, expected in ((7, 7), ("12", 12), (True, None), (0, None), (-3, None),
                                ("０７", None), ("1.5", None), ("abc", None), (None, None), (7.0, None)):
            self.assertEqual(agent_task_id(value), expected, value)

    def test_agent_error_text_prefers_the_upstream_prose(self):
        for payload, expected in (({"error": "a"}, "a"), ({"message": "b"}, "b"), ({"detail": "c"}, "c"),
                                  ({"error": "   ", "message": "b"}, "b"), ({}, "fallback"),
                                  ("not an object", "fallback"), (None, "fallback")):
            self.assertEqual(agent_error_text(payload, "fallback"), expected, payload)


if __name__ == "__main__":
    unittest.main()
