"""Deterministic Path Strategy and Planning Mode selector."""

from __future__ import annotations

from typing import Any, Dict, Mapping


class StrategySelectionError(ValueError):
    """Raised when the request cannot map to a valid strategy selection."""


class StrategySelector:
    """Select semantic motion behavior; never compute the physical trajectory."""

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = config or {}

    def select(
        self,
        request: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Return a deterministic selection result.

        Context may be used to determine whether the initial planning mode should
        be direct or collision-free. The motion planner remains responsible for
        proving feasibility.
        """
        context = context or {}
        target = request.get("target", {})
        motion = request.get("motion", {})
        planning = request.get("planning", {})

        target_type = target.get("type")
        target_name = target.get("semantic_name") or target.get("name")
        requested_path = motion.get("path_type", "auto")
        phase = motion.get("phase", "transit")

        path_strategy, reason = self._select_path_strategy(
            target_type=target_type,
            target_name=target_name,
            requested_path=requested_path,
            phase=phase,
            strategy_params=request.get("strategy_params", {}),
        )
        planning_mode, planning_reason = self._select_planning_mode(
            requested_mode=planning.get("mode", "auto"),
            context=context,
        )

        return {
            "path_strategy": path_strategy,
            "planning_mode": planning_mode,
            "selection_reason": f"{reason}; {planning_reason}",
            "fallback_used": False,
        }

    def _select_path_strategy(
        self,
        *,
        target_type: str | None,
        target_name: str | None,
        requested_path: str,
        phase: str,
        strategy_params: Mapping[str, Any],
    ) -> tuple[str, str]:
        if requested_path != "auto":
            mapping = {
                "joint": "joint_move",
                "linear": "linear_move",
                "circular": "circular_move",
            }
            if requested_path not in mapping:
                raise StrategySelectionError(
                    f"Unsupported path_type: {requested_path}"
                )
            # Asking for a circular path without arc geometry is honoured: CircularMove
            # derives an arc that lifts over the straight line. Note the asymmetry with
            # the auto branch below, which still requires geometry to be supplied -- a
            # caller who says "circular" has chosen an arc, whereas one who says "auto"
            # has not, and inventing one for them would be a surprise.
            return mapping[requested_path], "explicit path_type"

        if target_type in {"joint", "named_state"}:
            return "joint_move", f"{target_type} target"

        if target_name in {"end_effector", "tcp", "tool", "__end_effector__"}:
            return "linear_move", "relative end-effector target"

        if self._has_arc_definition(strategy_params):
            return "circular_move", "valid arc geometry supplied"

        if phase in {"approach", "retreat", "contact"}:
            return "linear_move", f"task phase is {phase}"

        if target_type == "pose":
            return "joint_move", "unconstrained pose transit defaults to joint PTP"

        raise StrategySelectionError(
            f"Cannot select strategy for target type: {target_type}"
        )

    def _select_planning_mode(
        self,
        *,
        requested_mode: str,
        context: Mapping[str, Any],
    ) -> tuple[str, str]:
        if requested_mode == "direct":
            return "direct", "explicit direct-only planning"
        if requested_mode == "collision_free":
            return "direct", "direct path is probed before collision-free fallback"
        if requested_mode != "auto":
            raise StrategySelectionError(
                f"Unsupported planning mode: {requested_mode}"
            )

        return "direct", "auto mode begins with direct planning"

    @staticmethod
    def _has_arc_definition(params: Mapping[str, Any]) -> bool:
        has_via = params.get("via_point") is not None
        has_center_axis = all(
            params.get(key) is not None
            for key in ("arc_center", "arc_axis", "arc_angle")
        )
        return has_via or has_center_axis
