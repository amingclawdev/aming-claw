"""Built-in CLI and endpoint adapters."""

from .claude_cli import ClaudeAdapterError, ClaudeCliAdapter
from .claude_gateway import (
    ClaudeCompatibleGatewayAdapter,
    ClaudeGatewayAdapter,
    ClaudeGatewayAdapterError,
    ClaudeGatewayEndpointAdapter,
    ClaudeGatewayLaunchSpec,
)
from .codex_cli import CodexAdapterError, CodexCliAdapter, CodexLaunchSpec
from .codex_oss import (
    CodexOssAdapter,
    CodexOssAdapterError,
    CodexOssEndpointAdapter,
    CodexOssLaunchSpec,
)


__all__ = (
    "ClaudeAdapterError",
    "ClaudeCliAdapter",
    "ClaudeCompatibleGatewayAdapter",
    "ClaudeGatewayAdapter",
    "ClaudeGatewayAdapterError",
    "ClaudeGatewayEndpointAdapter",
    "ClaudeGatewayLaunchSpec",
    "CodexAdapterError",
    "CodexCliAdapter",
    "CodexLaunchSpec",
    "CodexOssAdapter",
    "CodexOssAdapterError",
    "CodexOssEndpointAdapter",
    "CodexOssLaunchSpec",
)
