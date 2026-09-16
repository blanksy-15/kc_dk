"""Explicit operator flow. Preflight copies databases; it never migrates live data."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from .discord_orchestration import H2H, TOURNAMENT
from .orchestration_config import OrchestrationConfig
from .orchestration_state import OrchestrationState
from .persistence import connect_database, initialize_database
from .result_orchestration import prepare_result, publish_result, result_preflight


def snapshot(source, destination):
    if Path(source).exists():
        original = sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            original.backup(destination)
        finally:
            original.close()


def add_parsers(subparsers):
    listener = subparsers.add_parser('discord-bot', help='Run the Discord event listener')
    listener.add_argument('--config', type=Path, default=Path('data/discord_config.json'))
    listener.add_argument('--check-config', action='store_true', help='Validate local config without connecting')
    listener.set_defaults(handler=run_bot)
    parser = subparsers.add_parser('results', help='Independently prepare/publish H2H and tournament results')
    parser.add_argument('--config', type=Path, default=Path('data/discord_config.json'))
    parser.add_argument('--h2h-csv', type=Path)
    parser.add_argument('--tournament-csv', type=Path)
    parser.add_argument('--tournament-payout', type=Path)
    parser.add_argument('--allow-unknown-tournament-payouts', action='store_true')
    parser.add_argument('--week-number', type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--live', action='store_true', help='Show preflight, then require typed confirmation')
    mode.add_argument('--dry-run', action='store_true', help='Default; isolated local preview only')
    parser.set_defaults(handler=run_results)


def run_bot(args):
    config = OrchestrationConfig.load(args.config)
    if args.check_config:
        print('Discord configuration is valid; no connection made')
        return 0
    from .discord_service import run_listener
    run_listener(config)
    return 0


def run_results(args):
    selected = [(kind, source) for kind, source in
                ((H2H, args.h2h_csv), (TOURNAMENT, args.tournament_csv)) if source]
    if not selected:
        if args.tournament_payout:
            raise ValueError('A tournament payout requires --tournament-csv')
        print('No contest types selected; nothing to do')
        return 0
    if args.week_number is None or args.week_number < 1:
        raise ValueError('--week-number is required for selected results')
    config = OrchestrationConfig.load(args.config)
    files = [Path(config.members_path), *(source for _, source in selected)]
    if args.tournament_payout:
        if not args.tournament_csv:
            raise ValueError('A tournament payout requires --tournament-csv')
        files.append(args.tournament_payout)
    fingerprints = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}

    def prepare(db, directory):
        return [prepare_result(db, config, kind, source, args.week_number, directory,
                    payout_path=args.tournament_payout if kind == TOURNAMENT else None,
                    allow_unknown_payouts=args.allow_unknown_tournament_payouts)
                for kind, source in selected]

    with TemporaryDirectory(prefix='kcdk-discord-preflight-') as directory:
        db = connect_database(':memory:')
        state = OrchestrationState()
        try:
            snapshot(config.database_path, db)
            initialize_database(db)
            snapshot(config.state_path, state.connection)
            bundles = prepare(db, directory)
            preview = [dict(result_preflight(bundle, config, state), source_file=str(source),
                       payout_file=str(args.tournament_payout) if bundle.kind == TOURNAMENT and args.tournament_payout else None)
                       for bundle, (_, source) in zip(bundles, selected)]
            print(json.dumps({'mode': 'live preflight' if args.live else 'dry-run', 'results': preview}, indent=2))
        finally:
            db.close()
            state.close()
        if not args.live:
            return 0
        if input('Type PUBLISH to import and publish these results: ').strip() != 'PUBLISH':
            print('Cancelled; no production changes')
            return 0
        if any(hashlib.sha256(path.read_bytes()).hexdigest() != digest for path, digest in fingerprints.items()):
            raise ValueError('Source files changed after preflight; rerun for a fresh preview')
        from .discord_service import BotTransport, bot_token
        token = bot_token()
        db = connect_database(config.database_path)
        state = OrchestrationState(config.state_path)
        async def publish():
            transport = BotTransport(config, token)
            try:
                results = []
                for bundle in prepare(db, directory):
                    results.append(await publish_result(bundle, config, state, transport))
                return results
            finally:
                await transport.close()
        try:
            print(json.dumps(asyncio.run(publish()), indent=2))
        finally:
            db.close()
            state.close()
    return 0
