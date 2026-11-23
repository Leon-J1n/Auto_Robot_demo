# Robotiq 2F-85 Reference Bundle

This bundle reproduces the reviewed templates in `../robotiq_2f85.json`.

## Contents

- `reference_meshes/`: the 33 OBJ files referenced by the Robotiq geometries. Each filename is the SHA256 of its file contents.
- `runtime_snapshot.json`: the 80 Robotiq runtime geometry records used during template export, with portable paths into `reference_meshes/`.
- `check_templates.py`: a self-contained check against the `robotiq_2f85.json` included in this bundle.
- `validated_sphere_source.yml`: the exact cuRobo robot YAML used to source the retained voxel sphere fits. Its historical `asset_root_path` is Windows-specific; the export tool reads only its `kinematics.collision_spheres` mapping and does not load that path.
- `robotiq_2f85.json`: the reviewed template data currently shipped by this project.
- `manifest.json`: source YAML hash, cuRobo commit, counts, and reference mesh hashes.

## Reproduce on Linux

Run from this directory with the project Python environment, PyYAML, and the project checkout available:

```bash
python -B check_templates.py --snapshot runtime_snapshot.json
python -B ../export_templates.py \
  --snapshot runtime_snapshot.json \
  --robot-yaml validated_sphere_source.yml \
  --output regenerated_templates.json
```

The check command should report all geometries as `matched`. Compare the `templates` object in `regenerated_templates.json` with `robotiq_2f85.json`; they should be identical. Exporting copies existing sphere parameters and is not a new geometric or motion-safety validation. The reviewed template may be used only with geometry whose hashes match this bundle; another mesh requires its own collision and trajectory validation.

The sphere source YAML was generated with cuRobo commit `78fd485fa82d9b9a063fb4985e371814587e666a`. Its SHA256 is `d00251d42c66dcaf7fcb697dd18327d24d9419f6c4ebad82d7734cc6225e5745`.
