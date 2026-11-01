"""EAIK model preparation and URDF entry point; optional imports stay lazy."""


__all__ = ["load_urdf", "UrdfRobotModel"]


def __getattr__(name):
    if name in __all__:
        from . import urdf
        return getattr(urdf, name)
    raise AttributeError(name)
