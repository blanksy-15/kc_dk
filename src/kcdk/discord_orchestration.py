"""Discord event orchestration; independent of imports and analytics."""
import asyncio
from collections import defaultdict, deque
import hashlib
import inspect
import random
import re
import time
from typing import Protocol
from urllib.parse import urlsplit

from .discord import DiscordMessage
from .general_drunk import ChannelMessage, ConversationGate, bounded_context

H2H="KCDK_H2H"
TOURNAMENT="TOURNAMENT"


class ChannelTransport(Protocol):
    async def send(self,channel_id: str,message: DiscordMessage,*,reply_to: str | None=None) -> str: ...
    async def exists(self,channel_id: str,message_id: str) -> bool: ...
    async def edit(self,channel_id: str,message_id: str,message: DiscordMessage) -> None: ...


async def resolve(value):
    return await value if inspect.isawaitable(value) else value


def contest_urls(text):
    """No scraping, redirects or arbitrary DK marketing links. Exact host/path checks."""
    found=[]
    for token in re.findall(r'https?://[^\s<>]+',text):
        token=token.rstrip('.,!);]')
        try:
            parsed=urlsplit(token)
            if parsed.port not in {None,443}:
                continue
        except ValueError:
            continue
        if parsed.scheme!='https' or parsed.username or parsed.password:
            continue
        if parsed.hostname not in {'draftkings.com','www.draftkings.com'}:
            continue
        # DFS contest links commonly use /contest/ID or /draft/contest/ID.
        if not re.fullmatch(r'/(?:draft/)?contest/(?:draftteam/)?\d+/?',parsed.path,re.I):
            continue
        if token not in found:
            found.append(token)
    return tuple(found)


async def send_once(state,transport,key,channel,message,*,details=None,reply_to=None):
    if not state.claim(key,dict(details or {},channel_id=channel)):
        old=state.get(key)
        if old['status']=='posted':
            return old['message_id']
        raise ValueError(f"Unresolved publication operation: {key}; reconcile before retrying")
    try:
        message.to_payload()
        message_id=await transport.send(channel,message,reply_to=reply_to)
        if not message_id:
            raise ValueError('Discord did not return a message ID')
        state.finish(key,message_id)
        return message_id
    except BaseException:
        state.fail(key)
        raise


async def update_persistent_leaderboard(state,transport,config,kind,message):
    if kind not in {H2H,TOURNAMENT}:
        raise ValueError('Unknown persistent leaderboard type')
    message.to_payload()
    channel=config.h2h_leaderboard_channel_id if kind==H2H else config.tournament_leaderboard_channel_id
    scope=(config.guild_id,channel,config.season_identifier,kind)
    key='leaderboard-lock:'+':'.join(scope)
    if not state.claim(key,{'scope':scope}):
        raise ValueError('Persistent leaderboard update is pending reconciliation')
    old=state.leaderboard(*scope)
    creating=False
    try:
        if old and await transport.exists(channel,old['message_id']):
            await transport.edit(channel,old['message_id'],message)
            message_id=old['message_id']
        else:
            creating=True
            message_id=await transport.send(channel,message)
            if not message_id:
                raise ValueError('Discord did not return a leaderboard message ID')
        state.save_leaderboard(*scope,message_id)
        state.release(key)
        return message_id
    except BaseException:
        # Fetch/edit can be safely repeated. An ambiguous create cannot.
        if creating:
            state.fail(key)
        else:
            state.release(key)
        raise


class DiscordOrchestrator:
    def __init__(self,config,state,transport,general_generator,*,own_user_id=None,
                 random_provider=None,clock=time.monotonic):
        self.config=config
        self.state=state
        self.transport=transport
        self.generator=general_generator
        self.own_user_id=own_user_id
        self.gate=ConversationGate(config.general_drunk,random_provider or random.random,clock)
        self.context=defaultdict(lambda: deque(maxlen=config.general_drunk.context_message_count))
        self.lock=asyncio.Lock()

    async def on_message(self,message):
        c=self.config
        if message.guild_id!=c.guild_id or message.is_bot or message.is_webhook or message.author_id==self.own_user_id:
            return 'ignored'
        async with self.lock:
            kind={c.h2h_channel_id:H2H,c.tournament_channel_id:TOURNAMENT}.get(message.channel_id)
            if kind:
                if message.author_id not in c.authorized_poster_ids:
                    return 'unauthorized'
                urls=contest_urls(message.content)
                if not urls:
                    return 'ignored'
                # One official contest per source message avoids guessing between links.
                if len(urls)!=1:
                    return 'ambiguous_links'
                key=f'announcement:{c.guild_id}:{message.id}'
                if self.state.get(key):
                    return 'duplicate'
                index=int(hashlib.sha256(message.id.encode()).hexdigest(),16)%len(c.announcement_templates)
                copy=c.announcement_templates[index].format(
                    week=f'{c.announcement_week} ' if c.announcement_week else '',
                    label='H2H' if kind==H2H else 'tournament',
                    fee='$5, one entry per member.' if kind==H2H else '$3 per entry; multiple entries allowed.',
                    channel_id=message.channel_id,url=urls[0])
                announced=await send_once(self.state,self.transport,key,c.general_channel_id,
                    DiscordMessage(content=copy),details={'source_message_id':message.id,
                    'guild_id':message.guild_id,'source_channel_id':message.channel_id,'contest_type':kind,
                    'contest_url':urls[0],'week':c.announcement_week})
                if c.general_drunk.announcement_followups:
                    trigger=ChannelMessage(announced,c.guild_id,c.general_channel_id,message.author_id,copy,message.author_name)
                    if self.gate.reserve(trigger):
                        await self._reply(trigger,(trigger,),f'announcement-followup:{c.guild_id}:{message.id}')
                return 'announced'
            if message.channel_id!=c.general_channel_id:
                return 'ignored'
            context=self.context[message.channel_id]
            if any(item.id==message.id for item in context):
                return 'duplicate'
            context.append(bounded_context((message,),c.general_drunk)[0])
            key=f'general-drunk:{c.guild_id}:{message.id}'
            if self.state.get(key) or not self.gate.reserve(message):
                return 'ineligible'
            await self._reply(message,bounded_context(context,c.general_drunk),key)
            return 'replied'

    async def _reply(self,trigger,context,key):
        # Claim before generation as well as sending: duplicated events incur no cost.
        if not self.state.claim(key,{'guild_id':trigger.guild_id,'channel_id':trigger.channel_id,
                                     'source_message_id':trigger.id,'mode':'GENERAL_DRUNK'}):
            return
        try:
            text=await resolve(self.generator(context,trigger,self.config.general_drunk))
            if not isinstance(text,str) or not 0<len(text.strip())<=self.config.general_drunk.max_reply_chars:
                raise ValueError('Invalid GENERAL_DRUNK reply length')
            reply=DiscordMessage(content=text.strip())
            reply.to_payload()
            message_id=await self.transport.send(trigger.channel_id,reply,reply_to=trigger.id)
            if not message_id:
                raise ValueError('Discord reply has no message ID')
            self.state.finish(key,message_id)
        except BaseException:
            self.state.fail(key)
            raise
