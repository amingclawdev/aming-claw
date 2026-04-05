# Gatekeeper Role

The Gatekeeper is responsible for pre-release gate validation and override decisions in the Aming Claw governance pipeline.

## Responsibilities

- Validate gate conditions between pipeline stages
- Override blocked gates when human judgment determines it is safe
- Ensure release quality standards are met before merge

## Permissions

- `task.complete` — Complete assigned gatekeeper tasks
- `node.verify` — Update node verification status
- `gate.override` — Override a blocked gate with justification

## Related

- [Gates Documentation](../governance/gates.md)
- [Auto-Chain Pipeline](../governance/auto-chain.md)
- [Role Permissions](../config/role-permissions.md)
