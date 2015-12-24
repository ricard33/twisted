# -*- test-case-name: twisted.test.test_lockfile -*-
# Copyright (c) 2005 Divmod, Inc.
# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Filesystem-based interprocess mutex.
"""

from __future__ import absolute_import, division

import errno
import os

from time import time as _uniquefloat

from twisted.python.runtime import platform
from twisted.python.compat import _PY3

def unique():
    return str(int(_uniquefloat() * 1000))

from os import rename

if not _PY3:
    # Shh, pyflakes -- TimeoutError only exists on Python 3
    TimeoutError = Exception

if not platform.isWindows():
    from os import kill
    from os import symlink
    from os import readlink
    from os import remove as rmlink
    _windows = False
else:
    _windows = True

    # On UNIX, a symlink can be made to a nonexistent location, and
    # FilesystemLock uses this by making the target of the symlink an
    # imaginary, non-existing file named that of the PID of the process with
    # the lock. This has some benefits on UNIX -- making and removing this
    # symlink is atomic. However, because Windows doesn't support symlinks (at
    # least as how we know them), we have to fake this and actually write a
    # file with the PID of the process holding the lock instead.
    # These functions below perform that unenviable, probably-fraught-with-
    # race-conditions duty. - hawkie

    try:
        from win32api import OpenProcess
        import pywintypes
    except ImportError:
        kill = None
    else:
        ERROR_ACCESS_DENIED = 5
        ERROR_INVALID_PARAMETER = 87

        def kill(pid, signal):
            try:
                OpenProcess(0, 0, pid)
            except pywintypes.error as e:
                if e.args[0] == ERROR_ACCESS_DENIED:
                    return
                elif e.args[0] == ERROR_INVALID_PARAMETER:
                    raise OSError(errno.ESRCH, None)
                raise
            else:
                raise RuntimeError("OpenProcess is required to fail.")

    # For monkeypatching in tests
    _open = open


    def symlink(value, filename):
        """
        Write a file at C{filename} with the contents of C{value}. See the
        above comment block as to why this is needed.
        """
        # XXX Implement an atomic thingamajig for win32
        newlinkname = filename + "." + unique() + '.newlink'
        newvalname = os.path.join(newlinkname, "symlink")
        os.mkdir(newlinkname)

        # Python 3 does not support the 'commit' flag of fopen in the MSVCRT
        # (http://msdn.microsoft.com/en-us/library/yeby3zcb%28VS.71%29.aspx)
        if _PY3:
            mode = 'w'
        else:
            mode = 'wc'

        with _open(newvalname, mode) as f:
            f.write(value)
            f.flush()

        if _PY3:
            from time import sleep

            readValue = ""
            iterations = 0
            # Python 3 has no 'commit' flag for fopen, so let Windows catch
            # up... we do this by looping and reading the file, hoping to get
            # the correct value. It sucks, but, what can you do? Locks are
            # global state, and as we all know, global state is BAD and EVIL.
            # NOT EVEN ONCE - Amber
            while readValue != value:
                with _open(newvalname, "r") as f:
                    readValue = f.read()
                iterations += 1
                print(iterations)
                sleep(0.0001)

                # What is a reasonable number here? Well, you give an inch, and
                # Windows takes a mile. There are 63,360 inches in a mile, so
                # that seems as reasonable as any other number. This means we
                # will try to get a lock for at least five seconds.
                if iterations > 63360:
                    try:
                        # Try and remove the failed lock. We have given up at
                        # this point, so if we can't remove it, we can't really
                        # try much.
                        os.remove(newvalname)
                    except:
                        pass
                    # We ought to play sad_trombone.mp3 here. Give up and throw
                    # an exception.
                    raise TimeoutError("Unable to get a lock.")

        try:
            rename(newlinkname, filename)
        except:
            os.remove(newvalname)
            os.rmdir(newlinkname)
            raise


    def readlink(filename):
        """
        Read the contents of C{filename}. See the above comment block as to why
        this is needed.
        """
        try:
            fObj = _open(os.path.join(filename, 'symlink'), 'r')
        except IOError as e:
            if e.errno == errno.ENOENT or e.errno == errno.EIO:
                raise OSError(e.errno, None)
            raise
        else:
            result = fObj.read()
            fObj.close()
            return result


    def rmlink(filename):
        os.remove(os.path.join(filename, 'symlink'))
        os.rmdir(filename)



class FilesystemLock(object):
    """
    A mutex.

    This relies on the filesystem property that creating
    a symlink is an atomic operation and that it will
    fail if the symlink already exists.  Deleting the
    symlink will release the lock.

    @ivar name: The name of the file associated with this lock.

    @ivar clean: Indicates whether this lock was released cleanly by its
        last owner.  Only meaningful after C{lock} has been called and
        returns True.

    @ivar locked: Indicates whether the lock is currently held by this
        object.
    """

    clean = None
    locked = False

    def __init__(self, name):
        self.name = name


    def lock(self):
        """
        Acquire this lock.

        @rtype: C{bool}
        @return: True if the lock is acquired, false otherwise.

        @raise: Any exception os.symlink() may raise, other than
        EEXIST.
        """
        clean = True
        while True:
            try:
                symlink(str(os.getpid()), self.name)
            except OSError as e:
                if _windows and e.errno in (errno.EACCES, errno.EIO):
                    # The lock is in the middle of being deleted because we're
                    # on Windows where lock removal isn't atomic.  Give up, we
                    # don't know how long this is going to take.
                    return False
                if e.errno == errno.EEXIST:
                    try:
                        pid = readlink(self.name)
                    except (IOError, OSError) as e:
                        if e.errno == errno.ENOENT:
                            # The lock has vanished, try to claim it in the
                            # next iteration through the loop.
                            continue
                        elif _windows and e.errno == errno.EACCES:
                            # The lock is in the middle of being
                            # deleted because we're on Windows where
                            # lock removal isn't atomic.  Give up, we
                            # don't know how long this is going to
                            # take.
                            return False
                        raise
                    try:
                        if kill is not None:
                            kill(int(pid), 0)
                    except OSError as e:
                        if e.errno == errno.ESRCH:
                            # The owner has vanished, try to claim it in the
                            # next iteration through the loop.
                            try:
                                rmlink(self.name)
                            except OSError as e:
                                if e.errno == errno.ENOENT:
                                    # Another process cleaned up the lock.
                                    # Race them to acquire it in the next
                                    # iteration through the loop.
                                    continue
                                raise
                            clean = False
                            continue
                        raise
                    return False
                raise
            self.locked = True
            self.clean = clean
            return True


    def unlock(self):
        """
        Release this lock.

        This deletes the directory with the given name.

        @raise: Any exception os.readlink() may raise, or
        ValueError if the lock is not owned by this process.
        """
        pid = readlink(self.name)
        if int(pid) != os.getpid():
            raise ValueError(
                "Lock %r not owned by this process" % (self.name,))
        rmlink(self.name)
        self.locked = False



def isLocked(name):
    """
    Determine if the lock of the given name is held or not.

    @type name: C{str}
    @param name: The filesystem path to the lock to test

    @rtype: C{bool}
    @return: True if the lock is held, False otherwise.
    """
    l = FilesystemLock(name)
    result = None
    try:
        result = l.lock()
    finally:
        if result:
            l.unlock()
    return not result



__all__ = ['FilesystemLock', 'isLocked']
