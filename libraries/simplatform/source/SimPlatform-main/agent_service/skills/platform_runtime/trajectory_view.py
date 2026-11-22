"""Showing candidate trajectories in sim_editor, and mirroring motion into it.

The editor is a separate process that renders a scene but never steps physics, while the
skills run against their own in-process world. Nothing is shared but a TCP socket, so
anything the editor is to show has to be sent to it. Two things are sent from here: the
gripper path of each candidate trajectory, drawn as coloured markers, and the live robot
pose during execution.

A trajectory is drawn at the gripper tip only. A joint path says nothing directly about
where the hand goes, so each waypoint is put through forward kinematics for the pinch
frame -- the point between the fingertips -- and the resulting curve is what gets drawn.
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Sequence

import numpy as np

# Okabe-Ito, an eight-colour qualitative palette designed to stay distinguishable under
# the common forms of colour blindness. Black is replaced by a light grey that reads
# against the editor's dark viewport. Candidates take these in order and wrap around,
# which only happens past eight paths on screen at once.
PALETTE: tuple[tuple[float, float, float], ...] = (
    (0.337, 0.706, 0.914),  # sky blue
    (0.902, 0.624, 0.000),  # orange
    (0.000, 0.620, 0.451),  # bluish green
    (0.941, 0.894, 0.259),  # yellow
    (0.000, 0.447, 0.698),  # blue
    (0.835, 0.369, 0.000),  # vermillion
    (0.800, 0.475, 0.655),  # reddish purple
    (0.780, 0.780, 0.780),  # light grey
)

DEFAULT_MARKER_SPACING = 0.02
DEFAULT_MARKER_RADIUS = 0.008
DEFAULT_MIRROR_FPS = 30.0


def trajectory_color(index: int) -> tuple[float, float, float]:
    """The palette entry for a candidate, wrapping past the end of the palette."""
    return PALETTE[int(index) % len(PALETTE)]


def thin_by_arc_length(points: Sequence[Sequence[float]],
                       spacing: float = DEFAULT_MARKER_SPACING) -> list[list[float]]:
    """Drop waypoints until consecutive markers are at least `spacing` metres apart.

    Spacing is measured along the path rather than by index. Waypoint density varies
    enormously between path types -- a joint interpolation is evenly spaced in joint
    angle, which bunches up near the shoulder and stretches out at full reach, while an
    RRT path after shortcutting has no regular spacing at all -- so a fixed stride would
    look dense on one path and sparse on another. Distance gives every path the same
    on-screen density whatever produced it.

    The first and last points always survive: they are where the motion starts and ends,
    which is the part of a candidate a reader most needs to see.
    """
    if spacing <= 0.0:
        raise ValueError("marker spacing must be positive")
    positions = np.atleast_2d(np.asarray(points, dtype=float))
    if positions.size == 0:
        return []
    if positions.shape[1] != 3:
        raise ValueError("each waypoint must contain three coordinates")
    kept = [positions[0]]
    for point in positions[1:-1]:
        if float(np.linalg.norm(point - kept[-1])) >= spacing:
            kept.append(point)
    if len(positions) > 1:
        # The last point is unconditional, so it can land closer than `spacing` to its
        # predecessor. Better a slightly tight final pair than a path that stops short of
        # where the arm actually ends up.
        kept.append(positions[-1])
    return [[float(value) for value in point] for point in kept]


def gripper_path(kinematics: Any, joint_path: Sequence[Sequence[float]], *,
                 spacing: float = DEFAULT_MARKER_SPACING) -> list[list[float]]:
    """The gripper-tip curve of a joint-space trajectory, thinned for display.

    Takes the FK service rather than the runtime because the runtime does not own one --
    each skill builds its own, and the move skill's is already configured for the pinch
    frame this draws.

    FK runs for the whole path in a single platform call rather than per waypoint, which
    matters because a shortcut RRT path can carry a few hundred waypoints and this is on
    the interactive path between a command and its answer.
    """
    waypoints = np.atleast_2d(np.asarray(joint_path, dtype=float))
    if waypoints.size == 0:
        return []
    poses = kinematics.forward_batch(waypoints)
    return thin_by_arc_length([pose["position"] for pose in poses], spacing)


class EditorTrajectoryViewer:
    """Mirrors the in-process world into sim_editor, and draws candidate paths there.

    Satisfies the runtime's viewer seam: `attach_viewer()` accepts anything with a
    `sync()`, which the runtime calls as motion advances. That seam is called from inside
    the physics loop -- up to several hundred times per waypoint -- so `sync()` rate-limits
    itself. Sending every call would spend the whole execution in TCP round trips for
    frames no display could show: the editor draws on a 16 ms timer and only when the scene
    changed, so it can absorb about 60 mirrors a second and anything beyond that is
    discarded work. The rate below is deliberately under that ceiling rather than at it,
    leaving the editor's frame budget to the frames themselves.
    """

    def __init__(self, session: Any, runtime: Any, *,
                 objects: Iterable[str] = (),
                 mirror_fps: float = DEFAULT_MIRROR_FPS) -> None:
        if mirror_fps <= 0.0:
            raise ValueError("mirror_fps must be positive")
        self.session = session
        self.runtime = runtime
        self.mirror_fps = float(mirror_fps)
        self._interval = 1.0 / self.mirror_fps
        self._last_sync = 0.0
        self._drawn: list[str] = []
        # Resolve editor entities by name rather than trusting that ids agree across the
        # two processes. They do agree today, both worlds being restored from one
        # simstate, but a name is what the caller actually knows.
        #
        # A name can appear several times: importing an OBJ builds a nested chain of
        # entities that all carry the mesh's name, so `green_box` is three entities deep.
        # Only the outermost one holds the object's pose -- the children sit at identity
        # relative to it -- and mirror_pose writes a *local* transform. Writing a world
        # pose onto a child therefore stacks it on top of its ancestors' transforms and
        # throws the object across the scene, so the shallowest match is the one to keep.
        depths: dict[str, int] = {}
        parents = {int(item["entity"]): item["parent"] for item in session.scene_hierarchy()}

        def depth(entity: int) -> int:
            steps = 0
            parent = parents.get(entity)
            while parent is not None and steps < len(parents):
                steps += 1
                parent = parents.get(int(parent))
            return steps

        editor_entities: dict[str, int] = {}
        for item in session.scene_hierarchy():
            name, entity = str(item["name"]), int(item["entity"])
            level = depth(entity)
            if name not in editor_entities or level < depths[name]:
                editor_entities[name], depths[name] = entity, level
        self._objects: list[tuple[str, int]] = []
        for name in objects:
            entity = editor_entities.get(str(name))
            if entity is not None:
                self._objects.append((str(name), entity))

    @property
    def mirrored_objects(self) -> tuple[str, ...]:
        """Scene objects whose pose is pushed to the editor alongside the arm."""
        return tuple(name for name, _ in self._objects)

    def sync(self) -> bool:
        """Push the current pose if enough time has passed. Returns whether it was sent.

        Called by the runtime as motion advances, including from inside the physics
        stepping loop, so the rate limit here is what keeps execution from turning into a
        stream of round trips. Dropping a frame is free: each mirror carries the whole
        pose rather than a delta, so the next one that goes out is fully current.
        """
        now = time.perf_counter()
        if now - self._last_sync < self._interval:
            return False
        self._last_sync = now
        self.force_sync()
        return True

    def force_sync(self) -> None:
        """Push the pose unconditionally, for the end of a motion.

        The rate limit in `sync()` means the final waypoint of a motion is usually the one
        dropped, leaving the editor showing a pose the arm has already left. Calling this
        once a motion returns makes the display agree with the world.
        """
        objects = []
        for name, entity in self._objects:
            pose = self.runtime.object_pose(name)
            if pose is not None:
                objects.append((entity, pose["position"], pose["quaternion_wxyz"]))
        self.session.mirror_pose(joints=self.runtime.full_joint_positions(),
                                 objects=objects)

    def draw_candidates(self, candidates: Sequence[Any], kinematics: Any, *,
                        spacing: float = DEFAULT_MARKER_SPACING,
                        radius: float = DEFAULT_MARKER_RADIUS) -> int:
        """Draw every candidate's gripper path at once, each in its own colour.

        One request carries the whole set so the editor rebuilds its outliner once, and so
        the paths appear together -- they are meant to be compared, which needs them all on
        screen at the same time. Returns the total marker count.
        """
        self.clear()
        drawings = []
        for index, candidate in enumerate(candidates):
            points = gripper_path(kinematics, candidate.trajectory["waypoints"],
                                  spacing=spacing)
            if len(points) < 2:
                # A candidate whose gripper barely moves would draw as a single dot that
                # any other path would hide. Skipping it keeps the display honest about
                # what can be told apart.
                continue
            name = f"candidate_{index + 1}"
            drawings.append((name, points, trajectory_color(index)))
            self._drawn.append(name)
        if not drawings:
            return 0
        return int(self.session.draw_trajectories(drawings, radius=radius))

    def clear(self) -> int:
        """Erase every trajectory this viewer drew."""
        if not self._drawn:
            return 0
        self._drawn.clear()
        return int(self.session.clear_trajectories())
