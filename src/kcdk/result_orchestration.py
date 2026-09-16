"""Independent result preparation/publication above the existing core."""
from dataclasses import dataclass, replace
from pathlib import Path
import hashlib

from .analytics import season_leaderboard_with_movement, tournament_leaderboard_with_movement
from .commentary import generate_weekly_commentary, render_discord_markdown
from .discord import DiscordMessage, DiscordAttachment
from .discord_orchestration import H2H, TOURNAMENT, resolve, update_persistent_leaderboard
from .drunk_bot import DrunkBotConfig
from .drunk_bot_commentary import generate_drunk_bot_commentary, render_drunk_bot_message
from .facts import build_weekly_fact_report, generate_tournament_facts, select_facts, WeeklyFactReport
from .leaderboard_graphics import render_kcdk_standings_png, render_tournament_performance_png
from .persistence import import_h2h
from .publishing import preview_commentary
from .tournaments import import_tournament, tournament_aggregates


@dataclass(frozen=True)
class ResultBundle:
    kind: str
    contest_id: int
    revision: str
    week: str
    report: WeeklyFactReport
    historical_image: DiscordAttachment
    persistent_message: DiscordMessage
    import_summary: dict


def _image(renderer,frame,path,config,week):
    if frame.empty:
        raise ValueError('No matched member results are available for this leaderboard')
    renderer(frame,path,season_label=config.season_name,week_label=week, weeks_completed=int(frame.weeks_played.max()))
    data=path.read_bytes()
    if len(data)>8*1024*1024:
        raise ValueError('Result leaderboard exceeds 8 MiB upload budget')
    return DiscordAttachment(path.name,data,f'{config.season_name} standings through {week}')


def prepare_result(connection,config,kind,csv_path,week_number,output_dir,*,payout_path=None,allow_unknown_payouts=False):
    """Local import and rendering only. Caller chooses isolated or live database."""
    if kind not in {H2H,TOURNAMENT} or type(week_number) is not int or week_number<1:
        raise ValueError('Valid contest type and positive week number are required')
    output=Path(output_dir)
    output.mkdir(parents=True,exist_ok=True)
    week=f'Week {week_number}'
    common=dict(season_name=config.season_name,season_identifier=config.season_identifier,
                season_year=config.season_year,week_number=week_number)
    if kind==H2H:
        summary=import_h2h(connection,csv_path,config.members_path,dk_fee_cents=config.h2h_dk_fee_cents,**common)
        contest=connection.execute('SELECT c.* FROM contests c JOIN seasons s ON s.id=c.season_id WHERE s.identifier=? AND c.week_label=?',(config.season_identifier,week)).fetchone()
        raw=build_weekly_fact_report(connection,config.season_identifier,contest['id'])
        facts=tuple(f for f in raw.candidate_facts if f.category!='tournament' and 'tournament' not in f.tags)
        report=replace(raw,candidate_facts=facts,selected_facts=tuple(select_facts(facts,max_count=12)),generated_candidate_count=len(facts))
        historical=season_leaderboard_with_movement(connection,config.season_identifier,contest['id'])
        persistent=season_leaderboard_with_movement(connection,config.season_identifier)
        latest=connection.execute('SELECT c.week_label FROM contests c JOIN seasons s ON s.id=c.season_id WHERE s.identifier=? ORDER BY COALESCE(c.week_number,2147483647) DESC,c.id DESC LIMIT 1',(config.season_identifier,)).fetchone()[0]
        renderer=render_kcdk_standings_png
        revision=f"{contest['source_fingerprint']}:{contest['dk_fee_cents']}"
        contest_id=contest['id']
    else:
        if payout_path is None and not allow_unknown_payouts:
            raise ValueError('Tournament payouts required unless unknown payouts explicitly allowed')
        summary=import_tournament(connection,csv_path,config.members_path,payout_path=payout_path,**common)
        contest=connection.execute('SELECT * FROM tournaments WHERE id=?',(summary.tournament_id,)).fetchone()
        season=connection.execute('SELECT * FROM seasons WHERE id=?',(contest['season_id'],)).fetchone()
        facts=tuple(generate_tournament_facts(connection,config.season_identifier,week))
        report=WeeklyFactReport(season['id'],config.season_identifier,config.season_name,contest['id'],week,
            len(facts),facts,tuple(select_facts(facts,max_count=12)),summary.warnings)
        historical=tournament_aggregates(connection,config.season_identifier,through_week_number=week_number)
        persistent=tournament_leaderboard_with_movement(connection,config.season_identifier)
        latest=connection.execute('SELECT week_label FROM tournaments WHERE season_id=? ORDER BY COALESCE(week_number,2147483647) DESC,id DESC LIMIT 1',(season['id'],)).fetchone()[0]
        renderer=render_tournament_performance_png
        revision=f"{contest['source_fingerprint']}:{contest['payout_fingerprint']}"
        contest_id=contest['id']
    old_image=_image(renderer,historical,output/f'{kind.lower()}_historical.png',config,week)
    current_image=_image(renderer,persistent,output/f'{kind.lower()}_current.png',config,latest)
    title='KCDK H2H standings' if kind==H2H else 'KCDK tournament performance'
    return ResultBundle(kind,contest_id,hashlib.sha256(revision.encode()).hexdigest(),week,report,old_image,
        DiscordMessage(content=f'{title}\nUpdated through {latest}',files=(current_image,),replace_attachments=True),summary.to_dict())


def commissioner_text(report):
    generation=generate_weekly_commentary(report)
    return render_discord_markdown(generation.commentary)


def results_drunk_text(report,settings):
    generation=generate_drunk_bot_commentary(report,config=settings)
    return render_drunk_bot_message(generation.commentary).content if generation.commentary else None


async def publish_result(bundle,config,state,transport,*,commissioner=commissioner_text,
                         results_drunk=results_drunk_text,drunk_config=None):
    """Each contest gets a new historical post; only its scoped current board is edited."""
    channel=config.h2h_channel_id if bundle.kind==H2H else config.tournament_channel_id
    key=f'result:{config.guild_id}:{channel}:{config.season_identifier}:{bundle.kind}:{bundle.contest_id}:{bundle.revision}'
    prior=state.get(key)
    if prior and prior['status']!='posted':
        raise ValueError(f'Unresolved result publication: {key}; reconcile before retrying')
    if not prior:
        # Reserve before model generation. On a failure there is no automatic
        # paid regeneration or potentially duplicate Discord send.
        if not state.claim(key,{'guild_id':config.guild_id,'channel_id':channel,'season':config.season_identifier,
                                'contest_type':bundle.kind,'contest_id':bundle.contest_id,'week':bundle.week}):
            raise ValueError('Result publication is already running')
        try:
            text=await resolve(commissioner(bundle.report)) if config.commissioner_enabled else render_discord_markdown(preview_commentary(bundle.report))
            title=f"{bundle.week} — {'H2H' if bundle.kind==H2H else 'Tournament'} results"
            if not isinstance(text,str) or not text.strip():
                raise ValueError('Commissioner returned no recap')
            chunks=[text[i:i+4000] for i in range(0,len(text),4000)]
            embeds=tuple({'title':title if i==0 else 'Continued','description':chunk} for i,chunk in enumerate(chunks))
            message=DiscordMessage(embeds=embeds,files=(bundle.historical_image,))
            message.to_payload()
            historical_id=await transport.send(channel,message)
            if not historical_id:
                raise ValueError('No historical result message ID returned')
            state.finish(key,historical_id)
        except BaseException:
            state.fail(key)
            raise
    else:
        historical_id=prior['message_id']
    drunk_status='disabled'
    settings=drunk_config or (DrunkBotConfig.from_env() if config.results_drunk_enabled else DrunkBotConfig(enabled=False))
    if config.results_drunk_enabled and settings.enabled:
        from .drunk_bot import select_drunk_bot_facts
        eligibility=select_drunk_bot_facts(bundle.report,settings)
        drunk_key=key+':results-drunk'
        prior_drunk=state.get(drunk_key)
        drunk_status=prior_drunk['status'] if prior_drunk else 'ineligible'
        if eligibility.eligible and not prior_drunk:
            if state.claim(drunk_key,{'mode':'RESULTS_DRUNK','contest_type':bundle.kind,'channel_id':channel}):
                try:
                    text=await resolve(results_drunk(bundle.report,settings))
                    if text:
                        reply=DiscordMessage(content=text)
                        reply.to_payload()
                        reply_id=await transport.send(channel,reply,reply_to=historical_id)
                        if not reply_id:
                            raise ValueError('No RESULTS_DRUNK message ID returned')
                        state.finish(drunk_key,reply_id)
                        drunk_status='posted'
                    else:
                        state.finish(drunk_key,'skipped')
                except Exception:
                    state.fail(drunk_key)
                    drunk_status='failed; inspect state before recovery'
    board_id=await update_persistent_leaderboard(state,transport,config,bundle.kind,bundle.persistent_message)
    return {'contest_type':bundle.kind,'historical_channel_id':channel,'historical_message_id':historical_id,
            'historical_action':'skipped' if prior else 'created','leaderboard_message_id':board_id,'results_drunk':drunk_status}


def result_preflight(bundle,config,state=None):
    channel=config.h2h_channel_id if bundle.kind==H2H else config.tournament_channel_id
    board=config.h2h_leaderboard_channel_id if bundle.kind==H2H else config.tournament_leaderboard_channel_id
    stored=state.leaderboard(config.guild_id,board,config.season_identifier,bundle.kind) if state else None
    key=f'result:{config.guild_id}:{channel}:{config.season_identifier}:{bundle.kind}:{bundle.contest_id}:{bundle.revision}'
    prior=state.get(key) if state else None
    return {'contest_type':bundle.kind,'week':bundle.week,'import':bundle.import_summary,
            'historical_channel_id':channel,'leaderboard_channel_id':board,
            'stored_leaderboard_message_id':stored['message_id'] if stored else None,
            'leaderboard_action':'fetch then edit; replace only if confirmed deleted' if stored else 'create and save ID',
            'historical_operation_status':prior['status'] if prior else 'new',
            'historical_action':'skip existing recap' if prior and prior['status']=='posted' else 'reconcile before retry' if prior else 'create new recap',
            'commissioner_enabled':config.commissioner_enabled,'results_drunk_enabled':config.results_drunk_enabled,
            'fact_types':[f.fact_type for f in bundle.report.candidate_facts],
            'preview_commentary':render_discord_markdown(preview_commentary(bundle.report)),
            'image_bytes':len(bundle.historical_image.data)}
