"""Probe the live caller surface (§5.9/§10.1) of the real local gateway.

Failures are parity gaps, not waived or expected-success tests. The gateway is a
channel and the browser owns the scene (§1.1), so no probe here invents a
`browser.response`: each check either measures what the gateway does on its own -
route registration, parameter gating, media type and size caps, status codes - or
asserts the documented answer it owes a caller while no browser is connected.
The one exception is `browser.state_sync`, the reporting half of §5.8, which is
how the world cache gets filled at all. Run from the standalone project.
"""
from __future__ import annotations
import asyncio
import io
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime, timezone

from aiohttp import FormData, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import local_gateway as gateway

# §5.9 in full: every route a caller may use, and what a gateway with no browser
# connected owes them. A JSON error here is proof the route is live; the SPA shell
# would be proof it was swallowed by the static catch-all (坑 2).
def traj_form(*fields: tuple[str, bytes]) -> FormData:
    """§5.3 (Q99): a caller posts one `trajs` part per clip, in list order."""
    form = FormData()
    for name, value in fields:
        # BytesIO, not bytes: aiohttp v4 will stop treating a bare bytes value as a file.
        form.add_field(name, io.BytesIO(value))
    return form


CALLER_SURFACE = [
    ("GET", "/getWorld", None, 503, "NO_BROWSER_CONNECTED"),
    ("GET", "/captureScene?id=2&type=rgbd", None, 503, "NO_BROWSER_CONNECTED"),
    ("GET", "/get2DAABB?id=3&camera_id=2", None, 503, "NO_BROWSER_CONNECTED"),
    ("POST", "/updateWorld", {"scene_schema_version": 1, "scene_version": 1, "scene_id": 1, "objects": []}, 503, "NO_BROWSER_CONNECTED"),
    ("POST", "/updateItem?id=3", {"object_id_in_scene": 3}, 503, "NO_BROWSER_CONNECTED"),
    ("POST", "/addObject", {"object_name_in_scene": "cube"}, 503, "NO_BROWSER_CONNECTED"),
    ("POST", "/removeObject?id=3", None, 503, "NO_BROWSER_CONNECTED"),
    ("POST", "/updateTraj", "multipart", 503, "NO_BROWSER_CONNECTED"),
    ("GET", "/api/sessionState", None, 200, None),
    # §4/§5: the two reads that answer from the gateway's own state, so a caller with no
    # browser and no Brain still gets a shape rather than a 503.
    ("GET", "/sessions", None, 200, None),
    ("GET", "/brainCapabilities", None, 200, None),
    ("GET", "/allocateObjectID?browserID=browser-1", None, 503, "NO_BROWSER_CONNECTED"),
    ("GET", "/api/health", None, 200, None),
    ("GET", "/api/runtimeConfig", None, 200, None),
]

# §10.1: the old surface must be gone, either as 405 (the catch-all is GET only)
# or as the SPA shell it falls through to. Never a JSON scene API.
DELETED_SURFACE = [
    ("POST", "/api/commands"), ("GET", "/api/bootstrap"), ("GET", "/api/assets"),
    ("POST", "/api/assets/upload"), ("POST", "/api/assets/import-remote"),
    ("GET", "/api/assets/remote-proxy"), ("DELETE", "/api/assets/1"),
    ("GET", "/api/assets/1/file"), ("GET", "/api/sensors/1/frame"),
    ("POST", "/api/state/load-simstate"), ("GET", "/api/diagnostics"), ("GET", "/api/metrics"),
    ("GET", "/api/presentation"), ("POST", "/api/presentation/mode"),
    ("POST", "/api/agent-control/captures"), ("GET", "/api/timelines/1/frames/0"),
    ("GET", "/api/timelines/1/data"),
]

# Gating the gateway owes a caller before it touches the channel at all: these must
# answer 400 with the §5.13 code even while no browser is connected.
PARAMETER_GATES = [
    ("GET", "/captureScene?type=rgb", None, "MISSING_PARAM"),
    ("GET", "/captureScene?id=2", None, "MISSING_PARAM"),
    ("GET", "/captureScene?id=2&type=depth", None, "INVALID_PARAM"),
    ("GET", "/captureScene?id=-2&type=rgb", None, "INVALID_PARAM"),
    ("GET", "/get2DAABB?id=3", None, "MISSING_PARAM"),
    ("GET", "/get2DAABB?camera_id=2", None, "MISSING_PARAM"),
    ("GET", "/get2DAABB?id=3&camera_id=x", None, "INVALID_PARAM"),
    ("POST", "/removeObject", None, "MISSING_PARAM"),
    ("POST", "/removeObject?id=3&detach_children=maybe", None, "INVALID_PARAM"),
    ("GET", "/allocateObjectID", None, "MISSING_PARAM"),
    # §4/§5/§9: every one of these refuses in front of the forward, so a malformed session
    # request never reaches the Agent service and never half-binds a browser.
    ("POST", "/sessions", {"camera_id": 5}, "MISSING_PARAM"),
    # Camera selection is optional: the Gateway chooses from the browser's candidate list (or
    # lets Brain report the scene/profile error).  Keep the parameter gate on a genuinely malformed
    # optional field instead of treating a valid camera-less session request as missing input.
    ("POST", "/sessions", {"browserID": "browser-1", "expectedScene": []}, "INVALID_PARAM"),
    ("POST", "/sessions", {"browserID": 7, "camera_id": 5}, "INVALID_PARAM"),
    ("POST", "/sessions", {"browserID": "browser-1", "camera_id": "5"}, "web_uint32_required"),
    ("POST", "/updateItem?id=3", {"object_id_in_scene": 4}, "BODY_ID_MISMATCH"),
    ("POST", "/updateWorld", "not json", "INVALID_WORLD_SCHEMA"),
    ("POST", "/updateWorld", [1, 2], "INVALID_WORLD_SCHEMA"),
]

TRAJ_MAGIC = b"TRAJ" + bytes(range(256))


async def audit():
    results = []

    def check(name, passed, actual):
        results.append({"check": name, "status": "PASS" if passed else "GAP", "actual": actual})

    logging.getLogger("aiohttp.server").setLevel(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="gateway-surface-") as directory:
        old_root = gateway.ROOT
        gateway.ROOT = Path(directory)
        shutil.copyfile(ROOT / "features.json", gateway.ROOT / "features.json")
        # §11.7: the catch-all now serves the built SPA, so stage a dist/ shell for the
        # "swallowed by the SPA route" reading below to mean anything.
        (gateway.ROOT / "dist").mkdir()
        shutil.copyfile(ROOT / "editor" / "index.html", gateway.ROOT / "dist" / "index.html")
        try:
            config = json.loads(json.dumps(gateway.DEFAULT_CONSTANT_CONFIG))
            # The parity harness must never create a durable session in a real Brain service merely
            # to test Gateway parameter gates.  The service itself is outside this probe's scope.
            config["agent_service_url"] = ""
            config["browser_request_timeout_seconds"] = 0.3
            config["capture_timeout_seconds"] = 0.3
            client = TestClient(TestServer(gateway.create_app(config)))
            await client.start_server()
            try:
                # --- §5.9 surface -------------------------------------------------------
                for method, path, body, status, code in CALLER_SURFACE:
                    if body is None:
                        response = await client.request(method, path)
                    elif body == "multipart":
                        response = await client.request(method, path,
                                                        data=traj_form((gateway.TRAJ_PART_FIELD, b"TRAJ-bytes")))
                    else:
                        response = await client.request(method, path, json=body)
                    text = await response.text()
                    if code is None:
                        check(f"surface {method} {path}", response.status == status
                              and "application/json" in response.headers.get("Content-Type", ""),
                              {"status": response.status, "body": text[:120]})
                        continue
                    try:
                        actual_code = json.loads(text)["error"]["code"]
                    except (ValueError, KeyError, TypeError):
                        actual_code = f"not JSON: {text[:60]}"
                    check(f"surface {method} {path}", response.status == status and actual_code == code,
                          {"status": response.status, "code": actual_code})

                # Agent routes stay as they were (§5.9 "保留"); the others must not answer.
                agent = await client.get("/api/agent/jobs")
                check("agent routes still registered", agent.status == 200, agent.status)

                # --- §10.1 deleted surface ----------------------------------------------
                for method, path in DELETED_SURFACE:
                    response = await client.request(method, path,
                                                   data=json.dumps({"anything": 1}).encode(),
                                                   headers={"Content-Type": "application/json"})
                    body = await response.read()
                    content_type = response.headers.get("Content-Type", "")
                    shell = "text/html" in content_type
                    json_api = "application/json" in content_type
                    check(f"deleted {method} {path}",
                          (response.status in (404, 405) or shell) and not json_api,
                          {"status": response.status, "contentType": content_type,
                           "starts": body[:24].decode("utf-8", "replace")})

                # --- parameter gates ----------------------------------------------------
                for method, path, body, code in PARAMETER_GATES:
                    if body is None:
                        response = await client.request(method, path)
                    elif isinstance(body, str):
                        # A str body is deliberately malformed JSON, so it must still
                        # arrive as application/json to reach the schema gate (§11.2).
                        response = await client.request(method, path, data=body.encode(),
                                                        headers={"Content-Type": "application/json"})
                    else:
                        response = await client.request(method, path, json=body)
                    text = await response.text()
                    try:
                        actual = json.loads(text)["error"]["code"]
                    except (ValueError, KeyError, TypeError):
                        actual = f"not JSON: {text[:60]}"
                    check(f"gate {method} {path}", response.status == 400 and actual == code,
                          {"status": response.status, "code": actual})

                # --- media type and size caps (§11.1/§11.2) -----------------------------
                wrong = await client.post("/updateTraj", data=TRAJ_MAGIC,
                                          headers={"Content-Type": "application/json"})
                check("updateTraj rejects a non-multipart body", wrong.status == 400, wrong.status)
                empty = await client.post("/updateTraj", data=b"--X\r\n--X--\r\n",
                                          headers={"Content-Type": "multipart/form-data; boundary=X"})
                check("updateTraj refuses a body with no readable parts",
                      empty.status == 400 and "INVALID_PARAM" in await empty.text(), empty.status)
                wrong_name = await client.post("/updateTraj",
                                                data=traj_form(("traj", TRAJ_MAGIC)))
                check("updateTraj rejects a part not named trajs",
                      wrong_name.status == 400 and "trajs" in await wrong_name.text(), wrong_name.status)
                plain = await client.post("/updateWorld", data=b"{\"objects\": []}",
                                          headers={"Content-Type": "text/plain"})
                check("updateWorld requires application/json", plain.status == 400, plain.status)
                big = json.loads(json.dumps(gateway.DEFAULT_CONSTANT_CONFIG))
                big["max_payload_bytes"] = 64
                limited = TestClient(TestServer(gateway.create_app(big)))
                await limited.start_server()
                try:
                    over = await limited.post("/updateWorld", json={"objects": ["x" * 200]})
                    check("payload cap answers 413", over.status == 413, over.status)
                finally:
                    await limited.close()

                # --- the channel itself -------------------------------------------------
                socket = await client.ws_connect("/events")
                quiet = await client.get("/getWorld")
                check("a socket without browser.hello is not a browser", quiet.status == 503, quiet.status)
                await socket.send_json({"type": "browser.hello", "browser_id": "probe", "role": "viewer"})
                # Registration is the gateway's own async work, so wait on what a caller can
                # observe rather than on its internals: an unregistered socket is no browser
                # (503), a registered but silent one is asked and times out (504).
                world = None
                for _ in range(200):
                    world = await client.get("/getWorld")
                    await world.read()
                    if world.status != 503:
                        break
                    await asyncio.sleep(0.005)
                check("a silent browser means 504, not a silent success",
                      world.status == 504 and (await world.json())["error"]["code"] == "BROWSER_TIMEOUT",
                      world.status)
                frame = await socket.receive_json()
                check("the browser is asked with the original envelope",
                      frame["type"] == "browser.request" and frame["action"] == "getWorld"
                      and frame["payload"] == {} and "expects_binary" not in frame,
                      sorted(frame))
                directed = await client.get("/getWorld?browser_id=nobody")
                check("a directed request to an absent browser is 503", directed.status == 503, directed.status)

                # §5.3 (Q99): two parts in, one frame out, lengths announced up front.
                second_clip = b"SECOND" + bytes(64)
                traj = await client.post("/updateTraj",
                                         data=traj_form((gateway.TRAJ_PART_FIELD, TRAJ_MAGIC),
                                                        (gateway.TRAJ_PART_FIELD, second_clip)),
                                         params={"count": "2"})
                check("updateTraj is relayed, not answered", traj.status == 504, traj.status)
                request_frame = await socket.receive_json()
                binary = await socket.receive()
                check("a batch rides as one binary frame after its request frame",
                      request_frame["action"] == "updateTraj" and request_frame["expects_binary"]
                      and request_frame["payload"] == {"count": 2,
                                                       "byte_lengths": [len(TRAJ_MAGIC), len(second_clip)],
                                                       "byte_length": len(TRAJ_MAGIC) + len(second_clip)}
                      and binary.type is WSMsgType.BINARY
                      and bytes(binary.data) == TRAJ_MAGIC + second_clip,
                      {"payload": request_frame.get("payload"), "frame": binary.type.name})

                # §5.8 then §1.4: the reported world is what /getWorld serves once the
                # browsers stop answering, and the header says which one it was.
                world_document = {"scene_schema_version": 1, "scene_version": 4, "scene_id": 11,
                                  "scene_name": "Probe", "objects": []}
                await socket.send_json({"type": "browser.state_sync", "payload": {"world": world_document}})
                await socket.close()
                cached = None
                for _ in range(200):
                    cached = await client.get("/getWorld")
                    await cached.read()
                    if cached.headers.get("X-World-Source") == "cache":
                        break
                    await asyncio.sleep(0.005)
                check("the cached world serves callers with no browser",
                      cached.status == 200 and cached.headers.get("X-World-Source") == "cache"
                      and await cached.json() == world_document,
                      {"status": cached.status, "source": cached.headers.get("X-World-Source")})

                # --- §9.1 runtime config ------------------------------------------------
                runtime = await (await client.get("/api/runtimeConfig")).json()
                check("runtimeConfig carries the whole §9.1 contract",
                      {"library_server_url", "camera_preview", "max_payload_bytes", "features"} <= set(runtime)
                      and runtime["features"],
                      {"keys": len(runtime), "features": len(runtime.get("features", {}))})
            finally:
                await client.close()
        finally:
            gateway.ROOT = old_root

    report = {"generatedAt": datetime.now(timezone.utc).isoformat(),
              "scope": "§5.9 caller surface, §10.1 deleted routes and the gateway's own "
                       "gating, probed without a browser answer; not a full browser "
                       "acceptance suite (see tools/browser_smoke.cjs for that)",
              "checks": results}
    output = ROOT / "docs" / "parity-probes.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    gaps = [item["check"] for item in results if item["status"] == "GAP"]
    print(json.dumps({"total": len(results), "passed": len(results) - len(gaps), "gaps": gaps}, indent=2))
    return bool(gaps)


if __name__ == "__main__":
    sys.exit(asyncio.run(audit()))
