# Written by Arno Bakker, Jie Yang
# Improved and Modified by Niels Zeilemaker
# see LICENSE.txt for license information
import functools
import gc
import inspect
import logging
import os
import re
import shutil
import sys
import time
import unittest
from tempfile import mkdtemp
from threading import enumerate as enumerate_threads
from traceback import print_exc

# set wxpython version before importing wx or anything from Tribler
import wxversion
wxversion.select("2.8-unicode")

import wx

from Tribler.Core.Utilities.twisted_thread import reactor

from .util import process_unhandled_exceptions
from Tribler.Core import defaults
from Tribler.Core.Session import Session
from Tribler.Core.SessionConfig import SessionStartupConfig
from Tribler.dispersy.util import blocking_call_on_reactor_thread


TESTS_DIR = os.path.abspath(os.path.dirname(os.path.realpath(__file__)))
TESTS_DATA_DIR = os.path.abspath(os.path.join(TESTS_DIR, u"data"))
TESTS_API_DIR = os.path.abspath(os.path.join(TESTS_DIR, u"API"))

defaults.sessdefaults['general']['minport'] = -1
defaults.sessdefaults['general']['maxport'] = -1
defaults.sessdefaults['dispersy']['dispersy_port'] = -1

DEBUG = False

OUTPUT_DIR = os.environ.get('OUTPUT_DIR', 'output')


class BaseTestCase(unittest.TestCase):
    def __init__(self, *args, **kwargs):
        super(BaseTestCase, self).__init__(*args, **kwargs)

        def wrap(fun):
            @functools.wraps(fun)
            def check(*argv, **kwargs):
                try:
                    result = fun(*argv, **kwargs)
                except:
                    raise
                else:
                    process_unhandled_exceptions()
                return result
            return check

        for name, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if name.startswith("test_"):
                setattr(self, name, wrap(method))


class AbstractServer(BaseTestCase):

    _annotate_counter = 0

    def setUp(self, annotate=True):
        self._logger = logging.getLogger(self.__class__.__name__)

        self.session_base_dir = mkdtemp(suffix="_tribler_test_session")
        self.state_dir = os.path.join(self.session_base_dir, u"dot.Tribler")
        self.dest_dir = os.path.join(self.session_base_dir, u"TriblerDownloads")

        defaults.sessdefaults['general']['state_dir'] = self.state_dir
        defaults.dldefaults["downloadconfig"]["saveas"] = self.dest_dir

        self.setUpCleanup()
        os.makedirs(self.session_base_dir)
        self.annotate_dict = {}

        if annotate:
            self.annotate(self._testMethodName, start=True)

    def setUpCleanup(self):
        shutil.rmtree(unicode(self.session_base_dir), ignore_errors=True)

    def tearDown(self, annotate=True):
        self.tearDownCleanup()
        if annotate:
            self.annotate(self._testMethodName, start=False)

        delayed_calls = reactor.getDelayedCalls()
        if delayed_calls:
            self._logger.debug("The reactor was dirty:")
            for dc in delayed_calls:
                self._logger.debug(">     %s" % dc)
        self.assertFalse(delayed_calls, "The reactor was dirty when tearing down the test")
        self.assertFalse(Session.has_instance(), 'A session instance is still present when tearing down the test')

    def tearDownCleanup(self):
        self.setUpCleanup()

    def getStateDir(self, nr=0):
        state_dir = self.state_dir + (str(nr) if nr else '')
        if not os.path.exists(state_dir):
            os.mkdir(state_dir)
        return state_dir

    def getDestDir(self, nr=0):
        dest_dir = self.dest_dir + (str(nr) if nr else '')
        if not os.path.exists(dest_dir):
            os.mkdir(dest_dir)
        return dest_dir

    def annotate(self, annotation, start=True, destdir=OUTPUT_DIR):
        if not os.path.exists(destdir):
            os.makedirs(destdir)

        if start:
            self.annotate_dict[annotation] = time.time()
        else:
            filename = os.path.join(destdir, u"annotations.txt")
            if os.path.exists(filename):
                f = open(filename, 'a')
            else:
                f = open(filename, 'w')
                print >> f, "annotation start end"

            AbstractServer._annotate_counter += 1
            _annotation = re.sub('[^a-zA-Z0-9_]', '_', annotation)
            _annotation = u"%d_" % AbstractServer._annotate_counter + _annotation

            print >> f, _annotation, self.annotate_dict[annotation], time.time()
            f.close()


class TestAsServer(AbstractServer):

    """
    Parent class for testing the server-side of Tribler
    """

    def setUp(self):
        super(TestAsServer, self).setUp(annotate=False)
        self.setUpPreSession()

        self.quitting = False

        self.session = Session(self.config)
        upgrader = self.session.prestart()
        while not upgrader.is_done:
            time.sleep(0.1)
        assert not upgrader.failed, upgrader.current_status
        self.session.start()

        self.hisport = self.session.get_listen_port()

        while not self.session.lm.initComplete:
            time.sleep(1)

        self.annotate(self._testMethodName, start=True)

    def setUpPreSession(self):
        """ Should set self.config_path and self.config """
        self.config = SessionStartupConfig()
        self.config.set_tunnel_community_enabled(False)
        self.config.set_tunnel_community_optin_dialog_shown(True)
        self.config.set_state_dir(self.getStateDir())
        self.config.set_torrent_checking(False)
        self.config.set_multicast_local_peer_discovery(False)
        self.config.set_megacache(False)
        self.config.set_dispersy(False)
        self.config.set_mainline_dht(False)
        self.config.set_torrent_store(False)
        self.config.set_enable_torrent_search(False)
        self.config.set_enable_channel_search(False)
        self.config.set_torrent_collecting(False)
        self.config.set_libtorrent(False)
        self.config.set_dht_torrent_collecting(False)
        self.config.set_videoplayer(False)

    def tearDown(self):
        self.annotate(self._testMethodName, start=False)

        """ unittest test tear down code """
        if self.session is not None:
            self._shutdown_session(self.session)
            Session.del_instance()

        time.sleep(10)
        gc.collect()

        ts = enumerate_threads()
        self._logger.debug("test_as_server: Number of threads still running %d", len(ts))
        for t in ts:
            self._logger.debug("Thread still running %s, daemon: %s, instance: %s", t.getName(), t.isDaemon(), t)

        super(TestAsServer, self).tearDown(annotate=False)

    def _shutdown_session(self, session):
        session_shutdown_start = time.time()
        waittime = 60

        session.shutdown()
        while not session.has_shutdown():
            diff = time.time() - session_shutdown_start
            assert diff < waittime, "test_as_server: took too long for Session to shutdown"

            self._logger.debug(
                "Waiting for Session to shutdown, will wait for an additional %d seconds", (waittime - diff))
            time.sleep(1)

        self._logger.debug("Session has shut down")

    def assert_(self, boolean, reason=None, do_assert=True, tribler_session=None, dump_statistics=False):
        if not boolean:
            # print statistics if needed
            if tribler_session and dump_statistics:
                self._print_statistics(tribler_session.get_statistics())

            self.quit()
            assert boolean, reason

    @blocking_call_on_reactor_thread
    def _print_statistics(self, statistics_dict):
        def _print_data_dict(data_dict, level):
            for k, v in data_dict.iteritems():
                indents = u'-' + u'-' * 2 * level

                if isinstance(v, basestring):
                    self._logger.debug(u"%s %s: %s", indents, k, v)
                elif isinstance(v, dict):
                    self._logger.debug(u"%s %s:", indents, k)
                    _print_data_dict(v, level + 1)
                else:
                    # ignore other types for the moment
                    continue
        self._logger.debug(u"========== Tribler Statistics BEGIN ==========")
        _print_data_dict(statistics_dict, 0)
        self._logger.debug(u"========== Tribler Statistics END ==========")

    def startTest(self, callback):
        self.quitting = False
        callback()

    def Call(self, seconds, callback):
        if not self.quitting:
            if seconds:
                time.sleep(seconds)
            callback()

    def CallConditional(self, timeout, condition, callback, assertMsg=None, assertCallback=None,
                        tribler_session=None, dump_statistics=False):
        t = time.time()

        def DoCheck():
            if not self.quitting:
                # only use the last two parts as the ID because the full name is too long
                test_id = self.id()
                test_id = '.'.join(test_id.split('.')[-2:])

                if time.time() - t < timeout:
                    try:
                        if condition():
                            self._logger.debug("%s - condition satisfied after %d seconds, calling callback '%s'",
                                               test_id, time.time() - t, callback.__name__)
                            callback()
                        else:
                            self.Call(0.5, DoCheck)

                    except:
                        print_exc()
                        self.assert_(False, '%s - Condition or callback raised an exception, quitting (%s)' %
                                     (test_id, assertMsg or "no-assert-msg"), do_assert=False)
                else:
                    self._logger.debug("%s - %s, condition was not satisfied in %d seconds (%s)",
                                       test_id,
                                       ('calling callback' if assertCallback else 'quitting'),
                                       timeout,
                                       assertMsg or "no-assert-msg")
                    assertcall = assertCallback if assertCallback else self.assert_
                    kwargs = {}
                    if assertcall == self.assert_:
                        kwargs = {'tribler_session': tribler_session, 'dump_statistics': dump_statistics}

                    assertcall(False, "%s - %s - Condition was not satisfied in %d seconds" %
                               (test_id, assertMsg, timeout), do_assert=False, **kwargs)
        self.Call(0, DoCheck)

    def quit(self):
        self.quitting = True


class TestGuiAsServer(TestAsServer):

    """
    Parent class for testing the gui-side of Tribler
    """

    def setUp(self):
        self.assertFalse(Session.has_instance(), 'A session instance is already present when setting up the test')
        AbstractServer.setUp(self, annotate=False)

        self.app = wx.GetApp()
        if not self.app:
            from Tribler.Main.tribler_main import TriblerApp
            self.app = TriblerApp(redirect=False)

        self.guiUtility = None
        self.frame = None
        self.lm = None
        self.session = None

        self.hadSession = False
        self.quitting = False

        self.asserts = []
        self.annotate(self._testMethodName, start=True)

    def assert_(self, boolean, reason, do_assert=True, tribler_session=None, dump_statistics=False):
        if not boolean:
            # print statistics if needed
            if tribler_session and dump_statistics:
                self._print_statistics(tribler_session.get_statistics())

            self.screenshot("ASSERT: %s" % reason)
            self.quit()

            self.asserts.append((boolean, reason))

            if do_assert:
                assert boolean, reason

    def startTest(self, callback, min_timeout=5, autoload_discovery=True,
                  use_torrent_search=True, use_channel_search=True):
        from Tribler.Main.vwxGUI.GuiUtility import GUIUtility
        from Tribler.Main import tribler_main
        tribler_main.ALLOW_MULTIPLE = True
        tribler_main.SKIP_TUNNEL_DIALOG = True

        self.hadSession = False
        starttime = time.time()

        def call_callback():
            took = time.time() - starttime
            if took > min_timeout:
                callback()
            else:
                self.Call(min_timeout - took, callback)

        def wait_for_frame():
            self._logger.debug("GUIUtility ready, starting to wait for frame to be ready")
            self.frame = self.guiUtility.frame
            self.frame.Maximize()
            self.CallConditional(30, lambda: self.frame.ready, call_callback)

        def wait_for_init():
            self._logger.debug("lm initcomplete, starting to wait for GUIUtility to be ready")
            self.guiUtility = GUIUtility.getInstance()
            self.CallConditional(30, lambda: self.guiUtility.registered, wait_for_frame)

        def wait_for_guiutility():
            self._logger.debug("waiting for guiutility instance")
            self.lm = self.session.lm
            self.CallConditional(30, lambda: GUIUtility.hasInstance(), wait_for_init)

        def wait_for_instance():
            self._logger.debug("found instance, starting to wait for lm to be initcomplete")
            self.session = Session.get_instance()
            self.hadSession = True
            self.CallConditional(30, lambda: self.session.lm and self.session.lm.initComplete, wait_for_guiutility)

        self._logger.debug("waiting for session instance")
        self.CallConditional(30, Session.has_instance, lambda: TestAsServer.startTest(self, wait_for_instance))

        # modify argv to let tribler think its running from a different directory
        sys.argv = [os.path.abspath('./.exe')]
        tribler_main.run(autoload_discovery=autoload_discovery,
                         use_torrent_search=use_torrent_search,
                         use_channel_search=use_channel_search)

        assert self.hadSession, 'Did not even create a session'

    def Call(self, seconds, callback):
        if not self.quitting:
            if seconds:
                wx.CallLater(seconds * 1000, callback)
            elif not wx.Thread_IsMain():
                wx.CallAfter(callback)
            else:
                callback()

    def quit(self):
        if self.frame:
            self.frame.OnCloseWindow()

        else:
            def close_dialogs():
                for item in wx.GetTopLevelWindows():
                    if isinstance(item, wx.Dialog):
                        if item.IsModal():
                            item.EndModal(wx.ID_CANCEL)
                        else:
                            item.Destroy()
                    else:
                        item.Close()

            def do_quit():
                self.app.ExitMainLoop()
                wx.WakeUpMainThread()

            self.Call(1, close_dialogs)
            self.Call(2, do_quit)
            self.Call(3, self.app.Exit)

        self.quitting = True

    def tearDown(self):
        self.annotate(self._testMethodName, start=False)

        """ unittest test tear down code """
        del self.guiUtility
        del self.frame
        del self.lm
        del self.session

        time.sleep(1)
        gc.collect()

        ts = enumerate_threads()
        if ts:
            self._logger.debug("Number of threads still running %s", len(ts))
            for t in ts:
                self._logger.debug("Thread still running %s, daemon %s, instance: %s", t.getName(), t.isDaemon(), t)

        dhtlog = os.path.join(self.state_dir, 'pymdht.log')
        if os.path.exists(dhtlog):
            self._logger.debug("Content of pymdht.log")
            f = open(dhtlog, 'r')
            for line in f:
                line = line.strip()
                if line:
                    self._logger.debug("> %s", line)
            f.close()
            self._logger.debug("Finished printing content of pymdht.log")

        AbstractServer.tearDown(self, annotate=False)

        for boolean, reason in self.asserts:
            assert boolean, reason

    def screenshot(self, title=None, destdir=OUTPUT_DIR, window=None):
        try:
            from PIL import Image
        except ImportError:
            self._logger.error("Could not load PIL: not making screenshots")
            return

        if window is None:
            app = wx.GetApp()
            window = app.GetTopWindow()
            if not window:
                self._logger.error("Couldn't obtain top window and no window was passed as argument, bailing out")
                return

        rect = window.GetClientRect()
        size = window.GetSize()
        rect = wx.Rect(rect.x, rect.y, size.x, size.y)

        screen = wx.WindowDC(window)
        bmp = wx.EmptyBitmap(rect.GetWidth(), rect.GetHeight() + 30)

        mem = wx.MemoryDC(bmp)
        mem.Blit(0, 30, rect.GetWidth(), rect.GetHeight(), screen, rect.GetX(), rect.GetY())

        titlerect = wx.Rect(0, 0, rect.GetWidth(), 30)
        mem.DrawRectangleRect(titlerect)
        if title:
            mem.DrawLabel(title, titlerect, wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL)
        del mem

        myWxImage = wx.ImageFromBitmap(bmp)
        im = Image.new('RGB', (myWxImage.GetWidth(), myWxImage.GetHeight()))
        im.fromstring(myWxImage.GetData())

        if not os.path.exists(destdir):
            os.makedirs(destdir)
        index = 1
        filename = os.path.join(destdir, 'Screenshot-%.2d.png' % index)
        while os.path.exists(filename):
            index += 1
            filename = os.path.join(destdir, 'Screenshot-%.2d.png' % index)
        im.save(filename)

        del bmp
