"""Headless planning example; input/output use the host project's JSON formats."""
import argparse
import json
from pathlib import Path
import sys

PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scene',type=Path,required=True,help='Current complete scene JSON')
    parser.add_argument('--request',type=Path,required=True,help='Existing Move request JSON')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--robot-object-id',type=int)
    parser.add_argument('--tool',type=Path,help='Optional TCP JSON: object_id/link/position/quaternion_xyzw')
    parser.add_argument('--ols',help='OLS HTTP base URL')
    parser.add_argument('--iterations',type=int,default=1)
    args=parser.parse_args()
    scene=json.loads(args.scene.read_text('utf-8'))
    request=json.loads(args.request.read_text('utf-8'))
    tool=json.loads(args.tool.read_text('utf-8')) if args.tool else None
    from planning.planners.curobo import CuroboPlanner
    config={'runtime':{'ols':{'base_url':args.ols}}} if args.ols else {}
    results=[]
    with CuroboPlanner(config,scene=scene,robot_object_id=args.robot_object_id,tool=tool) as action:
        for _ in range(args.iterations):
            result=action.plan(request)
            results.append(result)
            print(json.dumps({'success':result['success'],'timing':result.get('timing'),
                              'error':result.get('error')},ensure_ascii=False),flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(results[0] if len(results)==1 else results,indent=2,ensure_ascii=False),encoding='utf-8')
    raise SystemExit(0 if all(r['success'] for r in results) else 1)


if __name__=='__main__':
    main()
