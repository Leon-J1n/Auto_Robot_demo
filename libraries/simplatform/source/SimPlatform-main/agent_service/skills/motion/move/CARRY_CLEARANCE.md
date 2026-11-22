# Held-object obstacle clearance

`carry_clearance_config.json` is reloaded for each held-object move.
`safety_distance_m` defaults to `0.005` metres (5 mm) and accepts values from 0 to 1.
Zero removes the extra separation margin; actual obstacle collisions are still checked.
The larger of this value and the command's requested safety distance is used.
Collision avoidance remains enabled while carrying.

This is a margin around the robot collision geometry, not an exact swept volume
of the carried object. The platform's configuration query does not move a
physically clamped scene object along hypothetical robot configurations. Increase
the margin for an object protruding beyond the gripper; oversized objects still
need an attached collision-geometry query for a geometric guarantee.

A grasp may start close to a support. For each initially near (but not colliding)
robot/environment pair, the required gap grows from its initial distance to the
configured margin over 0.2 radians of maximum joint displacement. This permits
retreat without exempting that obstacle throughout the path. It rejects paths
that worsen the initial gap or continue grazing after leaving the initial pose.
Actual environment collisions are never forgiven by the self-contact baseline.
If no path satisfies the clearance, planning fails instead of reducing the margin.
