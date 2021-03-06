# This file is part of beets.
# Copyright 2013, Peter Schnebel.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

# requires python-mpd to run. install with:  pip install python-mpd

import logging
import mpd
import socket
import select
import time
import os

from beets import ui
from beets import config
from beets import plugins
from beets import library
from beets.util import displayable_path

log = logging.getLogger('beets')

# If we lose the connection, how many times do we want to retry and how
# much time should we wait between retries?
RETRIES = 10
RETRY_INTERVAL = 5


# Use the MPDClient internals to get unicode.
# see http://www.tarmack.eu/code/mpdunicode.py for the general idea
class MPDClient(mpd.MPDClient):
    def _write_command(self, command, args=[]):
        args = [unicode(arg).encode('utf-8') for arg in args]
        super(MPDClient, self)._write_command(command, args)

    def _read_line(self):
        line = super(MPDClient, self)._read_line()
        if line is not None:
            return line.decode('utf-8')
        return None


class Client(object):
    def __init__(self, lib):
        self.lib = lib

        self.music_directory = config['mpdstats']['music_directory'].get()
        self.do_rating = config['mpdstats']['rating'].get(bool)
        self.rating_mix = config['mpdstats']['rating_mix'].get(float)

        self.client = MPDClient()

    def mpd_connect(self):
        """Connect to the MPD.
        """
        host = config['mpd']['host'].get(unicode)
        port = config['mpd']['port'].get(int)
        log.info(u'mpdstats: connecting to {0}:{1}'.format(host, port))
        try:
            self.client.connect(host, port)
        except socket.error as e:
            raise ui.UserError('could not connect to MPD: {0}'.format(e))

        password = config['mpd']['password'].get(unicode)
        if password:
            try:
                self.client.password(password)
            except mpd.CommandError as e:
                raise ui.UserError(
                    'could not authenticate to MPD: {0}'.format(e)
                )

    def mpd_disconnect(self):
        """Disconnect from the MPD.
        """
        self.client.close()
        self.client.disconnect()

    def is_url(self, path):
        """Try to determine if the path is an URL.
        """
        # FIXME:  cover more URL types ...
        return path[:7] == "http://"

    def mpd_playlist(self):
        """Return the currently active playlist.  Prefixes paths with the
        music_directory, to get the absolute path.
        """
        result = {}
        for entry in self.mpd_func('playlistinfo'):
            if not self.is_url(entry['file']):
                result[entry['id']] = os.path.join(
                    self.music_directory, entry['file'])
            else:
                result[entry['id']] = entry['file']
        return result

    def mpd_status(self):
        """Return the current status of the MPD.
        """
        return self.mpd_func('status')

    def beets_get_item(self, path):
        """Return the beets item related to path.
        """
        query = library.MatchQuery('path', path)
        item = self.lib.items(query).get()
        if item:
            return item
        else:
            log.info(u'mpdstats: item not found: {0}'.format(
                displayable_path(path)
            ))

    def rating(self, play_count, skip_count, rating, skipped):
        """Calculate a new rating based on play count, skip count, old rating
        and the fact if it was skipped or not.
        """
        if skipped:
            rolling = (rating - rating / 2.0)
        else:
            rolling = (rating + (1.0 - rating) / 2.0)
        stable = (play_count + 1.0) / (play_count + skip_count + 2.0)
        return self.rating_mix * stable \
                + (1.0 - self.rating_mix) * rolling

    def beetsrating(self, item, skipped):
        """ Update the rating of the beets item.
        """
        if self.do_rating and item:
            attribute = 'rating'
            item[attribute] = self.rating(
                    (int)(item.get('play_count', 0)),
                    (int)(item.get('skip_count', 0)),
                    (float)(item.get(attribute, 0.5)),
                    skipped)
            log.debug(u'mpdstats: updated: {0} = {1} [{2}]'.format(
                attribute,
                item[attribute],
                displayable_path(item.path),
            ))
            item.write()
            if item._lib:
                item.store()

    def beets_update(self, item, attribute, value=None, increment=None):
        """ Update the beets item.  Set attribute to value or increment the
        value of attribute.
        """
        if item is not None:
            changed = False
            if value is not None:
                changed = True
                item[attribute] = value
            if increment is not None:
                changed = True
                item[attribute] = (float)(item.get(attribute, 0)) + increment
            if changed:
                log.debug(u'mpdstats: updated: {0} = {1} [{2}]'.format(
                    attribute,
                    item[attribute],
                    displayable_path(item.path),
                ))
                item.write()
                if item._lib:
                    item.store()

    def mpd_func(self, func, **kwargs):
        """Wrapper for requests to the MPD server.  Tries to re-connect if the
        connection was lost ...
        """
        for i in range(RETRIES):
            try:
                if func == 'send_idle':
                    # special case, wait for an event
                    self.client.send_idle()
                    try:
                        select.select([self.client], [], [])
                    except select.error:
                        # Happens during shutdown and during MPDs
                        # library refresh.
                        time.sleep(RETRY_INTERVAL)
                        self.mpd_connect()
                        continue
                    except KeyboardInterrupt:
                        self.running = False
                        return None
                    return self.client.fetch_idle()
                elif func == 'playlistinfo':
                    return self.client.playlistinfo()
                elif func == 'status':
                    return self.client.status()
            except (select.error, mpd.ConnectionError) as err:
                # happens during shutdown and during MPDs library refresh
                log.error(u'mpdstats: {0}'.format(err))
                time.sleep(RETRY_INTERVAL)
                self.mpd_disconnect()
                self.mpd_connect()
                continue
        else:
            # if we exited without breaking, we couldn't reconnect in time :(
            raise ui.UserError(u'failed to re-connect to MPD server')

    def run(self):
        self.mpd_connect()
        self.running = True # exit condition for our main loop
        startup = True # we need to do some special stuff on startup
        now_playing = None # the currently playing song
        current_playlist = None # the currently active playlist
        while self.running:
            if startup:
                # don't wait for an event, read in status and playlist
                events = ['player']
                startup = False
            else:
                # wait for an event from the MPD server
                events = self.mpd_func('send_idle')
                if events is None:
                    continue # probably KeyboardInterrupt
                log.debug(u'mpdstats: events: {0}'.format(events))

            if 'player' in events:
                status = self.mpd_status()
                if status is None:
                    continue # probably KeyboardInterrupt
                if status['state'] == 'stop':
                    log.info(u'mpdstats: stop')
                    now_playing = None
                elif status['state'] == 'pause':
                    log.info(u'mpdstats: pause')
                    now_playing = None
                elif status['state'] == 'play':
                    current_playlist = self.mpd_playlist()
                    if len(current_playlist) == 0:
                        continue # something is wrong ...
                    song = current_playlist[status['songid']]
                    if self.is_url(song):
                        # we ignore streams
                        log.info(u'mpdstats: play/stream: {0}'.format(song))
                    else:
                        beets_item = self.beets_get_item(song)
                        t = status['time'].split(':')
                        remaining = int(t[1]) - int(t[0])

                        if now_playing and \
                           now_playing['path'] != song:
                            # song change
                            # get the difference of when the song was supposed
                            # to end to now.  if it's smaller then 10 seconds,
                            # we consider if fully played.
                            diff = abs(now_playing['remaining'] -
                                    (time.time() -
                                    now_playing['started']))
                            if diff < 10.0:
                                log.info(u'mpdstats: played: {0}'.format(
                                    displayable_path(now_playing['path'])
                                ))
                                skipped = False
                            else:
                                log.info(u'mpdstats: skipped: {0}'.format(
                                    displayable_path(now_playing['path'])
                                ))
                                skipped = True
                            if skipped:
                                self.beets_update(now_playing['beets_item'],
                                        'skip_count', increment=1)
                            else:
                                self.beets_update(now_playing['beets_item'],
                                        'play_count', increment=1)
                            self.beetsrating(now_playing['beets_item'],
                                    skipped)
                        now_playing = {
                            'started':    time.time(),
                            'remaining':  remaining,
                            'path':       song,
                            'beets_item': beets_item,
                        }
                        log.info(u'mpdstats: playing: {0}'.format(
                            displayable_path(now_playing['path'])
                        ))
                        self.beets_update(now_playing['beets_item'],
                                'last_played', value=int(time.time()))
                else:
                    log.info(u'mpdstats: status: {0}'.format(status))


class MPDStatsPlugin(plugins.BeetsPlugin):
    def __init__(self):
        super(MPDStatsPlugin, self).__init__()
        self.config.add({
            'music_directory': config['directory'].as_filename(),
            'rating':          True,
            'rating_mix':      0.75,
        })
        config['mpd'].add({
            'host':            u'localhost',
            'port':            6600,
            'password':        u'',
        })

    def commands(self):
        cmd = ui.Subcommand('mpdstats',
                help='run a MPD client to gather play statistics')
        cmd.parser.add_option('--host', dest='host',
                type='string',
                help='set the hostname of the server to connect to')
        cmd.parser.add_option('--port', dest='port',
                type='int',
                help='set the port of the MPD server to connect to')
        cmd.parser.add_option('--password', dest='password',
                type='string',
                help='set the password of the MPD server to connect to')

        def func(lib, opts, args):
            self.config.set_args(opts)

            # Overrides for MPD settings.
            if opts.host:
                config['mpd']['host'] = opts.host.decode('utf8')
            if opts.port:
                config['mpd']['host'] = int(opts.port)
            if opts.password:
                config['mpd']['password'] = opts.password.decode('utf8')

            Client(lib).run()

        cmd.func = func
        return [cmd]
