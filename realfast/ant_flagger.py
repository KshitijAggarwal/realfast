from __future__ import print_function, division, absolute_import #, unicode_literals # not casa compatible
from builtins import bytes, dict, object, range, map, input#, str # not casa compatible
from future.utils import itervalues, viewitems, iteritems, listvalues, listitems
from io import open
from future.moves.urllib.parse import urlparse, urlunparse, urlencode
from future.moves.urllib.request import urlopen

import os.path
from lxml import etree, objectify
import logging
logger = logging.getLogger(__name__)

_install_dir = os.path.abspath(os.path.dirname(__file__))
_xsd_dir = os.path.join(_install_dir, 'xsd')
# TODO: get schema
_antflagger_xsd = os.path.join(_xsd_dir, 'AntFlaggerMessage.xsd')
_antflagger_parser = objectify.makeparser(
        schema=etree.XMLSchema(file=_antflagger_xsd))

_host = 'mctest.evla.nrao.edu'


class ANTFlagger(object):
    """ Use mcaf to get online antenna flags """

    _E = objectify.ElementMaker(annotate=False)

    def __init__(self, datasetId=None, startTime=None, endTime=None,
                 host=_host):
        self.datasetId = datasetId
        self.startTime = startTime
        self.endTime = endTime
        self.host = host

    @property
    def _url(self):
        query = '?'
        if self.startTime is not None:
            query += 'startTime='+self.startTime
        if self.endTime is not None:
            query += 'endTime='+self.endTime
        url = 'https://{0}/{1}/{2}/{3}'.format(self.host, 'evla-mcaf-test/dataset',
                                               self.datasetId, 'flags')
        if query:
            url += query
        return url

    def send(self):
        response_xml = urlopen(self._url).read()
        if b'error' in response_xml:
            self.response = None
        else:
            self.response = objectify.fromstring(response_xml,
                                                 parser=_antflagger_parser)

    @property
    def flags(self):
        try:
            return [flag.attrib for flag in self.response.findall('flag')]
        except AttributeError:
            logger.warn("No ant flags found.")
            return None


def getflags(datasetId, startTime, endTime):
    """ 
    """

    logger.info("Getting flags for datasetId {0}"
                .format(datasetId))
    antf = ANTFlagger(datasetId, startTime, endTime)
    antf.send()

    return antf.flags
