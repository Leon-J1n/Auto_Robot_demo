#!/usr/bin/env python3
"""Caller-side demo for the standalone WebEditor gateway (spec v2 §5.9).

One runnable tour of **every** interface in the §5.9 caller list:

    GET  /api/health        GET  /api/runtimeConfig     GET  /events
    POST /updateWorld       GET  /getWorld              POST /addObject
    POST /updateItem        GET  /get2DAABB             GET  /captureScene
    POST /updateTraj        POST /removeObject

Prerequisites - the demo assumes both services are already up:

* the gateway listens on http://127.0.0.1:8099 (`python local_gateway.py`)
* ObjectLibraryServer serves http://127.0.0.1:8000 (`library_server_url` in
  `config/constant_config.json`). Assets are fetched by the browser, never here.
* **one open browser tab of the editor.** The browser is the only scene
  authority (§1.1); the gateway is a channel that relays each caller request to
  a connected viewer over `/events`. Without a viewer every caller route --
  `/getWorld` included -- answers `503 NO_BROWSER_CONNECTED`: `X-World-Source`
  has exactly one value, `live`, and there is no cached fallback (§5.1, §5.8,
  §5.9). Step 4 checks this and stops with instructions instead of a stack of
  503s.

Usage:

    python examples/gateway_client_demo.py
    python examples/gateway_client_demo.py --gateway http://127.0.0.1:8098
    python examples/gateway_client_demo.py --keep         # leave the sample world loaded
    python examples/gateway_client_demo.py --skip-events  # no aiohttp, no /events step

The ten HTTP interfaces need nothing but the standard library. `/events` is a
WebSocket, so that one step uses `aiohttp` - already a gateway dependency, listed
in `requirements.txt`; if it is missing the step is skipped with an install hint
and the rest of the tour still runs (a caller may ignore §5.4 broadcasts).

The tour shows both ways to make a robot move from outside the browser: repeated
`/updateItem` calls drive the arm's `shoulder_pan_joint` through a full turn at
1 rad/s - one write per animation tick, each one a world change and a broadcast -
and `/updateTraj` hands the viewer whole clips in a single request that changes
no world state (§5.3: one `trajs` part per clip).

The sample world is embedded below as `SAMPLE_SCENE` (a verbatim copy of OLS
scene 6, `ur5e_table_twist`), so this file can be handed to anyone without
shipping a fixture or reading a path. The two clips pushed through `/updateTraj`
are the one exception: they are read from `examples/`, next to this script,
because they are the published example files a caller would really have on disk
(**Q103**). Query parameters are `snake_case`, ids are
uint32, and every failure shares one envelope (§5.6):
`{"error": {"code", "message", "request_id"}}`.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import json
import math
import queue
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_GATEWAY = "http://127.0.0.1:8099"
TRAJ_MAGIC = 0x5452414A  # 'TRAJ' as a u32; the bytes on disk read "JART"
TRAJ_VERSION = 2
TRAJ_POSE_DIM = 7
TRAJ_WORLD_ROOT = 0xFFFFFFFF  # parent_id sentinel: hang the object on the scene root

# The two clips the `/updateTraj` step pushes as one batch (§13.30 Q103). They live next
# to this file and differ in joint order, angles and length, so the viewer's toolbar
# selector offers two rows a person can tell apart on screen.
EXAMPLE_TRAJS = ("scene3_wrist_first.traj", "scene3_alternate_joints.traj")

# ---------------------------------------------------------------------------
# The sample world (OLS scene 6, embedded on purpose - see the module docstring)
# ---------------------------------------------------------------------------

SAMPLE_SCENE: dict[str, Any] = {'scene_schema_version': 1,
 'scene_version': 3,
 'scene_id': 6,
 'scene_name': 'ur5e_table_twist',
 'objects': [{'object_id_in_scene': 0,
              'object_name_in_scene': 'AmbientLight',
              'components': [{'component_type': 'Transform',
                              'properties': {'position': [0, -1, 2],
                                             'quaternion_xyzw': [0, 0, 0, 1],
                                             'scale': [1, 1, 1],
                                             'parent': None}},
                             {'component_type': 'Light',
                              'properties': {'type': 'Ambient',
                                             'intensity': 0.5,
                                             'color': '#FFFFFF'}}]},
             {'object_id_in_scene': 7,
              'object_name_in_scene': 'TopDirectionalLight',
              'components': [{'component_type': 'Transform',
                              'properties': {'position': [0.525, 0, 3.0],
                                             'quaternion_xyzw': [0, 0, 0, 1],
                                             'scale': [1, 1, 1],
                                             'parent': None}},
                             {'component_type': 'Light',
                              'properties': {'type': 'Directional',
                                             'intensity': 0.7,
                                             'color': '#FFFFFF'}}]},
             {'object_id_in_scene': 1,
              'object_name_in_scene': 'WorkTable',
              'components': [{'component_type': 'Transform',
                              'properties': {'position': [0.525, 0, 0],
                                             'quaternion_xyzw': [0, 0, 0, 1],
                                             'scale': [1, 1, 1],
                                             'parent': None}},
                             {'component_type': 'MetadataRef',
                              'properties': {'path': '$LIBRARY_SERVER/29', 'type': 'model'}},
                             {'component_type': 'MeshRenderer', 'properties': {}},
                             {'component_type': 'Semantic',
                              'properties': {'semantic_name': 'work_table',
                                             'category': 'surface',
                                             'support': None}}]},
             {'object_id_in_scene': 2,
              'object_name_in_scene': 'UR5eArm',
              'components': [{'component_type': 'Transform',
                              'properties': {'position': [0, 0, 0.5],
                                             'quaternion_xyzw': [0, 0, 0, 1],
                                             'scale': [1, 1, 1],
                                             'parent': None}},
                             {'component_type': 'MetadataRef',
                              'properties': {'path': '$LIBRARY_SERVER/1', 'type': 'robot'}},
                             {'component_type': 'RobotDriver',
                              'properties': {'joint_sequence': ['shoulder_pan_joint',
                                                                'shoulder_lift_joint',
                                                                'elbow_joint',
                                                                'wrist_1_joint',
                                                                'wrist_2_joint',
                                                                'wrist_3_joint'],
                                             'joint_limits': {'shoulder_pan_joint': [-6.28319,
                                                                                     6.28319],
                                                              'shoulder_lift_joint': [-6.28319,
                                                                                      6.28319],
                                                              'elbow_joint': [-3.1415, 3.1415],
                                                              'wrist_1_joint': [-6.28319,
                                                                                6.28319],
                                                              'wrist_2_joint': [-6.28319,
                                                                                6.28319],
                                                              'wrist_3_joint': [-6.28319,
                                                                                6.28319]},
                                             'initial_joint_positions_rad': {'shoulder_pan_joint': 0.0,
                                                                             'shoulder_lift_joint': -2.094395,
                                                                             'elbow_joint': 1.570796,
                                                                             'wrist_1_joint': -1.5707963,
                                                                             'wrist_2_joint': -1.5707963,
                                                                             'wrist_3_joint': 0.0},
                                             'available_connect_joints': ['wrist_3_joint']}},
                             {'component_type': 'Semantic',
                              'properties': {'semantic_name': 'ur5e_arm',
                                             'category': 'robot',
                                             'support': None}}]},
             {'object_id_in_scene': 3,
              'object_name_in_scene': 'Robotiq2F85Gripper',
              'components': [{'component_type': 'Transform',
                              'properties': {'scale': [1, 1, 1], 'parent': 2}},
                             {'component_type': 'MetadataRef',
                              'properties': {'path': '$LIBRARY_SERVER/2', 'type': 'robot'}},
                             {'component_type': 'RobotDriver',
                              'properties': {'joint_sequence': ['robotiq_2f85_right_driver_joint'],
                                             'available_connect_joints': ['robotiq_2f85_base_joint']}},
                             {'component_type': 'RobotAttachment',
                              'properties': {'parent_object_id': 2,
                                             'parent_joint': 'wrist_3_joint',
                                             'parent_link': 'wrist_3_link',
                                             'child_link': 'robotiq_2f85_mount',
                                             'connection_type': 'fixed',
                                             'transform': {'position': [2.679489213841535e-09,
                                                                        0.0999999837652335,
                                                                        9.719897775539721e-09],
                                                           'quaternion_xyzw': [-0.7071067556514947,
                                                                               9.473426349705847e-09,
                                                                               -9.47342713345485e-09,
                                                                               0.7071068067215996],
                                                           'reference_frame': 'parent_link'}}},
                             {'component_type': 'Semantic',
                              'properties': {'semantic_name': 'robotiq_2f85',
                                             'category': 'end_effector',
                                             'support': None}}]},
             {'object_id_in_scene': 4,
              'object_name_in_scene': 'WristCameraD435i',
              'components': [{'component_type': 'Transform',
                              'properties': {'position': [0, -0.045, -0.013],
                                             'quaternion_xyzw': [0, 0, 1, 0],
                                             'scale': [1, 1, 1],
                                             'parent': 3}},
                             {'component_type': 'MetadataRef',
                              'properties': {'path': '$LIBRARY_SERVER/3', 'type': 'robot'}},
                             {'component_type': 'MeshRenderer', 'properties': {}},
                             {'component_type': 'Camera',
                              'properties': {'type': 'rgbd',
                                             'fov': 60,
                                             'near': 0.01,
                                             'far': 10,
                                             'resolution': [640, 480]}},
                             {'component_type': 'Semantic',
                              'properties': {'semantic_name': 'wrist_camera_d435i',
                                             'category': 'sensor',
                                             'support': None}}]},
             {'object_id_in_scene': 5,
              'object_name_in_scene': 'OverheadCameraD435i',
              'components': [{'component_type': 'Transform',
                              'properties': {'position': [0.65, 0, 2.1],
                                             'quaternion_xyzw': [0.7071067811865476,
                                                                 0.7071067811865476,
                                                                 0.0,
                                                                 0.0],
                                             'scale': [1, 1, 1],
                                             'parent': None}},
                             {'component_type': 'MetadataRef',
                              'properties': {'path': '$LIBRARY_SERVER/3', 'type': 'robot'}},
                             {'component_type': 'MeshRenderer', 'properties': {}},
                             {'component_type': 'Camera',
                              'properties': {'type': 'rgbd',
                                             'fov': 60,
                                             'near': 0.01,
                                             'far': 10,
                                             'resolution': [640, 480]}},
                             {'component_type': 'Semantic',
                              'properties': {'semantic_name': 'overhead_camera_d435i',
                                             'category': 'sensor',
                                             'support': None}}]},
             {'object_id_in_scene': 6,
              'object_name_in_scene': 'RobotPedestal',
              'components': [{'component_type': 'Transform',
                              'properties': {'position': [0, 0, 0.25],
                                             'quaternion_xyzw': [0, 0, 0, 1],
                                             'scale': [1, 1, 1],
                                             'parent': None}},
                             {'component_type': 'MetadataRef',
                              'properties': {'path': '$LIBRARY_SERVER/4', 'type': 'robot'}},
                             {'component_type': 'MeshRenderer', 'properties': {}},
                             {'component_type': 'Semantic',
                              'properties': {'semantic_name': 'robot_pedestal',
                                             'category': 'support',
                                             'support': None}}]},
             {'object_id_in_scene': 8,
              'object_name_in_scene': 'Button',
              'components': [{'component_type': 'Transform',
                              'properties': {'position': [0.38, -0.3, 0.6],
                                             'quaternion_xyzw': [0, 0, 0.0, 1.0],
                                             'scale': [1, 1, 1],
                                             'parent': None}},
                             {'component_type': 'MetadataRef',
                              'properties': {'path': '$LIBRARY_SERVER/33', 'type': 'model'}},
                             {'component_type': 'MeshRenderer', 'properties': {}},
                             {'component_type': 'Semantic',
                              'properties': {'semantic_name': 'button',
                                             'category': 'articulated_object',
                                             'support': 1}}]},
             {'object_id_in_scene': 9,
              'object_name_in_scene': 'Screwdriver',
              'components': [{'component_type': 'Transform',
                              'properties': {'position': [0.6601158810000001,
                                                          -0.3016581485,
                                                          0.6173207079999999],
                                             'quaternion_xyzw': [0, 0, 0.0, 1.0],
                                             'scale': [1, 1, 1],
                                             'parent': None}},
                             {'component_type': 'MetadataRef',
                              'properties': {'path': '$LIBRARY_SERVER/13', 'type': 'model'}},
                             {'component_type': 'MeshRenderer', 'properties': {}},
                             {'component_type': 'Semantic',
                              'properties': {'semantic_name': 'screwdriver',
                                             'category': 'graspable',
                                             'support': 1}}]},
             {'object_id_in_scene': 10,
              'object_name_in_scene': 'Bottle',
              'components': [{'component_type': 'Transform',
                              'properties': {'position': [0.40113129999999997,
                                                          0.20105082500000002,
                                                          0.6],
                                             'quaternion_xyzw': [0, 0, 0.0, 1.0],
                                             'scale': [1, 1, 1],
                                             'parent': None}},
                             {'component_type': 'MetadataRef',
                              'properties': {'path': '$LIBRARY_SERVER/32', 'type': 'model'}},
                             {'component_type': 'MeshRenderer', 'properties': {}},
                             {'component_type': 'Semantic',
                              'properties': {'semantic_name': 'bottle',
                                             'category': 'articulated_object',
                                             'support': 1}}]},
             {'object_id_in_scene': 11,
              'object_name_in_scene': 'Mug',
              'components': [{'component_type': 'Transform',
                              'properties': {'position': [0.659908,
                                                          0.23114200000000001,
                                                          0.600028],
                                             'quaternion_xyzw': [0, 0, 0.0, 1.0],
                                             'scale': [1, 1, 1],
                                             'parent': None}},
                             {'component_type': 'MetadataRef',
                              'properties': {'path': '$LIBRARY_SERVER/14', 'type': 'model'}},
                             {'component_type': 'MeshRenderer', 'properties': {}},
                             {'component_type': 'Semantic',
                              'properties': {'semantic_name': 'mug',
                                             'category': 'graspable',
                                             'support': 1}}]}]}

AMBIENT_LIGHT_ID = 0
WORK_TABLE_ID = 1
ARM_ID = 2
GRIPPER_ID = 3
WRIST_CAMERA_ID = 4
OVERHEAD_CAMERA_ID = 5
MUG_ID = 11

# The joint the animation step drives: the arm's base yaw, declared first in
# `joint_sequence` and limited to +-6.28319 rad by the scene, so a full turn fits.
SPIN_JOINT = "shoulder_pan_joint"
SPIN_SPEED_RAD_PER_S = 1.0
SPIN_TICK_HZ = 30.0


def scene_object(object_id: int) -> dict[str, Any]:
    """One embedded object description, straight out of `SAMPLE_SCENE`."""
    for item in SAMPLE_SCENE["objects"]:
        if item["object_id_in_scene"] == object_id:
            return item
    raise KeyError(f"object {object_id} is not in the sample scene")


def scene_properties(object_id: int, component_type: str) -> dict[str, Any]:
    for component in scene_object(object_id)["components"]:
        if component["component_type"] == component_type:
            return component["properties"]
    raise KeyError(f"object {object_id} has no {component_type}")


def pick_object(world: dict[str, Any], object_id: int) -> dict[str, Any]:
    """One object as the viewer holds it right now.

    A `/updateItem` body replaces the object whole (§4.2), so it has to be built
    from the live document rather than from a copy the tour read earlier - otherwise
    the write silently reverts whatever another caller put into that object since.
    """
    for item in world.get("objects") or []:
        if item.get("object_id_in_scene") == object_id:
            return item
    raise RuntimeError(f"object {object_id} is not in the live world")


def driver_of(object: dict[str, Any]) -> dict[str, Any] | None:
    """The `RobotDriver` properties of an object description, if it has one."""
    for component in object.get("components") or []:
        if component.get("component_type") == "RobotDriver":
            return component.get("properties")
    return None


# ---------------------------------------------------------------------------
# HTTP transport: urllib only, so the demo installs nowhere
# ---------------------------------------------------------------------------

class GatewayError(RuntimeError):
    """One §5.6 error envelope, kept whole: status + code + message + request_id."""

    def __init__(self, status: int, code: str, message: str, request_id: Any) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id


def brief(value: Any, limit: int = 170) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else f"{text[:limit]} ... ({len(text)} chars)"


def show(label: str, value: Any) -> None:
    print(f"    {label}: {brief(value)}")


# §5.3 (Q99): every trajectory in a batch rides in a part with this field name.
TRAJ_PART_FIELD = "trajs"


def encode_multipart(field: str, parts: list[bytes]) -> tuple[str, bytes]:
    """Build the `/updateTraj` body by hand, because this sample is stdlib-only.

    With `requests` the same call is `files=[(field, clip) for clip in clips]`;
    what matters is only the field name and the order of the parts, which is the
    order the viewer lists the clips in (§7.1).
    """
    boundary = f"web-frontend-{uuid.uuid4().hex}"
    body = bytearray()
    for part in parts:
        body += f"--{boundary}\r\n".encode("ascii")
        body += f'Content-Disposition: form-data; name="{field}"\r\n'.encode("ascii")
        body += b"Content-Type: application/octet-stream\r\n\r\n"
        body += part + b"\r\n"
    body += f"--{boundary}--\r\n".encode("ascii")
    return f"multipart/form-data; boundary={boundary}", bytes(body)


class GatewayClient:
    """Minimal caller for the §5.9 routes: JSON in, JSON out, failures typed."""

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def call(self, method: str, path: str, *, params: dict[str, Any] | None = None,
             json_body: Any = None, trajs: list[bytes] | None = None) -> tuple[Any, Any]:
        """One request. Returns `(payload, response_headers)`, raises GatewayError on §5.13."""
        query = ""
        if params:
            # Optional parameters a caller may leave out (§5.4 browser_id, §5.7 expected_version).
            kept = {key: value for key, value in params.items() if value is not None}
            query = f"?{urllib.parse.urlencode(kept)}" if kept else ""
        data: bytes | None = None
        headers = {"Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif trajs is not None:
            # §5.3: TRAJ travels as raw bytes, never base64, one part per clip.
            headers["Content-Type"], data = encode_multipart(TRAJ_PART_FIELD, trajs)
        request = urllib.request.Request(f"{self.base_url}{path}{query}",
                                         data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return read_json(response.read()), response.headers
        except urllib.error.HTTPError as error:
            raise as_gateway_error(error) from error
        except urllib.error.URLError as error:
            raise RuntimeError(
                f"cannot reach the gateway at {self.base_url} ({error.reason}). "
                "Start it with: python local_gateway.py") from error

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        payload, _ = self.call("GET", path, params=params)
        return payload

    def get_raw(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, Any]:
        """GET when the caller also needs the response headers (§5.9 X-World-Source)."""
        return self.call("GET", path, params=params)

    def post(self, path: str, json_body: Any = None, params: dict[str, Any] | None = None) -> Any:
        payload, _ = self.call("POST", path, params=params, json_body=json_body)
        return payload

    def post_trajs(self, path: str, clips: list[bytes], params: dict[str, Any] | None = None) -> Any:
        """§5.3 (Q99): N clips in one request, as N `trajs` parts."""
        payload, _ = self.call("POST", path, params=params, trajs=clips)
        return payload


def read_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError as error:
        raise RuntimeError(f"the gateway answered non-JSON: {raw[:120]!r}") from error


def as_gateway_error(error: urllib.error.HTTPError) -> GatewayError:
    raw = error.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        return GatewayError(error.code, f"HTTP_{error.code}",
                            raw.decode("utf-8", "replace")[:200] or error.reason, None)
    envelope = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(envelope, dict):
        return GatewayError(error.code, "UNDOCUMENTED_BODY", brief(payload), None)
    return GatewayError(error.code, str(envelope.get("code", "UNKNOWN")),
                        str(envelope.get("message", "")), envelope.get("request_id"))


# ---------------------------------------------------------------------------
# /events as a caller: listen, never answer
# ---------------------------------------------------------------------------

class EventsListener:
    """§5.4: after a write the gateway broadcasts `scene.applied` on /events.

    A caller only ever reads this socket. The gateway promotes a socket to a
    browser only when it receives `browser.hello` (§5.1), and this client never
    sends one, on purpose: a demo that did would register itself as a viewer,
    take `browser.request` traffic away from the real tab, and then sit on it -
    which is exactly the `504 BROWSER_TIMEOUT` a caller must not cause.
    """

    def __init__(self, base_url: str) -> None:
        self.ws_url = base_url.replace("http", "ws", 1) + "/events"
        self.frames: queue.Queue = queue.Queue()
        self.applied = 0
        self.error: str | None = None
        self.ready = threading.Event()
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._socket: Any = None

    def start(self) -> bool:
        try:
            import aiohttp  # noqa: F401  (the dependency the gateway itself runs on)
        except ImportError:
            self.error = ("aiohttp is not installed, so /events is skipped. "
                          "Install it with: python -m pip install -r requirements.txt")
            return False
        thread = threading.Thread(target=self._run, name="gateway-events", daemon=True)
        thread.start()
        if not self.ready.wait(timeout=5.0):
            self.error = "the /events listener did not connect within 5s"
            return False
        return self.error is None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._consume())
        finally:
            loop.close()

    async def _consume(self) -> None:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                # max_msg_size=0 mirrors the gateway: a 100 MB frame must not be dropped (§5.3).
                async with session.ws_connect(self.ws_url, heartbeat=30, max_msg_size=0) as socket:
                    self._socket = socket
                    self.ready.set()
                    async for message in socket:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue  # binary frames travel gateway -> browser only (§5.3)
                        frame = read_json(message.data) if message.data else None
                        if isinstance(frame, dict):
                            self.frames.put(frame)
                        if self._stop.is_set():
                            break
        except Exception as error:  # a listener failure must never fail the tour
            self.error = f"{type(error).__name__}: {error}"
        finally:
            self.ready.set()

    def drain(self, *, timeout: float = 0.8) -> list[dict[str, Any]]:
        """Frames that arrived since the last drain, waiting briefly for the first."""
        collected: list[dict[str, Any]] = []
        try:
            collected.append(self.frames.get(timeout=timeout))
        except queue.Empty:
            return collected
        while True:
            try:
                collected.append(self.frames.get_nowait())
            except queue.Empty:
                return collected

    def stop(self) -> None:
        self._stop.set()
        loop, socket = self._loop, self._socket
        if loop is None or socket is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(socket.close(), loop).result(timeout=5)
        except Exception:
            pass  # the socket dies with the daemon thread; nothing here is worth failing over


def report_events(listener: EventsListener | None, label: str) -> None:
    """Print the broadcasts the previous call produced - the §5.4 fan-out."""
    if listener is None:
        print("    /events: skipped")
        return
    frames = listener.drain()
    if not frames:
        print(f"    /events: no frame ({label})")
        return
    for frame in frames:
        kind = frame.get("type")
        if kind == "scene.applied":
            listener.applied += 1
            print(f"    scene.applied  version={frame.get('scene_version')} "
                  f"action={frame.get('action')} source_browser_id={frame.get('source_browser_id')}")
        else:
            print(f"    {kind}: {brief(frame, 90)}")


# ---------------------------------------------------------------------------
# A TRAJ v2 document, packed to the layout in docs/轨迹文件布局描述.md
# ---------------------------------------------------------------------------

def smoothstep(index: int, count: int) -> float:
    """The easing the generator script uses: s^2 * (3 - 2s)."""
    if count <= 1:
        return 1.0
    ratio = index / (count - 1)
    return ratio * ratio * (3.0 - 2.0 * ratio)


def arm_frame(pan_rad: float, gripper: float) -> list[float]:
    """An arm frame = joint angles in `joint_sequence` order, then the gripper value.

    The trailing gripper slot is only *recorded* by the viewer (O1 in the spec:
    no world file names the gripper joint, so the browser will not guess which
    geometry to fold). The six joint values are what moves the arm.
    """
    driver = scene_properties(ARM_ID, "RobotDriver")
    sequence = driver["joint_sequence"]
    if len(sequence) > TRAJ_POSE_DIM - 1:
        raise ValueError(f"{len(sequence)} joints do not fit one {TRAJ_POSE_DIM}-value frame")
    initial = driver["initial_joint_positions_rad"]
    values = [float(initial.get(name, 0.0)) for name in sequence]
    values += [0.0] * (TRAJ_POSE_DIM - 1 - len(values))
    values[0] = pan_rad  # shoulder_pan_joint is index 0 of the UR5e sequence
    return values + [gripper]


def object_frame(object_id: int) -> list[float]:
    """A static object frame = local position (m) + quaternion xyzw, read from the world."""
    transform = scene_properties(object_id, "Transform")
    return [float(v) for v in transform["position"]] + [float(v) for v in transform["quaternion_xyzw"]]


def build_sample_traj() -> bytes:
    """A 3 s, 90-frame trajectory over the sample scene (§5.3, §5.12).

    Three objects are declared: the arm (a real sweep), the mug (no segment, so
    it holds its default pose), and id 99, which is **not in the world** - that
    is what makes `ignored_objects` in the answer non-empty. The attach table
    hands the mug to the arm at frame 50 and back to the table at frame 80.
    """
    fps, total_frames = 30, 90
    # (start_frame, frame_count, step, pan from, pan to, gripper from, gripper to)
    arm_segments = [
        (0, 40, 1, 0.0, 0.6, 0.0, 0.7),
        (40, total_frames - 40, 2, 0.6, 0.0, 0.7, 0.7),
    ]
    attaches = [(50, MUG_ID, ARM_ID), (80, MUG_ID, WORK_TABLE_ID)]
    records = [
        (ARM_ID, arm_frame(0.0, 0.0), arm_segments),
        (MUG_ID, object_frame(MUG_ID), []),
        (99, [0.4, -0.1, 0.7, 0.0, 0.0, 0.0, 1.0], []),  # DEMO ONLY: absent on purpose
    ]
    index_bytes = sum(12 + TRAJ_POSE_DIM * 4 + 16 * len(segments) for _, _, segments in records)
    attach_offset = 32 + index_bytes
    data_offset = attach_offset + 16 * len(attaches)

    index = bytearray()
    data = bytearray()
    for object_id, default_pose, segments in records:
        index += struct.pack("<IIHH", object_id, TRAJ_WORLD_ROOT, TRAJ_POSE_DIM, len(segments))
        index += struct.pack(f"<{TRAJ_POSE_DIM}f", *default_pose)
        for start, count, step, pan_from, pan_to, grip_from, grip_to in segments:
            values: list[float] = []
            for frame in range(count):
                ratio = smoothstep(frame, count)
                values += arm_frame(pan_from + (pan_to - pan_from) * ratio,
                                    grip_from + (grip_to - grip_from) * ratio)
            index += struct.pack("<IIII", start, count, data_offset + len(data), step)
            data += struct.pack(f"<{len(values)}f", *values)

    header = struct.pack("<8I", TRAJ_MAGIC, TRAJ_VERSION, fps, total_frames,
                         len(records), len(attaches), 32, attach_offset)
    attach = b"".join(struct.pack("<IIII", frame, child, parent, 0)
                      for frame, child, parent in attaches)
    payload = bytes(header) + bytes(index) + attach + bytes(data)
    if len(payload) != data_offset + len(data):
        raise AssertionError("the announced offsets must match the bytes on disk")
    return payload


def read_example_trajs() -> list[bytes]:
    """The two clips in `examples/`, found next to this script however it is invoked.

    Regenerate them with `python -X utf8 examples/make_scene3_variants.py`. Unlike the
    world above, these are not embedded: a caller that has files on disk pushes the
    bytes it has, and re-packing two nine-segment UR5e clips here would only duplicate
    that script.
    """
    directory = Path(__file__).resolve().parent
    return [(directory / name).read_bytes() for name in EXAMPLE_TRAJS]


# ---------------------------------------------------------------------------
# The tour
# ---------------------------------------------------------------------------

class Steps:
    def __init__(self, total: int) -> None:
        self.total = total
        self.number = 0

    def __call__(self, title: str) -> None:
        self.number += 1
        print(f"\n=== step {self.number}/{self.total}: {title}")


def object_for_add(position: list[float]) -> dict[str, Any]:
    """A `/addObject` body **without** `object_id_in_scene`: the browser allocates it (§5.5)."""
    return {
        "object_name_in_scene": "DemoApple",
        "components": [
            {"component_type": "Transform", "properties": {
                "position": position,
                "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0],
                "scale": [1.0, 1.0, 1.0],
                "parent": None}},
            # $LIBRARY_SERVER is the template the browser expands with the
            # library_server_url it read from /api/runtimeConfig (OLS model 1 = apple).
            {"component_type": "MetadataRef",
             "properties": {"path": "$LIBRARY_SERVER/1", "type": "model"}},
            {"component_type": "MeshRenderer", "properties": {}},
            {"component_type": "Semantic", "properties": {
                "semantic_name": "demo_apple", "category": "graspable", "support": WORK_TABLE_ID}},
        ],
    }


def object_for_update(object_id: int, position: list[float]) -> dict[str, Any]:
    """`/updateItem` replaces one object whole, and its id must equal the query id (§4.2)."""
    description = object_for_add(position)
    description["object_id_in_scene"] = object_id
    description["object_name_in_scene"] = "DemoAppleMoved"
    for component in description["components"]:
        if component["component_type"] == "Transform":
            # 30 deg about Z (qz = sin 15 deg, qw = cos 15 deg) and 1.5x scale.
            component["properties"]["quaternion_xyzw"] = [0.0, 0.0, 0.2588190451, 0.9659258263]
            component["properties"]["scale"] = [1.5, 1.5, 1.5]
    return description


def object_for_joint(object: dict[str, Any], joint: str, angle: float) -> dict[str, Any]:
    """The live object back to `/updateItem`, with one joint angle merged in.

    Only `RobotDriver.now_joint_positions_rad` changes, and only for the joint named
    here: the other joints keep whatever the viewer derives from
    `initial_joint_positions_rad` (§13.17 Q75), so after a whole sweep the dictionary
    still holds exactly one key.
    """
    driven = copy.deepcopy(object)
    for component in driven.get("components") or []:
        if component.get("component_type") == "RobotDriver":
            properties = component["properties"]
            merged = dict(properties.get("now_joint_positions_rad") or {})
            merged[joint] = angle
            properties["now_joint_positions_rad"] = merged
    return driven


def get_aabb(client: GatewayClient, object_id: int, camera_id: int,
             *, attempts: int = 10, delay: float = 0.5) -> dict[str, Any]:
    """`/get2DAABB` for an object this tour just added.

    A write answers `ok:true` the moment the object enters the world, while the
    asset behind its `MetadataRef` is still being fetched and built, so the first
    calls can legitimately come back 400 EMPTY_AABB (there is no measurable
    geometry yet). Retrying briefly is what a real caller has to do after adding
    an object; a permanent EMPTY_AABB then means the id really has no geometry -
    which is the case for a camera or a light, whose markers are edit layer (§5.11).
    """
    last: GatewayError | None = None
    for attempt in range(attempts):
        try:
            if attempt:
                print(f"    retry {attempt}/{attempts - 1}: the viewer is still building the mesh")
            return client.get("/get2DAABB", params={"id": object_id, "camera_id": camera_id})
        except GatewayError as error:
            if error.code != "EMPTY_AABB":
                raise
            last = error
            time.sleep(delay)
    assert last is not None
    raise last


def box_shift(first: Any, second: Any) -> float:
    """The largest corner difference between two `aabb_world` values, in metres.

    §5.11 reports that box as `{"min": [x, y, z], "max": [x, y, z]}` in the world
    file's own axis order, so both corners have to be compared - one of them is
    enough to show a rotation happened, and it is the pair together that shows the
    box came back.
    """
    return max(abs(one - two)
               for corner in ("min", "max")
               for one, two in zip(first[corner], second[corner]))


def sweep_joint(client: GatewayClient, arm: dict[str, Any], joint: str, *, start: float,
                end: float, version: int, speed: float = SPIN_SPEED_RAD_PER_S,
                hz: float = SPIN_TICK_HZ) -> tuple[int, int]:
    """Call `/updateItem` once per tick until `joint` reaches `end`; return (writes, version).

    The angle comes from the wall clock, never from a tick counter. Each write is
    relayed to the browser and answered synchronously, so its cost varies with
    rendering and asset work; counting ticks would make "1 rad/s" whatever the
    latency happened to be that second. `hz` only decides how smooth the motion
    looks - a late tick loses a frame, not the speed.

    This is the caller-side alternative to `/updateTraj`: no TRAJ file, no playback
    state, and every tick is an ordinary world change, so each one bumps
    `scene_version` and broadcasts `scene.applied` (§5.4/§5.7). One turn measured
    between 28 and 66 writes across runs on this machine - the tick asks for 10 Hz but
    a relay round trip costs 100 to 250 ms depending on what the tab is rendering, and
    a missed tick is dropped rather than accumulated, because the angle reads the
    clock. Right for a demo or a live controller feed, wrong for shipping a pre-baked
    motion, which is what `/updateTraj` is for.
    """
    interval = 1.0 / hz
    direction = 1.0 if end >= start else -1.0
    travel = abs(end - start)
    started = time.monotonic()
    frames = 0
    while True:
        angle = start + direction * min(travel, speed * (time.monotonic() - started))
        try:
            answer = client.post("/updateItem", object_for_joint(arm, joint, angle),
                                 params={"id": arm["object_id_in_scene"], "expected_version": version})
        except GatewayError as error:
            if error.code != "VERSION_CONFLICT":
                raise
            print(f"    !! another caller wrote during the sweep, so expected_version={version} is stale: "
                  f"{error.message}")
            print("       an animation does not fight a concurrent writer over the version; it stops here.")
            return frames, version
        version = answer.get("scene_version", version)
        frames += 1
        if frames % 10 == 1:
            print(f"    write {frames:>3}: {joint} = {angle:+.3f} rad "
                  f"({direction * (angle - start):.3f} of {travel:.3f} rad travelled)")
        if abs(angle - end) < 1e-12:
            return frames, version
        time.sleep(max(0.0, frames * interval - (time.monotonic() - started)))


def spin_shoulder_pan(client: GatewayClient, listener: EventsListener | None,
                      version: int | None) -> int:
    """Drive the arm's shoulder pan through one full turn at 1 rad/s (§13.20 Q85)."""
    world = client.get("/getWorld")
    version = int(world.get("scene_version", version or 0))
    arm = pick_object(world, ARM_ID)
    driver = driver_of(arm)
    if driver is None or SPIN_JOINT not in (driver.get("joint_sequence") or []):
        raise RuntimeError(f"object {ARM_ID} does not declare {SPIN_JOINT}, so there is nothing to drive")
    limits = (driver.get("joint_limits") or {}).get(SPIN_JOINT) or []
    turn = math.tau
    # Sweep from the declared home, not from whatever `now` the world happens to
    # hold: a caller that turns one full turn from the current angle would need
    # range headroom in only one direction, and running this demo twice in a row
    # would then fail ("already at 2*pi, cannot add another one").
    home_angle = float((driver.get("initial_joint_positions_rad") or {}).get(SPIN_JOINT, 0.0))
    held = driver.get("now_joint_positions_rad") or {}
    # The viewer clamps to this range (drivers.ts:clampToLimit), so a scene whose
    # shoulder is narrower than 2*pi would quietly turn "one full turn" into a swing.
    if len(limits) != 2 or not (limits[0] <= home_angle and home_angle + turn <= limits[1]):
        raise RuntimeError(
            f"{SPIN_JOINT} cannot make a full turn here: home at {home_angle:.4f} with declared range "
            f"{limits}, which leaves less than {turn:.4f} rad ahead of it")

    print(f"    object {ARM_ID} = {arm.get('object_name_in_scene')}: {SPIN_JOINT} home at "
          f"{home_angle:+.4f} rad (the world holds {json.dumps(held)}), range "
          f"[{limits[0]:+.5f}, {limits[1]:+.5f}]")
    print(f"    one turn = {turn:.4f} rad at {SPIN_SPEED_RAD_PER_S} rad/s = "
          f"{turn / SPIN_SPEED_RAD_PER_S:.2f} s, one /updateItem per {1000.0 / SPIN_TICK_HZ:.0f} ms")

    before = get_aabb(client, ARM_ID, OVERHEAD_CAMERA_ID).get("aabb_world")
    to_half, version = sweep_joint(client, arm, SPIN_JOINT, start=home_angle,
                                   end=home_angle + math.pi, version=version)
    half = get_aabb(client, ARM_ID, OVERHEAD_CAMERA_ID).get("aabb_world")
    to_full, version = sweep_joint(client, arm, SPIN_JOINT, start=home_angle + math.pi,
                                   end=home_angle + turn, version=version)
    full = get_aabb(client, ARM_ID, OVERHEAD_CAMERA_ID).get("aabb_world")

    driven = driver_of(pick_object(client.get("/getWorld"), ARM_ID)) or {}
    print(f"    -> {to_half + to_full} writes put now_joint_positions_rad = "
          f"{json.dumps(driven.get('now_joint_positions_rad'))} in the world, scene_version={version}")
    print("       one key only: the other five joints keep their initial value (§13.17 Q75).")

    # Numbers changing is not the same as geometry changing (that mistake cost a whole
    # round on the gripper), so the arm is measured in metres: half a turn has to look
    # different from a full one, and a full turn has to land back on home. Both halves
    # of that round trip are sampled *after* the sweep on purpose - the box taken before
    # it is often only the base column, because a 200 from /get2DAABB can still be a
    # half-attached URDF (the EMPTY_AABB retries in step 9 are the loud part of the same
    # window). Waiting for such a sample to stop changing does not fix it either: on a
    # real run it paused at 0.16 m and then grew to 0.85 m.
    parked = client.post("/updateItem", object_for_joint(arm, SPIN_JOINT, home_angle),
                         params={"id": arm["object_id_in_scene"], "expected_version": version})
    version = parked.get("scene_version", version)
    again = get_aabb(client, ARM_ID, OVERHEAD_CAMERA_ID).get("aabb_world")

    print(f"    world box: |full - home again| = {box_shift(full, again) * 1000.0:.1f} mm, "
          f"|half - full| = {box_shift(half, full):.3f} m")
    show("aabb_world before the sweep (assets may still be attaching)", before)
    show("aabb_world at half a turn", half)
    show("aabb_world at a full turn", full)
    show("aabb_world parked back on home", again)
    print("    -> 2*pi is coming back around, so the full-turn box and the parked box have to agree; the")
    print("       half-turn box has to differ by decimetres or the arm only moved on paper.")
    print(f"    -> one more write parks {SPIN_JOINT} back on {home_angle:+.4f} rad. The next run would")
    print("       start from home either way, because the sweep reads its start from the declared")
    print("       initial_joint_positions_rad rather than from the angle the world happens to hold.")
    report_events(listener, "after the shoulder sweep")
    return version


def tour(client: GatewayClient, options: argparse.Namespace) -> int:
    steps = Steps(15)
    listener: EventsListener | None = None
    # The world the browser held before anything was written, so the tour can put
    # it back. Stale (cache) worlds are not worth restoring.
    baseline: dict[str, Any] | None = None
    version: int | None = None

    steps("GET /api/health - the one route that needs no browser")
    show("answer", client.get("/api/health"))
    print("    -> {status, mode, uptimeSeconds} + the three sub-items agent_service / browser / assets (§5.9).")
    print("       Use it to tell 'gateway down' from 'no viewer open', and 'Agent down' from 'Agent degraded'.")

    steps("GET /api/runtimeConfig - deployment constants + feature flags (§9.2)")
    config = client.get("/api/runtimeConfig")
    show("gateway", config.get("gateway"))
    show("library_server_url", config.get("library_server_url"))
    show("max_payload_bytes", config.get("max_payload_bytes"))
    show("browser_request_timeout_seconds", config.get("browser_request_timeout_seconds"))
    show("capture_timeout_seconds", config.get("capture_timeout_seconds"))
    features = config.get("features") or {}
    show(f"features ({len(features)} keys, first four)", dict(list(features.items())[:4]))
    print("    -> a caller checks max_payload_bytes before posting a body, and library_server_url")
    print("       to know which asset ids the viewer can resolve. The §5.9 timeouts also tell the")
    print("       caller how long a synchronous render may take before the gateway gives up (504).")

    steps("GET /events - the WebSocket the gateway relays over (§5.1/§5.4)")
    if options.skip_events:
        print("    skipped by --skip-events")
    else:
        candidate = EventsListener(client.base_url)
        if candidate.start():
            listener = candidate
            print(f"    listening on {candidate.ws_url} (read-only: never sends browser.hello)")
        else:
            print(f"    unavailable: {candidate.error}")

    steps("GET /getWorld - preflight: is a viewer really connected, and who is it?")
    try:
        baseline, headers = client.get_raw("/getWorld")
    except GatewayError as error:
        if error.code != "NO_BROWSER_CONNECTED":
            raise
        raise NoViewerConnected(error) from error
    world_source = str(headers.get("X-World-Source", "?"))
    objects = (baseline.get("objects") or []) if isinstance(baseline, dict) else []
    print(f"    X-World-Source: {world_source} | {len(objects)} objects | "
          f"scene_id={baseline.get('scene_id')} scene_version={baseline.get('scene_version')}")
    print("    -> live = a browser answered. There is no other value: the cached fallback left")
    print("       the route with 流程文档 §5.1 (2026-11-05), so no browser means 503, never a")
    print("       stale world. ?browser_id=<id> pins the read to the viewer that just applied a")
    print("       write, so a second viewer cannot serve a different scene.")
    if world_source != "live":
        baseline = None
        print("    -> not a live reading: the writes below still need a live viewer, and this world")
        print("       will not be restored at the end.")

    steps("POST /updateWorld - hand the sample scene to the viewer (§4.2)")
    result = client.post("/updateWorld", SAMPLE_SCENE)
    version = result.get("scene_version")
    show("answer", result)
    print(f"    -> {result.get('object_count')} objects accepted, "
          f"{len(result.get('rejected_objects') or [])} rejected, scene_version={version}.")
    report_events(listener, "after /updateWorld")

    steps("GET /getWorld - read it back and collect the ids this demo needs")
    world, headers = client.get_raw("/getWorld")
    ids = {item["object_id_in_scene"]: item.get("object_name_in_scene") for item in world["objects"]}
    print(f"    X-World-Source: {headers.get('X-World-Source')} | "
          f"scene_version={world.get('scene_version')}")
    print("    objects: " + ", ".join(f"{key}={value}" for key, value in sorted(ids.items())))
    for needed in (ARM_ID, GRIPPER_ID, WRIST_CAMERA_ID, OVERHEAD_CAMERA_ID, MUG_ID):
        if needed not in ids:
            raise RuntimeError(f"the sample scene lost object {needed}; the viewer applied another world")
    version = world.get("scene_version", version)

    steps("POST /addObject - omit the id and let the viewer allocate one (§5.5)")
    added = client.post("/addObject", object_for_add([0.65, 0.1, 0.62]),
                        params={"expected_version": version})
    demo_id = added.get("object_id_in_scene")
    version = added.get("scene_version", version)
    show("answer", added)
    print(f"    -> new id {demo_id}, scene_version={version}. Declare an id you already use and")
    print("       the answer is 409 ID_ALREADY_EXISTS; omit a Transform and it is 400 MISSING_TRANSFORM_COMPONENT.")
    report_events(listener, "after /addObject")

    steps(f"POST /updateItem?id={demo_id} - full replacement (§4.2)")
    updated = client.post("/updateItem", object_for_update(demo_id, [0.55, -0.12, 0.72]),
                          params={"id": demo_id, "expected_version": version})
    version = updated.get("scene_version", version)
    show("answer", updated)
    print("    -> the body repeats object_id_in_scene; a mismatch is 400 BODY_ID_MISMATCH, and")
    print("       a parent chain that reaches itself is 400 INVALID_PARENT_CYCLE.")
    report_events(listener, "after /updateItem")

    steps(f"GET /get2DAABB?id={demo_id}&camera_id={OVERHEAD_CAMERA_ID} - pixels, top-left origin (§5.11)")
    box = get_aabb(client, demo_id, OVERHEAD_CAMERA_ID)
    print(f"    visible={box.get('visible')} clipped={box.get('clipped')} "
          f"size={box.get('width')}x{box.get('height')} frame_index={box.get('frame_index')}")
    show("box (pixels)", box.get("box"))
    show("aabb_world (metres)", box.get("aabb_world"))
    print("    -> x right, y down, x_min <= x_max. Nothing of the object in the frustum is still")
    print("       HTTP 200 with visible=false and box=null (not 404). A camera or light id gives")
    print("       400 EMPTY_AABB: its markers are edit layer, so there is no geometry to measure.")

    steps(f"GET /captureScene?id={OVERHEAD_CAMERA_ID}&type=rgbd - render and read back (§5.10)")
    frame = client.get("/captureScene", params={"id": OVERHEAD_CAMERA_ID, "type": "rgbd"})
    width, height = int(frame.get("width") or 0), int(frame.get("height") or 0)
    image = base64.b64decode(frame.get("image_base64") or "")
    print(f"    {width}x{height} {frame.get('image_encoding')} image = {len(image)} bytes "
          f"(magic {image[:4].hex()} = PNG), scene_version={frame.get('scene_version')}, "
          f"frame_index={frame.get('frame_index')}")
    depth = base64.b64decode(frame.get("depth_base64") or "")
    expected_bytes = width * height * 4
    print(f"    depth: {frame.get('depth_encoding')} = {len(depth)} bytes (expected {expected_bytes}), "
          f"invalid={frame.get('depth_invalid')}")
    if len(depth) == expected_bytes:
        pixels = struct.unpack(f"<{width * height}f", depth)
        centre = pixels[(height // 2) * width + width // 2]
        misses = sum(1 for value in pixels if math.isnan(value))
        print(f"    centre pixel: {'NaN' if math.isnan(centre) else f'{centre:.3f} m'} | "
              f"{misses} of {len(pixels)} pixels hit nothing (NaN)")
    print("    -> row-major float32 little-endian, first row = top of the image, metres along")
    print("       camera +Z. type must equal that entity's Camera.type, and `rgb` omits depth_*.")

    steps(f"POST /updateItem once per tick - a full turn of the arm's {SPIN_JOINT} at "
          f"{SPIN_SPEED_RAD_PER_S} rad/s (§13.20 Q85)")
    version = spin_shoulder_pan(client, listener, version)

    steps("POST /updateTraj - two clips in one multipart request, no expected_version "
          "(§5.3, §13.7 Q33, §13.28 Q99)")
    clips = read_example_trajs()
    print("    posting " + " + ".join(f"{name} ({len(data)} bytes)"
                                      for name, data in zip(EXAMPLE_TRAJS, clips))
          + f" as {len(clips)} `trajs` parts")
    traj = client.post_trajs("/updateTraj", clips, params={"count": len(clips)})
    show("answer", traj)
    print(f"    -> count={traj.get('count')}: the batch arrives at the browser as ONE binary frame,")
    print("       sliced by the announced byte_lengths; nothing is decoded from the file headers.")
    for summary in traj.get("trajs", []):
        ignored = summary.get("ignored_objects") or []
        notes = summary.get("warnings") or []
        print(f"    -> trajs[{summary.get('index')}] is {summary.get('total_frames')} frames at "
              f"{summary.get('fps')} fps = {summary.get('duration_seconds')} s, "
              f"{summary.get('object_count')} objects, {summary.get('attach_count')} attaches, "
              f"{len(ignored)} ignored_objects, {len(notes)} warnings")
        for entry in ignored:
            print(f"       object {entry.get('object_id')}: {entry.get('reason')} - this clip names")
            print("       an id the applied world does not have, so it is skipped and playback goes on.")
        for note in notes:
            print(f"       {note} - an attach whose new parent is off stage is dropped (§8.6).")
    print("    -> each clip is judged against the same world on its own, so one file's missing")
    print("       id says nothing about the other. The toolbar selector lists both by frame count")
    print("       and plays the first; switching is local, pauses, and returns to frame 0 (§7.1 Q100).")
    print("       Picking one of several files in Load Traj appends to the same list (§4.3 Q104).")
    after = client.get("/getWorld").get("scene_version")
    print(f"    -> scene_version is still {after}: a trajectory is not a world change (§5.7),")
    print("       and unlike the writes above this one broadcasts nothing.")
    report_events(listener, "after /updateTraj: no scene.applied by design")

    steps(f"POST /removeObject?id={demo_id} - cascade by default (§5.14)")
    removed = client.post("/removeObject", params={"id": demo_id, "expected_version": version})
    version = removed.get("scene_version", version)
    show("answer", removed)
    print("    -> removed_object_ids is the whole subtree; reparented_object_ids is only")
    print("       non-empty with ?detach_children=true (direct children move to the grandparent).")
    report_events(listener, "after /removeObject")

    steps("§5.13 error samples - what a caller has to branch on")
    for description, call in error_samples(client, demo_id, version):
        try:
            call()
        except GatewayError as error:
            print(f"    {error.status:>3} {error.code:<24} {error.message[:80]}")
            print(f"        why: {description} | request_id={brief(error.request_id)}")
        else:
            print(f"    !! expected a failure: {description}")
    print("    -> one code namespace for gateway and viewer; the body is always")
    print("       {\"error\": {\"code\", \"message\", \"request_id\"}}, and 503 NO_BROWSER_CONNECTED")
    print("       is the one code this tour cannot show while a tab is open.")

    steps("Restore the world the viewer held before this tour")
    if options.keep:
        print("    --keep: the sample scene stays loaded, nothing written back.")
    elif baseline is None:
        print("    nothing to restore (step 4 captured no live world).")
    else:
        restored = client.post("/updateWorld", baseline, params={"expected_version": version})
        print(f"    -> back to scene_id={restored.get('scene_id')} "
              f"scene_version={restored.get('scene_version')} objects={restored.get('object_count')}")
    if listener is not None:
        print(f"    scene.applied broadcasts seen: {listener.applied}")
        listener.stop()
    print("\nAll eleven caller interfaces of §5.9 were exercised.")
    return 0


def error_samples(client: GatewayClient, demo_id: int,
                  version: int | None) -> list[tuple[str, Any]]:
    """(why it fails, the call). Every code here comes from §5.13, none is invented."""
    stale = (version or 0) + 1000
    return [
        ("type is required and has no default",
         lambda: client.get("/captureScene", params={"id": OVERHEAD_CAMERA_ID})),
        ("ids are uint32, so a negative one is refused by the gateway before it asks a viewer",
         lambda: client.get("/captureScene", params={"id": -3, "type": "rgb"})),
        ("that object is not in the world",
         lambda: client.get("/get2DAABB", params={"id": 999999, "camera_id": OVERHEAD_CAMERA_ID})),
        ("the camera is rgbd and the caller asked for rgb - no implicit mode switch",
         lambda: client.get("/captureScene", params={"id": OVERHEAD_CAMERA_ID, "type": "rgb"})),
        ("an ambient light has no renderable geometry outside the edit layer",
         lambda: client.get("/get2DAABB", params={"id": AMBIENT_LIGHT_ID, "camera_id": OVERHEAD_CAMERA_ID})),
        ("body id and query id must agree",
         lambda: client.post("/updateItem", object_for_update(demo_id, [0.0, 0.0, 0.6]),
                             params={"id": GRIPPER_ID})),
        ("someone else wrote first, so the expected version is stale",
         lambda: client.post("/addObject", object_for_add([0.0, 0.0, 0.6]),
                             params={"expected_version": stale})),
        ("a body that is not TRAJ is refused by the parser, whole file and all",
         lambda: client.post_trajs("/updateTraj", [bytes(32)])),
        ("a batch is all-or-nothing, and the failing part is named (§5.3, Q99)",
         lambda: client.post_trajs("/updateTraj", [build_sample_traj(), bytes(32)],
                                   params={"count": 2})),
    ]


class NoViewerConnected(RuntimeError):
    """The one precondition this demo cannot satisfy by itself."""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Demo every caller interface of §5.9 on the standalone WebEditor gateway.")
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY,
                        help=f"gateway base url (default {DEFAULT_GATEWAY})")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="per-request timeout in seconds; must beat the gateway's own (default 30)")
    parser.add_argument("--skip-events", action="store_true",
                        help="do not open the /events listener (no aiohttp needed)")
    parser.add_argument("--keep", action="store_true",
                        help="leave the sample scene loaded instead of restoring the previous world")
    options = parser.parse_args()
    # The animation step prints a frame every ~100 ms, and block buffering would hide
    # the whole tour until the process exits when stdout is a pipe (`| tee`, a CI log,
    # a supervisor watching for the step that is currently running).
    sys.stdout.reconfigure(line_buffering=True)

    client = GatewayClient(options.gateway, timeout=options.timeout)
    try:
        return tour(client, options)
    except NoViewerConnected as failure:
        error = failure.__cause__
        print(f"\nBLOCKED: {error}", file=sys.stderr)
        print(f"""
Every caller route except /api/health and /api/runtimeConfig is relayed to a
browser over /events (§5.1), so this tour needs one open editor tab. Serve it
from the gateway itself:

    python local_gateway.py            # then open http://127.0.0.1:8099/ in a browser
    python examples/gateway_client_demo.py

Or, in dev, `npm run dev` plus http://127.0.0.1:5173. The tab must stay open and
must be the one pointed at {client.base_url}.""", file=sys.stderr)
        return 2
    except GatewayError as failure:
        # A caller-side bug in this tour should read as one line, not as a traceback
        # through urllib: the code and the message are the whole diagnosis (§5.13).
        print(f"\nFAILED: {failure.status} {failure.code}: {failure.message} "
              f"(request_id={failure.request_id})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
