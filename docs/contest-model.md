# H2H and tournament model (schema 3)

## Contest identity and migration

`initialize_database` upgrades old databases additively. Existing `contests`,
`member_results`, `lineup_players`, season/member IDs, and prizes are retained.
Old contests receive `contest_type=LEGACY`; historical fees and net remain NULL.
No historical $3/$5 fee or contest type is invented.

New internal contests use `contests.contest_type=KCDK_H2H`. External tournaments
live in `tournaments`, with a fixed `contest_type=TOURNAMENT`. IDs in those two
tables are separate namespaces. Each allows one contest per season/week label,
so an H2H contest and a tournament can coexist without sharing result rows.

New tables:

* `tournaments`: metadata, field size, separate results and payout fingerprints.
* `tournament_payouts`: validated place ranges and integer gross payout cents.
* `tournament_entries`: member, EntryId, original EntryName, base username, source
  rank, normalized points, field size, percentile, gross, fee, net, original lineup.
* `tournament_lineup_players`: entry foreign key, player, slot, CPT/FLEX role,
  role-specific points and field ownership. The same player can occur in separate
  entries; duplicate players within one entry are not stored twice.

Aggregates are calculated from entries, never stored as another mutable copy.
Configured members are synchronized; external entrants never become members.

## Economics and aggregation

H2H has one $5 entry per participating active member. All source entrants must
match configured active members. Duplicate members, entry-count suffixes, missing
IDs/scores/ranks and tied top scores are rejected. Nonparticipating members are
not charged. Set `h2h_dk_fee_cents` to the actual total DraftKings fee (not a rate).
It must be nonnegative and cannot exceed the entrant pool. There is no assumed
rake. Winner gross is `entrants * 500 - fee`; every other gross is zero. Each net
is gross minus 500. A tied top score needs an explicit future payout policy.

Tournament entry fees are always 300 cents. Every member entry is retained and
charged. Entry net is gross minus 300; aggregate net is total gross minus all
entry fees. Score, percentile and rank averages weight each entry equally. Cash
rate is cashing entries / total entries, multiplied by 100; cash means gross > 0.
Participation counts distinct tournaments. Fees are always known. If any payout
is unknown, aggregate gross, net and cash rate stay unknown rather than reporting
a partial total as complete; known-prize-entry counts remain available.

Tournament standings order by net descending, average points descending, average
percentile descending, then display name. Gross remains a separate display field.
Only tournament participants get rows. The renderer shows entries, gross, fees,
net, average points/percentile, best rank and cash rate. Negative amounts use
`-$6.00`; unknown amounts use an em dash/unknown label.

## Payout formats

Use the real DK alternating one-column CSV format (ordinal place or range followed
by its dollar amount). Quote fields containing commas:

```csv
1st
"$1,000"
2nd - 3rd
$5.50
```

Or use normalized integer cents:

```csv
place_from,place_to,payout_cents
1,1,100000
2,3,550
```

Ranks outside paid tiers receive known zero when a valid file is provided.
Missing payout files leave gross/net unknown. Empty, overlapping, duplicate,
negative, reversed, nonfinite or out-of-field tiers are rejected. Dollar amounts
use Decimal half-up rounding to cents; normalized cents must be integers.
Matching uses the authoritative source rank exactly as requested, including ties;
the importer does not invent a prize-pooling/splitting rule across tied tiers.

## Names, scores and lineup metadata

Tournament matching strips only a terminal ` (positive_integer/positive_integer)`
suffix, then trims whitespace and case-folds. No fuzzy matching. EntryName stays
unchanged and EntryId is unique within its tournament. Source rank stays
authoritative. Scores normalize to two decimals using Decimal half-up before
comparisons, eliminating source float artifacts.

Metadata is keyed by normalized player name and roster position. CPT never
overwrites FLEX. Classic RB/WR/TE metadata can enrich FLEX only when there is one
unambiguous non-CPT candidate. Player identity still relies on names, not DK
player IDs; distinct players with identical names need a future identity adapter.

## APIs and legacy compatibility

* `import_h2h(..., dk_fee_cents=actual_fee, ...)`: explicit single-entry H2H.
* `import_tournament(..., payout_path=path, ...)`: entry-level external tournament.
* `tournament_entries(...)`: entry details.
* `tournament_aggregates(..., week_label=...)`: one aggregate per participating
  member for a tournament; omit week_label for season aggregates.
* `generate_tournament_facts(...)`: external results/economics facts without H2H
  winners, last places or unanimity.

`import_week` remains a legacy compatibility API by default. New CLI/runner flows
use the explicit H2H model. Existing internal season standings, streaks, movement,
and lineup-history analytics still read the original internal tables. Tournament
entry selections are not mixed into internal weekly player-usage denominators.

Legacy-only seasons retain their historical gross-based tournament *view*, since
entry fees and contest types were never recorded. Once a season has typed H2H or
tournament data, its tournament leaderboard uses only explicit tournament entries;
legacy rows are not silently converted into $3 tournament entries. The separately
named `legacy_tournament_performance_leaderboard` remains available to inspect old
semantics. Backfilling actual tournament history requires original source files.

## Idempotency and replacement

Results SHA-256 is scoped by season within each contest table; payout SHA-256 is
tracked separately. A tournament is unchanged only when both hashes match.
Adding/removing/changing payout data requires explicit `replace=True` if the
tournament exists. Identical sources are no-ops even with replace enabled.
Results already assigned to another week cannot be moved by replacement.

Replacement changes only that contest and dependent entries/selections/tiers,
preserving its ID and sibling contest. Nested savepoints make combined H2H and
tournament import atomic. Validation failure rolls back both. Changing H2H fee
configuration also requires explicit replacement. The interactive runner never
replaces implicitly. Database replacement does not rewrite previously published
Discord recaps; review publication/repost state separately.

## Runner and offline validation

The picker first selects H2H results, then offers tournament results and payout
selection. With explicit paths:

```powershell
python -m kcdk weekly-runner --csv path\h2h.csv `
  --tournament-csv path\tournament.csv --tournament-payout path\payout.csv `
  --week-number 1 --config path\local_config.json --dry-run
```

Local config requires the real member CSV and `h2h_dk_fee_cents`. Tournament inputs
are optional. If results are supplied without payouts, explicitly pass
`--allow-unknown-tournament-payouts`. Nonparticipants are not reported as missing
tournament members. Preflight shows separate types, entry counts, tiers and money.

Runner dry runs use temporary database copies and temporary leaderboard PNGs,
with no OpenAI or Discord calls. First-use config creation is a local write.
The scriptable `weekly` command requires `--h2h-dk-fee-cents`; its `--dry-run`
still imports into its selected database, so choose an isolated database for tests.

Repeat the real-file assertions with no production configuration required:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_tournament.py
```

The script creates an isolated bschobs-only member CSV and database, verifies both
real entries and all 12 selections, payouts, fees/net, idempotency, facts and a
one-row PNG, and checks that source hashes did not change. All temporary files
are removed. Add `--output-dir output\smoke\tournament` to retain only the PNG
and JSON report. No API calls or publishing occur.
