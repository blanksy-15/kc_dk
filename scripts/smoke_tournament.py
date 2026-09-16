"""Offline real-export smoke test. All databases/member configuration are temporary."""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kcdk.facts import generate_tournament_facts
from kcdk.leaderboard_graphics import render_tournament_performance_png
from kcdk.persistence import connect_database
from kcdk.tournaments import import_tournament, tournament_aggregates, tournament_entries


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results',type=Path,default=ROOT/'data/test_tourney_results.csv')
    parser.add_argument('--payout',type=Path,default=ROOT/'data/test_tourney_payout.csv')
    parser.add_argument('--output-dir',type=Path,help='Optional retained PNG/report directory; default is temporary.')
    args=parser.parse_args(argv)
    original={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in (args.results,args.payout)}
    with tempfile.TemporaryDirectory(prefix='kcdk-real-tournament-') as directory:
        temporary=Path(directory)
        members=temporary/'members.csv'
        members.write_text('member_key,draftkings_name,display_name,active\nbschobs,bschobs,bschobs,true\n',encoding='utf-8')
        connection=connect_database(temporary/'smoke.sqlite')
        try:
            kwargs=dict(season_name='Isolated real tournament smoke',season_identifier='real-smoke',week_number=1,payout_path=args.payout)
            summary=import_tournament(connection,args.results,members,**kwargs)
            entries=tournament_entries(connection,'real-smoke').sort_values('entry_id')
            assert summary.field_size==118906
            assert summary.matched_members==1 and summary.entry_count==2 and summary.lineup_count==12
            expected={'5246042705':(53734,68.19),'5246030417':(84378,55.94)}
            for row in entries.itertuples():
                assert (row.rank,row.points)==expected[row.entry_id]
                assert row.entry_fee_cents==300 and row.gross_winnings_cents==0 and row.net_profit_cents==-300
                total=connection.execute('SELECT SUM(player_fantasy_points),COUNT(*) FROM tournament_lineup_players WHERE entry_pk=?',(row.id,)).fetchone()
                assert total[1]==6 and abs(total[0]-row.points)<1e-9
            aggregate=tournament_aggregates(connection,'real-smoke')
            assert len(aggregate)==1
            member=aggregate.iloc[0]
            assert member.entry_count==2 and member.total_entry_fees_cents==600
            assert member.total_gross_winnings_cents==0 and member.net_profit_cents==-600
            assert member.cashes==0 and member.cash_rate==0
            assert abs(member.average_draftkings_fantasy_points-62.065)<1e-9
            assert abs(member.average_tournament_percentile-41.92422522181573)<1e-9
            assert connection.execute('SELECT COUNT(*) FROM members').fetchone()[0]==1
            assert connection.execute('SELECT COUNT(*) FROM member_results').fetchone()[0]==0
            assert import_tournament(connection,args.results,members,**kwargs).already_imported
            assert import_tournament(connection,args.results,members,replace=True,**kwargs).already_imported
            assert connection.execute('SELECT COUNT(*) FROM tournament_lineup_players').fetchone()[0]==12
            facts=generate_tournament_facts(connection,'real-smoke','Week 1')
            assert not {'weekly_winner','weekly_last_place','unanimous_player'} & {fact.fact_type for fact in facts}
            output=args.output_dir or temporary
            output.mkdir(parents=True,exist_ok=True)
            rendered=render_tournament_performance_png(aggregate,output/'tournament_performance.png',season_label='Real export smoke test',week_label='Isolated test',weeks_completed=1)
            assert (rendered.width,rendered.height)==(1400,478)
            report={'status':'passed','source_fingerprints':{str(path):value for path,value in original.items()},
                'summary':summary.to_dict(),'entries':json.loads(entries.to_json(orient='records')),
                'aggregates':json.loads(aggregate.to_json(orient='records')),
                'lineup_totals':{entry_id:points for entry_id,(_,points) in expected.items()},
                'facts':[fact.to_dict() for fact in facts],
                'png':str(output/'tournament_performance.png') if args.output_dir else 'verified in temporary directory',
                'dimensions':[rendered.width,rendered.height], 'database':'temporary; removed on exit'}
            text=json.dumps(report,indent=2,allow_nan=False)
            if args.output_dir:
                (output/'report.json').write_text(text+'\n',encoding='utf-8')
            print(text)
        finally:
            connection.close()
    assert all(hashlib.sha256(path.read_bytes()).hexdigest()==digest for path,digest in original.items())
    return 0


if __name__=='__main__':
    raise SystemExit(main())
