"""External tournaments: entry-level persistence and entry-weighted aggregates.

Tournament IDs are a separate namespace from legacy/H2H contest IDs. No external
entrant is created as a member. Prize tiers apply to the authoritative source rank.
"""
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from .dk_import import base_username, contest_result_rows, import_contest, percentile_from_rank
from .lineups import parse_member_lineup, player_key, player_metadata
from .members import load_members
from .payouts import load_payouts, payout_for_rank

TOURNAMENT_ENTRY_FEE_CENTS = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS tournaments (
 id INTEGER PRIMARY KEY,
 season_id INTEGER NOT NULL REFERENCES seasons(id),
 contest_type TEXT NOT NULL DEFAULT 'TOURNAMENT' CHECK(contest_type='TOURNAMENT'),
 week_label TEXT NOT NULL, week_number INTEGER,
 draftkings_contest_id TEXT, contest_name TEXT, contest_date TEXT,
 field_size INTEGER NOT NULL CHECK(field_size>0),
 source_filename TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
 payout_filename TEXT, payout_fingerprint TEXT,
 imported_at TEXT NOT NULL,
 UNIQUE(season_id,week_label), UNIQUE(season_id,source_fingerprint)
);
CREATE TABLE IF NOT EXISTS tournament_payouts (
 tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
 place_from INTEGER NOT NULL CHECK(place_from>0),
 place_to INTEGER NOT NULL CHECK(place_to>=place_from),
 payout_cents INTEGER NOT NULL CHECK(payout_cents>=0),
 PRIMARY KEY(tournament_id,place_from)
);
CREATE TABLE IF NOT EXISTS tournament_entries (
 id INTEGER PRIMARY KEY,
 tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
 member_id INTEGER NOT NULL REFERENCES members(id),
 entry_id TEXT NOT NULL, entry_name TEXT NOT NULL, base_username TEXT NOT NULL,
 rank INTEGER NOT NULL CHECK(rank>0), points REAL NOT NULL,
 field_size INTEGER NOT NULL CHECK(field_size>0), percentile REAL NOT NULL,
 gross_winnings_cents INTEGER CHECK(gross_winnings_cents>=0),
 entry_fee_cents INTEGER NOT NULL DEFAULT 300 CHECK(entry_fee_cents=300),
 net_profit_cents INTEGER, lineup TEXT,
 CHECK((gross_winnings_cents IS NULL AND net_profit_cents IS NULL) OR
       (gross_winnings_cents IS NOT NULL AND net_profit_cents IS NOT NULL
        AND net_profit_cents=gross_winnings_cents-entry_fee_cents)),
 UNIQUE(tournament_id,entry_id)
);
CREATE TABLE IF NOT EXISTS tournament_lineup_players (
 entry_pk INTEGER NOT NULL REFERENCES tournament_entries(id) ON DELETE CASCADE,
 player_id INTEGER NOT NULL REFERENCES players(id),
 roster_slot INTEGER NOT NULL, roster_position TEXT NOT NULL,
 player_fantasy_points REAL, ownership_percentage REAL,
 PRIMARY KEY(entry_pk,roster_slot), UNIQUE(entry_pk,player_id)
);
CREATE INDEX IF NOT EXISTS idx_tournament_entry_member ON tournament_entries(member_id,tournament_id);
"""


@dataclass(frozen=True)
class TournamentImportSummary:
    tournament_id: int
    field_size: int
    matched_members: int
    entry_count: int
    lineup_count: int
    payout_tiers: int
    already_imported: bool = False
    warnings: tuple[str, ...] = ()

    def to_dict(self):
        return asdict(self)


def prepare_tournament(csv_path, members_path, payout_path=None):
    """Pure validation/normalization for preflight and persistence."""
    contest = import_contest(Path(csv_path))
    # Repeated ownership-table rows may duplicate entries, but must agree.
    columns = [key for key in ("entry_id", "entry_name", "rank", "points", "lineup") if key in contest]
    if contest[columns].drop_duplicates()["entry_id"].duplicated().any():
        raise ValueError("Conflicting rows for the same tournament EntryId")
    results = contest_result_rows(contest)
    size = len(results)
    if not size or results[["entry_id", "rank", "points", "entry_name"]].isna().any().any():
        raise ValueError("Tournament entries require valid ID, username, rank and points")
    if not results["rank"].between(1, size).all():
        raise ValueError("Tournament rank is outside the field")
    members = load_members(Path(members_path), active_only=False)
    active = members.loc[members.active].copy()
    active["base_username"] = active.draftkings_name.str.strip().str.casefold()
    if active.base_username.duplicated().any() or active.display_name.duplicated().any():
        raise ValueError("Duplicate active member identity")
    results["base_username"] = results.entry_name.map(base_username)
    matched = results.merge(active[["base_username", "display_name"]], on="base_username", validate="many_to_one")
    if matched.empty:
        raise ValueError("Zero active KCDK members matched the tournament")
    tiers = load_payouts(payout_path) if payout_path is not None else None
    if tiers and tiers[-1].place_to > size:
        raise ValueError("Payout tier extends beyond tournament field size")
    matched["gross_winnings_cents"] = pd.array(
        [payout_for_rank(int(rank), tiers) for rank in matched["rank"]], dtype="Int64")
    matched["entry_fee_cents"] = TOURNAMENT_ENTRY_FEE_CENTS
    matched["net_profit_cents"] = matched.gross_winnings_cents - TOURNAMENT_ENTRY_FEE_CENTS
    matched["percentile"] = matched["rank"].map(lambda rank: percentile_from_rank(rank, size))
    return contest, members, matched, tiers, size


def import_tournament(connection, csv_path, members_path, *, season_name,
                      season_identifier, week_label=None, week_number=None,
                      season_year=None, payout_path=None, replace=False,
                      draftkings_contest_id=None, contest_name=None, contest_date=None):
    from .persistence import _ensure_season, _sync_members, _fingerprint, _utc_now, _normalized_name, atomic_import
    label = week_label or (f"Week {week_number}" if week_number is not None else None)
    if not label:
        raise ValueError("Tournament week label or number is required")
    if week_number is not None and (type(week_number) is not int or week_number < 1):
        raise ValueError("Tournament week number must be a positive integer")
    contest, members, matched, tiers, size = prepare_tournament(csv_path, members_path, payout_path)
    fingerprint = _fingerprint(Path(csv_path))
    payout_hash = _fingerprint(Path(payout_path)) if payout_path is not None else None
    metadata = player_metadata(contest)
    warnings = [] if tiers is not None else ["Tournament payouts unknown: no payout source supplied."]
    with atomic_import(connection):
        season_id = _ensure_season(connection, name=season_name, identifier=season_identifier, year=season_year)
        old = connection.execute("SELECT * FROM tournaments WHERE season_id=? AND week_label=?", (season_id, label)).fetchone()
        elsewhere = connection.execute("SELECT id, week_label FROM tournaments WHERE season_id=? AND source_fingerprint=? AND week_label<>?", (season_id, fingerprint, label)).fetchone()
        if elsewhere:
            raise ValueError("Tournament source already belongs to another week")
        if old and old["source_fingerprint"] == fingerprint and old["payout_fingerprint"] == payout_hash:
            count = connection.execute("SELECT COUNT(*) FROM tournament_lineup_players lp JOIN tournament_entries e ON e.id=lp.entry_pk WHERE e.tournament_id=?", (old["id"],)).fetchone()[0]
            return TournamentImportSummary(old["id"], size, matched.base_username.nunique(), len(matched), count, len(tiers or ()), True, tuple(warnings))
        if old and not replace:
            raise ValueError("Tournament results or payout source changed; use replace=True explicitly")
        member_ids = _sync_members(connection, season_id, members)
        if old:
            tournament_id = old["id"]
            connection.execute("DELETE FROM tournament_entries WHERE tournament_id=?", (tournament_id,))
            connection.execute("DELETE FROM tournament_payouts WHERE tournament_id=?", (tournament_id,))
            connection.execute("UPDATE tournaments SET week_number=?, draftkings_contest_id=?, contest_name=?, contest_date=?, field_size=?, source_filename=?, source_fingerprint=?, payout_filename=?, payout_fingerprint=?, imported_at=? WHERE id=?", (week_number, draftkings_contest_id, contest_name, contest_date, size, Path(csv_path).name, fingerprint, Path(payout_path).name if payout_path else None, payout_hash, _utc_now(), tournament_id))
        else:
            tournament_id = connection.execute("INSERT INTO tournaments(season_id,week_label,week_number,draftkings_contest_id,contest_name,contest_date,field_size,source_filename,source_fingerprint,payout_filename,payout_fingerprint,imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (season_id,label,week_number,draftkings_contest_id,contest_name,contest_date,size,Path(csv_path).name,fingerprint,Path(payout_path).name if payout_path else None,payout_hash,_utc_now())).lastrowid
        connection.executemany("INSERT INTO tournament_payouts VALUES (?,?,?,?)", [(tournament_id,t.place_from,t.place_to,t.payout_cents) for t in tiers or ()])
        lineup_count = 0
        for row in matched.to_dict("records"):
            gross = None if pd.isna(row["gross_winnings_cents"]) else int(row["gross_winnings_cents"])
            lineup = None if pd.isna(row.get("lineup")) else row.get("lineup")
            entry_pk = connection.execute("INSERT INTO tournament_entries(tournament_id,member_id,entry_id,entry_name,base_username,rank,points,field_size,percentile,gross_winnings_cents,net_profit_cents,lineup) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (tournament_id,member_ids[_normalized_name(row["base_username"])],str(row["entry_id"]),row["entry_name"],row["base_username"],int(row["rank"]),float(row["points"]),size,float(row["percentile"]),gross,None if gross is None else gross-300,lineup)).lastrowid
            selections = parse_member_lineup(lineup, metadata)
            if not selections:
                warnings.append(f"No parseable lineup for entry {row['entry_id']}")
            for slot, player in enumerate(selections, 1):
                key = player_key(player.player_name)
                connection.execute("INSERT INTO players(player_key,display_name) VALUES (?,?) ON CONFLICT(player_key) DO NOTHING", (key,player.player_name))
                player_id = connection.execute("SELECT id FROM players WHERE player_key=?", (key,)).fetchone()[0]
                connection.execute("INSERT INTO tournament_lineup_players VALUES (?,?,?,?,?,?)", (entry_pk,player_id,slot,player.roster_position,player.fantasy_points,player.ownership_percentage))
                lineup_count += 1
    return TournamentImportSummary(tournament_id,size,matched.base_username.nunique(),len(matched),lineup_count,len(tiers or ()),warnings=tuple(warnings))


def tournament_entries(connection, season_identifier, *, week_label=None, through_week_number=None):
    return pd.read_sql_query("""SELECT e.*, m.display_name, m.member_key, t.week_label, t.week_number
        FROM tournament_entries e JOIN tournaments t ON t.id=e.tournament_id
        JOIN seasons s ON s.id=t.season_id JOIN members m ON m.id=e.member_id
        WHERE s.identifier=? AND (? IS NULL OR t.week_label=?)
        AND (? IS NULL OR t.week_number<=?)""", connection,
        params=[season_identifier,week_label,week_label,through_week_number,through_week_number])


def tournament_aggregates(connection, season_identifier, *, week_label=None, through_week_number=None):
    """One row per member; averages/cash rate weight entries, not tournaments.

    Any unknown payout makes total gross/net and cash rate unknown. Known-entry
    counts remain available; fees are always known.
    """
    entries = tournament_entries(connection,season_identifier,week_label=week_label,through_week_number=through_week_number)
    records = []
    for (member_id,name), group in entries.groupby(["member_id","display_name"]):
        known = group.gross_winnings_cents.notna()
        complete = bool(known.all())
        gross = int(group.gross_winnings_cents.sum()) if complete else None
        fees = len(group)*300
        cashes = int(group.gross_winnings_cents.gt(0).sum()) if complete else None
        records.append(dict(member_id=int(member_id),display_name=name,entry_count=len(group),
            tournaments_played=group.tournament_id.nunique(),weeks_played=group.tournament_id.nunique(),
            known_prize_entries=int(known.sum()),weeks_with_prize_data=int(group.assign(known=known).groupby("tournament_id").known.all().sum()),
            prize_data_complete=complete,total_entry_fees_cents=fees,total_gross_winnings_cents=gross,
            net_profit_cents=None if gross is None else gross-fees,
            total_money_won=None if gross is None else gross/100,
            total_net_profit=None if gross is None else (gross-fees)/100,total_entry_fees=fees/100,
            average_draftkings_fantasy_points=group.points.mean(),highest_draftkings_fantasy_score=group.points.max(),
            average_tournament_percentile=group.percentile.mean(),best_tournament_rank=int(group['rank'].min()),
            worst_tournament_rank=int(group['rank'].max()),average_tournament_rank=group['rank'].mean(),
            cashes=cashes,cash_rate=None if cashes is None else cashes/len(group)*100,
            largest_single_tournament_win=None if not complete else group.gross_winnings_cents.max()/100))
    columns = ["display_name","entry_count","total_net_profit","average_draftkings_fantasy_points","average_tournament_percentile"]
    frame = pd.DataFrame(records) if records else pd.DataFrame(columns=columns)
    frame = frame.sort_values(["total_net_profit","average_draftkings_fantasy_points","average_tournament_percentile","display_name"], ascending=[False,False,False,True],na_position="last").reset_index(drop=True)
    frame.insert(0,"tournament_rank",range(1,len(frame)+1))
    return frame
