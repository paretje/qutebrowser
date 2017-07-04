# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2014-2017 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Initialization of qutebrowser and application-wide things."""

import os
import sys
import subprocess
import configparser
import functools
import json
import shutil
import tempfile
import atexit
import datetime
import tokenize

from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtGui import QDesktopServices, QPixmap, QIcon, QWindow
from PyQt5.QtCore import (pyqtSlot, qInstallMessageHandler, QTimer, QUrl,
                          QObject, QEvent, pyqtSignal)
try:
    import hunter
except ImportError:
    hunter = None

import qutebrowser
import qutebrowser.resources
from qutebrowser.completion.models import instances as completionmodels
from qutebrowser.commands import cmdutils, runners, cmdexc
from qutebrowser.config import style, config, websettings, configexc
from qutebrowser.browser import (urlmarks, adblock, history, browsertab,
                                 downloads)
from qutebrowser.browser.network import proxy
from qutebrowser.browser.webkit import cookies, cache
from qutebrowser.browser.webkit.network import networkmanager
from qutebrowser.keyinput import macros
from qutebrowser.mainwindow import mainwindow, prompt
from qutebrowser.misc import (readline, ipc, savemanager, sessions,
                              crashsignal, earlyinit, objects, domains)
from qutebrowser.misc import utilcmds  # pylint: disable=unused-import
from qutebrowser.utils import (log, version, message, utils, qtutils, urlutils,
                               objreg, usertypes, standarddir, error, debug)
# We import utilcmds to run the cmdutils.register decorators.


qApp = None


def run(args):
    """Initialize everything and run the application."""
    if args.temp_basedir:
        args.basedir = tempfile.mkdtemp(prefix='qutebrowser-basedir-')

    quitter = Quitter(args)
    objreg.register('quitter', quitter)

    global qApp
    qApp = Application(args)
    qApp.setOrganizationName("qutebrowser")
    qApp.setApplicationName("qutebrowser")
    qApp.setApplicationVersion(qutebrowser.__version__)
    qApp.lastWindowClosed.connect(quitter.on_last_window_closed)

    log.init.debug("Initializing directories...")
    standarddir.init(args)

    if args.version:
        print(version.version())
        sys.exit(usertypes.Exit.ok)

    crash_handler = crashsignal.CrashHandler(
        app=qApp, quitter=quitter, args=args, parent=qApp)
    crash_handler.activate()
    objreg.register('crash-handler', crash_handler)

    signal_handler = crashsignal.SignalHandler(app=qApp, quitter=quitter,
                                               parent=qApp)
    signal_handler.activate()
    objreg.register('signal-handler', signal_handler)

    try:
        server = ipc.send_or_listen(args)
    except ipc.Error:
        # ipc.send_or_listen already displays the error message for us.
        # We didn't really initialize much so far, so we just quit hard.
        sys.exit(usertypes.Exit.err_ipc)

    if server is None:
        sys.exit(usertypes.Exit.ok)
    else:
        server.got_args.connect(lambda args, target_arg, cwd:
                                process_pos_args(args, cwd=cwd, via_ipc=True,
                                                 target_arg=target_arg))

    init(args, crash_handler)
    ret = qt_mainloop()
    return ret


def qt_mainloop():
    """Simple wrapper to get a nicer stack trace for segfaults.

    WARNING: misc/crashdialog.py checks the stacktrace for this function
    name, so if this is changed, it should be changed there as well!
    """
    return qApp.exec_()


def init(args, crash_handler):
    """Initialize everything.

    Args:
        args: The argparse namespace.
        crash_handler: The CrashHandler instance.
    """
    log.init.debug("Starting init...")
    qApp.setQuitOnLastWindowClosed(False)
    _init_icon()

    try:
        _init_modules(args, crash_handler)
    except (OSError, UnicodeDecodeError, browsertab.WebTabError) as e:
        error.handle_fatal_exc(e, args, "Error while initializing!",
                               pre_text="Error while initializing")
        sys.exit(usertypes.Exit.err_init)

    log.init.debug("Initializing eventfilter...")
    event_filter = EventFilter(qApp)
    qApp.installEventFilter(event_filter)
    objreg.register('event-filter', event_filter)

    log.init.debug("Connecting signals...")
    config_obj = objreg.get('config')
    config_obj.style_changed.connect(style.get_stylesheet.cache_clear)
    qApp.focusChanged.connect(on_focus_changed)

    _process_args(args)

    QDesktopServices.setUrlHandler('http', open_desktopservices_url)
    QDesktopServices.setUrlHandler('https', open_desktopservices_url)
    QDesktopServices.setUrlHandler('qute', open_desktopservices_url)

    QTimer.singleShot(10, functools.partial(_init_late_modules, args))

    log.init.debug("Init done!")
    crash_handler.raise_crashdlg()


def _init_icon():
    """Initialize the icon of qutebrowser."""
    icon = QIcon()
    fallback_icon = QIcon()
    for size in [16, 24, 32, 48, 64, 96, 128, 256, 512]:
        filename = ':/icons/qutebrowser-{}x{}.png'.format(size, size)
        pixmap = QPixmap(filename)
        if pixmap.isNull():
            log.init.warning("Failed to load {}".format(filename))
        else:
            fallback_icon.addPixmap(pixmap)
    icon = QIcon.fromTheme('qutebrowser', fallback_icon)
    if icon.isNull():
        log.init.warning("Failed to load icon")
    else:
        qApp.setWindowIcon(icon)


def _process_args(args):
    """Open startpage etc. and process commandline args."""
    config_obj = objreg.get('config')
    for sect, opt, val in args.temp_settings:
        try:
            config_obj.set('temp', sect, opt, val)
        except (configexc.Error, configparser.Error) as e:
            message.error("set: {} - {}".format(e.__class__.__name__, e))

    domains.init()
    if not args.override_restore:
        _load_session(args.session)
    session_manager = objreg.get('session-manager')
    if not session_manager.did_load:
        log.init.debug("Initializing main window...")
        window = mainwindow.MainWindow(private=None)
        if not args.nowindow:
            window.show()
        qApp.setActiveWindow(window)

    process_pos_args(args.command)
    _open_startpage()
    _open_special_pages(args)

    delta = datetime.datetime.now() - earlyinit.START_TIME
    log.init.debug("Init finished after {}s".format(delta.total_seconds()))


def _load_session(name):
    """Load the default session.

    Args:
        name: The name of the session to load, or None to read state file.
    """
    state_config = objreg.get('state-config')
    session_manager = objreg.get('session-manager')
    if name is None and session_manager.exists('_autosave'):
        name = '_autosave'
    elif name is None:
        try:
            name = state_config['general']['session']
        except KeyError:
            # No session given as argument and none in the session file ->
            # start without loading a session
            return

    try:
        session_manager.load(name)
    except sessions.SessionNotFoundError:
        message.error("Session {} not found!".format(name))
    except sessions.SessionError as e:
        message.error("Failed to load session {}: {}".format(name, e))
    try:
        del state_config['general']['session']
    except KeyError:
        pass
    # If this was a _restart session, delete it.
    if name == '_restart':
        session_manager.delete('_restart')


def process_pos_args(args, via_ipc=False, cwd=None, target_arg=None):
    """Process positional commandline args.

    URLs to open have no prefix, commands to execute begin with a colon.

    Args:
        args: A list of arguments to process.
        via_ipc: Whether the arguments were transmitted over IPC.
        cwd: The cwd to use for fuzzy_url.
        target_arg: Command line argument received by a running instance via
                    ipc. If the --target argument was not specified, target_arg
                    will be an empty string.
    """
    if via_ipc and not args:
        win_id = mainwindow.get_window(via_ipc, force_window=True)
        _open_startpage(win_id)
        return
    win_id = None
    for cmd in args:
        if cmd.startswith(':'):
            if win_id is None:
                win_id = mainwindow.get_window(via_ipc, force_tab=True)
            log.init.debug("Startup cmd {!r}".format(cmd))
            commandrunner = runners.CommandRunner(win_id)
            commandrunner.run_safely_init(cmd[1:])
        elif not cmd:
            log.init.debug("Empty argument")
            win_id = mainwindow.get_window(via_ipc, force_window=True)
        else:
            if via_ipc and target_arg and target_arg != 'auto':
                open_target = target_arg
            else:
                open_target = config.get('general', 'new-instance-open-target')
            win_id = mainwindow.get_window(via_ipc, force_target=open_target)
            tabbed_browser = objreg.get('tabbed-browser', scope='window',
                                        window=win_id)
            log.init.debug("Startup URL {}".format(cmd))
            if not cwd:  # could also be an empty string due to the PyQt signal
                cwd = None
            try:
                url = urlutils.fuzzy_url(cmd, cwd, relative=True)
            except urlutils.InvalidUrlError as e:
                message.error("Error in startup argument '{}': {}".format(
                    cmd, e))
            else:
                background = open_target in ['tab-bg', 'tab-bg-silent']
                tabbed_browser.tabopen(url, background=background,
                                       explicit=True)


def _open_startpage(win_id=None):
    """Open startpage.

    The startpage is never opened if the given windows are not empty.

    Args:
        win_id: If None, open startpage in all empty windows.
                If set, open the startpage in the given window.
    """
    if win_id is not None:
        window_ids = [win_id]
    else:
        window_ids = objreg.window_registry
    for cur_win_id in list(window_ids):  # Copying as the dict could change
        tabbed_browser = objreg.get('tabbed-browser', scope='window',
                                    window=cur_win_id)
        if tabbed_browser.count() == 0:
            log.init.debug("Opening startpage")
            for urlstr in config.get('general', 'startpage'):
                try:
                    url = urlutils.fuzzy_url(urlstr, do_search=False)
                except urlutils.InvalidUrlError as e:
                    message.error("Error when opening startpage: {}".format(e))
                    tabbed_browser.tabopen(QUrl('about:blank'))
                else:
                    tabbed_browser.tabopen(url)


def _open_special_pages(args):
    """Open special notification pages which are only shown once.

    Currently this is:
      - Quickstart page if it's the first start.
      - Legacy QtWebKit warning if needed.

    Args:
        args: The argparse namespace.
    """
    if args.basedir is not None:
        # With --basedir given, don't open anything.
        return

    state_config = objreg.get('state-config')
    tabbed_browser = objreg.get('tabbed-browser', scope='window',
                                window='last-focused')

    # Legacy QtWebKit warning

    needs_warning = (objects.backend == usertypes.Backend.QtWebKit and
                     not qtutils.is_qtwebkit_ng())
    warning_shown = state_config['general'].get('backend-warning-shown') == '1'

    if not warning_shown and needs_warning:
        tabbed_browser.tabopen(QUrl('qute://backend-warning'),
                               background=False)
        state_config['general']['backend-warning-shown'] = '1'

    # Quickstart page

    quickstart_done = state_config['general'].get('quickstart-done') == '1'

    if not quickstart_done:
        tabbed_browser.tabopen(
            QUrl('https://www.qutebrowser.org/quickstart.html'))
        state_config['general']['quickstart-done'] = '1'


def _save_version():
    """Save the current version to the state config."""
    state_config = objreg.get('state-config', None)
    if state_config is not None:
        state_config['general']['version'] = qutebrowser.__version__


def on_focus_changed(_old, new):
    """Register currently focused main window in the object registry."""
    if new is None:
        return

    if not isinstance(new, QWidget):
        log.misc.debug("on_focus_changed called with non-QWidget {!r}".format(
            new))
        return

    window = new.window()
    if isinstance(window, mainwindow.MainWindow):
        objreg.register('last-focused-main-window', window, update=True)
        # A focused window must also be visible, and in this case we should
        # consider it as the most recently looked-at window
        objreg.register('last-visible-main-window', window, update=True)


def open_desktopservices_url(url):
    """Handler to open a URL via QDesktopServices."""
    win_id = mainwindow.get_window(via_ipc=True, force_window=False)
    tabbed_browser = objreg.get('tabbed-browser', scope='window',
                                window=win_id)
    tabbed_browser.tabopen(url)


def _init_modules(args, crash_handler):
    """Initialize all 'modules' which need to be initialized.

    Args:
        args: The argparse namespace.
        crash_handler: The CrashHandler instance.
    """
    # pylint: disable=too-many-statements
    log.init.debug("Initializing prompts...")
    prompt.init()

    log.init.debug("Initializing save manager...")
    save_manager = savemanager.SaveManager(qApp)
    objreg.register('save-manager', save_manager)
    save_manager.add_saveable('version', _save_version)

    log.init.debug("Initializing network...")
    networkmanager.init()

    log.init.debug("Initializing proxy...")
    proxy.init()

    log.init.debug("Initializing readline-bridge...")
    readline_bridge = readline.ReadlineBridge()
    objreg.register('readline-bridge', readline_bridge)

    log.init.debug("Initializing config...")
    config.init(qApp)
    save_manager.init_autosave()

    log.init.debug("Initializing web history...")
    history.init(qApp)

    log.init.debug("Initializing crashlog...")
    if not args.no_err_windows:
        crash_handler.handle_segfault()

    log.init.debug("Initializing sessions...")
    sessions.init(qApp)

    log.init.debug("Initializing websettings...")
    websettings.init(args)

    log.init.debug("Initializing adblock...")
    host_blocker = adblock.HostBlocker()
    host_blocker.read_hosts()
    objreg.register('host-blocker', host_blocker)

    log.init.debug("Initializing quickmarks...")
    quickmark_manager = urlmarks.QuickmarkManager(qApp)
    objreg.register('quickmark-manager', quickmark_manager)

    log.init.debug("Initializing bookmarks...")
    bookmark_manager = urlmarks.BookmarkManager(qApp)
    objreg.register('bookmark-manager', bookmark_manager)

    log.init.debug("Initializing cookies...")
    cookie_jar = cookies.CookieJar(qApp)
    ram_cookie_jar = cookies.RAMCookieJar(qApp)
    objreg.register('cookie-jar', cookie_jar)
    objreg.register('ram-cookie-jar', ram_cookie_jar)

    log.init.debug("Initializing cache...")
    diskcache = cache.DiskCache(standarddir.cache(), parent=qApp)
    objreg.register('cache', diskcache)

    log.init.debug("Initializing completions...")
    completionmodels.init()

    log.init.debug("Misc initialization...")
    if config.get('ui', 'hide-wayland-decoration'):
        os.environ['QT_WAYLAND_DISABLE_WINDOWDECORATION'] = '1'
    else:
        os.environ.pop('QT_WAYLAND_DISABLE_WINDOWDECORATION', None)
    macros.init()
    # Init backend-specific stuff
    browsertab.init()


def _init_late_modules(args):
    """Initialize modules which can be inited after the window is shown."""
    log.init.debug("Reading web history...")
    reader = objreg.get('web-history').async_read()
    with debug.log_time(log.init, 'Reading history'):
        while True:
            QApplication.processEvents()
            try:
                next(reader)
            except StopIteration:
                break
            except (OSError, UnicodeDecodeError) as e:
                error.handle_fatal_exc(e, args, "Error while initializing!",
                                       pre_text="Error while initializing")
                sys.exit(usertypes.Exit.err_init)


class Quitter:

    """Utility class to quit/restart the QApplication.

    Attributes:
        quit_status: The current quitting status.
        _shutting_down: Whether we're currently shutting down.
        _args: The argparse namespace.
    """

    def __init__(self, args):
        self.quit_status = {
            'crash': True,
            'tabs': False,
            'main': False,
        }
        self._shutting_down = False
        self._args = args

    def on_last_window_closed(self):
        """Slot which gets invoked when the last window was closed."""
        self.shutdown(last_window=True)

    def _compile_modules(self):
        """Compile all modules to catch SyntaxErrors."""
        if os.path.basename(sys.argv[0]) == 'qutebrowser':
            # Launched via launcher script
            return
        elif hasattr(sys, 'frozen'):
            return
        else:
            path = os.path.abspath(os.path.dirname(qutebrowser.__file__))
            if not os.path.isdir(path):
                # Probably running from a python egg.
                return

        for dirpath, _dirnames, filenames in os.walk(path):
            for fn in filenames:
                if os.path.splitext(fn)[1] == '.py' and os.path.isfile(fn):
                    with tokenize.open(os.path.join(dirpath, fn)) as f:
                        compile(f.read(), fn, 'exec')

    def _get_restart_args(self, pages=(), session=None):
        """Get the current working directory and args to relaunch qutebrowser.

        Args:
            pages: The pages to re-open.
            session: The session to load, or None.

        Return:
            An (args, cwd) tuple.
                args: The commandline as a list of strings.
                cwd: The current working directory as a string.
        """
        if os.path.basename(sys.argv[0]) == 'qutebrowser':
            # Launched via launcher script
            args = [sys.argv[0]]
            cwd = None
        elif hasattr(sys, 'frozen'):
            args = [sys.executable]
            cwd = os.path.abspath(os.path.dirname(sys.executable))
        else:
            args = [sys.executable, '-m', 'qutebrowser']
            cwd = os.path.join(os.path.abspath(os.path.dirname(
                               qutebrowser.__file__)), '..')
            if not os.path.isdir(cwd):
                # Probably running from a python egg. Let's fallback to
                # cwd=None and see if that works out.
                # See https://github.com/qutebrowser/qutebrowser/issues/323
                cwd = None

        # Add all open pages so they get reopened.
        page_args = []
        for win in pages:
            page_args.extend(win)
            page_args.append('')

        # Serialize the argparse namespace into json and pass that to the new
        # process via --json-args.
        # We do this as there's no way to "unparse" the namespace while
        # ignoring some arguments.
        argdict = vars(self._args)
        argdict['session'] = None
        argdict['url'] = []
        argdict['command'] = page_args[:-1]
        argdict['json_args'] = None
        # Ensure the given session (or none at all) gets opened.
        if session is None:
            argdict['session'] = None
            argdict['override_restore'] = True
        else:
            argdict['session'] = session
            argdict['override_restore'] = False
        # Ensure :restart works with --temp-basedir
        if self._args.temp_basedir:
            argdict['temp_basedir'] = False
            argdict['temp_basedir_restarted'] = True

        # Dump the data
        data = json.dumps(argdict)
        args += ['--json-args', data]

        log.destroy.debug("args: {}".format(args))
        log.destroy.debug("cwd: {}".format(cwd))

        return args, cwd

    @cmdutils.register(instance='quitter', name='restart')
    def restart_cmd(self):
        """Restart qutebrowser while keeping existing tabs open."""
        try:
            ok = self.restart(session='_restart')
        except sessions.SessionError as e:
            log.destroy.exception("Failed to save session!")
            raise cmdexc.CommandError("Failed to save session: {}!".format(e))
        except SyntaxError as e:
            log.destroy.exception("Got SyntaxError")
            raise cmdexc.CommandError("SyntaxError in {}:{}: {}".format(
                e.filename, e.lineno, e))
        if ok:
            self.shutdown(restart=True)

    def restart(self, pages=(), session=None):
        """Inner logic to restart qutebrowser.

        The "better" way to restart is to pass a session (_restart usually) as
        that'll save the complete state.

        However we don't do that (and pass a list of pages instead) when we
        restart because of an exception, as that's a lot simpler and we don't
        want to risk anything going wrong.

        Args:
            pages: A list of URLs to open.
            session: The session to load, or None.

        Return:
            True if the restart succeeded, False otherwise.
        """
        self._compile_modules()
        log.destroy.debug("sys.executable: {}".format(sys.executable))
        log.destroy.debug("sys.path: {}".format(sys.path))
        log.destroy.debug("sys.argv: {}".format(sys.argv))
        log.destroy.debug("frozen: {}".format(hasattr(sys, 'frozen')))
        # Save the session if one is given.
        if session is not None:
            session_manager = objreg.get('session-manager')
            session_manager.save(session)
        # Open a new process and immediately shutdown the existing one
        try:
            args, cwd = self._get_restart_args(pages, session)
            if cwd is None:
                subprocess.Popen(args)
            else:
                subprocess.Popen(args, cwd=cwd)
        except OSError:
            log.destroy.exception("Failed to restart")
            return False
        else:
            return True

    @cmdutils.register(instance='quitter', name=['quit', 'q'],
                       ignore_args=True)
    def shutdown(self, status=0, session=None, last_window=False,
                 restart=False):
        """Quit qutebrowser.

        Args:
            status: The status code to exit with.
            session: A session name if saving should be forced.
            last_window: If the shutdown was triggered due to the last window
                            closing.
            restart: If we're planning to restart.
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        log.destroy.debug("Shutting down with status {}, session {}...".format(
            status, session))
        session_manager = objreg.get('session-manager', None)
        if session_manager is not None:
            if session is not None:
                session_manager.save(session, last_window=last_window,
                                     load_next_time=True)
            elif config.get('general', 'save-session'):
                session_manager.save(sessions.default, last_window=last_window,
                                     load_next_time=True)

        if prompt.prompt_queue.shutdown():
            # If shutdown was called while we were asking a question, we're in
            # a still sub-eventloop (which gets quit now) and not in the main
            # one.
            # This means we need to defer the real shutdown to when we're back
            # in the real main event loop, or we'll get a segfault.
            log.destroy.debug("Deferring real shutdown because question was "
                              "active.")
            QTimer.singleShot(0, functools.partial(self._shutdown, status,
                                                   restart=restart))
        else:
            # If we have no questions to shut down, we are already in the real
            # event loop, so we can shut down immediately.
            self._shutdown(status, restart=restart)

    def _shutdown(self, status, restart):  # noqa
        """Second stage of shutdown."""
        log.destroy.debug("Stage 2 of shutting down...")
        if qApp is None:
            # No QApplication exists yet, so quit hard.
            sys.exit(status)
        # Remove eventfilter
        try:
            log.destroy.debug("Removing eventfilter...")
            event_filter = objreg.get('event-filter', None)
            if event_filter is not None:
                qApp.removeEventFilter(event_filter)
        except AttributeError:
            pass
        # Close all windows
        QApplication.closeAllWindows()
        # Shut down IPC
        try:
            objreg.get('ipc-server').shutdown()
        except KeyError:
            pass
        # Save everything
        try:
            save_manager = objreg.get('save-manager')
        except KeyError:
            log.destroy.debug("Save manager not initialized yet, so not "
                              "saving anything.")
        else:
            for key in save_manager.saveables:
                try:
                    save_manager.save(key, is_exit=True)
                except OSError as e:
                    error.handle_fatal_exc(
                        e, self._args, "Error while saving!",
                        pre_text="Error while saving {}".format(key))
        # Disable storage so removing tempdir will work
        websettings.shutdown()
        # Re-enable faulthandler to stdout, then remove crash log
        log.destroy.debug("Deactivating crash log...")
        objreg.get('crash-handler').destroy_crashlogfile()
        # Delete temp basedir
        if ((self._args.temp_basedir or self._args.temp_basedir_restarted) and
                not restart):
            atexit.register(shutil.rmtree, self._args.basedir,
                            ignore_errors=True)
        # Delete temp download dir
        downloads.temp_download_manager.cleanup()
        # If we don't kill our custom handler here we might get segfaults
        log.destroy.debug("Deactivating message handler...")
        qInstallMessageHandler(None)
        # Now we can hopefully quit without segfaults
        log.destroy.debug("Deferring QApplication::exit...")
        objreg.get('signal-handler').deactivate()
        session_manager = objreg.get('session-manager', None)
        if session_manager is not None:
            session_manager.delete_autosave()
        # We use a singleshot timer to exit here to minimize the likelihood of
        # segfaults.
        QTimer.singleShot(0, functools.partial(qApp.exit, status))

    @cmdutils.register(instance='quitter', name='wq')
    @cmdutils.argument('name', completion=usertypes.Completion.sessions)
    def save_and_quit(self, name=sessions.default):
        """Save open pages and quit.

        Args:
            name: The name of the session.
        """
        self.shutdown(session=name)


class Application(QApplication):

    """Main application instance.

    Attributes:
        _args: ArgumentParser instance.
        _last_focus_object: The last focused object's repr.
    """

    new_window = pyqtSignal(mainwindow.MainWindow)

    def __init__(self, args):
        """Constructor.

        Args:
            Argument namespace from argparse.
        """
        self._last_focus_object = None

        qt_args = qtutils.get_args(args)
        log.init.debug("Qt arguments: {}, based on {}".format(qt_args, args))
        super().__init__(qt_args)

        log.init.debug("Initializing application...")

        self._args = args
        objreg.register('args', args)
        objreg.register('app', self)

        self.launch_time = datetime.datetime.now()
        self.focusObjectChanged.connect(self.on_focus_object_changed)

    @pyqtSlot(QObject)
    def on_focus_object_changed(self, obj):
        """Log when the focus object changed."""
        output = repr(obj)
        if self._last_focus_object != output:
            log.misc.debug("Focus object changed: {}".format(output))
        self._last_focus_object = output

    def __repr__(self):
        return utils.get_repr(self)

    def exit(self, status):
        """Extend QApplication::exit to log the event."""
        log.destroy.debug("Now calling QApplication::exit.")
        if 'debug-exit' in self._args.debug_flags:
            if hunter is None:
                print("Not logging late shutdown because hunter could not be "
                      "imported!", file=sys.stderr)
            else:
                print("Now logging late shutdown.", file=sys.stderr)
                hunter.trace()
        super().exit(status)


class EventFilter(QObject):

    """Global Qt event filter.

    Attributes:
        _activated: Whether the EventFilter is currently active.
        _handlers; A {QEvent.Type: callable} dict with the handlers for an
                   event.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._activated = True
        self._handlers = {
            QEvent.MouseButtonDblClick: self._handle_mouse_event,
            QEvent.MouseButtonPress: self._handle_mouse_event,
            QEvent.MouseButtonRelease: self._handle_mouse_event,
            QEvent.MouseMove: self._handle_mouse_event,
            QEvent.KeyPress: self._handle_key_event,
            QEvent.KeyRelease: self._handle_key_event,
        }

    def _handle_key_event(self, event):
        """Handle a key press/release event.

        Args:
            event: The QEvent which is about to be delivered.

        Return:
            True if the event should be filtered, False if it's passed through.
        """
        if qApp.activeWindow() not in objreg.window_registry.values():
            # Some other window (print dialog, etc.) is focused so we pass the
            # event through.
            return False
        try:
            man = objreg.get('mode-manager', scope='window', window='current')
            return man.eventFilter(event)
        except objreg.RegistryUnavailableError:
            # No window available yet, or not a MainWindow
            return False

    def _handle_mouse_event(self, _event):
        """Handle a mouse event.

        Args:
            _event: The QEvent which is about to be delivered.

        Return:
            True if the event should be filtered, False if it's passed through.
        """
        # Mouse cursor shown (overrideCursor None) -> don't filter event
        # Mouse cursor hidden (overrideCursor not None) -> filter event
        return qApp.overrideCursor() is not None

    def eventFilter(self, obj, event):
        """Handle an event.

        Args:
            obj: The object which will get the event.
            event: The QEvent which is about to be delivered.

        Return:
            True if the event should be filtered, False if it's passed through.
        """
        try:
            if not self._activated:
                return False
            if not isinstance(obj, QWindow):
                # We already handled this same event at some point earlier, so
                # we're not interested in it anymore.
                return False
            try:
                handler = self._handlers[event.type()]
            except KeyError:
                return False
            else:
                return handler(event)
        except:
            # If there is an exception in here and we leave the eventfilter
            # activated, we'll get an infinite loop and a stack overflow.
            self._activated = False
            raise
