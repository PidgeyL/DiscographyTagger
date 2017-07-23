import acoustid
import argparse
import io
import json
import magic
import mutagen.flac
import mutagen.mp3
import os
import pathlib
import pydub
import re
import requests
import urllib.parse
import urllib.request
import PIL.Image

DESCRIPTION  = "Tool to crawl through a local music library and add"
DESCRIPTION += " metadata tags. It also converts files to the preffered"
DESCRIPTION += " filetype. Currently supports converting to FLAC"

SUPPORTED = ('.mp3')

ACOUSTID_KEY = "<api key>"
LASTFM_KEY   = "<api key>"

LASTFM_URL       = "https://ws.audioscrobbler.com/2.0/"
MBIDSEARCH       = LASTFM_URL+"?method=track.getInfo&api_key=%s&mbid=%s&format=json"
TITLESEARCH      = LASTFM_URL+"?method=track.getInfo&api_key=%s&artist=%s&track=%s&format=json"
ALBUMMBIDSEARCH  = LASTFM_URL+"?method=album.getinfo&api_key=%s&mbid=%s&format=json"
ALBUMTITLESEARCH = LASTFM_URL+"?method=album.getinfo&api_key=%s&artist=%s&album=%s&format=json"

ALBUM_CACHE = {}
COVER_CACHE = {}

def xtitle(string):
    skipList = ['a', 'an', 'of', 'the', 'is', 'am']
    return " ".join([x.lower() if x.lower() in skipList else x.capitalize()
                       for x in string.split()])


# Buffer images and convert them to JPEG format (takes less disk space)
def _get_cover(url):
    if not COVER_CACHE.get(url):
        data = io.BytesIO(urllib.request.urlopen(url).read())
        image = PIL.Image.open(data)
        image = image.convert("RGB")
        with io.BytesIO() as f:
            image.save(f, format='JPEG')
            COVER_CACHE[url] = f.getvalue()
    return COVER_CACHE[url]

def _make_request(url):
    r = requests.get(url)
    if r.status_code is 200:
        data = json.loads(r.text)
        if not data.get('error'):
            return data
    return None

# Structure:
# {'title':  <title>, 
#  'artist': <artist>,
#  'mbid':   <mbid>,
#  'track':  <track #>,
#  'cover':  <url to cover image>,
#  'tags':   [<tags>]}  # This will be parsed into possible Genres
def _parse_song(data):
    data = data.get('track', {})
    song = {"title":  data.get('name'),
            "artist": data.get('artist', {}).get('name'),
            "mbid":   data.get('mbid')}
    if data.get('album'):
        album = album_by_mbid(data['album'].get("mbid"))
        if not album:
            album = {}
        song['album'] = album.get('name')
        song['cover'] = album.get('cover')
        song['tags']  = album.get('tags')
    return song

# Structure:
# {'name':   <name>, 
#  'artist': <artist>,
#  'cover':  <url to cover image>,
#  'tags':  [<tags>],
#  'songs': {<song name>, <song track nr>}}
def _parse_album(data):
    data = data.get('album', {})
    album = {'name':   data.get('name'),
             'artist': data.get('artist')}
    for cover in data.get('image'):
        if cover.get("size") == "large":
            album['cover'] = cover.get('#text')
    album['tags'] = [xtitle(x.get('name', ""))
                       for x in data.get('tags', {}).get('tag',[])]
    album['songs'] = {x.get('name'): x.get('@attr', {}).get('rank')
                        for x in data.get('tracks', {}).get('track', [])}
    ALBUM_CACHE[data.get('mbid')] = album
    return album


def song_by_mbid(mbid):
    if ALBUM_CACHE.get('mbid'):
        return ALBUM_CACHE[mbid]
    song = _make_request(MBIDSEARCH%(LASTFM_KEY, mbid))
    return song and _parse_song(song) or None

def song_by_title(title, artist):
    title  = urllib.parse.quote(title.encode("utf-8"),  safe='')
    artist = urllib.parse.quote(artist.encode("utf-8"), safe='')
    song = _make_request(TITLESEARCH%(LASTFM_KEY, artist, title))
    return song and _parse_song(song) or None

def album_by_mbid(mbid):
    data = _make_request(ALBUMMBIDSEARCH%(LASTFM_KEY, mbid))
    
def album_by_title(artist, title):
    title  = urllib.parse.quote(title.encode("utf-8"),  safe='')
    artist = urllib.parse.quote(artist.encode("utf-8"), safe='')
    album = _make_request(ALBUMTITLESEARCH%(LASTFM_KEY, artist, title))
    return album and _parse_album(album) or None


class Song():
    __slots__ = ('path', 'mbid', 'required_tags', 'type', 'extras',
                 'title', 'artist', 'album', 'date', 'tracknumber',
                 'genre', 'cover', 'albumartist')
    def __init__(self, path_, type_=None, overwrite=False):
        self.required_tags = ('title', 'artist', 'album', 'date',
                              'tracknumber', 'genre', 'cover', 'albumartist')
      
        self.path   = path_
        self.mbid   = None
        self.type   = type_ or (os.path.splitext(path_)[-1])[1:]
        self.extras = {}
        self.read_tags()
        self._identify(overwrite)

    # Read current tags
    def read_tags(self):
        def read_tag(tag, is_list=False):
            if isinstance(tag, list) and not is_list:
                if len(tag) > 0: return tag[0]
                else:            return None
            return tag
        tags = mutagen.mp3.EasyMP3(self.path)
        self.title       = read_tag(tags.get('title'))
        self.artist      = read_tag(tags.get('artist'))
        self.album       = read_tag(tags.get('album'))
        self.date        = read_tag(tags.get('date'))
        year = re.search("^.*(\d{4}).*$", self.date)
        if year: self.date = year.group(1)
        self.tracknumber = read_tag(tags.get('tracknumber'))
        self.genre       = read_tag(tags.get('genre', []), True)
        self.albumartist = read_tag(tags.get('albumartist'))
        self.cover       = None
        self.extras      = {read_tag(k): v for k,v in tags.items()
                                     if k not in self.required_tags}

    # Identify the song
    # When overwrite is set to True, it will identify the song based on the content
    # When overwrite is set to False, it wil first try to identify the song via the tags
    #  if it cannot find the song, it will try again with overwrite True.
    def _identify(self, overwrite):
        if overwrite:
            possibilities = list(acoustid.match(ACOUSTID_KEY, self.path))
            if len(possibilities) == 0:
                self._get_info_from_metadata()
            else:
                score, mbid, title, artist = possibilities[0]
                self.title  = xtitle(title)
                self.artist = xtitle(artist)
                self.mbid   = mbid
        if not self._get_info_from_metadata(verbose = False):
            self._identify(True)

    # Try to identify the song based on the metadata
    def _get_info_from_metadata(self, verbose=True):
        if self.title and self.artist:
            data = song_by_title(self.title, self.artist)
            if data:
                self._parse_metadata(data)
                return True
        if verbose:
            print("[!] Could not identify song")
            print("[!]  -> %s"%self.path)
        return False

    # Parse the metadata from the LastFM API
    def _parse_metadata(self, metadata):
        self.title       = metadata.get('title')  or self.title
        self.artist      = metadata.get('artist') or self.artist
        self.album       = metadata.get('album')  or self.album
        self.tracknumber = metadata.get('track')  or self.tracknumber
        self.cover       = metadata.get('cover')  or self.cover
        self.mbid        = metadata.get('mbid')   or self.mbid
        if metadata.get('tags'):
            genres = []
            for tag in metadata['tags']:
                if xtitle(tag) not in [self.title, self.artist, self.album]:
                    # Assume it's a genre. Will be elaborated upon later
                    genres.append(tag)
            self.genre = genres


    def get_tags(self):
        tags = {}
        for tag in self.required_tags:
            if hasattr(self, tag):
                tags[tag] = getattr(self, tag)
        if tags.get('cover'):
            del tags['cover'] # will add image separately
        tags.update(self.extras)
        return tags


    def save(self, save_as="flac", delimiter=";"):
        save_as = save_as.lower()
        data = pydub.AudioSegment.from_file(self.path, self.type)
        name = "%s.%s"%(os.path.splitext(self.path)[0], save_as)
        tags=self.get_tags()
        if tags.get("genre") and save_as not in ['flac', 'wma', 'ogg']:
            tags['genre'] = delimiter.join(tags['genre'])
        data.export(name, format=save_as, tags=tags)
        if save_as == "flac" and self.cover:
            f = mutagen.flac.FLAC(name)
            i = mutagen.flac.Picture()
            i.type = 3
            i.desc = 'cover'
            i.data = _get_cover(self.cover)
            i.mime = 'image/png'
            f.add_picture(i)
            f.save()
        if os.path.realpath(name) is not os.path.realpath(self.path):
            os.remove(self.path)


class Album():
    __slots__ = ('songs', 'directory', 'force', 'lookup_done', 'tracks',
                 'name', 'artist', 'cover', 'tags')
    def __init__(self, directory, force=False):
        self.songs       = []
        self.tracks      = {}
        self.directory   = directory
        self.force       = force
        self.lookup_done = False
        self.name        = directory

    def add_song(self, file_):
        def get_album_info():
            data = album_by_title(song.artist, self._clean(self.name))
            if data:
                self._parse_metadata(data)
                self.lookup_done = True
                return True
            return False
        song = Song(file_, self.force)
        # Check if album data is available
        if not self.lookup_done and song.artist:
            if not get_album_info():
                if song.album:
                    self.name = song.album
                    get_album_info()
        self.songs.append(song)


    def save(self, save_as="flac", delimiter=";"):
        for song in self.songs:
            # manipulate song depending on album properties
            song.album       = self.name   or song.album
            song.cover       = self.cover  or song.cover
            song.albumartist = self.artist or song.albumartist
            # Append album genres to song genres
            song.genre = list(set(self.tags+[xtitle(x) for x in song.genre]))
            # Find correct track number
            song.tracknumber = self.tracks.get(self._clean(song.title))
            if song.tracknumber is None: # Parse all tracks to see if a subset is available
                print("Looking track for %s"%song.title)
                print(self.tracks)
                print(self._clean(song.title))
                for title, track in self.tracks.items():
                    if title in self._clean(song.title): # Gets around (HD) and (cover) problems
                        song.tracknumber = track
                        break
            song.save(save_as, delimiter)


    def _parse_metadata(self, metadata):
        self.name   = xtitle(metadata.get('name'))
        self.artist = xtitle(metadata.get('artist'))
        self.cover  = metadata.get('cover')
        self.tags = []
        for tag in metadata.get('tags'):
            if (not re.match("^\d{4}$", tag)):
                self.tags.append(xtitle(tag))
        self.tracks = {self._clean(k): v for k,v in metadata.get('songs').items()}

    def _clean(self, title):
        def re_search(d, pattern):
            m = re.search(pattern, d)
            if m: return m.group(1)
            return d
        title = title.replace("’", "'")
        title = re_search(title, "^\d{4}\s*-\s*(.+)$") # year - title
        title = re_search(title, "^\(\d{4}\)(.+)$")    # (year) title
        title = re_search(title, "^(.+)\(.*\)$")       # title (info)
        title = title.strip("- _")
        title = xtitle(title)
        return title


if __name__ == "__main__":
    argParser = argparse.ArgumentParser(description=DESCRIPTION)
    argParser.add_argument('-f',  action='store_true', help='Ignore current tags and try to identify the song based on the audio')
    argParser.add_argument('-d',  action='store_true', help='debug messages')
    argParser.add_argument('loc', type=str,            help='Starting point for the crawling')
    args = argParser.parse_args()

    for path, subdirs, files in os.walk(args.loc):
        if args.d:
            print("[+] Loading album %s"%os.path.basename(path))
        album = Album(os.path.basename(path))
        for name in files:
            _file = os.path.join(path, name)
            if _file.endswith(SUPPORTED):
                if args.d:
                    print("[+] Loading song %s"%os.path.basename(_file))
                album.add_song(_file)
        if args.d:
            print("[+] Saving Album %s"%os.path.basename(album.directory))
        album.save()

# TODO: - Try-catch around the urllibs, to retry on failure
#       - Count amount of songs in album, to add that info to the song
