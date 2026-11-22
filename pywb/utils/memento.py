import re

from warcio.timeutils import timestamp_to_http_date, http_date_to_timestamp

from pywb.utils.timemap_reader import TimeMapReader
from pywb.utils.wbexception import BadRequestException


FIND_DT = re.compile('datetime=\"([^\"]+)\"')


#=============================================================================
class MementoException(BadRequestException):
    pass


#=============================================================================
def _param_to_text(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value


#=============================================================================
class MementoUtils(object):
    @classmethod
    def parse_timemap(cls, link_header, def_name='timemap', final_url=None):
        """Parse a TimeMap into a :class:`TimeMapReader`.

        Relative targets are resolved against ``final_url`` (the final URL of
        the response that delivered the document), never against the
        document's own original/self links.
        """
        reader = TimeMapReader(final_url=final_url)
        reader.feed(link_header, 0)
        reader.finish()
        return reader

    @classmethod
    def parse_links(cls, link_header, def_name='timemap', final_url=None):
        """Parse a Link header / TimeMap into the legacy result mapping.

        The mapping keeps the historical shape (named relations plus a
        ``mementos`` list) but uses the byte-accurate reader underneath, so
        quoted separators, escaped quotes, multi-token relations and
        date-less captures are handled correctly.
        """
        reader = cls.parse_timemap(link_header, def_name, final_url)

        results = {}
        mementos = []
        relations = []

        for record in reader.records:
            if record.is_capture:
                if record.error:
                    # A capture we cannot date is reported separately and must
                    # not stop the captures that follow it.
                    continue

                item = dict(url=record.url,
                            rel=record.rel,
                            datetime=record.datetime)
                if final_url:
                    item['resolved'] = record.target
                mementos.append(item)
            else:
                key = record.rel or 'relation'
                if key == 'self':
                    key = def_name

                item = dict(url=record.url, rel=record.rel)
                if record.datetime is not None:
                    item['datetime'] = record.datetime
                for name, value in record.params:
                    if name == 'rel':
                        continue
                    text = _param_to_text(value)
                    if text is not None:
                        item[name] = text

                # Keep the last one under its relation name for backwards
                # compatibility, and every occurrence in ``relations`` so a
                # repeated or multi-valued relation can still be inspected.
                results[key] = item
                relations.append(item)

        results['relations'] = relations
        results['issues'] = [dict(start=rec.start, end=rec.end,
                                  reason=rec.error, rel=rec.rel,
                                  url=rec.url)
                             for rec in reader.issues]
        results['mementos'] = mementos
        return results

    @classmethod
    def parse_timemap_records(cls, link_header, final_url=None, def_name='timemap'):
        """Alias returning the richer, position-aware reader."""
        return cls.parse_timemap(link_header, def_name, final_url)

    @classmethod
    def make_timemap_memento_link(cls, cdx, datetime=None, rel='memento', end=',\n', memento_format=None):
        """Creates a memento link string for a timemap

        :param dict cdx: The cdx object
        :param str|None datetime: The datetime
        :param str rel: The rel type
        :param str end: Optional string appended to the end of the created link string
        :param str|None memento_format: Optional string used to format the URL
        :return: A memento link string
        :rtype: str
        """
        url = cdx.get('url')
        if not url:
            url = 'file://{0}:{1}:{2}'.format(cdx.get('filename'), cdx.get('offset'), cdx.get('length'))

        if not datetime:
            datetime = timestamp_to_http_date(cdx['timestamp'])

        return cls.make_memento_link(url, rel, datetime, cdx.get('source-coll'), memento_format) + end

    @classmethod
    def make_timemap(cls, cdx_iter, params):
        prev_cdx = None
        memento_format = params.get('memento_format')

        for cdx in cdx_iter:
            if prev_cdx:
                yield cls.make_timemap_memento_link(prev_cdx, memento_format=memento_format)

            prev_cdx = cdx

        # last memento link, if any
        if prev_cdx:
            yield cls.make_timemap_memento_link(prev_cdx, end='\n', memento_format=memento_format)

    @classmethod
    def wrap_timemap_header(cls, url, timegate_url, timemap_url, timemap):
        string = cls.make_link(timemap_url, "self")
        m = FIND_DT.search(timemap)
        if m:
            string += '; from="{0}"'.format(m.group(1))

        string += ',\n'

        string += cls.make_link(timegate_url, "timegate") + ',\n'
        string += cls.make_link(url, "original") + ',\n'
        string += timemap
        return string

    @classmethod
    def make_link(cls, url, type):
        if type in ('timemap', 'self'):
            return '<{0}>; rel="{1}"; type="application/link-format"'.format(url, type)

        return '<{0}>; rel="{1}"'.format(url, type)

    @classmethod
    def make_memento_link(cls, url, type, dt, coll=None, memento_format=None):
        """Creates a memento link string

        :param str url: A URL
        :param str type: The rel type
        :param str dt: The datetime of the URL
        :param str|None coll: Optional name of a collection
        :param str|None memento_format: Optional string used to format the supplied URL
        :return: A memento link string
        :rtype: str
        """
        if memento_format:
            memento_format = memento_format.format(url=url,
                                                   timestamp=http_date_to_timestamp(dt))
        else:
            memento_format = url

        res = '<{0}>; rel="{1}"; datetime="{2}"'.format(memento_format, type, dt)
        if coll:
            res += '; collection="{0}"'.format(coll)

        return res


