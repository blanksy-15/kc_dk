"""Gateway listener plus a bounded, non-retrying bot REST transport."""
import asyncio
from dataclasses import replace
import json
import os

from dotenv import dotenv_values
import httpx

from .discord import DiscordTransportError
from .discord_orchestration import DiscordOrchestrator
from .general_drunk import ChannelMessage, generate_general_reply
from .orchestration_state import OrchestrationState


def bot_token():
    token = os.getenv('DISCORD_BOT_TOKEN') or dotenv_values('.env').get('DISCORD_BOT_TOKEN')
    if not token or not token.strip():
        raise ValueError('DISCORD_BOT_TOKEN is required in the environment or ignored .env')
    return token.strip()


class BotTransport:
    """No gateway needed for result CLI. Never retry ambiguous POSTs."""
    def __init__(self, config, token, *, client=None):
        self.config = config
        self.client = client or httpx.AsyncClient(
            base_url='https://discord.com/api/v10',
            headers={'Authorization': 'Bot ' + token}, timeout=15,
            follow_redirects=False)
        self.owns_client = client is None
        self.channels = set()
        self.user_id = None

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def _request(self, method, path, *, missing_ok=False, **kwargs):
        try:
            response = await self.client.request(method, path, **kwargs)
        except Exception:
            raise DiscordTransportError('Discord bot request failed; reconcile publication state before retrying') from None
        if missing_ok and response.status_code == 404:
            try:
                if response.json().get('code') == 10008:  # Unknown Message only
                    return None
            except (ValueError, AttributeError):
                pass
        if not 200 <= response.status_code < 300:
            raise DiscordTransportError(f'Discord bot request rejected (HTTP {response.status_code}); no automatic retry')
        try:
            data = response.json()
        except ValueError:
            raise DiscordTransportError('Discord bot returned an invalid response') from None
        if not isinstance(data, dict):
            raise DiscordTransportError('Discord bot returned an invalid response object')
        return data

    async def _channel(self, channel):
        allowed = {self.config.general_channel_id, self.config.h2h_channel_id,
                   self.config.tournament_channel_id, self.config.h2h_leaderboard_channel_id,
                   self.config.tournament_leaderboard_channel_id}
        if channel not in allowed:
            raise ValueError('Destination is not a configured Discord channel')
        if channel not in self.channels:
            data = await self._request('GET', f'/channels/{channel}')
            if str(data.get('guild_id')) != self.config.guild_id or data.get('type') not in {0, 5}:
                raise ValueError('Destination must be a text channel in the configured guild')
            self.channels.add(channel)

    @staticmethod
    def _body(message, reply_to=None, channel=None):
        payload = message.to_payload()
        if reply_to:
            payload['message_reference'] = {'message_id': reply_to, 'channel_id': channel, 'fail_if_not_exists': False}
        payload['allowed_mentions'] = {'parse': [], 'replied_user': False}
        if message.files:
            return {'data': {'payload_json': json.dumps(payload)},
                    'files': {f'files[{i}]': (f.filename, f.data, 'image/png') for i, f in enumerate(message.files)}}
        return {'json': payload}

    async def send(self, channel_id, message, *, reply_to=None):
        await self._channel(channel_id)
        result = await self._request('POST', f'/channels/{channel_id}/messages',
                                     **self._body(message, reply_to, channel_id))
        value = result.get('id')
        if not isinstance(value, str) or not value.isdigit():
            raise DiscordTransportError('Discord bot returned no valid message ID')
        return value

    async def exists(self, channel_id, message_id):
        await self._channel(channel_id)
        if not str(message_id).isdigit():
            raise ValueError('Stored Discord message ID is invalid')
        result = await self._request('GET', f'/channels/{channel_id}/messages/{message_id}', missing_ok=True)
        if result is None:
            return False
        if self.user_id is None:
            self.user_id = (await self._request('GET', '/users/@me'))['id']
        if result.get('author', {}).get('id') != self.user_id or str(result.get('channel_id')) != channel_id:
            raise ValueError('Stored leaderboard is not owned by this bot in this channel')
        return True

    async def edit(self, channel_id, message_id, message):
        if channel_id not in {self.config.h2h_leaderboard_channel_id, self.config.tournament_leaderboard_channel_id}:
            raise ValueError('Only persistent leaderboard channels may be edited')
        if not await self.exists(channel_id, message_id):
            raise DiscordTransportError('Leaderboard disappeared during update; rerun to replace it')
        await self._request('PATCH', f'/channels/{channel_id}/messages/{message_id}', **self._body(message))


def run_listener(config):
    from .drunk_bot import DrunkBotConfig
    # The existing profanity prohibition remains a ceiling for both modes.
    config = replace(config, general_drunk=replace(config.general_drunk,
        allow_profanity=config.general_drunk.allow_profanity and DrunkBotConfig.from_env().allow_profanity))
    try:
        import discord
    except ImportError:
        raise ValueError('Listener requires the discord extra: pip install -e ".[discord]"') from None
    token = bot_token()
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    async def serve():
        state = OrchestrationState(config.state_path)
        transport = BotTransport(config, token)
        async def generate(context, trigger, settings):
            return await asyncio.to_thread(generate_general_reply, context, trigger, settings)
        orchestrator = DiscordOrchestrator(config, state, transport, generate)

        @client.event
        async def on_ready():
            orchestrator.own_user_id = str(client.user.id)
            print('KCDK Discord listener ready')

        @client.event
        async def on_message(message):
            if message.guild is None:
                return
            event = ChannelMessage(str(message.id), str(message.guild.id), str(message.channel.id),
                str(message.author.id), message.content, message.author.display_name,
                message.author.bot, message.webhook_id is not None)
            try:
                await orchestrator.on_message(event)
            except Exception as exc:
                # Do not log message bodies, tokens, or third-party exception text.
                print(f'KCDK event failed ({type(exc).__name__}); inspect orchestration state')

        try:
            async with client:
                await client.start(token)
        finally:
            await transport.close()
            state.close()
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    except Exception:
        raise ValueError('Discord listener stopped; verify token, channel permissions and Message Content intent') from None
