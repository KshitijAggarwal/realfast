from __future__ import print_function, division, absolute_import#, unicode_literals # not casa compatible
from builtins import bytes, dict, object, range, map, input#, str # not casa compatible
from future.utils import itervalues, viewitems, iteritems, listvalues, listitems
from io import open

import pickle
import os.path
from datetime import date
import random
import distributed
from astropy import time
from time import sleep
import numpy as np
import dask.utils
from evla_mcast.controller import Controller
from realfast import pipeline, elastic, mcaf_servers, heuristics, util

import logging
import matplotlib
import yaml

matplotlib.use('Agg')
ch = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s %(levelname)8s %(name)s | %(message)s')
ch.setFormatter(formatter)
logger = logging.getLogger('realfast_controller')

_vys_cfile_prod = '/home/cbe-master/realfast/lustre_workdir/vys.conf'  # production file
_vys_cfile_test = '/home/cbe-master/realfast/soft/vysmaw_apps/vys.conf'  # test file
_preffile = '/lustre/evla/test/realfast/realfast.yml'
#_distributed_host = '192.168.201.101'  # for ib0 on cbe-node-01
_distributed_host = '10.80.200.201:8786'  # for ib0 on rfnode021
_default_daskdir = '/lustre/evla/test/realfast/dask-worker-space'


# to parse tuples in yaml
class PrettySafeLoader(yaml.SafeLoader):
    def construct_python_tuple(self, node):
        return tuple(self.construct_sequence(node))


PrettySafeLoader.add_constructor(u'tag:yaml.org,2002:python/tuple',
                                 PrettySafeLoader.construct_python_tuple)


class realfast_controller(Controller):

    def __init__(self, preffile=None, inprefs={}, host=None, **kwargs):
        """ Creates controller object that can act on a scan configuration.
        Inherits a "run" method that starts asynchronous operation that calls
        handle_config. handle_sdm and handle_meta (with datasource "vys" or
        "sim") are also supported.
        host is ip:port for distributed scheduler server.
        host allows specification of 'localhost' for distributed client.

        kwargs can include:
        - tags, a comma-delimited string for cands to index
        - nameincludes, a string required to be in datasetId,
        - vys_timeout, factor over real-time for vys reading to wait,
        - vys_sec_per_spec, time in sec to allow for vys reading (overloaded by vys_timeout)
        - mockprob, chance (range 0-1) that a mock is added to scan,
        - saveproducts, boolean defining generation of mini-sdm,
        - indexresults, boolean defining push (meta)data to search index,
        - archiveproducts, boolean defining archiving mini-sdm,
        - throttle, integer defining slowing pipeline submission relative to realtime,
        - read_overhead, throttle param requires multiple of vismem in a READERs memory,
        - read_totfrac, throttle param requires fraction of total READER memory be available,
        - spill_limit, throttle param limiting maximum size (in GB) of data spill directory,
        - searchintents, a list of intent names to search,
        - indexprefix, a string defining set of indices to save results.
        """

        super(realfast_controller, self).__init__()

        from rfpipe import preferences

        self.inprefs = inprefs  # rfpipe preferences
        if host is None:
            self.client = distributed.Client(_distributed_host)
        elif host == 'localhost':
            self.client = distributed.Client(n_workers=1,
                                             threads_per_worker=2,
                                             resources={"READER": 1,
                                                        "GPU": 1,
                                                        "MEMORY": 10e9})
        else:
            self.client = distributed.Client(host)

        self.lock = dask.utils.SerializableLock()
        self.states = {}
        self.futures = {}
        self.futures_removed = {}
        self.finished = {}
        self.errors = {}
        self.known_segments = {}

        # define attributes from yaml file
        self.preffile = preffile if preffile is not None else _preffile
        prefs = {}
        if os.path.exists(self.preffile):
            with open(self.preffile, 'r') as fp:
                prefs = yaml.load(fp, Loader=PrettySafeLoader)['realfast']
                logger.info("Parsed realfast preferences from {0}"
                            .format(self.preffile))

                _ = self.client.run(preferences.parsepreffile, self.preffile,
                                    asynchronous=True)
        else:
            logger.warn("realfast preffile {0} given, but not found"
                        .format(self.preffile))

        # get arguments from preffile, optional overload from kwargs
        for attr in ['tags', 'nameincludes', 'mockprob', 'vys_timeout',
                     'vys_sec_per_spec', 'indexresults', 'saveproducts',
                     'archiveproducts', 'searchintents', 'throttle',
                     'read_overhead', 'read_totfrac', 'spill_limit',
                     'indexprefix', 'daskdir', 'requirecalibration',
                     'data_logging']:
            if attr == 'indexprefix':
                setattr(self, attr, 'new')
            elif attr == 'throttle':
                setattr(self, attr, 0.8)  # submit relative to realtime
            else:
                setattr(self, attr, None)

            if attr in prefs:
                setattr(self, attr, prefs[attr])
            if attr in kwargs:
                setattr(self, attr, kwargs[attr])

        if self.indexprefix is None:
            self.indexprefix = 'new'
        assert self.indexprefix in ['new', 'test', 'chime', 'aws'], "indexprefix must be None, 'new', 'test', 'chime', or 'aws'."
        if self.daskdir is None:
            self.daskdir = _default_daskdir

        # TODO: set defaults for these
        assert self.read_overhead and self.read_totfrac and self.spill_limit

    def __repr__(self):
        nseg = len([seg for (scanId, futurelist) in iteritems(self.futures)
                    for seg, data, cc, acc in futurelist])

        return ('realfast controller with {0} jobs'
                .format(nseg))

    @property
    def statuses(self):
        for (scanId, futurelist) in iteritems(self.futures):
            for seg, data, cc, acc in futurelist:
                if len(self.client.who_has()[data.key]):
                    dataloc = self.workernames[self.client.who_has()[data.key][0]]
                    logger.info('{0}, {1}: {2}, {3}, {4}. Data on {5}.'
                                .format(scanId, seg, data.status, cc.status,
                                        acc.status, dataloc))
                else:
                    logger.info('{0}, {1}: {2}, {3}, {4}.'
                                .format(scanId, seg, data.status, cc.status,
                                        acc.status))

    @property
    def exceptions(self):
        return ['{0}, {1}: {2}, {3}'.format(scanId, seg, data.exception(),
                                            cc.exception())
                for (scanId, futurelist) in iteritems(self.futures)
                for seg, data, cc, acc in futurelist
                if data.status == 'error' or cc.status == 'error']

    @property
    def processing(self):
        return dict((self.workernames[k], v)
                    for k, v in iteritems(self.client.processing()) if v)

    @property
    def workernames(self):
        return dict((k, v['id'])
                    for k, v in iteritems(self.client.scheduler_info()['workers']))

    @property
    def reader_memory_available(self):
        return heuristics.reader_memory_available(self.client)

    @property
    def reader_memory_used(self):
        return heuristics.reader_memory_used(self.client)

    @property
    def spilled_memory(self):
        return heuristics.spilled_memory(self.daskdir)

    @property
    def pending(self):
        """ Show number of segments in scanId that are still pending
        """

        return dict([(scanId,
                      len(list(filter(lambda x: x[3].status == 'pending',
                                      futurelist))))
                     for scanId, futurelist in iteritems(self.futures)])

    def restart(self):
        self.client.restart()

    def handle_config(self, config, cfile=_vys_cfile_prod, segments=None):
        """ Triggered when obs comes in.
        Downstream logic starts here.
        Default vys config file uses production parameters.
        segments arg can be used to submit a subset of all segments.

        """

        summarize(config)

        if search_config(config, preffile=self.preffile, inprefs=self.inprefs,
                         nameincludes=self.nameincludes,
                         searchintents=self.searchintents):

            # starting config of an OTF row will trigger subscan logic
            if config.otf:
                logger.info("Good OTF config: calling handle_subscan")
                self.handle_subscan(config, cfile=cfile)
            else:
                logger.info("Good Non-OTF config: setting state and starting pipeline")
                # for standard pointed mode, just set state and start pipeline
                self.set_state(config.scanId, config=config,
                               inmeta={'datasource': 'vys'})

                self.start_pipeline(config.scanId, cfile=cfile,
                                    segments=segments)

        else:
            logger.info("Config not suitable for realfast. Skipping.")

        self.cleanup()

    def handle_subscan(self, config, cfile=_vys_cfile_prod):
        """ Triggered when subscan info is updated (e.g., OTF mode).
        OTF requires more setup and management.
        """

        # set up OTF info
        # search pipeline needs [(startmjd, stopmjd, l1, m1), ...]
        phasecenters = []
        endtime_mjd_ = 0
        for ss in config.subscans:
            if ss.stopTime is not None:
                if ss.stopTime > endtime_mjd_:
                    endtime_mjd_ = ss.stopTime
                phasecenters.append((ss.startTime, ss.stopTime,
                                     ss.ra_deg, ss.dec_deg))
        t0 = min([startTime for (startTime, _, _, _) in phasecenters])
        t1 = max([stopTime for (_, stopTime, _, _) in phasecenters])
        logger.info("Calculated {0} phasecenters from {1} to {2}"
                    .format(len(phasecenters), t0, t1))

        # pass in first subscan and overload end time
        config0 = config.subscans[0]  # all tracked by first config of scan
        if config0.is_complete:
            if config0.scanId in self.known_segments:
                logger.info("Already submitted {0} segments for scanId {1}. "
                            "Fast state calculation."
                            .format(len(self.known_segments[config0.scanId]),
                                    config0.scanId))
                self.set_state(config0.scanId, config=config0, validate=False,
                               showsummary=False,
                               inmeta={'datasource': 'vys',
                                       'endtime_mjd_': endtime_mjd_,
                                       'phasecenters': phasecenters})
            else:
                logger.info("No submitted segments for scanId {0}. Submitting "
                            "Slow state calculation.".format(config0.scanId))
                self.set_state(config0.scanId, config=config0,
                               inmeta={'datasource': 'vys',
                                       'endtime_mjd_': endtime_mjd_,
                                       'phasecenters': phasecenters})

            allsegments = list(range(self.states[config0.scanId].nsegment))
            if config0.scanId not in self.known_segments:
                logger.debug("Initializing known_segments with scanId {0}"
                             .format(config0.scanId))
                self.known_segments[config0.scanId] = []

            # get new segments
            segments = [seg for seg in allsegments
                        if seg not in self.known_segments[config0.scanId]]

            # TODO: this may not actually submit if telcal not ready
            # should not update known_segments automatically?
            if len(segments):
                logger.info("Starting pipeline for {0} with segments {1}"
                            .format(config0.scanId, segments))
                self.start_pipeline(config0.scanId, cfile=cfile,
                                    segments=segments)
                # now all (currently known segments) have been started
                logger.info("Updating known_segments for {0} to {1}"
                            .format(config0.scanId, allsegments))
                self.known_segments[config0.scanId] = allsegments
            else:
                logger.info("No new segments to submit for {0}"
                            .format(config0.scanId))
        else:
            logger.info("First subscan config is not complete. Continuing.")

    def handle_sdm(self, sdmfile, sdmscan, bdfdir=None, segments=None):
        """ Parallel to handle_config, but allows sdm to be passed in.
        segments arg can be used to submit a subset of all segments.
        """

        # TODO: subscan assumed = 1
        sdmsubscan = 1
        scanId = '{0}.{1}.{2}'.format(os.path.basename(sdmfile.rstrip('/')),
                                      str(sdmscan), str(sdmsubscan))

        self.set_state(scanId, sdmfile=sdmfile, sdmscan=sdmscan, bdfdir=bdfdir,
                       inmeta={'datasource': 'sdm'})

        self.start_pipeline(scanId, segments=segments)

        self.cleanup()

    def handle_meta(self, inmeta, cfile=_vys_cfile_test, segments=None):
        """ Parallel to handle_config, but allows metadata dict to be passed in.
        Gets called explicitly.
        Default vys config file uses test parameters.
        inmeta datasource key ('vys', 'sim', or 'vyssim') is passed to rfpipe.
        segments arg can be used to submit a subset of all segments.
        """

        scanId = '{0}.{1}.{2}'.format(inmeta['datasetId'], str(inmeta['scan']),
                                      str(inmeta['subscan']))

        self.set_state(scanId, inmeta=inmeta)

        self.start_pipeline(scanId, cfile=cfile, segments=segments)

        self.cleanup()

    def set_state(self, scanId, config=None, inmeta=None, sdmfile=None,
                  sdmscan=None, bdfdir=None, validate=True, showsummary=True):
        """ Given metadata source, define state for a scanId.
        Uses metadata to set preferences used in preffile (prefsname).
        Preferences are then overloaded with self.inprefs.
        Will inject mock transient based on mockprob and other parameters.
        """

        from rfpipe import preferences, state

        prefsname = get_prefsname(inmeta=inmeta, config=config,
                                  sdmfile=sdmfile, sdmscan=sdmscan,
                                  bdfdir=bdfdir)

        inprefs = preferences.parsepreffile(self.preffile, name=prefsname,
                                            inprefs=self.inprefs)

        # alternatively, overload prefs with compiled rules (req Python>= 3.5)
#        inprefs = {**inprefs, **heuristics.band_prefs(inmeta)}

        st = state.State(inmeta=inmeta, config=config, inprefs=inprefs,
                         lock=self.lock, sdmfile=sdmfile, sdmscan=sdmscan,
                         bdfdir=bdfdir, validate=validate, showsummary=showsummary)

        logger.info('State set for scanId {0}. Requires {1:.1f} GB read and'
                    ' {2:.1f} GPU-sec to search.'
                    .format(st.metadata.scanId,
                            heuristics.total_memory_read(st),
                            heuristics.total_compute_time(st)))

        self.states[scanId] = st

    def start_pipeline(self, scanId, cfile=None, segments=None):
        """ Start pipeline conditional on cluster state.
        Sets futures and state after submission keyed by scanId.
        segments arg can be used to select or slow segment submission.
        """

        st = self.states[scanId]
        w_memlim = self.read_overhead*st.vismem*1e9
        if segments is None:
            segments = list(range(st.nsegment))

        vys_timeout = self.vys_timeout
        if st.metadata.datasource in ['vys', 'vyssim']:
            if self.vys_timeout is not None:
                logger.debug("vys_timeout factor set to fixed value of {0:.1f}x"
                             .format(vys_timeout))
            else:
                assert self.vys_sec_per_spec is not None, "Must define vys_sec_per_spec to estimate vys_timeout"
                nspec = st.readints*st.nbl*st.nspw*st.npol
                vys_timeout = (st.t_segment + self.vys_sec_per_spec*nspec)/st.t_segment
                logger.debug("vys_timeout factor scaled by nspec to {0:.1f}x"
                             .format(vys_timeout))

        mockseg = random.choice(segments) if random.uniform(0, 1) < self.mockprob else None
        if mockseg is not None:
            logger.info("Mock set for scanId {0} in segment {1}"
                        .format(scanId, mockseg))

        # vys data means realtime operations must timeout within a scan time
        if st.metadata.datasource == 'vys':
            timeout = 0.9*st.metadata.inttime*st.metadata.nints  # bit shorter than scan
        else:
            timeout = 0
        throttletime = self.throttle*st.metadata.inttime*st.metadata.nints/st.nsegment
        logger.info('Submitting {0} segments for scanId {1} with {2:.1f}s per segment'
                    .format(len(segments), scanId, throttletime))
        logger.debug('Read_overhead {0}, read_totfrac {1}, and '
                     'spill_limit {2} with timeout {3}s'
                     .format(self.read_overhead, self.read_totfrac,
                             self.spill_limit, timeout))

        tot_memlim = self.read_totfrac*sum([v['resources']['MEMORY']
                                            for v in itervalues(self.client.scheduler_info()['workers'])
                                            if 'READER' in v['resources']])

        # submit segments
        t0 = time.Time.now().unix
        elapsedtime = 0
        nsubmitted = 0  # count number submitted from list segments
        segments = iter(segments)
        segment = next(segments)
        telcalset = self.set_telcalfile(scanId)
        while True:
            segsubtime = time.Time.now().unix
            if st.metadata.datasource == 'vys':
                endtime = time.Time(st.segmenttimes[segment][1], format='mjd').unix
                if endtime < segsubtime-2:  # TODO: define buffer delay better
                    logger.warning("Segment {0} time window has passed ({1} < {2}). Skipping."
                                   .format(segment, endtime, segsubtime-1))
                    try:
                        segment = next(segments)
                        continue
                    except StopIteration:
                        logger.debug("No more segments for scanId {0}".format(scanId))
                        break

            # try setting telcal
            if not telcalset:
                telcalset = self.set_telcalfile(scanId)

            # submit if cluster ready and telcal available
            if (heuristics.reader_memory_ok(self.client, w_memlim) and
                heuristics.readertotal_memory_ok(self.client, tot_memlim) and
                heuristics.spilled_memory_ok(limit=self.spill_limit,
                                             daskdir=self.daskdir) and
                (telcalset if self.requirecalibration else True)):

                # first time initialize scan
                if scanId not in self.futures:
                    self.futures[scanId] = []
                    self.errors[scanId] = 0
                    self.finished[scanId] = 0

                    if self.indexresults:
                        elastic.indexscan(inmeta=self.states[scanId].metadata,
                                          preferences=self.states[scanId].prefs,
                                          indexprefix=self.indexprefix)
                    else:
                        logger.info("Not indexing scan or prefs.")

                futures = pipeline.pipeline_seg(st, segment, cl=self.client,
                                                cfile=cfile,
                                                vys_timeout=vys_timeout,
                                                mem_read=w_memlim,
                                                mem_search=2*st.vismem*1e9,
                                                mockseg=mockseg)
                self.futures[scanId].append(futures)
                nsubmitted += 1

                if self.data_logging:
                    segment, data, cc, acc = futures
                    distributed.fire_and_forget(self.client.submit(util.data_logger,
                                                                   st, segment,
                                                                   data,
                                                                   fifo_timeout='0s',
                                                                   priority=-1))
                if self.indexresults:
                    elastic.indexscanstatus(scanId, pending=self.pending[scanId],
                                            finished=self.finished[scanId],
                                            errors=self.errors[scanId],
                                            indexprefix=self.indexprefix,
                                            nsegment=st.nsegment)

                try:
                    segment = next(segments)
                except StopIteration:
                    logger.info("No more segments for scanId {0}".format(scanId))
                    break

            else:
                if not heuristics.reader_memory_ok(self.client, w_memlim):
                    logger.info("System not ready. No reader available with required memory {0}"
                                .format(w_memlim))
                elif not heuristics.readertotal_memory_ok(self.client,
                                                          tot_memlim):
                    logger.info("System not ready. Total reader memory exceeds limit of {0}"
                                .format(tot_memlim))
                elif not heuristics.spilled_memory_ok(limit=self.spill_limit,
                                                      daskdir=self.daskdir):
                    logger.info("System not ready. Spilled memory exceeds limit of {0}"
                                .format(self.spill_limit))
                elif not (self.set_telcalfile(scanId)
                          if self.requirecalibration else True):
                    logger.info("System not ready. No telcalfile available for {0}"
                                .format(scanId))

            # periodically check on submissions. always, if memory limited.
            if not (segment % 2) or not (heuristics.reader_memory_ok(self.client, w_memlim) and
                                         heuristics.readertotal_memory_ok(self.client, tot_memlim) and
                                         heuristics.spilled_memory_ok(limit=self.spill_limit,
                                                                      daskdir=self.daskdir)):
                self.cleanup(keep=scanId)  # do not remove keys of ongoing submission

            # check timeout and wait time for next segment
            elapsedtime = time.Time.now().unix - t0
            if elapsedtime > timeout and timeout:
                logger.info("Submission timed out. Submitted {0}/{1} segments "
                            "in ScanId {2}".format(nsubmitted, st.nsegment,
                                                   scanId))
                break
            else:
                dt = time.Time.now().unix - segsubtime
                if dt < throttletime:
                    logger.debug("Waiting {0:.1f}s to submit segment."
                                 .format(throttletime-dt))
                    sleep(throttletime-dt)

    def cleanup(self, badstatuslist=['cancelled', 'error', 'lost'], keep=None):
        """ Clean up job list.
        Scans futures, removes finished jobs, and pushes results to relevant indices.
        badstatuslist can include 'cancelled', 'error', 'lost'.
        keep defines a scanId (string) key that should not be removed from dicts.
        """

        removed = 0
        cindexed = 0
        sdms = 0

        scanIds = [scanId for scanId in self.futures]
        if len(scanIds):
            logger.info("Checking on scanIds: {0}"
                        .format(','.join(scanIds)))

        # clean futures and get finished jobs
        removed = self.removefutures(badstatuslist)
        for scanId in self.futures:

            # check on finished
            finishedlist = [(seg, data, cc, acc)
                            for (scanId0, futurelist) in iteritems(self.futures)
                            for seg, data, cc, acc in futurelist
                            if (acc.status == 'finished') and
                               (scanId0 == scanId)]
            self.finished[scanId] += len(finishedlist)
            if self.indexresults:
                elastic.indexscanstatus(scanId, pending=self.pending[scanId],
                                        finished=self.finished[scanId],
                                        errors=self.errors[scanId],
                                        indexprefix=self.indexprefix)

            # TODO: check on error handling for fire_and_forget
            for futures in finishedlist:
                seg, data, cc, acc = futures
                ncands, mocks = acc.result()

                # index mocks
                if self.indexresults and mocks:
                    distributed.fire_and_forget(self.client.submit(elastic.indexmock,
                                                                   scanId,
                                                                   mocks,
                                                                   indexprefix=self.indexprefix))
                else:
                    logger.debug("No mocks indexed from scanId {0}"
                                 .format(scanId))

                # index noises
                noisefile = self.states[scanId].noisefile
                if self.indexresults and os.path.exists(noisefile):
                    distributed.fire_and_forget(self.client.submit(elastic.indexnoises,
                                                                   noisefile,
                                                                   scanId,
                                                                   indexprefix=self.indexprefix))
                else:
                    logger.debug("No noises indexed from scanId {0}."
                                 .format(scanId))

                # index cands
                if self.indexresults and ncands:
                    workdir = self.states[scanId].prefs.workdir
                    distributed.fire_and_forget(self.client.submit(util.indexcands_and_plots,
                                                                   cc,
                                                                   scanId,
                                                                   self.tags,
                                                                   self.indexprefix,
                                                                   workdir,
                                                                   priority=5))
                else:
                    logger.debug("No cands indexed from scanId {0}"
                                 .format(scanId))

                # optionally save and archive sdm/bdfs for segment
                if self.saveproducts and ncands:
                    distributed.fire_and_forget(self.client.submit(createproducts,
                                                                   cc, data,
                                                                   self.archiveproducts,
                                                                   indexprefix=self.indexprefix,
                                                                   priority=5))
                    logger.info("Creating an SDM for {0}, segment {1}, with {2} candidates"
                                .format(scanId, seg, ncands))
                    sdms += 1
                else:
                    logger.debug("No SDMs plots moved for scanId {0}."
                                 .format(scanId))

                # remove job from list
                self.futures[scanId].remove(futures)
                removed += 1

        # clean up self.futures
        removeids = [scanId for scanId in self.futures
                     if (len(self.futures[scanId]) == 0) and (scanId != keep)]
        if removeids:
            logstr = ("No jobs left for scanIds: {0}."
                      .format(', '.join(removeids)))
            if keep is not None:
                logstr += (". Cleaning state and futures dicts (keeping {0})"
                           .format(keep))
            else:
                logstr += ". Cleaning state and futures dicts."
            logger.info(logstr)

            for scanId in removeids:
                _ = self.futures.pop(scanId)
                _ = self.states.pop(scanId)
                _ = self.finished.pop(scanId)
                _ = self.errors.pop(scanId)
                try:
                    _ = self.known_segments.pop(scanId)
                except KeyError:
                    pass

#        _ = self.client.run(gc.collect)
        if removed or cindexed or sdms:
            logger.info('Removed {0} jobs, indexed {1} cands, made {2} SDMs.'
                        .format(removed, cindexed, sdms))

    def cleanup_loop(self, timeout=None):
        """ Clean up until all jobs gone or timeout elapses.
        """

        if timeout is None:
            timeout_string = "no"
            timeout = -1
        else:
            timeout_string = "{0} s".format(timeout)

        logger.info("Cleaning up all futures with {0} timeout"
                    .format(timeout_string))

        t0 = time.Time.now().unix
        badstatuslist = ['cancelled', 'error', 'lost']
        while len(self.futures):
            elapsedtime = time.Time.now().unix - t0
            if (elapsedtime > timeout) and (timeout >= 0):
                badstatuslist += ['pending']
            self.cleanup(badstatuslist=badstatuslist)
            sleep(10)

    def set_telcalfile(self, scanId):
        """ Find and set telcalfile in state prefs, if not set already.
        Returns True if set, False if not available.
        """

        st = self.states[scanId]

        if st.gainfile is not None:
            return True
        else:
            gainfile = ''
            today = date.today()
            directory = '/home/mchammer/evladata/telcal/{0}'.format(today.year)
            name = '{0}.GN'.format(st.metadata.datasetId)
            for path, dirs, files in os.walk(directory):
                for f in filter(lambda x: name in x, files):
                    gainfile = os.path.join(path, name)

            if os.path.exists(gainfile) and os.path.isfile(gainfile):
                logger.debug("Found telcalfile {0} for scanId {1}."
                            .format(gainfile, scanId))
                st.prefs.gainfile = gainfile
                return True
            else:
                return False

    def removefutures(self, badstatuslist=['cancelled', 'error', 'lost'],
                      keep=False):
        """ Remove jobs with status in badstatuslist.
        badstatuslist can be a single status as a string.
        keep argument defines whether to remove or move to futures_removed
        """

        if isinstance(badstatuslist, str):
            badstatuslist = [badstatuslist]

        removed = 0
        for scanId in self.futures:
            # create list of futures (a dict per segment) that are cancelled

            removelist = [(seg, data, cc, acc)
                          for (scanId0, futurelist) in iteritems(self.futures)
                          for seg, data, cc, acc in futurelist
                          if ((data.status in badstatuslist) or
                              (cc.status in badstatuslist) or
                              (acc.status in badstatuslist)) and
                             (scanId0 == scanId)]

            self.errors[scanId] += len(removelist)
            for removefuts in removelist:
                (seg, data, cc, acc) = removefuts
                logger.warn("scanId {0} segment {1} bad status: {2}, {3}, {4}"
                            .format(scanId, seg, data.status, cc.status,
                                    acc.status))

            # clean them up
            errworkers = [(fut, self.client.who_has(fut))
                          for futs in removelist
                          for fut in futs[1:] if fut.status == 'error']
            errworkerids = [(fut, self.workernames[worker[0][0]])
                            for fut, worker in errworkers
                            for ww in listvalues(worker) if ww]
            for i, errworkerid in enumerate(errworkerids):
                fut, worker = errworkerid
                logger.warn("Error on workers {0}: {1}"
                            .format(worker, fut.exception()))

            for futures in removelist:
                self.futures[scanId].remove(futures)
                removed += 1

                if keep:
                    if scanId not in self.futures_removed:
                        self.futures_removed[scanId] = []
                    self.futures_removed[scanId].append(futures)

        if removed:
            logger.warn("{0} bad jobs removed from scanId {1}".format(removed,
                                                                      scanId))

        return removed

    def handle_finish(self, dataset):
        """ Triggered when obs doc defines end of a script.
        """

        logger.info('End of scheduling block message received.')


def search_config(config, preffile=None, inprefs={},
                  nameincludes=None, searchintents=None):
    """ Test whether configuration specifies a scan config that realfast should
    search
    """

    # find config properties of interest
    intent = config.scan_intent
    antennas = config.get_antennas()
    antnames = [str(ant.name) for ant in antennas]
    subbands = config.get_subbands()
    inttimes = [subband.hw_time_res for subband in subbands]
    pols = [subband.pp for subband in subbands]
    nchans = [subband.spectralChannels for subband in subbands]
    chansizes = [subband.bw/subband.spectralChannels for subband in subbands]
    reffreqs = [subband.sky_center_freq*1e6 for subband in subbands]

    # Do not process if...
    # 1) chansize changes between subbands
    if not all([chansizes[0] == chansize for chansize in chansizes]):
        logger.warn("Channel size changes between subbands: {0}"
                    .format(chansizes))
        return False

    # 2) start and stop time is after current time
    now = time.Time.now().unix
    startTime = time.Time(config.startTime, format='mjd').unix
    stopTime = time.Time(config.stopTime, format='mjd').unix
    if (startTime < now) and (stopTime < now):
        logger.warn("Scan startTime and stopTime are in the past ({0}, {1} < {2})"
                    .format(startTime, stopTime, now))
        return False

    # 3) if nameincludes set, reject if datasetId does not have it
    if nameincludes is not None:
        if nameincludes not in config.datasetId:
            logger.warn("datasetId {0} does not include nameincludes {1}"
                        .format(config.datasetId, nameincludes))
            return False

    # 4) only search if in searchintents
    if searchintents is not None:
        if not any([searchintent in intent for searchintent in searchintents]):
            logger.warn("intent {0} not in searchintents list {1}"
                        .format(intent, searchintents))
            return False

    # 5) only two antennas
    if len(antnames) <= 2:
        logger.warn("Only {0} antennas in array".format(len(antnames)))
        return False

    # 6) only if state validates
    prefsname = get_prefsname(config=config)
    if not heuristics.state_validates(config=config, preffile=preffile,
                                      prefsname=prefsname, inprefs=inprefs):
        logger.warn("State not valid for scanId {0}"
                    .format(config.scanId))
        return False
    # 7) only if some fast sampling is done (faster than VLASS final inttime)
    t_fast = 0.4
    if not any([inttime < t_fast for inttime in inttimes]):
        logger.warn("No subband has integration time faster than {0} s"
                    .format(t_fast))
        return False

    return True


def get_prefsname(inmeta=None, config=None, sdmfile=None, sdmscan=None,
                  bdfdir=None):
    """ Given a scan, set the name of the realfast preferences to use
    Allows configuration of pipeline based on scan properties.
    (e.g., galactic/extragal, FRB/pulsar).
    """

    from rfpipe import metadata

    meta = metadata.make_metadata(inmeta=inmeta, config=config,
                                  sdmfile=sdmfile, sdmscan=sdmscan,
                                  bdfdir=bdfdir)

    band = heuristics.reffreq_to_band(meta.spw_reffreq)
    if band is not None:
        # currently only 'L' and 'S' are defined
        # TODO: parse preffile to check available prefsnames
        if band in ['C', 'X', 'Ku', 'K', 'Ka', 'Q']:
            band = 'S'
        prefsname = 'NRAOdefault' + band
    else:
        prefsname = 'default'

    return prefsname


def summarize(config):
    """ Print summary info for config
    """

    try:
        logger.info(':: ConfigID {0} ::'.format(config.configId))
        logger.info('\tScan {0}, source {1}, intent {2}'
                    .format(config.scanNo, config.source,
                            config.scan_intent))

        logger.info('\t(RA, Dec) = ({0}, {1})'
                    .format(config.ra_deg, config.dec_deg))
        subbands = config.get_subbands()
        reffreqs = [subband.sky_center_freq for subband in subbands]
        logger.info('\tFreq: {0} - {1}'
                    .format(min(reffreqs), max(reffreqs)))

        nchans = [subband.spectralChannels for subband in subbands]
        chansizes = [subband.bw/subband.spectralChannels
                     for subband in subbands]
        sb0 = subbands[0]
        logger.info('\t(nspw, chan/spw, nchan) = ({0}, {1}, {2})'
                    .format(len(nchans), nchans[0], sum(nchans)))
        logger.info('\t(BW, chansize) = ({0}, {1}) MHz'
                    .format(sb0.bw, chansizes[0]))
        if not all([chansizes[0] == chansize for chansize in chansizes]):
            logger.info('\tNot all spw have same configuration.')

        logger.info('\t(nant, npol) = ({0}, {1})'
                    .format(config.numAntenna, sb0.npp))
        dt = 24*3600*(config.stopTime-config.startTime)
        logger.info('\t(StartMJD, duration) = ({0}, {1}s).'
                    .format(config.startTime, round(dt, 1)))
        logger.info('\t({0}/{1}) ints at (HW/Final) integration time of ({2:.3f}/{3:.3f}) s'
                    .format(int(round(dt/sb0.hw_time_res)),
                            int(round(dt/sb0.final_time_res)),
                            sb0.hw_time_res, sb0.final_time_res))
    except:
        logger.warn("Failed to fully parse config to print summary."
                    "Proceeding.")


def createproducts(candcollection, data, archiveproducts=False,
                   indexprefix=None,
                   savebdfdir='/lustre/evla/wcbe/data/realfast/'):
    """ Create SDMs and BDFs for a given candcollection (time segment).
    Takes data future and calls data only if windows found to cut.
    This uses the mcaf_servers module, which calls the sdm builder server.
    Currently BDFs are moved to no_archive lustre area by default.
    """

    from rfpipe import calibration

    if isinstance(candcollection, distributed.Future):
        candcollection = candcollection.result()
    if isinstance(data, distributed.Future):
        data = data.result()

    assert isinstance(data, np.ndarray) and data.dtype == 'complex64'

    if len(candcollection.array) == 0:
        logger.info('No candidates to generate products for.')
        return []

    metadata = candcollection.metadata
    segment = candcollection.segment
    if not isinstance(segment, int):
        logger.warning("Cannot get unique segment from candcollection")

    st = candcollection.state

    candranges = util.gencandranges(candcollection)  # finds time windows to save from segment
    logger.info('Getting data for candidate time ranges {0} in segment {1}.'
                .format(candranges, segment))

    ninttot, nbl, nchantot, npol = data.shape
    nchan = metadata.nchan_orig//metadata.nspw_orig
    nspw = metadata.nspw_orig

    sdmlocs = []
    # make sdm for each unique time range (e.g., segment)
    for (startTime, endTime) in set(candranges):
        i = (86400*(startTime-st.segmenttimes[segment][0])/metadata.inttime).astype(int)
        nint = np.round(86400*(endTime-startTime)/metadata.inttime, 1).astype(int)
        logger.info("Cutting {0} ints from int {1} for candidate at {2} in segment {3}"
                    .format(nint, i, startTime, segment))
        data_cut = data[i:i+nint].reshape(nint, nbl, nspw, 1, nchan, npol)

        # TODO: fill in annotation dict as defined in confluence doc on realfast collections
        annotation = {}
        calScanTime = np.unique(calibration.getsols(st)['mjd'])
        if len(calScanTime) > 1:
            logger.warn("Using first of multiple cal times: {0}."
                        .format(calScanTime))
        calScanTime = calScanTime[0]

        sdmloc = mcaf_servers.makesdm(startTime, endTime, metadata.datasetId,
                                      data_cut, calScanTime,
                                      annotation=annotation)
        if sdmloc is not None:
            # update index to link to new sdm
            if indexprefix is not None:
                candIds = elastic.candid(cc=candcollection)
                for Id in candIds:
                    elastic.update_field(indexprefix+'cands', 'sdmname',
                                         sdmloc, Id=Id)

            sdmlocs.append(sdmloc)
            logger.info("Created new SDMs at: {0}".format(sdmloc))
            # TODO: migrate bdfdir to newsdmloc once ingest tool is ready
            mcaf_servers.makebdf(startTime, endTime, metadata, data_cut,
                                 bdfdir=savebdfdir)
            # try archiving it
            try:
                if archiveproducts:
                    runingest(sdmloc)  # TODO: implement this

            except distributed.scheduler.KilledWorker:
                logger.warn("Lost SDM generation due to killed worker.")
        else:
            logger.warn("No sdm/bdf made for {0} with start/end time {1}-{2}"
                        .format(metadata.datasetId, startTime, endTime))

    return sdmlocs


def runingest(sdms):
    """ Call archive tool or move data to trigger archiving of sdms.
    This function will ultimately be triggered by candidate portal.
    """

    NotImplementedError
#    /users/vlapipe/workflows/test/bin/ingest -m -p /home/mctest/evla/mcaf/workspace --file 


class config_controller(Controller):

    def __init__(self, pklfile=None, preffile=_preffile):
        """ Creates controller object that saves scan configs.
        If pklfile is defined, it will save pickle there.
        If preffile is defined, it will attach a preferences to indexed scan.
        Inherits a "run" method that starts asynchronous operation.
        """

        super(config_controller, self).__init__()
        self.pklfile = pklfile
        self.preffile = preffile

    def handle_config(self, config):
        """ Triggered when obs comes in.
        Downstream logic starts here.
        """

        from rfpipe import preferences

        logger.info('Received complete configuration for {0}, '
                    'scan {1}, subscan {2}, source {3}, intent {4}'
                    .format(config.scanId, config.scanNo, config.subscanNo,
                            config.source, config.scan_intent))

        if self.pklfile:
            with open(self.pklfile, 'ab') as pkl:
                pickle.dump(config, pkl)

        if self.preffile:
            prefs = preferences.Preferences(**preferences.parsepreffile(self.preffile,
                                                                        name='default'))
            elastic.indexscan(config=config, preferences=prefs)
