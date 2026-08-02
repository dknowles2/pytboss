# Reviewing changes to pytboss

`pytboss` is the protocol layer. It is consumed by
[ha-pitboss](https://github.com/dknowles2/ha-pitboss), the Home Assistant
integration, which pins it in `requirements.txt`. That repo has its own
`REVIEW.md` describing the same boundary from the other side.

See [AGENTS.md](AGENTS.md) for environment setup, generated-file rules, and
the checks to run before committing. This document is about *where* a change
belongs, not how to make it.

## What lives here

Everything between the grill and a parsed Python value:

- BLE and WebSocket transports, the Mongoose OS RPC framing, the password codec
- Status and temperature frame parsing (`FE0B` / `FE0C`)
- Command construction
- Which models exist, which control board each ships with, and what each
  supports — `pytboss/grills.json`
- The typed surface over that data: `Grill`, `ControlBoard`, `Command`

Nothing here should know that Home Assistant exists.

## What does not

Entity naming, units of measure, device registry entries, config flows,
polling intervals, and anything phrased as "in Home Assistant it should…".
Those belong downstream.

## Deciding which repo a bug belongs to

| Symptom | Repo |
| --- | --- |
| A model is missing, or can't be picked during setup | pytboss — definitions |
| Temperatures are wrong, doubled, or shifted by a constant | pytboss — board parsing |
| A command does nothing, or errors | pytboss |
| A field is absent from state for one board only | pytboss |
| An entity is missing, misnamed, or in the wrong unit | ha-pitboss |
| Setup UX, options flow, reauth | ha-pitboss |

The useful question is: *would this still be wrong if the caller weren't Home
Assistant?* If yes, it belongs here.

## The rule this repo exists to enforce

**Control board differences are resolved here, never by remapping a board
downstream.**

Two real cases:

- ha-pitboss#258 proposed aliasing the BLE prefix `LBL` to the board `PBL`, so
  those grills could be selected during setup. It would have worked as far as
  the config flow, then read `LBL` frames at `PBL` offsets — the frames are
  three bytes shorter, having no smoker field, which the vendor firmware's
  `powerStatusPos` table confirms (21 for `LBL`/`LFS`, 24 for `PBL`).
- ha-pitboss carried `if control_board == "PBL2": control_board = "PBL3"` for
  the same reason. `PBL3` applies a fahrenheit-to-celsius conversion that
  `PBL2` has commented out, so a `PBL2` grill set to celsius was converted
  twice — 225°F displayed as 107.

Both were unblocking a model by pretending it had a different board. If a
proposed fix renames, aliases, or maps one control board onto another outside
this repo, it is in the wrong repo.

## grills.json is generated — never hand-edit it

`scripts/dump_grills.py` pulls definitions from the Dansons API (credentials in
`~/.pitboss`; CI uses the `PITBOSS_USERNAME` / `PITBOSS_PASSWORD` secrets).
`.github/workflows/update-grills.yml` runs it weekly, Thursdays at 00:00 UTC,
and opens a PR.

- To change **what is stored**, edit the field sets in `dump_grills.py`
  (`_DROPPED_FIELDS`, `_DROPPED_GRILL_FIELDS`, `_DROPPED_COMMAND_FIELDS`) and
  regenerate. Do not edit the JSON directly; the next refresh overwrites it.
- To change **how a value is interpreted**, do it in `Grill.from_dict` /
  `ControlBoard.from_dict` / `Command.from_dict` rather than reshaping the
  dump. `has_lights` is the pattern: the vendor's `lights` count is stored as
  reported and turned into a `bool` at parse time.
- Fields the typed classes don't name are still reachable through the untyped
  `Grill.json`. Before dropping one, grep **both repos** — `image_url` looks
  like storefront data but `scripts/update_readme.py` reads it.

Review the automated PR rather than rubber-stamping it. Newly added models
sometimes ship parsing routines that fail the suite, which is why the PR body
lists added and removed models.

## Don't rewrite the vendor's JavaScript

The parsing routines in `grills.json` are the control board firmware's own
logic, executed via `dukpy`. Keeping them verbatim is what preserves parity.

When a board's routine reads fields its frames don't carry, **filter after
parsing** — `DROPPED_STATUS_FIELDS` and `DROPPED_TEMPERATURE_FIELDS` in
`grills.py`, keyed by board name. Editing the JS was tried and abandoned: it is
brittle in ways that fail silently, and CRLF line endings in the vendor data
once made a regex match nothing at all while appearing to work.

`_COMMAND_SLUG_OVERRIDES` is the same idea for a vendor typo
(`set-prove-1-temperature`), corrected on the way in rather than in the data.

## Models served on two control boards

Seven models are sold on two board generations. `grills.json` keeps both, the
older one under a shadow key of the form `NAME (BOARD)`.

- `get_grills()` with no filter yields each model **once**, so callers listing
  models for a picker don't show duplicates.
- `get_grills(board)` yields that board's set.
- `get_grill(name, control_board=...)` resolves a specific pairing.

`PitBoss(..., control_board=...)` lets a caller that knows the board — it is
the prefix a grill advertises over Bluetooth — pin the right one. Omitting it
selects whichever the vendor lists most recently, which is not always correct.
Tests must cover every model/board pairing, not one entry per model; that is
what `all_variants()` in `tests/test_grills.py` is for.

## Releasing

Cutting a release is what propagates a change downstream:

1. Merge here and publish. Label the PR — `release-drafter` categorises notes
   by label. `breaking` deserves care: the dataclasses in `grills.py` are
   public, so removing an attribute or reordering a constructor is a breaking
   change even when nothing in either repo used it.
2. ha-pitboss picks the new version up via Dependabot, or a manual bump.
3. That bump triggers `update-grill-docs.yml` there, which regenerates
   `docs/SUPPORTED_GRILLS.md` automatically.

So a change to which models are supported reaches users' documentation only
after a release. Don't hand-edit the downstream doc to compensate.
