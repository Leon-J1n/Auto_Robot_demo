"""§9.1: what a fake browser has to say beside its pixels for the gateway to accept them.

The identity gate on `/captureScene` and `/get2DAABB` compares six reported fields against a reading
the gateway holds, so every test that relays one of those two routes needs the same fixture: a world
to adopt as the 比对基准 and an answer that describes it. This module holds only that -- the bodies
themselves stay in the file that owns the route, because §5.10 and §5.11 are different shapes.
"""
import base64
import struct

from gateway_state import scene_identity
from local_gateway import PNG_IEND, PNG_MAGIC

# The 比对基准 most of these tests adopt: a bare scene document is enough, the gate only reads
# scene_id/scene_version/scene_sub_version and the digest scene_identity() draws from it.
WORLD = {"scene_schema_version": 1, "scene_id": 3, "scene_version": 7, "scene_sub_version": 0,
         "scene_name": "frame", "objects": []}


def png_bytes(width: int, height: int) -> bytes:
    """A png the §9 gate can read: magic, an IHDR that declares `width`x`height`, a closing IEND.

    The CRCs are zero bytes on purpose -- the gate asks the file what size it claims and whether it
    was cut off, and decoding a real image is the thing the gateway must not start doing.
    """
    ihdr = struct.pack(">II", width, height) + bytes(5)
    return PNG_MAGIC + b"\x00\x00\x00\rIHDR" + ihdr + bytes(4) + PNG_IEND


def image_base64(width: int, height: int) -> str:
    return base64.b64encode(png_bytes(width, height)).decode()


def identity_fields(world: dict = WORLD, browser_id: str = "browser-1", **overrides):
    """§9's six: the four-tuple of `world` plus who rendered it and at what play state.

    Built through `scene_identity` rather than read off the document, because that is what the gate
    compares against and a document may leave `scene_sub_version` out entirely.
    """
    value = {"browserID": browser_id, **scene_identity(world), "playback_state": "stopped"}
    value.update(overrides)
    return value
