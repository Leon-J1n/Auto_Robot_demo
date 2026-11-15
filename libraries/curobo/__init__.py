"""Optional cuRobo adapter. Importing this package never loads CUDA."""

from .client import CuroboClient

__all__ = ['CuroboClient']
