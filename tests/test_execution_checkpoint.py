from types import SimpleNamespace
import numpy as np
import pytest
from execution.runtime.checkpoint import restore


def test_no_checkpoint_leaves_runtime_untouched():
    restore(SimpleNamespace(), None)


def test_invalid_checkpoint_fails_explicitly_and_clears_provisional_grasp():
    calls = []
    backend = SimpleNamespace(gripper_controller=SimpleNamespace(detach=lambda: calls.append('detach')))
    with pytest.raises(ValueError, match='GRASP_RESTORE_FAILED'):
        restore(backend, {'schema_version': 99})
    assert calls == ['detach']
