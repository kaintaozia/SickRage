# -*- coding: utf-8 -*-
import bisect
import io
import json
import logging
import zipfile

from babelfish import Language
from guessit import guessit
from requests import Session

from . import ParserBeautifulSoup, Provider
from .. import __short_version__
from ..cache import SHOW_EXPIRATION_TIME, region
from ..exceptions import AuthenticationError, ConfigurationError, ProviderError
from ..subtitle import Subtitle, fix_line_ending, guess_matches
from ..utils import sanitize
from ..video import Episode, Movie

logger = logging.getLogger(__name__)


class SubsCenterSubtitle(Subtitle):
    provider_name = 'subscenter'

    def __init__(self, language, hearing_impaired, page_link, series, season, episode, title, subtitle_id, subtitle_key,
                 downloaded, releases):
        super(SubsCenterSubtitle, self).__init__(language, hearing_impaired, page_link)
        self.series = series
        self.season = season
        self.episode = episode
        self.title = title
        self.subtitle_id = subtitle_id
        self.subtitle_key = subtitle_key
        self.downloaded = downloaded
        self.releases = releases

    @property
    def id(self):
        return str(self.subtitle_id)

    def get_matches(self, video):
        matches = set()

        # episode
        if isinstance(video, Episode):
            # series
            if video.series and sanitize(self.series) == sanitize(video.series):
                matches.add('series')
            # season
            if video.season and self.season == video.season:
                matches.add('season')
            # episode
            if video.episode and self.episode == video.episode:
                matches.add('episode')
            # guess
            for release in self.releases:
                matches |= guess_matches(video, guessit(release, {'type': 'episode'}))
        # movie
        elif isinstance(video, Movie):
            # guess
            for release in self.releases:
                matches |= guess_matches(video, guessit(release, {'type': 'movie'}))

        # title
        if video.title and sanitize(self.title) == sanitize(video.title):
            matches.add('title')

        return matches


class SubsCenterProvider(Provider):
    languages = {Language.fromalpha2(l) for l in ['he']}
    server = 'http://subscenter.cinemast.com/he/'

    def __init__(self, username=None, password=None):
        if username is not None and password is None or username is None and password is not None:
            raise ConfigurationError('Username and password must be specified')

        self.username = username
        self.password = password
        self.logged_in = False

    def initialize(self):
        self.session = Session()
        self.session.headers['User-Agent'] = 'Subliminal/%s' % __short_version__

        # login
        if self.username is not None and self.password is not None:
            logger.debug('Logging in')
            url = self.server + 'subscenter/accounts/login/'

            # retrieve CSRF token
            self.session.get(url)
            csrf_token = self.session.cookies['csrftoken']

            # actual login
            data = {'username': self.username, 'password': self.password, 'csrfmiddlewaretoken': csrf_token}
            r = self.session.post(url, data, allow_redirects=False, timeout=10)

            if r.status_code != 302:
                raise AuthenticationError(self.username)

            logger.info('Logged in')
            self.logged_in = True

    def terminate(self):
        # logout
        if self.logged_in:
            logger.info('Logging out')
            r = self.session.get(self.server + 'subscenter/accounts/logout/', timeout=10)
            r.raise_for_status()
            logger.info('Logged out')
            self.logged_in = False

        self.session.close()

    @region.cache_on_arguments(expiration_time=SHOW_EXPIRATION_TIME)
    def _search_url_title(self, title, kind):
        """Search the URL title for the given `title`.

        :param str title: title to search for.
        :param str kind: kind of the title, ``movie`` or ``series``.
        :return: the URL version of the title.
        :rtype: str or None

        """
        # make the search
        logger.info('Searching title name for %r', title)
        r = self.session.get(self.server + 'subtitle/search/', params={'q': title}, allow_redirects=False, timeout=10)
        r.raise_for_status()

        # if redirected, get the url title from the Location header
        if r.is_redirect:
            parts = r.headers['Location'].split('/')

            # check kind
            if parts[-3] == kind:
                return parts[-2]

            return None

        # otherwise, get the first valid suggestion
        soup = ParserBeautifulSoup(r.content, ['lxml', 'html.parser'])
        suggestions = soup.select('#processes div.generalWindowTop a')
        logger.debug('Found %d suggestions', len(suggestions))
        for suggestion in suggestions:
            parts = suggestion.attrs['href'].split('/')

            # check kind
            if parts[-3] == kind:
                return parts[-2]

    def query(self, series=None, season=None, episode=None, title=None):
        # set the correct parameters depending on the kind
        if series and season and episode:
            url_series = self._search_url_title(series, 'series')
            url = self.server + 'cinemast/data/series/sb/{}/{}/{}/'.format(url_series, season, episode)
            page_link = self.server + 'subtitle/series/{}/{}/{}/'.format(url_series, season, episode)
        elif title:
            url_title = self._search_url_title(title, 'movie')
            url = self.server + 'cinemast/data/movie/sb/{}/'.format(url_title)
            page_link = self.server + 'subtitle/movie/{}/'.format(url_title)
        else:
            raise ValueError('One or more parameters are missing')

        # get the list of subtitles
        logger.debug('Getting the list of subtitles')
        r = self.session.get(url)
        r.raise_for_status()
        results = json.loads(r.text)

        # loop over results
        subtitles = {}
        for language_code, language_data in results.items():
            for quality_data in language_data.values():
                for quality, subtitles_data in quality_data.items():
                    for subtitle_item in subtitles_data.values():
                        # read the item
                        language = Language.fromalpha2(language_code)
                        hearing_impaired = bool(subtitle_item['hearing_impaired'])
                        subtitle_id = subtitle_item['id']
                        subtitle_key = subtitle_item['key']
                        downloaded = subtitle_item['downloaded']
                        release = subtitle_item['subtitle_version']

                        # add the release and increment downloaded count if we already have the subtitle
                        if subtitle_id in subtitles:
                            logger.debug('Found additional release %r for subtitle %d', release, subtitle_id)
                            bisect.insort_left(subtitles[subtitle_id].releases, release)  # deterministic order
                            subtitles[subtitle_id].downloaded += downloaded
                            continue

                        # otherwise create it
                        subtitle = SubsCenterSubtitle(language, hearing_impaired, page_link, series, season, episode,
                                                      title, subtitle_id, subtitle_key, downloaded, [release])
                        logger.debug('Found subtitle %r', subtitle)
                        subtitles[subtitle_id] = subtitle

        return subtitles.values()

    def list_subtitles(self, video, languages):
        series = None
        season = None
        episode = None
        title = video.title

        if isinstance(video, Episode):
            series = video.series
            season = video.season
            episode = video.episode

        return [s for s in self.query(series, season, episode, title) if s.language in languages]

    def download_subtitle(self, subtitle):
        # download
        url = self.server + 'subtitle/download/{}/{}/'.format(subtitle.language.alpha2, subtitle.subtitle_id)
        params = {'v': subtitle.releases[0], 'key': subtitle.subtitle_key}
        r = self.session.get(url, params=params, headers={'Referer': subtitle.page_link}, timeout=10)
        r.raise_for_status()

        # open the zip
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            # remove some filenames from the namelist
            namelist = [n for n in zf.namelist() if not n.endswith('.txt')]
            if len(namelist) > 1:
                raise ProviderError('More than one file to unzip')

            subtitle.content = fix_line_ending(zf.read(namelist[0]))
