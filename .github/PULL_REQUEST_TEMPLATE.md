<!--
Two things decide whether this gets read carefully: which criterion it serves, and what you
executed. "All tests pass" is not evidence of either. Paste printed output, not a description of it.
Delete this comment.
-->

**Serves:** `REQ-…` / `AC-…` / bug #( )
**Does not serve any criterion because:** (only for a chore/refactor — say why it is still worth the diff)

## What changed, in one paragraph

## What was executed

| Command | Result, as printed |
|---|---|
| `make test` | |
| `make lint` | |
| <the gate for this area, e.g. `make ledger-mutations`> | |

<!--
Fill the third row with the gate that grades what you touched, not with `make test` again.
If you changed a check, run its mutation and report the count it caught:
`RESULT: MUTATION CAUGHT (n of m cases failed)`. A mutation you could not make fail is the finding
worth writing up — say so plainly and the review gets shorter, not longer.
-->

## Claim labels

- [ ] Every number here is **measured** — I ran it and pasted what came out
- [ ] Anything **projected** (an estimate) is labelled as one
- [ ] Anything **gated** (CI will run it, I could not — Windows, hosted CI) is labelled as one
- [ ] Literals quoted from files were re-read from disk in this session, not from memory

## Freedom Constraints

- [ ] No new dependency — or: FC-1 grades it, and `make licenses` is in the table above
- [ ] No new network call in the analysis path (FC-4); `make airgap` still exits 0
- [ ] No model without an open-weights manifest and per-group error rates (FC-3)
- [ ] Relaxing an FC requires a **Rejection of FC** section below; without it, the constraint stands

### Rejection of FC

*(Only if applicable: which constraint, why the goal survives without it.)*

## Docs kept in step

- [ ] `README.md` — user-visible behaviour, quick start, Known limitations
- [ ] `docs/goal-spec.md` — §6 status, §9 milestones, §13 decision log for anything interpreted
- [ ] `docs/architecture.md` / `docs/api.md` / `docs/security.md` — design, surface, controls
- [ ] `CHANGELOG.md` — if a user would notice
- [ ] `TODO.md` — the checkbox this PR closes

<!--
The hygiene test will fail a doc that names an interpreter path or an OS prompt specific to your
machine, and a Makefile whose install set disagrees with pyproject.toml and CI. That test exists
because a clean clone once shipped fourteen red tests for exactly that reason.
-->

## Schema

- [ ] No migration needed, **or** `synthverify db-upgrade` applied against a database that already had
      rows, on **both** dialects, with `make test` green afterwards
- [ ] The audit ledger still verifies after the upgrade (`./.venv/bin/python -m synthverify.cli audit-verify`)

## Notes for the reviewer

<!-- Where you looked and did not change something, and why. This is the most useful section. -->
