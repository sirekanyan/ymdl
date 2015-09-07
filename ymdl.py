#!/usr/bin/env python3

# Yandex.Music downloader
#
# Copyright (c) 2015 Daniel Plachotich
#
# This software is provided 'as-is', without any express or implied
# warranty. In no event will the authors be held liable for any damages
# arising from the use of this software.
#
# Permission is granted to anyone to use this software for any purpose,
# including commercial applications, and to alter it and redistribute it
# freely, subject to the following restrictions:
#
# 1. The origin of this software must not be misrepresented; you must not
#    claim that you wrote the original software. If you use this software
#    in a product, an acknowledgement in the product documentation would be
#    appreciated but is not required.
# 2. Altered source versions must be plainly marked as such, and must not be
#    misrepresented as being the original software.
# 3. This notice may not be removed or altered from any source distribution.

# TODO:
# * Authentication for downloading private playlists.

import argparse
import logging

import os

import urllib.request
import urllib.parse
from urllib.error import URLError
import json
from hashlib import md5
import mimetypes
import itertools

from mutagen import id3, mp3


class YmdlError(Exception):
    pass

class YmdlWrongUrlError(YmdlError, ValueError):
    pass


LINE_WIDTH = 79
LINE = '=' * LINE_WIDTH

YM_URL = 'https://music.yandex.ru'

YM_TRACK_SRC_INFO = (
    'https://storage.mds.yandex.net/download-info/{storageDir}/2?format=json'
    )

YM_TRACK_INFO = YM_URL + '/handlers/track.jsx?track={track}'
YM_ALBUM_INFO = YM_URL + '/handlers/album.jsx?album={album}'
YM_ARTIST_INFO = YM_URL + '/handlers/artist.jsx?artist={artist}&what={what}'
YM_PLAYLIST_INFO = (
    YM_URL + '/handlers/playlist.jsx?owner={users}&kinds={playlists}'
    )

# Some additional fields in YM info for folder names and ID3 tags.
# 'artists' field will be separated to 'artists' and 'composers'.
FLD_COMPOSERS = 'composers'
# Number of volume.
FLD_VOLUMENUM = 'volumeNum'
# Track number (in volume).
FLD_TRACKNUM = 'trackNum'

FMT_TITLE = '%t'
FMT_ARTIST = '%a'
FMT_ALBUM = '%A'
FMT_TRACKN = '%n'
FMT_NTRACKS = '%N'
FMT_YEAR = '%y'
FMT_LABEL = '%l'

DTN_SINGLE = '%a - %t'
DTN_ALBUM = '%n - %t'
DTN_PLAYLIST = '%n - %a - %t'
DTN_ARTIST = '%t'

DAN_SINGLE_ALBUM = '%a - %A (%y)'
DAN_ARTIST_ALBUMS = '%y - %A'


NAME_INFO = (
    'Name formatting:\n'
    '  ' + FMT_TITLE   + ' - track title\n'
    '  ' + FMT_ARTIST  + ' - artist\n'
    '  ' + FMT_ALBUM   + ' - album\n'
    '  ' + FMT_TRACKN  + ' - track number\n'
    '  ' + FMT_NTRACKS + ' - total tracks in album (for track - in volume)\n'
    '  ' + FMT_YEAR    + ' - year\n'
    '  ' + FMT_LABEL   + ' - label\n'
    '%t and %n will be ignored (deleted) in -a/--album_name.\n'
    '\n'
    'Default track name format:\n'
    '  Single track:      "'  + DTN_SINGLE + '"\n'
    '  Track in album:    "'  + DTN_ALBUM + '"\n'
    '  Playlist:          "'  + DTN_PLAYLIST + '"\n'
    '  Artist\'s tracks:   "' + DTN_ARTIST + '"\n'
    '\n'
    'Default album directory name format:\n'
    '  Single album:      "'  + DAN_SINGLE_ALBUM + '"\n'
    '  Artist\'s albums:   "' + DAN_ARTIST_ALBUMS + '"\n'
    '\n'
    )

# These sizes were available on Yandex.Music at the moment of writing these
# lines. Any other values will be clamped to nearest bigger one.'
COVER_SIZES = [
    30, 40, 50, 75, 80, 100, 150, 160, 200, 300, 400, 460, 600, 700, 1000
    ]


parser = argparse.ArgumentParser(
    description='Yandex.Music downloader',
    usage='%(prog)s [OPTIONS] URL [URL..]',
    epilog=NAME_INFO,
    formatter_class=argparse.RawDescriptionHelpFormatter)

parser.add_argument(
    '-v', '--version', action='version', version='%(prog)s 0.5.1')
parser.add_argument(
    '-q', '--quiet', action='store_true',
    help='Don\'t print anything.')

parser.add_argument(
    'url', nargs=argparse.REMAINDER,
    help='URL from Yandex.Music (track, album, artist or public playlist).')
parser.add_argument(
    '-b', '--batch_file', metavar='FILE',
    type=argparse.FileType('r', encoding='utf-8'),
    help='File containing URLs to download (or "-" for stdin).')
parser.add_argument(
    '-o', '--out', default='.',
    help='Output directory. Default is current directory.')

parser.add_argument(
    '-t', '--track_name', metavar='NAME',
    help=('Formatted name of tracks. '
          'Default value depends on URL (read below).'))
parser.add_argument(
    '-a', '--album_name', metavar='NAME',
    help=('Formatted name of album directories. '
          'Default value depends on URL (read below).'))
parser.add_argument(
    '-V', '--volume_prefix', metavar='PREFIX', default='CD',
    help='Prefix of album volume folders (default = "CD").')
parser.add_argument(
    '-c', '--cover', metavar='SIZE', type=int, default=700, dest='cover_size',
    help=('Size of cover that will be saved to the album folder '
          '(default = 700). Zero means no cover.'))
parser.add_argument(
    '-C', '--cover_id3', metavar='SIZE', type=int, default=300,
    dest='cover_id3_size',
    help='Same as -c/--cover, but for ID3 cover (default = 300).')
parser.add_argument(
    '--also', action='store_true',
    help=('If artist\'s URL given, download other albums associated '
          'with artist (compilations, soundtracks, etc.) instead of main '
          'albums.'))
parser.add_argument(
    '--genre', action='store_true',
    help='Write "genre" tag to ID3.')
parser.add_argument(
    '--m3u', action='store_true',
    help='Create m3u8 playlist.')

args = parser.parse_args()


def size_to_str(byte_size):
    '''Convert size in bytes to string in SI format.'''
    size = byte_size / 1024 / 1024
    for s in ('MB', 'GB', 'TB'):
        if size < 1024:
            break
        size /= 1024
    return '{:.2f} {}'.format(size, s)


def time_to_str(ms):
    '''Convert time in milliseconds to string in min:sec format.'''
    minutes, ms = divmod(ms, 60000)
    seconds = ms // 1000
    return '{}:{:02}'.format(minutes, seconds)


_FNAME_TRANS = {ord('"'): "''"}
_FNAME_TRANS.update(str.maketrans(
    '\\/*', '--_', '<>:|?'))

def filename(s):
    '''Create file name compatible with MS file systems.'''
    return s.translate(_FNAME_TRANS).rstrip('. ')


def make_extinf(track, file_path):
    return '#EXTINF:{},{} - {}\n{}\n'.format(
        track['durationMs'] // 1000, track['artists'], track['title'],
        file_path)


def save_m3u(extinfs, save_path):
    if not extinfs:
        return

    os.makedirs(save_path, exist_ok=True)
    with open(os.path.join(save_path, 'play.m3u8'), 'w',
              encoding='utf-8-sig') as f:
        f.write('#EXTM3U\n')
        f.writelines(extinfs)


def write_id3(mp3_file, track, cover=None):
    t = mp3.Open(mp3_file)
    if not t.tags:
        t.add_tags()

    album = track['albums'][0]

    t_add = t.tags.add
    t_add(id3.TIT2(encoding=3, text=track['title']))
    t_add(id3.TPE1(encoding=3, text=track['artists']))
    t_add(id3.TCOM(encoding=3, text=track[FLD_COMPOSERS]))
    t_add(id3.TALB(encoding=3, text=album['title']))

    if 'labels' in album:
        t_add(id3.TPUB(encoding=3,
                       text=', '.join(l['name'] for l in album['labels'])))
    if FLD_TRACKNUM in track:
        tnum = '{}/{}'.format(track[FLD_TRACKNUM], album['trackCount'])
        t_add(id3.TRCK(encoding=3, text=tnum))
    if FLD_VOLUMENUM in album:
        t_add(id3.TPOS(encoding=3, text=str(album[FLD_VOLUMENUM])))
    if 'year' in album:
        t_add(id3.TDRC(encoding=3, text=str(album['year'])))
    if args.genre:
        t_add(id3.TCON(encoding=3, text=album['genre'].title()))
    if cover:
        t_add(id3.APIC(encoding=3, desc='', mime=cover.mime,
                       type=3, data=cover.data))

    t.tags.update_to_v23()
    t.save(v1=id3.ID3v1SaveOptions.CREATE, v2_version=3)


_DL_CHUNK_SIZE = 128 * 1024
_DL_BAR_SIZE = 40
_DL_PART_EXT = '.part'

def download_file(url, save_as):
    file_dir, file_name = os.path.split(save_as)
    if os.path.exists(save_as):
        raise FileExistsError('{} already exists'.format(file_name))

    request = urllib.request.Request(url)
    file_part = save_as + _DL_PART_EXT
    if os.path.isfile(file_part):
        file_part_size = os.path.getsize(file_part)
        mode = 'ab'
        request.add_header('Range', 'bytes={}-'.format(file_part_size))
    else:
        file_part_size = 0
        mode = 'wb'

    response = urllib.request.urlopen(request)
    file_size = file_part_size + int(response.getheader('Content-Length'))
    os.makedirs(file_dir, exist_ok=True)

    info = ('\r[{:<' + str(_DL_BAR_SIZE) + '}] '
            '{:>6.1%} ({} / ' + size_to_str(file_size) + ')')
    with open(file_part, mode) as f:
        while True:
            chunk = response.read(_DL_CHUNK_SIZE)
            if not chunk:
                if not args.quiet:
                    print()
                break

            file_part_size += len(chunk)
            percent = file_part_size / file_size

            progressbar = '#' * round(_DL_BAR_SIZE * percent)
            if not args.quiet:
                print(info.format(
                    progressbar, percent, size_to_str(file_part_size)), end='')
            f.write(chunk)
    os.rename(file_part, save_as)


class AlbumCover:
    def __init__(self, url):
        with urllib.request.urlopen(url) as r:
            self.data = r.read()
            self.mime = r.getheader('Content-Type')

        # If mime has multiple extensions, guess_extension returns random one.
        if self.mime == 'image/jpeg':
            self.extension = '.jpg'
        else:
            self.extension = mimetypes.guess_extension(self.mime)

    @classmethod
    def download(cls, uri, size):
        if size <= 0:
            return None

        for n in COVER_SIZES:
            if size <= n:
                break
        try:
            return cls('https://' + uri.replace('%%', '{0}x{0}'.format(n)))
        except URLError as e:
            logging.error('Can\'t download cover: %s', e)
            return None

    def save(self, path):
        try:
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, 'cover' + self.extension), 'wb') as f:
                f.write(self.data)
        except OSError as e:
            logging.error('Can\'t save cover: %s', e)


def _info_js(template):
    def info_loader(**kwargs):
        with urllib.request.urlopen(template.format(**kwargs), timeout=6) as r:
            return json.loads(r.read().decode())
    return info_loader

track_src_info = _info_js(YM_TRACK_SRC_INFO)
track_info = _info_js(YM_TRACK_INFO)
album_info = _info_js(YM_ALBUM_INFO)
artist_info = _info_js(YM_ARTIST_INFO)
playlist_info = _info_js(YM_PLAYLIST_INFO)


def get_track_url(track):
    info = track_src_info(**track)
    info['path'] = info['path'].lstrip('/')
    h = md5('XGRlBW9FXlekgbPrRHuSiA{path}{s}'.format_map(info).encode())
    info['md5'] = h.hexdigest()
    return 'https://{host}/get-mp3/{md5}/{ts}/{path}'.format_map(info)


def split_artists(all_artists):
    '''Split "artists" to artists itself and composers.'''
    artists = []
    composers = []
    for a in all_artists:
        if a['composer']:
            composers.append(a['name'])
        else:
            artists.append(a['name'])
    return ', '.join(artists or composers), ', '.join(composers)


def print_track_info(track):
    info = '{} ({})'.format(track['title'], time_to_str(track['durationMs']))
    if FLD_TRACKNUM in track:
        album = track['albums'][0]
        info = '[{}/{}] {}'.format(
            track[FLD_TRACKNUM], album['trackCount'], info)

    print(info)
    print('by', track['artists'])


def print_album_info(album, num=None):
    '''Print album info.

    num -- pair (current album number, total albums)
    '''
    ntracks = 0
    duration = 0
    for vol in album['volumes']:
        ntracks += len(vol)
        duration += sum(track['durationMs'] for track in vol)

    print('{:=^{}}'.format(' {}/{} '.format(*num) if num else '', LINE_WIDTH))
    print('Album   ', album['title'])
    print('Artist  ', album['artists'])
    if 'year' in album:
        print('Year    ', album['year'])
    if 'labels' in album:
        print('Label   ', ', '.join(l['name'] for l in album['labels']))
    print('Tracks  ', ntracks)
    print('Volumes ', len(album['volumes']))
    print('Length  ', time_to_str(duration))
    print(LINE)


def download_track(track, save_path=args.out, name_mask=None, cover_id3=None):
    track['artists'], track[FLD_COMPOSERS] = split_artists(track['artists'])
    if 'version' in track:
        track['title'] = '{title} ({version})'.format_map(track)

    album = track['albums'][0]
    if 'version' in album:
        album['title'] = '{title} ({version})'.format_map(album)

    # Format file name
    if not name_mask:
        name_mask = args.track_name or DTN_SINGLE
    fmt = {}
    fmt[FMT_TITLE] = track['title']
    fmt[FMT_ARTIST] = track['artists']
    fmt[FMT_ALBUM] = album['title']
    if FLD_TRACKNUM in track:
        fill = max(len(str(album['trackCount'])), 2)
        trackn = str(track[FLD_TRACKNUM]).zfill(fill)
    else:
        trackn = ''
    fmt[FMT_TRACKN] = trackn
    fmt[FMT_NTRACKS] = str(album['trackCount'])
    fmt[FMT_YEAR] = str(album.get('year', ''))
    fmt[FMT_LABEL] = ', '.join(l['name'] for l in album.get('labels', []))
    for f, t in fmt.items():
        name_mask = name_mask.replace(f, t)

    track_name = filename(name_mask)
    if not track_name.lower().endswith('.mp3'):
        track_name += '.mp3'
    track_path = os.path.join(save_path, track_name)

    if not args.quiet:
        print_track_info(track)

    try:
        download_file(get_track_url(track), track_path)
    except FileExistsError as e:
        logging.info(e)
    except URLError as e:
        logging.error('Can\'t download track: %s', e)
    else:
        if not cover_id3 and 'coverUri' in album:
            cover_id3 = AlbumCover.download(
                album['coverUri'], args.cover_id3_size)
        try:
            write_id3(track_path, track, cover_id3)
        except OSError as e:
            logging.error('Can\'t write ID3: %s', e)

    return make_extinf(track, track_name)


def download_tracks(tracks, save_path, name_mask,
                    cover_id3=None, vol_num=None):
    os.makedirs(save_path, exist_ok=True)

    extinfs = []
    ntracks = len(tracks)

    for n, track in enumerate(tracks, 1):
        if isinstance(track, (int, str)):
            track = track_info(track=track)['track']

        track[FLD_TRACKNUM] = n
        album = track['albums'][0]
        album['trackCount'] = ntracks
        if vol_num:
            album[FLD_VOLUMENUM] = vol_num

        extinf = download_track(track, save_path, name_mask, cover_id3)
        extinfs.append(extinf)

    if args.m3u:
        try:
            save_m3u(extinfs, save_path)
        except OSError as e:
            logging.error('Can\'t save M3U: %s', e)


def download_album_vol(vol, save_path, cover=None, cover_id3=None,
                       vol_num=None):
    if cover:
        cover.save(save_path)
    download_tracks(vol, save_path, args.track_name or DTN_ALBUM,
                    cover_id3, vol_num)


def download_album(album, save_path=args.out, name_mask=None, num=None):
    nvolumes = len(album['volumes'])
    if nvolumes == 0:
        logging.info('Album "%s" is empty.', album['title'])
        return

    album['artists'], album[FLD_COMPOSERS] = split_artists(album['artists'])

    if 'version' in album:
        album['title'] = '{title} ({version})'.format_map(album)

    # Format directory name
    if not name_mask:
        name_mask = args.album_name or DAN_SINGLE_ALBUM
    fmt = {}
    fmt[FMT_TITLE] = ''
    fmt[FMT_ARTIST] = album['artists']
    fmt[FMT_ALBUM] = album['title']
    fmt[FMT_TRACKN] = ''
    fmt[FMT_NTRACKS] = str(album['trackCount'])
    fmt[FMT_YEAR] = str(album.get('year', ''))
    fmt[FMT_LABEL] = ', '.join(l['name'] for l in album.get('labels', []))
    for f, t in fmt.items():
        name_mask = name_mask.replace(f, t)

    album_path = os.path.join(save_path, filename(name_mask))

    if 'coverUri' in album:
        cover_uri = album['coverUri']
        cover = AlbumCover.download(cover_uri, args.cover_size)
        if args.cover_id3_size == args.cover_size:
            cover_id3 = cover
        else:
            cover_id3 = AlbumCover.download(cover_uri, args.cover_id3_size)
    else:
        cover = None
        cover_id3 = None

    if not args.quiet:
        print_album_info(album, num)

    if nvolumes == 1:
        download_album_vol(album['volumes'][0], album_path, cover, cover_id3)
    else:
        fill = len(str(nvolumes))
        for n, vol in enumerate(album['volumes'], 1):
            download_album_vol(
                vol,
                os.path.join(
                    album_path,
                    '{}{:0{}}'.format(filename(args.volume_prefix), n, fill)),
                cover, cover_id3, n)


def download_albums(albums, save_path=args.out):
    nalbums = len(albums)
    for n, album in enumerate(albums, 1):
        if isinstance(album, (int, str)):
            album = album_info(album=album)
        download_album(
            album, save_path,
            args.album_name or DAN_ARTIST_ALBUMS, (n, nalbums))


def download_artist(artist):
    save_path = os.path.join(args.out, filename(artist['artist']['name']))

    if 'trackIds' in artist:
        download_tracks(
            artist['trackIds'], save_path, args.track_name or DTN_ARTIST)
    else:
        download_albums(
            artist['alsoAlbumIds' if args.also else 'albumIds'], save_path)


def download_playlist(pls):
    # If some tracks have an "error" (I met "no-rights"), they just not appear
    # on Yandex Music. Such entries do not contain any file-related information
    # and therefore useless. We also pretend they don't exist for honesty.
    tracks = [t for t in pls['tracks'] if 'error' not in t]
    if not tracks:
        logging.info('Playlist "%s" is empty.', pls['title'])
        return

    save_path = os.path.join(args.out, filename(pls['title']))

    if 'cover' in pls and pls['cover']['type'] == 'pic':
        cover = AlbumCover.download(pls['cover']['uri'], args.cover_size)
        if cover:
            cover.save(save_path)

    download_tracks(tracks, save_path, args.track_name or DTN_PLAYLIST)


def parse_url(url):
    url_info = urllib.parse.urlsplit(url)
    if not (url_info.scheme in ('http', 'https') and
            url_info.netloc.startswith('music.yandex')):
        raise YmdlWrongUrlError

    pairs = url_info.path.strip('/').split('/')

    # 'what' argument for artist's info
    if len(pairs) % 2 != 0:
        what = pairs[-1]
        if what not in ['albums', 'tracks', 'similar']:
            raise YmdlWrongUrlError
    else:
        what = 'albums'

    i = iter(pairs)
    info = dict(zip(i, i))
    info['what'] = what

    if what == 'similar':
        raise YmdlError((
            'URL {} points to artists similar to {}. '
            'Please select one and give appropriate URL.').format(
                url, artist_info(**info)['artist']['name']))

    if 'track' in info:
        download_track(track_info(**info)['track'])
    elif 'album' in info:
        download_album(album_info(**info))
    elif 'artist' in info:
        download_artist(artist_info(**info))
    elif 'playlists' in info:
        download_playlist(playlist_info(**info)['playlist'])
    else:
        raise YmdlWrongUrlError


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s')
    if args.quiet:
        logging.disable(logging.CRITICAL)

    if args.batch_file:
        urls = itertools.chain(
            args.url,
            filter(None, (l.strip() for l in args.batch_file)))
    else:
        if not args.url:
            parser.error('You must provide at least one URL.')
        urls = args.url

    try:
        for url in urls:
            try:
                parse_url(url)
            except YmdlWrongUrlError:
                logging.error('Wrong or unsupported URL: %s', url)
            except YmdlError as e:
                logging.error(e)
            except URLError as e:
                logging.error('%s: %s', url, e)
    except KeyError:
        logging.exception('Seems like API was changed.')
    except OSError as e:
        logging.exception(e)


main()
