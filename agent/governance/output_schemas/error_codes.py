"""Error code constants for stage-output preflight validation (PR1).

All codes are stable identifiers used by validators, CLI tools, retry logic,
and observer dashboards. Code names are SHOUT_SNAKE_CASE; values match.
"""

# Fatal codes — any of these in errors causes valid=False in all but
# 'disabled' mode.
MALFORMED_JSON = "MALFORMED_JSON"
MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
EMPTY_NODE_ID = "EMPTY_NODE_ID"
INVALID_PARENT_LAYER_TYPE = "INVALID_PARENT_LAYER_TYPE"
UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
UNAUTHORIZED_SELF_WAIVER = "UNAUTHORIZED_SELF_WAIVER"
PHANTOM_CREATE_FOR_DECLARED_REMOVED = "PHANTOM_CREATE_FOR_DECLARED_REMOVED"
PHANTOM_CREATE_FOR_UNMAPPED_FILE = "PHANTOM_CREATE_FOR_UNMAPPED_FILE"
# PR1d / PR1f: PM-side declaration enforcement. The constant is retained for
# backward compatibility (existing tests / docs reference the name and any
# legacy emitter paths can still raise it), but PR1f demoted the code OUT of
# FATAL_CODES — PM is a proposer, not an executor; declarations are advisory.
# Filtering of phantom creates relies on dev's graph_delta.removes plus
# filesystem truth (PR1e) and the auto-inferrer's PRD-declaration hint (PR1c).
MISSING_DECLARATION_FOR_DELETED_FILE = "MISSING_DECLARATION_FOR_DELETED_FILE"

# Warning codes — only CREATE_NOT_IN_PROPOSED_NODES is demoted to a warning
# under mode='warn' (default). Intentional drift from PM proposed_nodes is
# acceptable in some flows. The two phantom-create codes above were promoted
# to FATAL (PR1b) because they signal real graph-delta inconsistency against
# explicit PM declarations (removed_nodes / unmapped_files).
CREATE_NOT_IN_PROPOSED_NODES = "CREATE_NOT_IN_PROPOSED_NODES"

FATAL_CODES = frozenset({
    MALFORMED_JSON,
    MISSING_REQUIRED_FIELD,
    EMPTY_NODE_ID,
    INVALID_PARENT_LAYER_TYPE,
    UNSUPPORTED_SCHEMA_VERSION,
    UNAUTHORIZED_SELF_WAIVER,
    PHANTOM_CREATE_FOR_DECLARED_REMOVED,
    PHANTOM_CREATE_FOR_UNMAPPED_FILE,
})
