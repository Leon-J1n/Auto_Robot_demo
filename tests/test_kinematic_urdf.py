import xml.etree.ElementTree as ET
from collision.assets.kinematic_urdf import write_kinematic_copy


def test_controller_joint_names_do_not_shadow_real_joints(tmp_path):
    source = tmp_path / 'asset' / 'arm.urdf'
    source.parent.mkdir()
    content = '''<robot name="arm"><transmission name="drive"><joint name="j"/></transmission>
    <link name="base"/><link name="tip"><collision><geometry><mesh filename="mesh.obj"/></geometry></collision></link>
    <joint name="j" type="revolute"><parent link="base"/><child link="tip"/>
    <limit lower="-1" upper="1" effort="10" velocity="1"/></joint></robot>'''
    source.write_text(content)
    result = write_kinematic_copy(source, tmp_path / 'derived' / 'arm.urdf')
    root = ET.parse(result).getroot()
    assert len(list(root.iter('joint'))) == 1
    assert root.find('joint/parent').get('link') == 'base'
    assert root.find('joint/limit').get('lower') == '-1'
    assert root.find('.//mesh').get('filename') == str(source.parent / 'mesh.obj')
    assert source.read_text() == content
