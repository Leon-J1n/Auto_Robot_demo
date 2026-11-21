"""SSIK integration; the optional upstream package is imported on first use."""
from .urdf import load_urdf

__all__ = ["load_urdf"]
