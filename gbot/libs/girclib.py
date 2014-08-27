#!/usr/bin/env python3
# Goshubot IRC Bot
# written by Daniel Oaks <daniel@danieloaks.net>
# licensed under the BSD 2-clause license

import collections
import bisect
import ssl
import irc, irc.client, irc.modes
import datetime
import calendar
import time
import threading
from functools import partial


# lots of this taken from PyPsd's istring:
# http://gitlab.com/rizon/pypsd
class IString(str):
    """String wrapper for IRC chans/nicks, based on casemapping standards."""
    # setting info
    def set_std(self, std):
        """Set the standard we'll be using (isupport CASEMAPPING)."""
        # translation based on std
        self._lower_chars = None
        self._upper_chars = None

        self._lower_trans = None
        self._upper_trans = None

        self._std = std.lower()

        if self._std == 'ascii':
            pass
        elif self._std == 'rfc1459':
            self._lower_chars = ''.join(chr(i) for i in range(91, 95))
            self._upper_chars = ''.join(chr(i) for i in range(123, 127))

        elif self._std == 'rfc1459-strict':
            self._lower_chars = ''.join(chr(i) for i in range(91, 94))
            self._upper_chars = ''.join(chr(i) for i in range(123, 126))

        if self._lower_chars:
            self.lower_trans = str.maketrans(self._lower_chars, self._upper_chars)
        if self._upper_chars:
            self.upper_trans = str.maketrans(self._upper_chars, self._lower_chars)

    # upperlower
    def lower(self):
        new_string = IString(self._irc_lower(self))
        new_string.set_std(self._std)
        return new_string

    def upper(self):
        new_string = IString(self._irc_upper(self))
        new_string.set_std(self._std)
        return new_string

    def _irc_lower(self, in_string):
        """Convert us to our lower-case equivalent, given our std."""
        conv_string = in_string
        if self._lower_trans is not None:
            conv_string = conv_string.translate(self._lower_trans)
        return str.lower(conv_string)

    def _irc_upper(self, in_string):
        """Convert us to our upper-case equivalent, given our std."""
        if self._upper_trans is not None:
            conv_string = in_string.translate(self._upper_trans)
        return str.upper(conv_string)

    # magic
    def __eq__(self, other):
        if not other or len(self) != len(other):
            return False
        for i in range(0, len(self)):
            if ord(self[i]) != ord(other[i]):
                return False
        return True

    def __lt__(self, other):
        for i in range(0, min(len(self), len(other))):
            if ord(self[i]) < ord(self[i]):
                return True
        return len(self) < len(other)

    def __le__(self, other):
        return self.__lt__(other) or len(self) < len(other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __gt__(self, other):
        return not self.__le__(other)

    def __ge__(self, other):
        return not self.__lt__(other)

    def __hash__(self):
        return hash(str(self._irc_lower(self)))

    # str methods
    # this is so we can just do normal .title() / .split / .etc calls as though IString were a str class
    def __getattribute__(self, name):
        f = str.__getattribute__(self, name)

        if not callable(f):
            return f

        this_dict = object.__getattribute__(self, '__dict__')
        if '_std' in this_dict:
            this_std = object.__getattribute__(self, '_std')

        def callback(*args, **kwargs):
            r = f(*args, **kwargs)
            if isinstance(r, str):
                new_string = IString(r)
                if '_std' in this_dict:
                    new_string.set_std(this_std)
                return new_string
            return r

        return partial(callback)


class ChannelJoiner(threading.Thread):
    """Thread to async join the server's channels, to not pause the server for ages."""

    def __init__(self, server, name, channels, wait_time):
        threading.Thread.__init__(self, name='ChannelJoiner-' + name)

        self.server = server
        self.channels = channels
        self.wait_time = wait_time

    def run(self):
        time.sleep(self.wait_time)

        if self.server.connected:
            for channel in self.channels:
                self.server.join(channel)


class IRC:
    """Wrapper for irclib's IRC class."""

    def __init__(self):
        self.irc = irc.client.IRC()

        self.info_funcs = []  # funcs to call when info updates

        self.servers = {}  # server connections
        self.connections = []  # dcc connections
        self.handlers = {
            'in': {},
            'out': {},
            'all': {},
        }

        self.irc.add_global_handler('all_events', self._handle_irclib)
        self.irc.remove_global_handler('irc', irc.client._ping_ponger)
        self.add_handler('in', 'ping', self._handle_ping, -42)
        self.add_handler('in', 'cap', self._handle_cap)

    # Servers
    def server(self, name, **kwargs):
        connection = ServerConnection(name, self, **kwargs)
        self.servers[name] = connection
        return connection

    def dcc(self, dcctype="chat"):
        c = irc.client.DCCConnection(dcctype)
        self.connections.append(c)
        return c

    def name(self, connection):
        """Given connection, return server name."""
        for server in self.servers:
            if self.servers[server].connection == connection:
                return server

    # Processing
    def process_once(self, timeout=0):
        self.irc.process_once(timeout)

    def process_forever(self, timeout=0.2):
        self.irc.process_forever(timeout)

    # Handling
    def add_handler(self, direction, event, handler, priority=0):
        if event not in self.handlers[direction]:
            self.handlers[direction][event] = []
        bisect.insort(self.handlers[direction][event], ((priority, handler)))

    def remove_handler(self, direction, event, handler):
        if event not in self.handlers[direction]:
            return 0
        for h in self.handlers[direction][event]:
            if handler == h[1]:
                self.handlers[direction][event].remove(h)

    def _handle_irclib(self, connection, event):
        if event.type in ['privmsg', 'pubmsg', 'privnotice', 'pubnotice', 'action', 'currenttopic',
                          'motd', 'endofmotd', 'yourhost', 'endofnames', 'ctcp', 'topic', 'quit',
                          'part', 'kick', 'kick', 'join', ]:
            event_arguments = []
            for arg in event.arguments:
                event_arguments.append(escape(arg))
        else:
            event_arguments = event.arguments

        #     event_arguments = []
        #     for arg in event.arguments():
        #         event_arguments.append(escape(arg))
        # if 'raw' not in event.eventtype():
        #     print("    ", event.eventtype(), ' ', str(event_arguments))

        new_event = Event(self, self.name(connection), 'in', event.type, event.source, event.target, event_arguments)
        self._handle_event(new_event)

    def _handle_event(self, event):
        """Call handle functions, and all that fun stuff."""
        self.servers[event.server].update_info(event)
        called = []
        for event_direction in [event.direction, 'all']:
            for event_type in [event.type, 'all']:
                if event_type in self.handlers[event_direction]:
                    for h in self.handlers[event_direction][event_type]:
                        if h[1] not in called:
                            called.append(h[1])
                            h[1](event)

    def _handle_ping(self, event):
        self.servers[event.server].pong(event.arguments[0])
        self.servers[event.server].last_ping = ping_timestamp()

    def _handle_cap(self, event):
        if event.arguments[0] == 'ACK':
            if self.servers[event.server]._first_cap:
                self.servers[event.server].cap('END')

    # Disconnect
    def disconnect_all(self, message):
        for name in self.servers.copy():
            self.servers[name].disconnect(message)
            del self.servers[name]

# ping timeouts
timeout_check_interval = {
    'minutes': 3,
}
timeout_length = {
    'minutes': 6,
}


def timestamp(**length_of_time_dict):
    """Returns timestamp for given length of time."""
    time_diff = datetime.timedelta(**length_of_time_dict)
    return time_diff.total_seconds()


def ping_timestamp():
    """Returns a ping timestamp for right now."""
    time_now = datetime.datetime.utcnow()
    return calendar.timegm(time_now.timetuple())


class ServerConnection:
    """IRC Server Connection."""

    def __init__(self, name, irc, timeout_check_interval=timeout_check_interval, timeout_length=timeout_length):
        self.name = name
        self.irc = irc
        self.info = {
            'name': name,
            'connection': {},
            'channels': {},
            'users': {},
            'server': {}
        }
        self.connected = False

        # check if timed out every interval, defaults to 2 minutes
        self.timeout_check_interval = timeout_check_interval
        # if this long has passed without server ping, we consider ourselves timed out. defaults to 5 minutes
        self.timeout_length = timeout_length

    # Connection
    def connect(self, address, port, nick, password=None, username=None, ircname=None, localaddress="", localport=0, sslsock=False, ipv6=False, autojoin_channels=[], wait_time=5):
        self.connection = self.irc.irc.server()
        self.info['connection'] = {
            'address': address,
            'port': port,
            'nick': nick,
        }
        if password is not None:
            self.info['connection']['password'] = password
        if username is not None:
            self.info['connection']['username'] = username
        if ircname is not None:
            self.info['connection']['ircname'] = ircname
        if localaddress != "":
            self.info['connection']['localaddress'] = localaddress
        if localport != 0 or localaddress != "":
            self.info['connection']['localport'] = localport
        if sslsock is not False:
            self.info['connection']['sslsock'] = sslsock
        if ipv6 is not False:
            self.info['connection']['ipv6'] = ipv6

        if sslsock:
            Factory = irc.connection.Factory(wrapper=ssl.wrap_socket, ipv6=ipv6)
        else:
            Factory = irc.connection.Factory(ipv6=ipv6)

        self.autojoin_channels = autojoin_channels
        self.autojoin_wait_time = wait_time

        self.connection.connect(address, port, nick, password, username, ircname, Factory)
        self.connection.buffer.errors = 'replace'

        self._send_startup()

        self.irc.irc.execute_every(timestamp(**self.timeout_check_interval), self._timeout_check)

    def _timeout_check(self):
        """Checks if we've timed out. Reconnects if so."""
        if self.connection.connected:
            timeout_seconds = self.last_ping + timestamp(**self.timeout_length)
            now_seconds = ping_timestamp()
            if now_seconds > timeout_seconds:
                self.disconnect('Ping timeout.')
                # we disconnect now, wait another `timeout_check_interval`, and then reconnect
                return

        else:
            self.reconnect()

    def reconnect(self):
        self.connection.reconnect()

        self._send_startup()

    def disconnect(self, message):
        # don't wipe info['connection'] in case we reconnect
        self.info['channels'] = {}
        self.info['users'] = {}

        self.connected = False
        self.connection.disconnect(message)

    def _send_startup(self):
        """Send the stuff we need to at startup."""
        self._first_cap = True
        self.cap('REQ', 'multi-prefix')

        self.connected = True
        self.last_ping = ping_timestamp()

        # autojoining channels
        if self.autojoin_channels:
            ChannelJoiner(self, self.name, self.autojoin_channels, self.autojoin_wait_time).start()

    # IRC Commands
    def action(self, target, action):
        self.connection.action(target, unescape(action))
        self.irc._handle_event(Event(self.irc, self.name, 'out', 'action', self.info['connection']['nick'], target, [action]))

    def admin(self, server=''):
        self.connection.admin(server)
        self.irc._handle_event(Event(self.irc, self.name, 'out', 'admin', self.info['connection']['nick'], server))

    def cap(self, subcommand, args=''):
        self.connection.cap(subcommand, args)
        if args:
            self.irc._handle_event(Event(self.irc, self.name, 'out', 'cap', self.info['connection']['nick'], [subcommand, args]))
        else:
            self.irc._handle_event(Event(self.irc, self.name, 'out', 'cap', self.info['connection']['nick'], [subcommand]))

    def ctcp(self, type, target, string):
        self.connection.ctcp(type, target, string)
        if len(string.split()) > 1:
            (ctcp_type, ctcp_args) = string.split(' ', 1)
        else:
            (ctcp_type, ctcp_args) = (string, '')
        self.irc._handle_event(Event(self.irc, self.name, 'out', 'ctcp', self.info['connection']['nick'], target, [ctcp_type, ctcp_args]))

    def ctcp_reply(self, ip, string):
        self.connection.ctcp_reply(ip, string)
        if len(string.split()) > 1:
            (ctcp_type, ctcp_args) = string.split(' ', 1)
        else:
            (ctcp_type, ctcp_args) = (string, '')
        self.irc._handle_event(Event(self.irc, self.name, 'out', 'ctcp_reply', self.info['connection']['nick'], ip, [ctcp_type, ctcp_args]))

    def join(self, channel, key=''):
        self.connection.join(channel, key)
        self.irc._handle_event(Event(self.irc, self.name, 'out', 'join', self.info['connection']['nick'], channel, [key]))

    def mode(self, target, modes=''):
        self.connection.mode(target, modes)

    def part(self, channel, message=''):
        self.connection.part(channel, message)

    def pong(self, target):
        self.connection.pong(target)
        self.irc._handle_event(Event(self.irc, self.name, 'out', 'pong', self.info['connection']['nick'], target))

    def privmsg(self, target, message, chanserv_escape=True):
        if irc.client.is_channel(target):
            command = 'pubmsg'
            if chanserv_escape and message[0] == '.':
                message_escaped = message[0]
                message_escaped += '@b@b'
                message_escaped += message[1:]
                message = message_escaped
        else:
            command = 'privmsg'

        self.connection.privmsg(target, unescape(message))
        self.irc._handle_event(Event(self.irc, self.name, 'out', command, self.info['connection']['nick'], target, [message]))

    # string handling
    def istring(self, in_string):
        """Returns an IString without servers' casemapping."""
        new_string = IString(in_string)
        new_string.set_std(self._istring_casemapping)
        return new_string

    # Internal book-keeping
    def update_info(self, event):
        changed = False

        if event.type == 'cap':
            if 'cap' not in self.info['server']:
                self.info['server']['cap'] = {}  # dict for future compatability

            if len(event.arguments) > 0 and event.arguments[0] == 'ACK':
                for capability in event.arguments[1].split():
                    if capability[0] == '-':
                        self.info['server']['cap'][capability[1:]] = False
                    else:
                        self.info['server']['cap'][capability] = True

        elif event.type == 'featurelist':
            if 'isupport' not in self.info['server']:
                self.info['server']['isupport'] = {}

            for feature in event.arguments[:-1]:
                # negating
                if feature[0] == '-':
                    feature = feature[1:]
                    if feature in self.info['server']['isupport']:
                        del self.info['server']['isupport'][feature]
                # setting
                elif ('=' in feature) and (len(feature.split('=')) > 1):
                    feature_name, feature_value = feature.split('=')

                    if feature_name == 'PREFIX':  # channel user prefixes
                        channel_modes, channel_chars = feature_value.split(')')
                        channel_modes = channel_modes[1:]
                        self.info['server']['isupport'][feature_name] = [channel_modes, channel_chars]

                    elif feature_name == 'CHANMODES':  # channel mode letters
                        self.info['server']['isupport'][feature_name] = feature_value.split(',')

                    else:
                        self.info['server']['isupport'][feature_name] = feature_value

                    # sets up our istring casemapping
                    if feature_name == 'CASEMAPPING':
                        self._istring_casemapping = feature_value
                else:
                    if feature[-1] == '=':
                        feature = feature[:-1]
                    self.info['server']['isupport'][feature] = True

        elif event.type == 'join' and event.direction == 'in':
            user = self.istring(event.source).lower()
            user_nick = NickMask(user).nick
            channel = self.istring(event.target).lower()

            self.create_user(user)
            self.create_channel(channel)
            self.get_channel_info(channel)['users'][user_nick] = ''

            # request channel modes on join
            if user_nick == self.info['connection']['nick']:
                self.mode(channel)
            changed = True

        elif event.type == 'namreply':
            channel = self.istring(event.arguments[1]).lower()
            names_list = self.istring(event.arguments[2]).lower().split()

            # merge user list if it already exists, used for heaps of nicks
            if 'users' not in self.get_channel_info(channel):
                self.get_channel_info(channel)['users'] = {}
            for user in names_list:
                # supports multi-prefix
                user_privs = ''
                while user[0] in self.info['server']['isupport']['PREFIX'][1]:
                    user_privs += user[0]
                    user = user[1:]
                user_nick = user

                self.create_user(user_nick)
                self.get_channel_info(channel)['users'][user_nick] = user_privs
                changed = True

        elif event.type == 'currenttopic':
            channel = self.istring(event.arguments[0]).lower()

            self.get_channel_info(channel)['topic']['topic'] = event.arguments[1]
            changed = True

        elif event.type == 'topicinfo':
            channel = self.istring(event.arguments[0]).lower()

            self.get_channel_info(channel)['topic']['user'] = event.arguments[1]
            self.get_channel_info(channel)['topic']['time'] = event.arguments[2]
            changed = True

        elif event.type == 'nick':
            user_old = self.istring(event.source).lower()
            user_old_nick = NickMask(user_old).nick
            user_new_nick = self.istring(event.target).lower()

            for channel in self.info['channels'].copy():
                if user_old_nick in self.get_channel_info(channel)['users']:
                    self.get_channel_info(channel)['users'][user_new_nick] = self.get_channel_info(channel)['users'][user_old_nick]
                    del self.get_channel_info(channel)['users'][user_old_nick]
            self.info['users'][user_new_nick] = self.info['users'][user_old_nick]
            del self.info['users'][user_old_nick]
            changed = True

        elif event.type == 'part':
            user = self.istring(event.source).lower()
            user_nick = NickMask(user).nick
            channel = self.istring(event.target).lower()

            if user_nick == self.info['connection']['nick']:
                self.del_channel_info(channel)
            else:
                del self.get_channel_info(channel)['users'][user_nick]
            changed = True

        elif event.type == 'kick':
            user_nick = self.istring(event.arguments[0]).lower()
            channel = self.istring(event.target).lower()

            if event.arguments[0] == self.info['connection']['nick']:
                self.del_channel_info(channel)
            else:
                del self.get_channel_info(channel)['users'][user_nick]
            changed = True

        elif event.type == 'quit':
            user = self.istring(event.source).lower()
            user_nick = NickMask(user).nick

            for channel in self.info['channels']:
                if user_nick in self.get_channel_info(channel)['users']:
                    del self.get_channel_info(channel)['users'][user_nick]
            del self.info['users'][user_nick]
            changed = True

        elif event.type == 'channelcreate':
            channel = self.istring(event.arguments[0]).lower()

            self.get_channel_info(channel)['created'] = event.arguments[1]

        elif event.type in ['mode', 'channelmodeis']:
            unary_modes = self.info['server']['isupport']['PREFIX'][0] + self.info['server']['isupport']['CHANMODES'][0] + self.info['server']['isupport']['CHANMODES'][1] + self.info['server']['isupport']['CHANMODES'][2]

            if event.type == 'mode':
                channel = self.istring(event.target).lower()
                mode_list = ' '.join(event.arguments)
            elif event.type == 'channelmodeis':
                channel = self.istring(event.arguments[0]).lower()
                mode_list = ' '.join(event.arguments[1:])

            if channel not in self.info['channels']:
                return

            for mode in irc.modes._parse_modes(mode_list, unary_modes):

                # User prefix modes - voice, op, etc
                if mode[1] in self.info['server']['isupport']['PREFIX'][0]:
                    mode_letter, mode_char = mode[1], self.info['server']['isupport']['PREFIX'][1][self.info['server']['isupport']['PREFIX'][0].index(mode[1])]

                    if mode[0] == '-':
                        if mode_char in self.get_channel_info(channel)['users'][mode[2]]:
                            self.get_channel_info(channel)['users'][mode[2]] = self.get_channel_info(channel)['users'][mode[2]].replace(mode_char, '')
                    elif mode[0] == '+':
                        if mode_char not in self.get_channel_info(channel)['users'][mode[2]]:
                            self.get_channel_info(channel)['users'][mode[2]] += mode_char

                # List modes
                if mode[1] in self.info['server']['isupport']['CHANMODES'][0]:
                    if mode[0] == '-':
                        if mode[2] in self.get_channel_info(channel)['modes'][mode[1]]:
                            del self.get_channel_info(channel)['modes'][mode[1]][self.get_channel_info(channel)['modes'][mode[1]].index(mode[2])]
                    elif mode[0] == '+':
                        self.get_channel_info(channel)['modes'][mode[1]].append(mode[2])

                # Channel modes, paramaters
                if mode[1] in (self.info['server']['isupport']['CHANMODES'][1] + self.info['server']['isupport']['CHANMODES'][2]):
                    if mode[0] == '-':
                        if mode[1] in self.get_channel_info(channel)['modes']:
                            del self.get_channel_info(channel)['modes'][mode[1]]
                    elif mode[0] == '+':
                        self.get_channel_info(channel)['modes'][mode[1]] = mode[2]

                # Channel modes, no params
                if mode[1] in self.info['server']['isupport']['CHANMODES'][3]:
                    if mode[0] == '-':
                        if mode[1] in self.get_channel_info(channel)['modes']:
                            del self.get_channel_info(channel)['modes'][mode[1]]
                    elif mode[0] == '+':
                        self.get_channel_info(channel)['modes'][mode[1]] = True

            changed = True

        if changed:
            for func in self.irc.info_funcs:
                func()

    def create_user(self, user):
        user = self.istring(user).lower()
        user_nick = NickMask(user).nick

        if user_nick not in self.info['users']:
            self.info['users'][user_nick] = {}

    def create_channel(self, channel):
        channel = self.istring(channel).lower()

        if channel not in self.info['channels']:
            self.info['channels'][channel] = {
                'topic': {},
                'users': {},
                'modes': {}
            }
            for mode in self.info['server']['isupport']['CHANMODES'][0]:
                self.get_channel_info(channel)['modes'][mode] = []

    # setting/getting info
    def del_channel_info(self, channel_name):
        """Delete channel info dict."""
        channel_name = self.istring(channel_name).lower()
        del self.info['channels'][channel_name]

    def get_channel_info(self, channel_name):
        """Return channel info dict."""
        channel_name = self.istring(channel_name).lower()
        return self.info['channels'][channel_name]

    def get_user_info(self, user_nick):
        """Return user info dict."""
        user_nick = self.istring(user_nick).lower()
        return self.info['users'][user_nick]

    # privs
    def is_prived(self, user_privs, required_level):
        """Check if the given user privs meet the required level or above.

        Args:
            user_privs: String like '&@+', '&', etc
            required_level: String like 'o', 'h', 'v'
        """
        privs_we_support = self.info['server']['isupport']['PREFIX']

        # changing h, q, a to something we can use if necessary
        if required_level not in privs_we_support[0]:
            conversion_dict = {
                'h': 'o',
                'a': 'o',
                'q': 'o',
            }
            required_level = conversion_dict.get(required_level, None)
            if required_level is None:
                print('We do not have required_level:', required_level)
                return False

        # get list of levels we can use
        index = privs_we_support[0].index(required_level)
        acceptable_prefixes = privs_we_support[1][:index + 1]

        for prefix in user_privs:
            if prefix in acceptable_prefixes:
                return True

        return False


class Event:
    """IRC Event."""

    def __init__(self, irc, server, direction, type, source, target, arguments=None):
        self.server = server
        self.direction = direction
        self.type = type
        self.source = source
        self.target = target
        if arguments:
            self.arguments = arguments
        else:
            self.arguments = []
        if direction == 'in':
            if target == irc.servers[server].info['connection']['nick']:
                self.from_to = str(source).split('!')[0]
            else:
                self.from_to = str(target).split('!')[0]
        else:
            self.from_to = str(target).split('!')[0]


# String escaping/unescaping
_unescape_dict = {
    '@': '@',
    'b': '\x02',  # bold
    'c': '\x03',  # color
    'i': '\x1d',  # italic
    'u': '\x1f',  # underline
    'r': '\x0f',  # reset
}


def escape(string):
    """Change IRC codes into goshu codes."""
    string = string.replace('@', '@{@}')
    string = string.replace('\x02', '@b')  # bold
    string = string.replace('\x03', '@c')  # color
    string = string.replace('\x1d', '@i')  # italic
    string = string.replace('\x1f', '@u')  # underline
    string = string.replace('\x0f', '@r')  # reset
    return string


def unescape(in_string, unescape=_unescape_dict):
    """Change goshu codes into IRC codes.

    Basically, you can either have a one-character control code after @,
    or you can have curly brackets, along with a string."""
    if len(in_string) < 1:
        return ''

    out_string = ''
    curly_buffer = ''
    curly_buffer_active = False
    while True:

        # multi-char sequences
        if curly_buffer_active and (in_string[0] == '}'):
            if curly_buffer in unescape:
                # you can also pass functions, rather than strings
                # needed for stuff like {randomchannelnick}
                out_string += unescape_format(unescape[curly_buffer])
            else:
                out_string += '@{' + curly_buffer + '}'
            curly_buffer = ''
            curly_buffer_active = False

        elif curly_buffer_active:
            curly_buffer += in_string[0]

        # single-char
        elif in_string[0] == '@':
            if len(in_string) < 2:
                break

            if in_string[1] == '{':
                curly_buffer_active = True

            elif in_string[1] in unescape:
                out_string += unescape_format(unescape[in_string[1]])

            else:
                out_string += '@' + in_string[1]

            in_string = in_string[1:]

        # regular text
        else:
            out_string += in_string[0]

        # book-keeping
        in_string = in_string[1:]

        if len(in_string) < 1:
            break

    return out_string


def unescape_format(format):
    if isinstance(format, str):
        return format
    elif isinstance(format, collections.Sequence):
        return format[0](format[1])
    elif isinstance(format, collections.Callable):
        return format()


def remove_control_codes(line):
    new_line = ''
    while len(line) > 0:
        try:
            if line[0] == '@':
                line = line[1:]

                if line[0] == '@':
                    new_line += '@'
                    line = line[1:]

                elif line[0] == 'c':
                    line = line[1:]
                    if line[0].isdigit():
                        line = line[1:]
                        if line[0].isdigit():
                            line = line[1:]
                            if line[0] == ',':
                                line = line[1:]
                                if line[0].isdigit():
                                    line = line[1:]
                                    if line[0].isdigit():
                                        line = line[1:]
                        elif line[0] == ',':
                            line = line[1:]
                            if line[0].isdigit():
                                line = line[1:]
                                if line[0].isdigit():
                                    line = line[1:]

                elif line[0] == '{':
                    while line[0] != '}':
                        line = line[1:]
                    line = line[1:]

                else:
                    line = line[1:]

            else:
                new_line += line[0]
                line = line[1:]
        except IndexError:
            ...
    return new_line


# Wrappers to default irc classes/functions
class NickMask(irc.client.NickMask):
    ...


def is_channel(name):
    return irc.client.is_channel(name)
