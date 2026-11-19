"""Read-only adapter for SDKs that recursively collect non-kinematic joints."""
from pathlib import Path
import xml.etree.ElementTree as ET


def write_kinematic_copy(source, destination):
    source, destination = Path(source), Path(destination)
    root = ET.parse(source).getroot()
    # These blocks configure controllers/plugins, not collision or link geometry.
    for child in list(root):
        if child.tag in {'transmission', 'ros2_control', 'gazebo'}:
            root.remove(child)
    # The copy lives outside OLS; keep relative geometry references resolvable.
    for item in list(root.iter('mesh')) + list(root.iter('texture')):
        filename = item.get('filename')
        if filename and '://' not in filename and not Path(filename).is_absolute():
            item.set('filename', str((source.parent / filename).resolve()))
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(destination, encoding='utf-8', xml_declaration=True)
    return destination
