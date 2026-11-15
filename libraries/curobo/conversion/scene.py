"""Convert one complete current scene snapshot into an assembled robot and world."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation

from .assets import AssetStore, ConversionError, asset_reference, component, fingerprint
from .urdf import URDFModel, origin, pose, rigid, set_origin, vector


def official_pose(matrix):
    m,_ = rigid(matrix)
    q = Rotation.from_matrix(m[:3,:3]).as_quat()
    return [*m[:3,3].tolist(),float(q[3]),*q[:3].tolist()]


def goal_in_base(goal,base_world,current_tool_pose):
    """Resolve a project pose target, preserving current orientation when omitted."""
    p=goal['position']
    xyz=[p[k] for k in ('x','y','z')] if isinstance(p,dict) else p
    orientation=goal.get('orientation')
    if orientation is None:
        quat=current_tool_pose[3:]
        rotation=Rotation.from_quat([*quat[1:],quat[0]]).as_matrix()
        if goal.get('frame','base')=='world':
            rotation=np.asarray(base_world)[:3,:3] @ rotation
    elif orientation.get('representation','quaternion')=='quaternion':
        rotation=Rotation.from_quat([orientation[k] for k in ('x','y','z','w')]).as_matrix()
    else:
        rotation=Rotation.from_euler('xyz',[orientation[k] for k in ('roll','pitch','yaw')],degrees=True).as_matrix()
    matrix=np.eye(4)
    matrix[:3,3]=vector(xyz,3,'goal position')
    matrix[:3,:3]=rotation
    return np.linalg.inv(base_world) @ matrix if goal.get('frame','base')=='world' else matrix


class SceneConverter:
    def __init__(self, store: AssetStore, cache_dir):
        self.store, self.cache = store, Path(cache_dir)
        self.models = {}

    def convert(self, snapshot, *, robot_object_id=None, tool=None):
        scene = deepcopy(snapshot)
        if not isinstance(scene, dict) or not isinstance(scene.get('objects'), list):
            raise ConversionError('scene must contain an objects array')
        objects = {}
        assets, models, states, fk = {}, {}, {}, {}
        notes = []
        for obj in scene['objects']:
            ident = obj.get('object_id_in_scene')
            if isinstance(ident, bool) or not isinstance(ident, int) or ident < 0 or ident in objects:
                raise ConversionError('Scene object IDs must be unique nonnegative integers')
            objects[ident] = obj
            ref = asset_reference(obj)
            if ref:
                asset = self.store.resolve(*ref)
                assets[ident] = asset
                if asset['entry'].suffix.lower() == '.urdf':
                    key = str(asset['entry'])
                    if key not in self.models:
                        self.models[key] = URDFModel(asset['entry'])
                    model = self.models[key]
                    models[ident] = model
                    defaults = deepcopy(asset['metadata'].get('robot_driver', {}).get('properties', {}))
                    # Scene state is authoritative; metadata supplies missing conventions only.
                    defaults.update(component(obj, 'RobotDriver'))
                    states[ident] = model.positions(defaults)
                    fk[ident] = model.fk(states[ident])
            elif not (component(obj, 'Light') or component(obj, 'Camera') or
                      component(obj, 'Collider') or not obj.get('components')):
                physical = {'MeshRenderer', 'RobotDriver', 'RobotModelRef', 'AssetRef', 'ArticulatedModel'}
                if any(c.get('component_type') in physical for c in obj.get('components', [])):
                    raise ConversionError(f'Physical object {ident} has no OLS MetadataRef or primitive Collider')

        def driver(ident):
            result = deepcopy(assets.get(ident, {}).get('metadata', {}).get('robot_driver', {}).get('properties', {}))
            result.update(component(objects[ident], 'RobotDriver'))
            return result

        parents, mounts = {}, {}
        for ident,obj in objects.items():
            transform = component(obj, 'Transform')
            parent = transform.get('parent')
            if parent == 0xFFFFFFFF:
                parent = None
            attachment = component(obj, 'RobotAttachment')
            if attachment:
                if attachment.get('parent_object_id') != parent or parent not in objects:
                    raise ConversionError(f'Invalid RobotAttachment parent on object {ident}')
                if attachment.get('connection_type', 'fixed') != 'fixed':
                    raise ConversionError('Only fixed RobotAttachment mounts are supported')
                mounted = attachment.get('transform', {})
                if not ('position' in mounted or 'quaternion_xyzw' in mounted):
                    raise ConversionError(f'RobotAttachment has no transform: {ident}')
                local = pose({**mounted, 'scale':transform.get('scale', [1,1,1])})
                reference = mounted.get('reference_frame') or 'parent_link'
                link = attachment.get('parent_link') if reference == 'parent_link' else None
                if reference not in {'parent_link', 'parent_object'}:
                    raise ConversionError(f'Unknown attachment reference_frame: {reference}')
                if reference == 'parent_link' and (parent not in models or link not in models[parent].links):
                    raise ConversionError(f'Attachment refers to missing parent URDF link: {ident}/{link}')
            else:
                local, link = pose(transform), None
            if parent is not None and parent not in objects:
                raise ConversionError(f'Missing Transform parent for object {ident}')
            parents[ident] = parent
            mounts[ident] = (link, local)

        worlds = {}
        def world(ident, trail=()):
            if ident in trail:
                raise ConversionError('Scene parent hierarchy contains a cycle')
            if ident not in worlds:
                parent=parents[ident]
                link,local=mounts[ident]
                matrix=np.eye(4) if parent is None else world(parent, trail+(ident,))
                if link is not None:
                    matrix=matrix @ fk[parent][link]
                worlds[ident]=matrix @ local
            return worlds[ident]
        for ident in objects:
            world(ident)
        candidates = [ident for ident in models if driver(ident).get('role') != 'passive'
                      and len(driver(ident).get('joint_sequence', [])) > 1
                      and not component(objects[ident], 'RobotAttachment')
                      and (component(objects[ident], 'RobotModelRef') or asset_reference(objects[ident])[0]=='robot')]
        if robot_object_id is None:
            if len(candidates) != 1:
                raise ConversionError(f'Select robot_object_id explicitly; arm candidates are {candidates}')
            robot_object_id=candidates[0]
        if robot_object_id not in candidates:
            raise ConversionError(f'Selected scene object is not an arm: {robot_object_id}')
        arm=robot_object_id
        arm_rigid,arm_scale=rigid(worlds[arm])
        if not np.allclose(arm_scale,1):
            raise ConversionError('Scaling the planned robot is unsupported')
        arm_inverse=np.linalg.inv(arm_rigid)
        def descendant(ident):
            parent=parents[ident]
            return ident == arm or (parent is not None and descendant(parent))
        assembly={ident for ident in objects if descendant(ident)}
        sequence=list(driver(arm).get('joint_sequence', []))
        if len(sequence)!=len(set(sequence)) or set(sequence)!=set(models[arm].independent):
            raise ConversionError('Arm joint_sequence must name every independent URDF joint exactly once')
        prefix=lambda ident:f'o{ident}_'
        xml=ET.Element('robot',name='ols_assembly')
        roots={}
        locked={}
        for ident in sorted(assembly):
            if not np.allclose(rigid(worlds[ident])[1],1):
                raise ConversionError(f'Attached robot object must have unit scale: {ident}')
            if ident in models:
                child=models[ident].namespaced(prefix(ident))
                roots[ident]=prefix(ident)+models[ident].root
                for element in child:
                    if element.tag in {'link','joint','material'}:
                        xml.append(element)
                if ident != arm:
                    locked.update({prefix(ident)+name:states[ident][name] for name in models[ident].independent})
            elif ident in assets:
                path=assets[ident]['entry']
                if path.suffix.lower() not in {'.obj','.stl','.ply','.dae','.glb','.gltf'}:
                    raise ConversionError(f'Unsupported attached mesh format: {path}')
                roots[ident]=prefix(ident)+'root'
                link=ET.SubElement(xml,'link',name=roots[ident])
                for tag in ('visual','collision'):
                    geometry=ET.SubElement(ET.SubElement(link,tag),'geometry')
                    ET.SubElement(geometry,'mesh',filename=str(path))
            else:
                if any(c.get('component_type') not in {'Transform','Light','Camera','LocalAxes','Semantic'}
                       for c in objects[ident].get('components',[])):
                    raise ConversionError(f'Unsupported attached geometry on object {ident}')
                roots[ident]=prefix(ident)+'root'
                ET.SubElement(xml,'link',name=roots[ident])
            if ident != arm:
                parent=parents[ident]
                parent_link,local=mounts[ident]
                source_link=prefix(parent)+(parent_link or models[parent].root) if parent in models else prefix(parent)+'root'
                joint=ET.SubElement(xml,'joint',name=prefix(ident)+'mount',type='fixed')
                ET.SubElement(joint,'parent',link=source_link)
                ET.SubElement(joint,'child',link=roots[ident])
                set_origin(joint,local)

        ref=component(objects[arm],'RobotModelRef')
        collision_ignores={}
        link_candidates={}
        for ident in sorted(assembly):
            if ident in models:
                for name in models[ident].links:
                    link_candidates.setdefault(name,[]).append(prefix(ident)+name)
        for ident in sorted(assembly):
            for pair in component(objects[ident],'RobotModelRef').get('allowed_collision_link_pairs',[]):
                if not isinstance(pair,(list,tuple)) or len(pair)!=2:
                    raise ConversionError('allowed_collision_link_pairs must contain pairs of link names')
                mapped=[]
                for name in pair:
                    candidates=link_candidates.get(name,[])
                    own=prefix(ident)+name if isinstance(name,str) else None
                    if own in candidates:
                        mapped.append(own)
                    elif len(candidates)==1:
                        mapped.append(candidates[0])
                    else:
                        raise ConversionError(f'Allowed collision link is missing or ambiguous: {name}')
                a,b=mapped
                collision_ignores.setdefault(a,[]).append(b)
                collision_ignores.setdefault(b,[]).append(a)
        requested=deepcopy(tool or {})
        if not requested:
            if ref.get('tip_link'):
                requested={'object_id':arm,'link':ref['tip_link'],'matrix':ref.get('tcp_transform',np.eye(4).tolist())}
            elif 'attachment_site' in models[arm].links:
                requested={'object_id':arm,'link':'attachment_site'}
                notes.append('No project TCP was supplied; tool is the arm attachment_site frame.')
            else:
                raise ConversionError('A tool link/TCP definition is required for this robot')
        tool_object=requested.get('object_id',arm)
        if tool_object not in assembly or tool_object not in models or requested.get('link') not in models[tool_object].links:
            raise ConversionError('TCP must reference a link in the assembled robot')
        tcp=np.asarray(requested['matrix'],dtype=float) if 'matrix' in requested else pose(requested)
        if tcp.shape!=(4,4) or not np.isfinite(tcp).all() or not np.allclose(tcp[3],[0,0,0,1]):
            raise ConversionError('TCP matrix must be a finite homogeneous transform')
        tool_link='curobo_tcp'
        ET.SubElement(xml,'link',name=tool_link)
        joint=ET.SubElement(xml,'joint',name='curobo_tcp_fixed',type='fixed')
        ET.SubElement(joint,'parent',link=prefix(tool_object)+requested['link'])
        ET.SubElement(joint,'child',link=tool_link)
        set_origin(joint,tcp)
        xml_text=ET.tostring(xml,encoding='unicode')
        robot_key=fingerprint({'xml':xml_text,'assets':[assets[i]['digest'] for i in sorted(assembly) if i in assets]})
        directory=self.cache/'robots'/robot_key
        directory.mkdir(parents=True,exist_ok=True)
        urdf_path=directory/'assembly.urdf'
        if not urdf_path.exists():
            urdf_path.write_text(xml_text,encoding='utf-8')
        world_config={}
        for ident in sorted(objects):
            if ident in assembly:
                continue
            matrix=arm_inverse @ worlds[ident]
            if ident in models:
                for name,link in models[ident].links.items():
                    geometries=link.findall('collision')
                    if not geometries:
                        geometries=link.findall('visual')
                        if geometries:
                            notes.append(f'Object {ident} link {name}: no collision tag; using its visual geometry.')
                    for index,geom in enumerate(geometries):
                        self._geometry(world_config,f'o{ident}_{name}_{index}',geom.find('geometry'),
                                       matrix @ fk[ident][name] @ origin(geom.find('origin')),models[ident].path.parent)
            elif ident in assets:
                self._mesh(world_config,f'o{ident}_mesh',assets[ident]['entry'],matrix,np.ones(3))
            else:
                collider=component(objects[ident],'Collider')
                if collider:
                    if collider.get('enabled') is False:
                        raise ConversionError(f'Disabled physical Collider is unsupported: {ident}')
                    shape=collider.get('shape')
                    geometry=ET.Element('geometry')
                    size=vector(collider.get('size'),3,'Collider size')
                    if shape=='box':
                        ET.SubElement(geometry,'box',size=' '.join(map(str,size)))
                    elif shape=='cylinder' and np.isclose(size[0],size[1]):
                        ET.SubElement(geometry,'cylinder',radius=str(size[0]),length=str(size[2]))
                    else:
                        raise ConversionError(f'Unsupported primitive Collider: {ident}/{shape}')
                    self._geometry(world_config,f'o{ident}_collider',geometry,matrix,None)
        effective={prefix(arm)+name:states[arm][name] for name in sequence}
        effective.update(locked)
        tool_world=worlds[tool_object] @ fk[tool_object][requested['link']] @ tcp
        result={'robot_key':robot_key,'robot_dir':str(directory),'urdf':str(urdf_path),
                'base_link':roots[arm],'tool_frame':tool_link,'tool_spec':requested,
                'joint_names':sequence,'active_names':[prefix(arm)+n for n in sequence],
                'positions':effective,'locked_joints':locked,'world':world_config,
                'base_world':arm_rigid.tolist(),'current_tool_pose':official_pose(arm_inverse @ tool_world),
                'robot_object_id':arm,'scene_limits':driver(arm).get('joint_limits',{}),
                'collision_ignores':collision_ignores,
                'notes':list(dict.fromkeys(notes)),
                'asset_revisions':{str(i):a['digest'] for i,a in assets.items()}}
        manifest=directory/'conversion_manifest.json'
        if not manifest.exists():
            manifest.write_text(json.dumps({k:v for k,v in result.items() if k!='world'},indent=2),encoding='utf-8')
        return result

    def _geometry(self, config, name, node, matrix, folder):
        if node is None or len(node)!=1:
            raise ConversionError(f'Invalid URDF geometry: {name}')
        geom=node[0]
        transform,scales=rigid(matrix)
        item={'pose':official_pose(transform)}
        if geom.tag=='box':
            dims=vector(geom.get('size','').split(),3,'box size')*scales
            if np.any(dims<=0):
                raise ConversionError('Box dimensions must be positive')
            config.setdefault('cuboid',{})[name]={**item,'dims':dims.tolist()}
        elif geom.tag=='sphere' and np.allclose(scales,scales[0]):
            config.setdefault('sphere',{})[name]={**item,'radius':float(geom.get('radius'))*float(scales[0])}
        elif geom.tag=='cylinder' and np.isclose(scales[0],scales[1]):
            config.setdefault('cylinder',{})[name]={**item,'radius':float(geom.get('radius'))*float(scales[0]),
                                                  'height':float(geom.get('length'))*float(scales[2])}
        elif geom.tag=='mesh':
            from .assets import safe_relative
            relative=safe_relative(geom.get('filename',''))
            path=folder.joinpath(*relative.parts).resolve()
            self._mesh(config,name,path,matrix,vector(geom.get('scale','1 1 1').split(),3,'mesh scale'))
        elif geom.tag in {'sphere','cylinder'}:
            import trimesh
            mesh=trimesh.creation.icosphere(subdivisions=3,radius=float(geom.get('radius'))) if geom.tag=='sphere' else trimesh.creation.cylinder(radius=float(geom.get('radius')),height=float(geom.get('length')),sections=64)
            key=fingerprint([name,ET.tostring(node,encoding='unicode'),matrix.tolist()])
            output=self.cache/'geometry'/f'{key}.obj'
            output.parent.mkdir(parents=True,exist_ok=True)
            mesh.apply_transform(matrix)
            mesh.export(output)
            config.setdefault('mesh',{})[name]={'pose':[0,0,0,1,0,0,0],'file_path':str(output)}
        else:
            raise ConversionError(f'Unsupported URDF geometry: {geom.tag}')

    def _mesh(self,config,name,path,matrix,scales):
        if not path.is_file():
            raise ConversionError(f'Collision mesh is absent from the OLS cache: {path}')
        transform,object_scale=rigid(matrix)
        config.setdefault('mesh',{})[name]={'pose':official_pose(transform),'file_path':str(path),
                                          'scale':(object_scale*scales).tolist()}
