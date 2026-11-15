"""Check the selected Python/CUDA environment without loading a scene."""
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from libraries.curobo import CuroboClient

if __name__=='__main__':
    with CuroboClient() as client:
        result=client.doctor()
        print(json.dumps(result,indent=2))
        if result.get('cuda_kernel_test') != 'ok':
            raise SystemExit('CUDA kernel test failed; install a PyTorch build supporting this GPU architecture.')
