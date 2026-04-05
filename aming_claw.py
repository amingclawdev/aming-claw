"""Package name shim: ``from aming_claw import AmingConfig`` works after pip install.

This module re-exports the public API from the ``agent`` package so that
``import aming_claw`` maps to the source tree at ``agent/``.
"""

from agent import AmingConfig, bootstrap_project, create_task

__all__ = ["AmingConfig", "bootstrap_project", "create_task"]
