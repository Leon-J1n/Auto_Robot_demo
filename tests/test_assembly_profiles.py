import json
from pathlib import Path

from demo.scene.assembler import SceneAssembler


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "services" / "object_library_server" / "data"
SCENE = Path("/home/cscvlab/lht/var/webeditor/tasks/8a769e5e027f422dbb38c2bb876eb204/e21f8842939f4ff6b7dd5793969d7929/scene_config.json")


class LocalMetadata:
    def _get(self, kind, identifier):
        folder = "models" if kind == "model" else "robots"
        directory = ASSETS / folder / str(identifier)
        index = json.loads((ASSETS / folder / ("model_index.json" if kind == "model" else "robot_index.json")).read_text())
        name = index["by_id"][str(identifier)]
        return json.loads((directory / f"{name}.json").read_text())

    def get_model(self, identifier):
        return self._get("model", identifier)

    def get_robot(self, identifier):
        return self._get("robot", identifier)


def test_nero_robotiq_identity_mount_uses_control_calibration():
    if not SCENE.is_file():
        return
    scene = json.loads(SCENE.read_text())
    source_attachment = next(
        c["properties"] for obj in scene["objects"]
        for c in obj.get("components", [])
        if c.get("component_type") == "RobotAttachment"
    )
    assert source_attachment["transform"]["position"] == [0, 0, 0]
    assembled = SceneAssembler(ASSETS, LocalMetadata()).assemble(scene)
    attachment = next(
        c["properties"] for obj in assembled["objects"]
        for c in obj.get("components", [])
        if c.get("component_type") == "RobotAttachment"
    )
    assert attachment["transform"]["position"] == [0.0, 0.0, 0.01]
    assert assembled["_assembly"]["profile"]["profile_id"] == "nero+robotiq_2f85:v1"
    # Assembly operates on an execution copy; the authoring scene is unchanged.
    assert source_attachment["transform"]["position"] == [0, 0, 0]
