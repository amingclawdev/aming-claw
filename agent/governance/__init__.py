"""Workflow Governance Service — 工作流治理服务

三层架构:
  Layer 1: Graph Definition (规则层, JSON + NetworkX)
  Layer 2: Runtime State   (运行态, SQLite)
  Layer 3: Event Log       (事件流, JSONL append-only)
"""

__version__ = "0.1.0"
