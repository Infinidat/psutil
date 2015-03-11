#!/usr/bin/env python

# Copyright (c) 2009, Giampaolo Rodola'. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""AIX platform implementation."""

import errno
import os
import socket
import subprocess
import sys
from collections import namedtuple

from . import _common
from . import _psposix
from . import _psutil_posix as cext_posix
from . import _psutil_aix as cext
from ._common import isfile_strict, socktype_to_enum, sockfam_to_enum
from ._common import usage_percent
from ._compat import PY3


__extra__all__ = []


PAGE_SIZE = os.sysconf('SC_PAGE_SIZE')
AF_LINK = cext_posix.AF_LINK

PROC_STATUSES = {

    cext.SIDL: _common.STATUS_IDLE,
    cext.SRUN: _common.STATUS_RUNNING,
    cext.SSLEEP: _common.STATUS_SLEEPING,
    cext.SSWAP: _common.STATUS_RUNNING,      # TODO what status is this?
    cext.SSTOP: _common.STATUS_STOPPED,
    cext.SZOMB: _common.STATUS_ZOMBIE
}

TCP_STATUSES = {
    cext.TCPS_ESTABLISHED: _common.CONN_ESTABLISHED,
    cext.TCPS_SYN_SENT: _common.CONN_SYN_SENT,
    cext.TCPS_SYN_RCVD: _common.CONN_SYN_RECV,
    cext.TCPS_FIN_WAIT_1: _common.CONN_FIN_WAIT1,
    cext.TCPS_FIN_WAIT_2: _common.CONN_FIN_WAIT2,
    cext.TCPS_TIME_WAIT: _common.CONN_TIME_WAIT,
    cext.TCPS_CLOSED: _common.CONN_CLOSE,
    cext.TCPS_CLOSE_WAIT: _common.CONN_CLOSE_WAIT,
    cext.TCPS_LAST_ACK: _common.CONN_LAST_ACK,
    cext.TCPS_LISTEN: _common.CONN_LISTEN,
    cext.TCPS_CLOSING: _common.CONN_CLOSING,
    cext.PSUTIL_CONN_NONE: _common.CONN_NONE,
}

scputimes = namedtuple('scputimes', ['user', 'system', 'idle', 'iowait'])
svmem = namedtuple('svmem', ['total', 'available', 'percent', 'used', 'free'])
pmmap_grouped = namedtuple('pmmap_grouped', ['path', 'rss', 'anon', 'locked'])
pmmap_ext = namedtuple(
    'pmmap_ext', 'addr perms ' + ' '.join(pmmap_grouped._fields))

# set later from __init__.py
NoSuchProcess = None
ZombieProcess = None
AccessDenied = None
TimeoutExpired = None

# --- functions

disk_usage = _psposix.disk_usage
net_if_addrs = cext_posix.net_if_addrs


def virtual_memory():
    total, free, pinned, inuse = cext.virtual_mem()
    total = total * PAGE_SIZE
    avail = free * PAGE_SIZE
    used = inuse * PAGE_SIZE
    percent = usage_percent((total - avail), total, _round=1)
    return svmem(total, avail, percent, used, free)


def pids():
    """Returns a list of PIDs currently running on the system."""
    return [int(x) for x in os.listdir('/proc') if x.isdigit()]


def pid_exists(pid):
    """Check for the existence of a unix pid."""
    return _psposix.pid_exists(pid)


def cpu_times():
    """Return system-wide CPU times as a named tuple"""
    ret = cext.per_cpu_times()
    return scputimes(*[sum(x) for x in zip(*ret)])


def per_cpu_times():
    """Return system per-CPU times as a list of named tuples"""
    ret = cext.per_cpu_times()
    return [scputimes(*x) for x in ret]


def cpu_count_logical():
    """Return the number of logical CPUs in the system."""
    try:
        return os.sysconf("SC_NPROCESSORS_ONLN")
    except ValueError:
        # mimic os.cpu_count() behavior
        return None


def boot_time():
    """The system boot time expressed in seconds since the epoch."""
    return cext.boot_time()


def users():
    """Return currently connected users as a list of namedtuples."""
    retlist = []
    rawlist = cext.users()
    localhost = (':0.0', ':0')
    for item in rawlist:
        user, tty, hostname, tstamp, user_process = item
        # note: the underlying C function includes entries about
        # system boot, run level and others.  We might want
        # to use them in the future.
        if not user_process:
            continue
        if hostname in localhost:
            hostname = 'localhost'
        nt = _common.suser(user, tty, hostname, tstamp)
        retlist.append(nt)
    return retlist


def wrap_exceptions(fun):
    """Call callable into a try/except clause and translate ENOENT,
    EACCES and EPERM in NoSuchProcess or AccessDenied exceptions.
    """
    def wrapper(self, *args, **kwargs):
        try:
            return fun(self, *args, **kwargs)
        except EnvironmentError as err:
            # support for private module import
            if (NoSuchProcess is None or AccessDenied is None or
                    ZombieProcess is None):
                raise
            # ENOENT (no such file or directory) gets raised on open().
            # ESRCH (no such process) can get raised on read() if
            # process is gone in meantime.
            if err.errno in (errno.ENOENT, errno.ESRCH):
                if not pid_exists(self.pid):
                    raise NoSuchProcess(self.pid, self._name)
                else:
                    raise ZombieProcess(self.pid, self._name, self._ppid)
            if err.errno in (errno.EPERM, errno.EACCES):
                raise AccessDenied(self.pid, self._name)
            raise
    return wrapper


class Process(object):
    """Wrapper class around underlying C implementation."""

    __slots__ = ["pid", "_name", "_ppid"]

    def __init__(self, pid):
        self.pid = pid
        self._name = None
        self._ppid = None

    @wrap_exceptions
    def name(self):
        # note: this is limited to 15 characters
        return cext.proc_name_and_args(self.pid)[0]

    @wrap_exceptions
    def exe(self):
        # there is no way to get executable path in AIX other than to guess, and guessing is
        # more complex than what's in the wrapping class - so we do it here
        exe = self.cmdline()[0]
        if os.path.sep in exe:
            # relative or absolute path
            if not os.path.isabs(exe):
                # if cwd has changed, we're out of luck - this may be wrong!
                exe = os.path.abspath(os.path.join(self.cwd(), exe))
            if (os.path.isabs(exe) and
                os.path.isfile(exe) and
                os.access(exe, os.X_OK)):
                return exe
            # not found, move to search in PATH using basename only
            exe = os.path.basename(exe)
        # search for exe name PATH
        for path in os.environ["PATH"].split(":"):
            possible_exe = os.path.abspath(os.path.join(path, exe))
            if (os.path.isfile(possible_exe) and
                os.access(possible_exe, os.X_OK)):
                return possible_exe
        return ''

    @wrap_exceptions
    def cmdline(self):
        return cext.proc_name_and_args(self.pid)[1].split(' ')

    @wrap_exceptions
    def create_time(self):
        return cext.proc_basic_info(self.pid)[3]

    @wrap_exceptions
    def num_threads(self):
        return cext.proc_basic_info(self.pid)[5]

    @wrap_exceptions
    def nice_get(self):
        # For some reason getpriority(3) return ESRCH (no such process)
        # for certain low-pid processes, no matter what (even as root).
        # The process actually exists though, as it has a name,
        # creation time, etc.
        # The best thing we can do here appears to be raising AD.
        # Note: tested on Solaris 11; on Open Solaris 5 everything is
        # fine.
        try:
            return cext_posix.getpriority(self.pid)
        except EnvironmentError as err:
            # 48 is 'operation not supported' but errno does not expose
            # it. It occurs for low system pids.
            if err.errno in (errno.ENOENT, errno.ESRCH, 48):
                if pid_exists(self.pid):
                    raise AccessDenied(self.pid, self._name)
            raise

    @wrap_exceptions
    def nice_set(self, value):
        if self.pid in (2, 3):
            # Special case PIDs: internally setpriority(3) return ESRCH
            # (no such process), no matter what.
            # The process actually exists though, as it has a name,
            # creation time, etc.
            raise AccessDenied(self.pid, self._name)
        return cext_posix.setpriority(self.pid, value)

    @wrap_exceptions
    def ppid(self):
        return cext.proc_basic_info(self.pid)[0]

    @wrap_exceptions
    def uids(self):
        real, effective, saved, _, _, _ = cext.proc_cred(self.pid)
        return _common.puids(real, effective, saved)

    @wrap_exceptions
    def gids(self):
        _, _, _, real, effective, saved = cext.proc_cred(self.pid)
        return _common.puids(real, effective, saved)

    @wrap_exceptions
    def cpu_times(self):
        user, system = cext.proc_cpu_times(self.pid)
        return _common.pcputimes(user, system)

    @wrap_exceptions
    def terminal(self):
        raise NotImplementedError()

    @wrap_exceptions
    def cwd(self):
        try:
            return os.readlink("/proc/%s/cwd" % self.pid)
        except OSError as err:
            if err.errno == errno.ENOENT:
                os.stat("/proc/%s" % self.pid)
                return None
            raise

    @wrap_exceptions
    def memory_info(self):
        ret = cext.proc_basic_info(self.pid)
        rss, vms = ret[1] * 1024, ret[2] * 1024
        return _common.pmem(rss, vms)

    # it seems Solaris uses rss and vms only
    memory_info_ex = memory_info

    @wrap_exceptions
    def status(self):
        code = cext.proc_basic_info(self.pid)[6]
        # XXX is '?' legit? (we're not supposed to return it anyway)
        return PROC_STATUSES.get(code, '?')

    @wrap_exceptions
    def open_files(self):
        retlist = []
        hit_enoent = False
        pathdir = '/proc/%d/path' % self.pid
        for fd in os.listdir('/proc/%d/fd' % self.pid):
            path = os.path.join(pathdir, fd)
            if os.path.islink(path):
                try:
                    file = os.readlink(path)
                except OSError as err:
                    # ENOENT == file which is gone in the meantime
                    if err.errno == errno.ENOENT:
                        hit_enoent = True
                        continue
                    raise
                else:
                    if isfile_strict(file):
                        retlist.append(_common.popenfile(file, int(fd)))
        if hit_enoent:
            # raise NSP if the process disappeared on us
            os.stat('/proc/%s' % self.pid)
        return retlist

    def _get_unix_sockets(self, pid):
        """Get UNIX sockets used by process by parsing 'pfiles' output."""
        # TODO: rewrite this in C (...but the damn netstat source code
        # does not include this part! Argh!!)
        cmd = "pfiles %s" % pid
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        stdout, stderr = p.communicate()
        if PY3:
            stdout, stderr = [x.decode(sys.stdout.encoding)
                              for x in (stdout, stderr)]
        if p.returncode != 0:
            if 'permission denied' in stderr.lower():
                raise AccessDenied(self.pid, self._name)
            if 'no such process' in stderr.lower():
                raise NoSuchProcess(self.pid, self._name)
            raise RuntimeError("%r command error\n%s" % (cmd, stderr))

        lines = stdout.split('\n')[2:]
        for i, line in enumerate(lines):
            line = line.lstrip()
            if line.startswith('sockname: AF_UNIX'):
                path = line.split(' ', 2)[2]
                type = lines[i - 2].strip()
                if type == 'SOCK_STREAM':
                    type = socket.SOCK_STREAM
                elif type == 'SOCK_DGRAM':
                    type = socket.SOCK_DGRAM
                else:
                    type = -1
                yield (-1, socket.AF_UNIX, type, path, "", _common.CONN_NONE)

    nt_mmap_grouped = namedtuple('mmap', 'path rss anon locked')
    nt_mmap_ext = namedtuple('mmap', 'addr perms path rss anon locked')

    @wrap_exceptions
    def num_fds(self):
        return len(os.listdir("/proc/%s/fd" % self.pid))

    @wrap_exceptions
    def num_ctx_switches(self):
        return _common.pctxsw(*cext.proc_num_ctx_switches(self.pid))

    @wrap_exceptions
    def wait(self, timeout=None):
        try:
            return _psposix.wait_pid(self.pid, timeout)
        except _psposix.TimeoutExpired:
            # support for private module import
            if TimeoutExpired is None:
                raise
            raise TimeoutExpired(timeout, self.pid, self._name)
