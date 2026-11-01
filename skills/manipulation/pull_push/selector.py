"""Deterministic Push/Pull trajectory selector."""
from typing import Any, Dict, Mapping


class StrategySelectionError(ValueError):
    pass


class StrategySelector:
    def select(self, request: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> Dict[str, Any]:
        # Path geometry belongs to the tool motion.  The target can be a
        # hinge, slide or free body without changing this selection.
        trajectory = request.get("motion", {}).get(
            "path_type", request["manipulation"]["trajectory"]
        )
        if trajectory not in {"linear", "circular"}:
            raise StrategySelectionError(f"Unsupported trajectory: {trajectory}")
        return {"pull_push_strategy": f"{trajectory}_pull_push", "push_pull_strategy": f"{trajectory}_push_pull",
                "selection_reason": f"hard {trajectory} contact trajectory requested"}
