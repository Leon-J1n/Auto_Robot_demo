"""Independent linear motion action."""
from __future__ import annotations
from copy import deepcopy
from typing import Any, Mapping
from atomic_action.motion.common.action import _MoveExecutor

class MoveLinearAction(_MoveExecutor):
    path_type = "linear"
    def execute(self, request: Mapping[str, Any], **kwargs):
        normalized = deepcopy(dict(request))
        normalized.setdefault("motion", {})
        normalized["motion"] = dict(normalized["motion"])
        normalized["motion"]["path_type"] = self.path_type
        return super().execute(normalized, **kwargs)

Linear_MoveExecutor = MoveLinearAction
MoveLinearSkill = MoveLinearAction
