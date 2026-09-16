from pathlib import Path
import json
import sqlite3

import pandas as pd
import pytest

from kcdk.dk_import import base_username, import_contest
from kcdk.facts import generate_tournament_facts, build_weekly_fact_report
from kcdk.lineups import parse_member_lineup, player_metadata
from kcdk.payouts import load_payouts, payout_for_rank
from kcdk.persistence import connect_database, import_h2h, initialize_database
from kcdk.tournaments import import_tournament, tournament_aggregates, tournament_entries
from kcdk.analytics import season_leaderboard, tournament_performance_leaderboard
from kcdk.leaderboard_graphics import render_tournament_performance_png, format_currency
from kcdk.weekly_runner import LocalWeeklyConfig, build_preflight, run_dry_run


@pytest.fixture
def sources(tmp_path):
    members=tmp_path/'members.csv'
    members.write_text('draftkings_name,display_name,active\nbschobs,Brian,true\nother,Other,true\nabsent,Absent,true\n')
    tournament=tmp_path/'tournament.csv'
    tournament.write_text('Rank,EntryId,EntryName,Points,Lineup,Player,Roster Position,%Drafted,FPTS\n'
        '1,101,outsider,101.020004,CPT Alpha FLEX Beta,Alpha,FLEX,50%,10\n'
        '2,102,bschobs (1/2),80.000004,CPT Alpha FLEX Beta,Alpha,CPT,10%,15\n'
        '3,103,bschobs (2/2),70,CPT Beta FLEX Alpha,Beta,FLEX,60%,20\n'
        '4,104,bschobsSimilar,60,CPT Alpha FLEX Beta,Beta,CPT,20%,30\n')
    payout=tmp_path/'payout.csv';payout.write_text('place_from,place_to,payout_cents\n1,1,1000\n')
    h2h=tmp_path/'h2h.csv';h2h.write_text('Rank,EntryId,EntryName,Points,Lineup\n1,201,other,90,QB Alpha\n2,202,bschobs,80,QB Beta\n')
    return members,tournament,payout,h2h


def ingest(db,sources,**kwargs):
    return import_tournament(db,sources[1],sources[0],season_name='Test',season_identifier='test',week_number=1,payout_path=kwargs.pop('payout_path',sources[2]),**kwargs)


@pytest.mark.parametrize('raw,expected',[
    ('bschobs (1/2)','bschobs'),(' BSCHOBS (2/2) ','bschobs'),
    ('bschobs(1/2)','bschobs(1/2)'),('bschobs (1/2) x','bschobs (1/2) x'),
    ('a(b)12','a(b)12'),('bschobs2','bschobs2'),('bschobs (0/2)','bschobs (0/2)')])
def test_suffix_exact(raw,expected):
    assert base_username(raw)==expected


def test_entries_economics_roles_idempotency_and_isolation(sources,tmp_path):
    db=connect_database(':memory:')
    h=import_h2h(db,sources[3],sources[0],dk_fee_cents=100,season_name='Test',season_identifier='test',week_number=1)
    h_id=db.execute('SELECT id FROM contests').fetchone()[0]
    summary=ingest(db,sources)
    assert summary.entry_count==2 and summary.matched_members==1 and summary.lineup_count==4
    entries=tournament_entries(db,'test')
    assert entries.entry_id.tolist()==['102','103']
    assert entries['rank'].tolist()==[2,3]
    assert entries.entry_fee_cents.tolist()==[300,300]
    assert entries.gross_winnings_cents.tolist()==[0,0]
    assert entries.net_profit_cents.tolist()==[-300,-300]
    assert entries.entry_name.tolist()==['bschobs (1/2)','bschobs (2/2)']
    aggregate=tournament_aggregates(db,'test').iloc[0]
    assert aggregate.entry_count==2 and aggregate.total_entry_fees_cents==600
    assert aggregate.net_profit_cents==-600 and aggregate.cashes==0 and aggregate.cash_rate==0
    assert aggregate.average_draftkings_fantasy_points==75
    assert aggregate.best_tournament_rank==2 and aggregate.worst_tournament_rank==3
    assert len(tournament_performance_leaderboard(db,'test'))==1
    assert len(season_leaderboard(db,'test'))==2
    rows=db.execute('SELECT entry_fee_cents,prize_cents,net_profit_cents FROM member_results ORDER BY kcdk_finish').fetchall()
    assert [tuple(r) for r in rows]==[(500,900,400),(500,0,-500)]
    assert ingest(db,sources).already_imported
    assert ingest(db,sources,replace=True).already_imported
    assert db.execute('SELECT COUNT(*) FROM tournament_lineup_players').fetchone()[0]==4
    # Independently changed payout hash requires an explicit scoped replacement.
    sources[2].write_text('place_from,place_to,payout_cents\n1,2,1000\n')
    with pytest.raises(ValueError,match='changed'):
        ingest(db,sources)
    ingest(db,sources,replace=True)
    assert db.execute('SELECT id FROM contests').fetchone()[0]==h_id
    assert db.execute('SELECT COUNT(*) FROM member_results').fetchone()[0]==2
    assert tournament_aggregates(db,'test').iloc[0].net_profit_cents==400
    assert db.execute('SELECT COUNT(*) FROM tournament_entries').fetchone()[0]==2
    png=tmp_path/'one.png'
    image=render_tournament_performance_png(tournament_aggregates(db,'test'),png,season_label='Test',week_label='Week 1',weeks_completed=1)
    assert (image.width,image.height)==(1400,478)
    assert format_currency(-6)=='-$6.00'
    assert not db.execute("SELECT 1 FROM members WHERE draftkings_name LIKE 'outsider%'").fetchone()
    db.close()


def test_missing_payout_unknown_and_role_metadata(sources):
    db=connect_database(':memory:');ingest(db,sources,payout_path=None)
    row=tournament_aggregates(db,'test').iloc[0]
    assert pd.isna(row.total_net_profit) and pd.isna(row.total_money_won) and pd.isna(row.cash_rate)
    assert not row.prize_data_complete and row.total_entry_fees_cents==600
    contest=import_contest(sources[1]);metadata=player_metadata(contest)
    assert metadata[('alpha','CPT')]==(15,10)
    assert metadata[('alpha','FLEX')]==(10,50)
    selections=parse_member_lineup('CPT Alpha FLEX Beta',metadata)
    assert sum(p.fantasy_points for p in selections)==35
    assert contest.iloc[0].points==101.02


@pytest.mark.parametrize('content',[
    'place_from,place_to,payout_cents\n1,3,100\n3,4,50\n',
    'place_from,place_to,payout_cents\n1,1,100\n1,1,100\n',
    'place_from,place_to,payout_cents\n3,1,100\n',
    '1st\n-$5\n','1st\nNaN\n','1st\nInfinity\n',
    'place_from,place_to,payout_cents\n0,1,100\n',
    'place_from,place_to,payout_cents\n1,1,-100\n'])
def test_payout_rejects_invalid(tmp_path,content):
    path=tmp_path/'p.csv';path.write_text(content)
    with pytest.raises(ValueError):load_payouts(path)


def test_payout_alternating_ranges_decimal(tmp_path):
    path=tmp_path/'p.csv';path.write_text('1st\n"$1,000.005"\n2nd - 3rd\n$5.50\n')
    tiers=load_payouts(path)
    assert payout_for_rank(1,tiers)==100001
    assert payout_for_rank(3,tiers)==550
    assert payout_for_rank(4,tiers)==0
    assert payout_for_rank(4,None) is None


@pytest.mark.parametrize('rows',[
    '1,1,bschobs (1/2),90\n2,2,bschobs (2/2),80\n',
    '1,1,bschobs,90\n2,2,bschobs,80\n',
    '1,1,bschobs,90\n2,2,outsider,80\n',
    '1,1,bschobs,90.000004\n1,2,other,90\n'])
def test_h2h_rejects_multi_outsider_or_top_tie(sources,rows):
    sources[3].write_text('Rank,EntryId,EntryName,Points\n'+rows)
    db=connect_database(':memory:')
    with pytest.raises(ValueError):
        import_h2h(db,sources[3],sources[0],dk_fee_cents=100,season_name='Test',season_identifier='test',week_number=1)
    assert db.execute('SELECT COUNT(*) FROM contests').fetchone()[0]==0


def test_tournament_facts_have_no_internal_awards(sources):
    db=connect_database(':memory:');ingest(db,sources)
    facts=generate_tournament_facts(db,'test','Week 1')
    assert {f.subject_member_name for f in facts}=={'Brian'}
    assert {'tournament_economics','tournament_entries','tournament_finishes'} <= {f.fact_type for f in facts}
    assert not {'weekly_winner','weekly_last_place','unanimous_player'} & {f.fact_type for f in facts}
    import_h2h(db,sources[3],sources[0],dk_fee_cents=100,season_name='Test',season_identifier='test',week_number=1)
    report=build_weekly_fact_report(db,'test')
    assert any(f.fact_type=='tournament_economics' for f in report.candidate_facts)
    assert not any(f.fact_type=='winner_tournament_result' for f in report.candidate_facts)


def test_three_file_runner_isolated(sources,tmp_path,monkeypatch):
    from kcdk import publishing
    monkeypatch.setattr(publishing,'generate_weekly_commentary',lambda *a,**kw:pytest.fail('OpenAI called'))
    config=LocalWeeklyConfig('test','Test',members_path=str(sources[0]),database_path=str(tmp_path/'production.sqlite'),state_path=str(tmp_path/'production.json'),h2h_dk_fee_cents=100,drunk_bot_enabled=False)
    preflight=build_preflight(csv_path=sources[3],config=config,week_number=1,dry_run=True,tournament_csv_path=sources[1],tournament_payout_path=sources[2])
    assert preflight.can_continue, preflight.blocking_errors
    assert any('2 KCDK entries' in line for line in preflight.tournament_details)
    assert not any('missing' in line.lower() for line in preflight.tournament_details)
    result=run_dry_run(csv_path=sources[3],config=config,week_number=1,tournament_csv_path=sources[1],tournament_payout_path=sources[2])
    assert result.import_summary.matched_kcdk_members==2
    assert not Path(config.database_path).exists() and not Path(config.state_path).exists()


def test_net_primary_sort_not_gross():
    from kcdk.analytics import sort_tournament_leaderboard
    frame=pd.DataFrame(dict(display_name=['A','B'],total_money_won=[10,5],total_net_profit=[-20,2],average_draftkings_fantasy_points=[100,50],average_tournament_percentile=[90,50]))
    assert sort_tournament_leaderboard(frame).display_name.tolist()==['B','A']


def test_migration_preserves_legacy_rows(sources):
    from kcdk.persistence import import_week
    db=connect_database(':memory:')
    import_week(db,sources[3],sources[0],season_name='Test',season_identifier='test',week_number=1)
    before=[tuple(r) for r in db.execute('SELECT * FROM member_results')]
    initialize_database(db)
    assert before==[tuple(r) for r in db.execute('SELECT * FROM member_results')]
    assert db.execute('SELECT contest_type FROM contests').fetchone()[0]=='LEGACY'
    assert db.execute('SELECT MAX(version) FROM schema_versions').fetchone()[0]==3


def test_atomic_combined_import_rolls_back_tournament_on_h2h_conflict(sources,tmp_path):
    from kcdk.publishing import run_weekly_workflow
    db=connect_database(':memory:')
    import_h2h(db,sources[3],sources[0],dk_fee_cents=100,season_name='Test',season_identifier='test',week_number=1)
    sources[3].write_text(sources[3].read_text().replace(',90,',',91,'))
    with pytest.raises(ValueError,match='different source'):
        run_weekly_workflow(db,csv_path=sources[3],members_path=sources[0],season_name='Test',
            season_identifier='test',week_label='Week 1',week_number=1,strict_h2h=True,h2h_dk_fee_cents=100,
            tournament_csv_path=sources[1],tournament_payout_path=sources[2],dry_run=True)
    assert db.execute('SELECT COUNT(*) FROM tournaments').fetchone()[0]==0
    assert db.execute('SELECT draftkings_fantasy_points FROM member_results WHERE kcdk_finish=1').fetchone()[0]==90


def test_h2h_replace_preserves_tournament_and_stable_id(sources):
    db=connect_database(':memory:');ingest(db,sources)
    kwargs=dict(dk_fee_cents=100,season_name='Test',season_identifier='test',week_number=1)
    import_h2h(db,sources[3],sources[0],**kwargs)
    old_id=db.execute('SELECT id FROM contests').fetchone()[0]
    assert import_h2h(db,sources[3],sources[0],**kwargs).already_imported
    kwargs['dk_fee_cents']=200
    with pytest.raises(ValueError):import_h2h(db,sources[3],sources[0],**kwargs)
    import_h2h(db,sources[3],sources[0],replace=True,**kwargs)
    assert db.execute('SELECT id FROM contests').fetchone()[0]==old_id
    assert db.execute('SELECT prize_cents FROM member_results WHERE kcdk_finish=1').fetchone()[0]==800
    assert len(tournament_entries(db,'test'))==2


def test_changed_results_replacement_and_wrong_week_rejection(sources):
    db=connect_database(':memory:');summary=ingest(db,sources)
    with pytest.raises(ValueError,match='another week'):
        import_tournament(db,sources[1],sources[0],season_name='Test',season_identifier='test',week_number=2,payout_path=sources[2],replace=True)
    sources[1].write_text(sources[1].read_text().replace(',70,',',71,'))
    with pytest.raises(ValueError):ingest(db,sources)
    replacement=ingest(db,sources,replace=True)
    assert replacement.tournament_id==summary.tournament_id
    assert tournament_aggregates(db,'test').iloc[0].average_draftkings_fantasy_points==75.5


def test_partial_history_never_reports_partial_net_as_complete(sources):
    db=connect_database(':memory:');ingest(db,sources)
    sources[1].write_text(sources[1].read_text().replace(',70,',',71,'))
    import_tournament(db,sources[1],sources[0],season_name='Test',season_identifier='test',week_number=2)
    row=tournament_aggregates(db,'test').iloc[0]
    assert row.entry_count==4 and row.tournaments_played==2 and row.total_entry_fees_cents==1200
    assert row.known_prize_entries==2 and pd.isna(row.net_profit_cents) and pd.isna(row.cash_rate)


def test_payout_requires_explicit_unknown_choice(sources,tmp_path):
    config=LocalWeeklyConfig('test','Test',members_path=str(sources[0]),database_path=str(tmp_path/'new.sqlite'),h2h_dk_fee_cents=100,drunk_bot_enabled=False)
    args=dict(csv_path=sources[3],config=config,week_number=1,dry_run=True,tournament_csv_path=sources[1])
    assert not build_preflight(**args).can_continue
    assert build_preflight(**args,allow_unknown_tournament_payouts=True).can_continue


def test_source_rank_authoritative_and_equivalent_scores_tie(sources):
    from kcdk.dk_import import weekly_standings
    from kcdk.members import load_members
    sources[1].write_text(sources[1].read_text().replace(',70,',',80.000001,'))
    db=connect_database(':memory:');ingest(db,sources)
    frame=tournament_entries(db,'test')
    assert frame.points.tolist()==[80,80] and frame['rank'].tolist()==[2,3]
    data=import_contest(sources[1]);data['entry_name']=['other','bschobs','absent','nonmember']
    standings=weekly_standings(data,load_members(sources[0]))
    assert standings.loc[standings.points.eq(80),'kcdk_rank'].tolist()==[2,2]


def test_populated_v2_database_additive_upgrade(sources):
    from kcdk.persistence import SCHEMA, import_week
    populated=connect_database(':memory:')
    import_week(populated,sources[3],sources[0],season_name='Test',season_identifier='test',week_number=1)
    old=sqlite3.connect(':memory:');old.row_factory=sqlite3.Row
    old.executescript(SCHEMA)  # The original tables, without v3 additions.
    tables=['seasons','members','season_members','contests','member_results','players','lineup_players']
    before={}
    for table in tables:
        columns=[row[1] for row in old.execute(f'PRAGMA table_info({table})')]
        names=','.join(columns)
        rows=[tuple(row) for row in populated.execute(f'SELECT {names} FROM {table}')]
        old.executemany(f'INSERT INTO {table}({names}) VALUES ({",".join("?" for _ in columns)})',rows)
        before[table]=(names,rows)
    old.execute("INSERT INTO schema_versions VALUES (2,'original timestamp')");old.commit()
    initialize_database(old)
    for table,(names,rows) in before.items():
        assert [tuple(row) for row in old.execute(f'SELECT {names} FROM {table}')]==rows
    assert tuple(old.execute('SELECT entry_fee_cents,net_profit_cents FROM member_results').fetchone())==(None,None)
    assert old.execute('SELECT applied_at FROM schema_versions WHERE version=2').fetchone()[0]=='original timestamp'
    assert old.execute('PRAGMA foreign_key_check').fetchall()==[]
    ingest(old,sources)
    assert len(tournament_aggregates(old,'test'))==1
    assert len(season_leaderboard(old,'test'))==2


def test_tournament_movement_independent_and_historical(sources):
    from kcdk.analytics import tournament_leaderboard_with_movement
    db=connect_database(':memory:')
    ingest(db,sources)
    import_h2h(db,sources[3],sources[0],dk_fee_cents=100,season_name='Test',season_identifier='test',week_number=1)
    historical_id=db.execute('SELECT id FROM contests').fetchone()[0]
    sources[1].write_text(sources[1].read_text().replace('bschobs (','other ('))
    import_tournament(db,sources[1],sources[0],season_name='Test',season_identifier='test',week_number=2,payout_path=sources[2])
    sources[1].write_text(sources[1].read_text().replace('other (','bschobs (').replace(',70,',',71,'))
    import_tournament(db,sources[1],sources[0],season_name='Test',season_identifier='test',week_number=3,payout_path=sources[2])
    current=tournament_leaderboard_with_movement(db,'test')
    assert current.loc[current.display_name.eq('Brian'),'movement_status'].iloc[0]=='new'
    historical=tournament_leaderboard_with_movement(db,'test',historical_id)
    assert len(historical)==1 and historical.iloc[0].entry_count==2
    assert historical.iloc[0].total_net_profit==-6
