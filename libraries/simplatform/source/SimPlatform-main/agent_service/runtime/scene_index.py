"""Name-to-entity resolution over the platform scene graph, read through Live Python.

The in-process runtime this replaces addressed the scene through
`World.editor_entities()` and `Robot.links`. Neither is reachable from a separate
process, so the index is built from `LiveSession.scene_snapshot()` instead: one
request returning every entity's id, object id, name, parent, and world transform.

The questions answered are unchanged -- "which entity is green_box", "what should I
call this collider", "does this belong to the gripper" -- and remain subtree queries
over the one flat entity graph, where a visible object is a named root with its
meshes and colliders parented beneath it.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterator, Mapping, Sequence

GRIPPER_PREFIX = "robotiq_2f85"


class SceneIndex:
    """Resolve names to entities and report which link or object owns a collider."""

    def __init__(self, session: Any, robot: Any, *,
                 snapshot: Any | None = None) -> None:
        self._session = session
        self._robot = robot
        self.refresh(snapshot)

    def refresh(self, snapshot: Any | None = None) -> None:
        """Rebuild the index. Needed after entities are created or reparented."""
        records = self._records(snapshot)
        self._name: dict[int, str] = {}
        self._object_id: dict[int, str] = {}
        self._parent: dict[int, int | None] = {}
        self._world: dict[int, dict[str, tuple[float, ...]]] = {}
        self._dynamic: dict[int, bool] = {}
        self._gravity: dict[int, bool] = {}
        self._children: dict[int, list[int]] = defaultdict(list)
        robot_roots: list[int] = []
        for record in records:
            entity = int(record["id"])
            parent = record.get("parent")
            parent = None if parent in (None, 0) else int(parent)
            self._name[entity] = str(record.get("name") or "")
            self._object_id[entity] = str(record.get("objectId") or "")
            self._parent[entity] = parent
            self._world[entity] = _transform(record.get("world"))
            rigid_body = record.get("rigidBody")
            if isinstance(rigid_body, Mapping) and "dynamic" in rigid_body:
                self._dynamic[entity] = bool(rigid_body.get("dynamic"))
                self._gravity[entity] = bool(rigid_body.get("gravity", False))
            if parent:
                self._children[parent].append(entity)
            if "robot" in record:
                robot_roots.append(entity)

        self.robot_entity = int(self._robot.handle)
        if self.robot_entity not in self._name and robot_roots:
            # A snapshot taken across a scene generation bump can disagree with the
            # proxy's handle; the single robot record is then the better answer.
            self.robot_entity = robot_roots[0]

        # The nearest enclosing link owns an entity: a pad's collider belongs to
        # robotiq_2f85_left_pad, not to the wrist_3_link the whole gripper hangs
        # from. Assign shallowest-first and let deeper links overwrite their
        # ancestors, so the innermost enclosing link wins.
        self._link_name: dict[int, str] = {}
        handles = self._link_handles()
        for handle in sorted(handles, key=self._depth):
            for entity in self.subtree(handle):
                self._link_name[entity] = handles[handle]

        # Depth decides which entity a repeated name refers to: object roots carry
        # the same name as their visual children, and callers mean the root.
        by_name: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for entity, name in self._name.items():
            if name and entity not in self._link_name:
                by_name[name].append((self._depth(entity), entity))
        self._roots: dict[str, int] = {
            name: min(candidates)[1] for name, candidates in by_name.items()
        }

    def _records(self, snapshot: Any | None) -> Sequence[Mapping[str, Any]]:
        value = self._session.scene_snapshot() if snapshot is None else snapshot
        self.scene_revision = int(getattr(value, "revision", 0))
        self.scene_generation = int(getattr(value, "scene_generation", 0))
        return tuple(value.entities)

    def _link_handles(self) -> dict[int, str]:
        """Entity handle -> link name for the robot's links.

        `list_links` answers this in one request, and the platform is the authority on
        what counts as a link. Paid once per scene generation.

        The fallback probes candidate names from the robot's own subtree -- a link
        entity carries its link name -- one request each. It exists for a stand-in that
        only implements `get_link`; against the real client it never runs.
        """
        lister = getattr(self._robot, "list_links", None)
        if callable(lister):
            return {int(link.handle): str(name) for name, link in lister()}
        handles: dict[int, str] = {}
        for entity in self.subtree(self.robot_entity):
            name = self._name.get(entity, "")
            if not name or name in handles.values():
                continue
            try:
                link = self._robot.get_link(name)
            except Exception:
                continue
            handles[int(link.handle)] = name
        return handles

    def _depth(self, entity: int) -> int:
        depth = 0
        parent = self._parent.get(entity)
        while parent:
            depth += 1
            parent = self._parent.get(parent)
        return depth

    def subtree(self, root: int) -> Iterator[int]:
        """The entity and every descendant, root first."""
        pending = [root]
        while pending:
            entity = pending.pop()
            yield entity
            pending.extend(self._children.get(entity, ()))

    def object_entity(self, name: str | None) -> int | None:
        """The root entity of a scene object, or None when the name is unknown.

        Accepts a collider or mesh name too, which is what callers pass when a
        request names a collider rather than the object it belongs to.
        """
        if not name:
            return None
        if name in self._roots:
            return self._roots[name]
        # Shallowest wins, not whichever the entity map happens to list first: a
        # name repeats down an object's subtree and callers mean the outermost one.
        matches = [(self._depth(entity), entity)
                   for entity, entity_name in self._name.items()
                   if entity_name == name]
        if matches:
            return self.object_root(min(matches)[1])
        return None

    def object_root(self, entity: int) -> int:
        """Walk up to the outermost non-robot ancestor of a collider."""
        current = entity
        while True:
            parent = self._parent.get(current)
            if not parent or parent in self._link_name:
                return current
            current = parent

    def parent_of(self, entity: int) -> int | None:
        """An entity's parent, or None when it sits at the scene root."""
        return self._parent.get(entity) or None

    def colliders_of(self, root: int) -> set[int]:
        """Every entity in an object's subtree, any of which may carry a collider."""
        return set(self.subtree(root))

    def is_dynamic_object(self, entity: int) -> bool:
        """Whether an object's root rigid body is dynamic.

        Missing rigid-body metadata is treated as static so collision exemptions do
        not silently broaden when an older snapshot omits physics state.
        """
        root = self.object_root(entity)
        return bool(self._dynamic.get(root, False))

    def has_gravity(self, entity: int) -> bool:
        """Whether the object's root rigid body has gravity enabled."""
        root = self.object_root(entity)
        return bool(self._gravity.get(root, False))

    def name_of(self, entity: int) -> str:
        """A label for contact reporting."""
        direct = self._name.get(entity)
        if direct:
            return direct
        link = self._link_name.get(entity)
        if link:
            return f"{link}::{entity}"
        parent = self._parent.get(entity)
        if parent:
            return f"{self.name_of(parent)}::{entity}"
        return f"entity_{entity}"

    def object_id_of(self, entity: int) -> str | None:
        """The Live object id of an entity, for building a proxy to address it."""
        return self._object_id.get(entity) or None

    def world_transform(self, entity: int) -> dict[str, tuple[float, ...]] | None:
        """The world transform recorded in the snapshot this index was built from."""
        return self._world.get(entity)

    def link_of(self, entity: int) -> str | None:
        """The robot link owning this entity, or None when it is not robot-side."""
        return self._link_name.get(entity)

    def is_robot(self, entity: int) -> bool:
        return entity in self._link_name

    def is_gripper(self, entity: int) -> bool:
        link = self._link_name.get(entity)
        return bool(link and link.startswith(GRIPPER_PREFIX))

    def is_gripper_pad(self, entity: int) -> bool:
        link = self._link_name.get(entity)
        return bool(link and link.startswith(GRIPPER_PREFIX) and "pad" in link)

    def entities(self) -> tuple[int, ...]:
        return tuple(self._name)

    def link_names(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._link_name.values())))


def _transform(value: Any) -> dict[str, tuple[float, ...]]:
    if not isinstance(value, Mapping):
        return {"translation": (0.0, 0.0, 0.0), "rotation_wxyz": (1.0, 0.0, 0.0, 0.0),
                "scale": (1.0, 1.0, 1.0)}
    defaults = {"translation": (0.0, 0.0, 0.0), "rotation_wxyz": (1.0, 0.0, 0.0, 0.0),
                "scale": (1.0, 1.0, 1.0)}
    result: dict[str, tuple[float, ...]] = {}
    for name, fallback in defaults.items():
        items = value.get(name)
        result[name] = (tuple(float(item) for item in items)
                        if isinstance(items, (list, tuple)) and len(items) == len(fallback)
                        else fallback)
    return result
