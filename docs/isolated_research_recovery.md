# Resume isolated new-data research without rewriting history

Linked goal: AIAC-PPA-QUALIFIED-ALPHA. Implementation: Beads aiac-r1e.12.

The owner-authorized new-data experiment retains old lab run 1 (35 RESULT,
45 UNKNOWN), inspects available account activity after quarantine, and excludes
production and lab historical signatures. It does not require inventing terminal
outcomes for old requests. This differs from resuming an old production run.

## Recovery boundary

An unresolved production source remains blocked unless it is the current STOPPED
run with PPA_HANDOFF_ISOLATED and an audited exclusion record for the same task/run.
The experiment additionally needs fresh account inventory, complete historical
signature exclusions, unchanged frozen input, no active local work, current account
and theme evidence, and its independent lab database. The raw source pending count
is retained in every state report; it is never changed to zero to pass a gate.

The permitted account inventory checks both normal and hidden Alpha windows.
A 405 on the optional activity listing means unavailable visibility, not zero active
simulations. That residual uncertainty is retained under the owner's previously
accepted quarantine plan. Other errors, partial windows, old matching Alpha results,
or observed activity prevent new dispatch. Fresh UNKNOWN stops new requests.

`prepare --pilot-four` freezes exactly one four-case discovery group, preserving
the campaign's unlimited lifetime budget. The pilot stops after four charged slots,
first quality PASS, a rate-limit review or any uncertain request. It cannot expand
into another batch. Continuous research is a separate prepared campaign mode.

## Evidence and operations

Read-only production APIs expose active-run identities and complete historical
signature hashes. Experiments exclude all production history and all other lab
runs. Historical rows, encrypted credentials, old cursors and request positions are
not rewritten. Preparation and each dispatch revalidate history hashes.

The existing CLI remains `python -m scripts.ppa_discovery_campaign` with prepare,
run, next and report subcommands. Use private external artifact directories and
the existing lab environment. Local API calls do not inherit external HTTP proxies.
Provider-generated candidates remain subject to semantic, theme and novelty checks.

If the old manifest was lost from a temporary directory, preparation can use
`--old-lab-run-id 1` instead of `--old-manifest`. This reads all 80 original ledger
positions, retains 35 RESULT and 45 UNKNOWN, and writes a new explicitly labeled
ledger snapshot alongside the campaign. It retains the original manifest's hash
as a reference, not as a claim that the old file bytes were recovered. No old
candidate is reissued. Store new artifacts outside temporary directories.

Offline and CI checks establish execution safety only. Report actual WQ request
states, Alpha IDs, metrics, quality PASS and final PPA readiness. Do not claim an
effect improvement from a healthy service, a successful CI run or a completed chat.
No Alpha submission or old-run restart is authorized by this software change.
