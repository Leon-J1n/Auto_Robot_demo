"""Name-to-entity resolution over the platform scene graph.

The MuJoCo layer addressed the scene through mj_name2id/mj_id2name on bodies and
geoms. The platform has one flat entity graph instead, where a visible object is a
named root with its meshes and colliders parented beneath it, so the same questions
("which entity is green_box", "what should I call this collider", "does this belong
to the gripper") become subtree queries.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterator

GRIPPER_PREFIX = "robotiq_2f85"


class SceneIndex:
    """Resolve names to entities and report which link or object owns a collider."""

    def __init__(self, world: Any, robot: Any) -> None:
        self._world = world
        self._robot = robot
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the index. Needed after entities are created or reparented."""
        infos = list(self._world.editor_entities())
        self._name: dict[int, str] = {i.entity: (i.name or "") for i in infos}
        self._parent: dict[int, int | None] = {i.entity: i.parent for i in infos}
        self._children: dict[int, list[int]] = defaultdict(list)
        for info in infos:
            if info.parent:
                self._children[info.parent].append(info.entity)

        # The nearest enclosing link owns an entity: a pad's collider belongs to
        # robotiq_2f85_left_pad, not to the wrist_3_link the whole gripper hangs
        # from. Links arrive alphabetically and their subtrees nest, so assign
        # shallowest-first and let deeper links overwrite their ancestors.
        self._link_name: dict[int, str] = {}
        handles = {link.handle: link_name for link_name, link in self._robot.links}
        for handle in sorted(handles, key=self._depth):
            for entity in self.subtree(handle):
                self._link_name[entity] = handles[handle]

        # Depth decides which entity a repeated name refers to: object roots carry
        # the same name as their visual children, and callers mean the root.
        by_name: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for info in infos:
            if info.name and info.entity not in self._link_name:
                by_name[info.name].append((self._depth(info.entity), info.entity))
        self._roots: dict[str, int] = {
            name: min(candidates)[1] for name, candidates in by_name.items()
        }

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

        Accepts a collider or mesh name too, which is what the MuJoCo layer allowed
        when a request named a geom rather than a body.
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

    def name_of(self, entity: int) -> str:
        """A label for contact reporting, mirroring the old geom names."""
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
