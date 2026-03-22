"""Start the governance service."""
import os
os.environ.setdefault("GOVERNANCE_PORT", "30006")

from agent.governance.server import main
main()
