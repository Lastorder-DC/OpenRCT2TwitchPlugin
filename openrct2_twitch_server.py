# -*- coding: utf-8 -*-
#
# OpenRCT2 Twitch API Server
# (c) 2018 Fun-boong-e <https://tgd.kr/funzinnu>
# (c) 2019-2020 Lastorder <https://lastorder.xyz>
#
# Complies BSD license.
#
# Please note that this script requires Redis server to work. You may need to install redis python module with `pip install redis`.
#
# How to run:
#
# $ pip install -r requirements.txt
# $ export TS_USER_NAME=xxx       -- bot id to use(case insensitive)
# $ export TS_OAUTH_KEY=xxx       -- obtain one at https://twitchapps.com/tmi/ - heading `oauth:` not required
# $ export TS_CLIENT_ID=xxxxx     -- make one at Twitch developer console
# $ python openrct2_twitch_server.py -H 0.0.0.0 -p 8000
from gevent.monkey import patch_all; patch_all()  # noqa

import argparse
import collections
import enum
import logging
import os
import re
import sys
import typing
from time import time
import json
import redis
from redislite.patch import patch_redis
import socketserver as SocketServer
import json

from gevent import sleep, spawn, spawn_later
from requests import get
#pip3 install websocket-client<=0.48
from websocket import WebSocketApp

# This script requires Python 3.6 or above.
assert sys.version_info >= (3, 6), "This script requires Python 3.6 or above."

USER_NAME = os.getenv('TS_USER_NAME')
OAUTH_KEY = os.getenv('TS_OAUTH_KEY')
CLIENT_ID = os.getenv('TS_CLIENT_ID')
if not USER_NAME:
    raise RuntimeError('No environment variable: USER_NAME')
if not OAUTH_KEY:
    raise RuntimeError('No environment variable: TS_OAUTH_KEY')
if not CLIENT_ID:
    raise RuntimeError('No environment variable: TS_CLIENT_ID')

# API Setting
MAX_USER_COUNT = 100
MAX_NEWS_ENTRY_COUNT = 20
MAX_FOLLOWERS_COUNT = 1500
TMI_WS_ENDPOINT = 'ws://irc-ws.chat.twitch.tv'

logger = logging.getLogger('openrct2_twitch_api_server')
IrcLine = collections.namedtuple('IrcLine', ['ident', 'command', 'parts'])

class RateLimitException(Exception):
    pass

class ParseError(Exception):
    pass

def extract_irc_line(raw: str) -> IrcLine:
    parts = raw.split(' ', 2)
    if len(parts) < 1:
        raise ParseError(f'Unrecognized line: {raw}')
    if parts[0].startswith(':'):
        ident = parts[0][1:]
        base = 1
    else:
        ident = None
        base = 0
    if len(parts) < base + 2:
        return IrcLine(ident, parts[base], None)

    def parse(part):
        buf = ''
        whitespace = False
        fill_remaining = False
        for c in part:
            if c == ' ' and not fill_remaining:
                if buf:
                    yield buf
                    buf = ''
                whitespace = True
            elif c == ':' and whitespace and not fill_remaining:
                fill_remaining = True
                whitespace = False
            else:
                buf += c
                whitespace = False
        if buf:
            yield buf
    return IrcLine(ident, parts[base], [x for x in parse(parts[base+1])])

# Normalize channel name(Append #, lowercase all alphabet)
def normalize_channel_name(name: str) -> str:
    if name.startswith('#'):
        return name.lower()
    else:
        return f'#{name.lower()}'

# Extract username from IRC privmsg
def extract_username(ident: str) -> str:
    match = re.match(r':?(\w+)!\w+@[\w\.]+', ident)
    if match:
        return match.group(1)

# A very, very stripped down IRC client.
class IrcClient:
    EXPONENTIAL_BACKOFF_UPPER_BOUND = 30

    def on_message(self, ws, message):
        messages = message.split('\r\n')
        for message in messages:
            message = message.strip()
            if not message:
                continue
            try:
                line = extract_irc_line(message)
            except ParseError as e:
                logger.warning(e)
            if line.command in self.handlers:
                if line.parts is None:
                    self.handlers[line.command](self, line.ident)
                else:
                    self.handlers[line.command](self, line.ident, *line.parts)
            else:
                self.logger.info(f'Unhandled line: {message}')

    def on_error(self, ws, error):
        self.logger.error(f'Error occurred: {error}')
        self.retries += 1

    def on_close(self, ws):
        self.set_connected(False)

    def on_open(self, ws):
        self.retries = 0
        logger.info('Connection established.')
        # This is required to get information, user list from IRC
        # Without this, twitch will not return information about user(badge, emotes, etc)
        # and will not return user list
        self.ws.send('CAP REQ :twitch.tv/commands twitch.tv/membership')
        self.ws.send(f'PASS oauth:{OAUTH_KEY}')
        self.ws.send(f'NICK {USER_NAME}')
        self.ws.send(f'USER {USER_NAME} 8 * :{USER_NAME}')

    def send(self, command):
        if self.connected:
            self.ws.send(command)
        else:
            self.backlog.append(command)

    def _connect(self):
        while True:
            self.ws.on_open = self.on_open
            self.ws.run_forever()
            secs = min(self.EXPONENTIAL_BACKOFF_UPPER_BOUND, 2 ** self.retries)
            self.logger.info(f'Reestablishing connection in {secs} secs...')
            sleep(secs)

    def set_connected(self, connected):
        if not self.connected and connected:
            for command in self.backlog:
                self.ws.send(command)
            self.backlog.clear()
        self.connected = connected

    def connect(self):
        spawn(self._connect)

    def on(self, type: str):
        def decorator(f):
            self.handlers[type] = f
            return f
        return decorator

    def join(self, channel):
        self.send(f'JOIN {normalize_channel_name(channel)}')
    
    # This is actually not used(endpoint /part is disabled due to prevent abuse)
    def part(self, channel):
        self.send(f'PART {normalize_channel_name(channel)}')

    # Twitch IRC requires respond ping command with PONG.
    def pong(self, server_ident):
        self.send(f'PONG :{server_ident}')

    # Sends message to channel
    # This may not work if specific user is not VIP at channel(slow mode, same message restriction, etc...)
    def privmsg(self, channel, msg):
        self.send(f'PRIVMSG {channel} :{msg}')

    def __init__(self):
        self.logger = logging.getLogger('irc_client')
        self.retries = 0
        self.handlers = {}
        self.connected = False
        self.backlog = []
        self.ws = WebSocketApp(
            TMI_WS_ENDPOINT,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )

irc = IrcClient()
patch_redis("./orct2.db")
redis = redis.StrictRedis(decode_responses=True)

channels = {}
channel_language = {}

@irc.on('001')
def handle_001(client: IrcClient, ident, username, msg):
    client.username = username
    client.logger.info(f'Signed in as {username}')

@irc.on('PING')
def handle_ping(client: IrcClient, ident, server_ident):
    client.pong(server_ident)

@irc.on('GLOBALUSERSTATE')
def handle_globaluserstate(client: IrcClient, ident):
    # Rejoin all the channels if connection is lost
    for channel in channels:
        client.join(channel)
    client.set_connected(True)

@irc.on('JOIN')
def handle_join(client: IrcClient, ident, channel_name):
    channel_name = normalize_channel_name(channel_name)
    username = extract_username(ident)
    if username == client.username:
        if channel_name not in channels:
            channel = Channel(channel_name)
            channels[channel_name] = channel
            logger.info(f'Joined channel {channel_name}')
        else:
            channel = channels[channel_name]
        channel.status = ChannelStatus.connecting
        channel.clear_audiences()
    else:
        if channel_name in channels:
            channel = channels[channel_name]
            channel.append_audiences([username])
            if len(channel.join_window) == 0:
                spawn_later(0.1, lookup_user_batch, channel, 'login')
            channel.add_join_window(username)

@irc.on('PART')
def handle_part(client: IrcClient, ident, channel_name):
    channel_name = normalize_channel_name(channel_name)
    username = extract_username(ident)
    if username == client.username:
        if channel_name in channels:
            del channels[channel_name]
    else:
        if channel_name in channels:
            channels[channel_name].remove_audience(username)

@irc.on('PRIVMSG')
def handle_privmsg(client: IrcClient, ident, channel_name, msg):
    channel_name = normalize_channel_name(channel_name)
    if channel_name in channels and msg.lower().startswith('!news '):
        channel = channels[channel_name]
        while len(channel.newses) > MAX_NEWS_ENTRY_COUNT:
            channel.newses.pop()
        username = extract_username(ident)
        if redis.exists(str(username)):
            user = redis.hgetall(str(username))
            display_name = user['display_name']
        else:
            display_name = username
        channel.newses.appendleft(f'!news {display_name}: {msg[6:]}')
        try:
            if channel_language[channel] == "kr":
                client.privmsg(channel_name, f'@{username} 잠시후 메세지가 게임내 표시됩니다.')
            else:
                client.privmsg(channel_name, f'@{username} Your message will soon be displayed in few minutes.')
        except Exception:
            client.privmsg(channel_name, f'@{username} 잠시후 메세지가 게임내 표시됩니다.')
            channel_language[channel] = "kr"

@irc.on('353')
def handle_names(client: IrcClient, ident, username, _, channel, names):
    channels[channel].append_audiences(names.split())

@irc.on('366')
def handle_end_names(client: IrcClient, ident, username, channel_name, msg):
    channel = channels[channel_name]
    channel.status = ChannelStatus.connected
    spawn(lookup_user_cached, channel.audiences, 'login')
    user_info = lookup_user([channel_name.replace('#', '')], 'login')[0]
    # Start fetching followers simultaneously
    spawn(fetch_followers_cached, channel, user_info)

irc.connect()

class ChannelStatus(enum.Enum):
    connecting = 'connecting'
    connected = 'connected'

class Channel:
    def __init__(self, name: str):
        self.status = ChannelStatus.connecting
        self.name = name
        self.followers = []
        self.followers_set = set()
        self.audiences = []
        self.audiences_set = set()
        self.join_window = []
        self.newses = collections.deque()

    def clear_audiences(self):
        self.audiences.clear()
        self.audiences_set.clear()

    def append_followers(self, followers: typing.Sequence[str]):
        for i in followers:
            if i not in self.followers_set:
                self.followers.append(i)
                self.followers_set.add(i)

    def append_audiences(self, audiences: typing.Sequence[str]):
        for i in audiences:
            if i not in self.audiences_set:
                self.audiences.append(i)
                self.audiences_set.add(i)

    def remove_follower(self, follower: str):
        if follower in self.followers_set:
            self.followers.remove(follower)
            self.followers_set.remove(follower)

    def remove_audience(self, audience: str):
        if audience in self.audiences_set:
            self.audiences.remove(audience)
            self.audiences_set.remove(audience)

    def add_join_window(self, username: str):
        if username not in self.join_window:
            self.join_window.append(username)

    def consume_join_window(self):
        t = self.join_window
        self.join_window = []
        return t

def chunks(l: typing.Sequence[str], n: int):
    for i in range(0, len(l), n):
        yield l[i:i + n]

def lookup_user(u: list, parameter: str):
    assert len(u) <= MAX_USER_COUNT
    logger.info(f'looking up users: {u}')
    qs = '&'.join([f'{parameter}={i}' for i in u])
    url = f'https://api.twitch.tv/helix/users?{qs}'
    r = get(url, headers={'Client-ID': CLIENT_ID,'Authorization':f'Bearer {OAUTH_KEY}'})
    if r.status_code == 429:
        raise RateLimitException()
    elif r.status_code != 200:
        logger.error(f'Twitch API error: {r.status_code} {r.text}')
        return None
    json = r.json()
    return json['data']

def lookup_user_cached(u: list, parameter: str):
    if parameter not in ('login', 'id'):
        raise ValueError('parameter should be login or id.')
    if parameter == 'login':
        #store = users
        pass
    else:
        #store = ids
        pass
    c = chunks([i for i in u if not redis.exists(str(i))], MAX_USER_COUNT)
    for n, i in enumerate(c):
        if n > 0:
            sleep(0.5)  # a crude way to mitigate Twitch API rate limit
        try:
            result = lookup_user(i, parameter)
            for i in result:
                i['last_cached_time'] = time()
                redis.hmset(str(i['id']), i)
                redis.hmset(str(i['login']), i)
        except RateLimitException:
            logger.error('Twitch API rate limit exceeded. Will try later.')
            spawn_later(10, lookup_user_cached, i, parameter)


def lookup_user_batch(channel: Channel, parameter: str):
    c = channel.consume_join_window()
    if len(c) > 0:
        lookup_user_cached(c, parameter)

def lookup_user_expired(u: list, parameter: str):
    logger.info("Getting expired user information...")
    spawn_later(0, lookup_user_cached, u, parameter)

def fetch_followers(id: str, after: str=''):
    url = f'https://api.twitch.tv/helix/users/follows?to_id={id}' \
          f'&after={after}&first={MAX_USER_COUNT}'
    r = get(url, headers={'Client-ID': CLIENT_ID,'Authorization':f'Bearer {OAUTH_KEY}'})
    if r.status_code == 429:
        raise RateLimitException()
    elif r.status_code != 200:
        logger.error(f'Twitch API error: {r.status_code} {r.text}')
        return None
    json = r.json()
    return json

def fetch_followers_cached(channel: Channel, user_data: dict, after: str=''):
    has_more = True
    while has_more:
        try:
            result = fetch_followers(user_data['id'], after)
        except RateLimitException:
            spawn_later(10, fetch_followers_cached, channel, user_data, after)
            break
        if result is None:
            break
        after = result.get('pagination') and result['pagination'].get('cursor')
        has_more = bool(after)
        ids = [i['from_id'] for i in result['data']]
        channel.append_followers(ids)
        sleep(0.5)
        lookup_user_cached(ids, 'id')
        if len(channel.followers) > MAX_FOLLOWERS_COUNT:
            break
        elif has_more:
            sleep(0.5)

def join_channel(channel: str):
    if channel not in channels:
        irc.join(channel)
        channel_language[channel] = "kr"

def join_channel_en(channel: str):
    if channel not in channels:
        irc.join(channel)
        channel_language[channel] = "en"
    
def get_audiences(channel_name: str):
    channel_name = normalize_channel_name(channel_name)
    if channel_name not in channels:
        irc.join(channel_name)
        if channel_name not in channel_language:
            channel_language[channel_name] = "kr"
        return json.dumps([])
    if not redis.exists(f'/channel/{channel_name}/audience/last_time') or time() - float(redis.get(f'/channel/{channel_name}/audience/last_time')) > 120: # 2 min. passed since cache
        logger.info("Cache expired, rebuilding cache...")
        channel = channels[channel_name]
        while (channel.status is ChannelStatus.connecting and
               len(channel.audiences) + len(channel.followers) == 0):
            sleep(5)  # Wait until audience/followers list is sufficient
        result = []
        expired_ids = []
        audience_ids = set()
        for i in channel.audiences:
            if redis.exists(str(i)):
                u = redis.hgetall(str(i))
                if 'last_cached_time' not in u or time() - float(u['last_cached_time']) > 86400: # if 1day passed after cache
                    dn = u['display_name']
                    logger.info(f'User {dn} cache expired. removing...')
                    redis.delete(str(i)) # delete cached information
                    expired_ids.append(u['id'])
                audience_ids.add(u['id'])
                if len(u['display_name'].encode('utf-8')) < 32:
                    result.append({
                        'name': u['display_name'],
                        'inChat': True,
                        # Needs extra API call. So let's estimate.
                        'isFollower': u['id'] in channel.followers_set,
                        'isMod': False
                    })
        for i in channel.followers:
            if redis.exists(str(i)) and i not in audience_ids:
                u = redis.hgetall(str(i))
                if 'last_cached_time' not in u or time() - float(u['last_cached_time']) > 86400: # if 1day passed after cache
                    dn = u['display_name']
                    logger.info(f'User {dn} cache expired. removing...')
                    redis.delete(str(i)) # delete cached information
                    expired_ids.append(u['id'])
                if len(u['display_name'].encode('utf-8')) < 32:
                    result.append({
                        'name': u['display_name'],
                        'inChat': False,
                        'isFollower': True,
                        'isMod': False
                    })
        lookup_user_expired(expired_ids, 'id')
        redis.set(f'/channel/{channel_name}/audience/last_time',str(time()))
        redis.set(f'/channel/{channel_name}/audience/output',json.dumps(result))
    else:
        logger.info("Cache is still vaild, reusing cache...")
        result = json.loads(redis.get(f'/channel/{channel_name}/audience/output'))
    return json.dumps(result)

def get_messages(channel_name: str):
    channel_name = normalize_channel_name(channel_name)
    if channel_name not in channels:
        irc.join(channel_name)
        if channel_name not in channel_language:
            channel_language[channel_name] = "kr"
        return json.dumps([])
    channel = channels[channel_name]
    result = []
    while True:
        try:
            result.append({'message': channel.newses.pop()})
        except IndexError:
            break
    return json.dumps(result)

class TwitchTCPHandler(SocketServer.BaseRequestHandler):
    def handle(self):
        self.data = ''
        tcp_channel_id = ''
        while True:
            self.data = self.request.recv(1024).strip().decode()
            if self.data.find("CONNECT") != -1:
                print("CONNECT")
                tcp_channel_id = self.data.replace("CONNECT #","")
                join_channel(tcp_channel_id)
                self.request.sendall(("Connected to channel" + tcp_channel_id).encode())
            if self.data.find("get_audiences") != -1:
                print("get_audiences")
                send_data = get_audiences(tcp_channel_id).encode()
                print(send_data)
                self.request.sendall(send_data)
            if self.data.find("quit") != -1:
                break

def main():
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)
    parser = argparse.ArgumentParser()
    parser.add_argument('-H', '--host', type=str, default='127.0.0.1')
    parser.add_argument('-p', '--port', type=int, default=8000)
    parser.add_argument('-d', '--debug', action='store_true')
    args = parser.parse_args()
    server = SocketServer.TCPServer((args.host, args.port), TwitchTCPHandler)
    server.serve_forever()

if __name__ == '__main__':
    main()
