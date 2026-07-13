# Claude profile authentication spike

This spike answers one bounded question: can the current host identity read
Claude CLI authentication without interactive access when the CLI uses its
inherited profile and two independently empty `CLAUDE_CONFIG_DIR` profiles?

The probe produces an authentication-access decision only. It does not certify
model execution, role capability, or indefinite credential availability.

## Safety boundary

Run `scripts/claude-service-auth-spike.py` on the same host and under the same
service identity that would launch Claude. The script:

- runs `claude auth status --json` with no prompt, no TTY, and standard input
  closed;
- probes the inherited configuration first;
- creates two distinct, initially empty temporary configuration directories;
- sets `CLAUDE_CONFIG_DIR` separately for each clean profile and never copies
  files, Keychain items, cookies, or authentication material into either one;
- removes direct provider authentication variables from all three child
  environments so the result measures CLI profile/Keychain access;
- deletes both temporary directories after the probe; and
- emits only classifications, reason codes, exit codes, and output hashes.

Provider stdout and stderr are classified in memory and discarded. The report
never contains a credential, token value, provider message, configuration path,
raw prompt, or raw provider output. A live report is therefore suitable as a
public-safe diagnostic artifact, but it is not governance completion evidence.

## Run

```bash
python3 scripts/claude-service-auth-spike.py
```

Use `--claude` for an explicit executable. Without it, the probe honors
`CLAUDE_BIN` and then searches `PATH`. Use `--timeout` to change the per-profile
timeout. Exit status is `0` for `unattended-safe`, `2` for `interactive-only`,
and `3` for `reject`.

Do not redirect provider diagnostics separately or add verbose shell tracing
around this command. The script's JSON report is the only intended output.

## Profile classifications

| Classification | Meaning |
| --- | --- |
| `authenticated` | `auth status` returned a recognized authenticated JSON state without interaction. |
| `configuration_mismatch` | The installed CLI rejected the status command or reported an invalid/unsupported configuration contract. |
| `keychain_acl_or_gui_prompt` | The CLI reported a Keychain/GUI interaction condition, or the noninteractive status call timed out and may be waiting on one. |
| `unauthenticated_profile` | The profile explicitly reported that it is not authenticated. |
| `provider_cli_failure` | The executable was unavailable, failed to launch, returned an unrecognized successful response, or failed without a more specific classification. |

Timeouts deliberately fail closed into the Keychain/GUI category. A timeout
cannot prove unattended access, and this probe does not inspect GUI state to
disambiguate a blocked permission dialog from another hang.

## Decision matrix

The aggregate report uses exactly one of these decisions:

| Decision | Required observations | Service consequence |
| --- | --- | --- |
| `unattended-safe` | Inherited, clean-1, and clean-2 are all `authenticated`. | Authentication access is safe for an unattended trial on this host identity. Re-probe after CLI, macOS, identity, or Keychain ACL changes. |
| `interactive-only` | Any profile needs Keychain/GUI interaction, or the inherited profile works while either clean profile is unauthenticated. | Require an operator-owned login/permission flow or Desktop handoff. Do not schedule the profile unattended. |
| `reject` | Any configuration mismatch or provider CLI failure, the inherited profile is unauthenticated, the three-profile set is incomplete, or a state is unclassified. | Reject Claude subscription service launch until the underlying CLI/configuration/authentication problem is resolved and the full probe passes. |

Fatal configuration and provider failures take precedence over an
`interactive-only` observation. This prevents one apparent permission prompt
from hiding an incompatible or broken CLI result in another profile.

## Interpreting the result

An `unattended-safe` result demonstrates only that the status command could
access authentication three times under the current process identity. It does
not authorize copying a Claude home or Keychain entry, and it does not make the
CLI Agent Service a governance authority.

An `interactive-only` result is expected when clean profiles require an
operator login or macOS requires approval for the service identity. Complete
that interaction outside the daemon, then rerun the full three-profile probe.

A `reject` result must remain a rejection. Do not fall back to a different
account, copy authentication state from the inherited profile, or treat
`claude --version` as authentication evidence.
