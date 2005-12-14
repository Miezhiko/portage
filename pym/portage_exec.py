# portage.py -- core Portage functionality
# Copyright 1998-2004 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2
# $Id: /var/cvsroot/gentoo-src/portage/pym/portage_exec.py,v 1.13.2.4 2005/04/17 09:01:56 jstubbs Exp $


import os, atexit, signal, sys
import portage_data

from portage_const import BASH_BINARY, SANDBOX_BINARY


try:
	import resource
	max_fd_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
except ImportError:
	max_fd_limit = 256


sandbox_capable = (os.path.isfile(SANDBOX_BINARY) and
                   os.access(SANDBOX_BINARY, os.X_OK))


def spawn_bash(mycommand, debug=False, opt_name=None, **keywords):
	args = [BASH_BINARY]
	if not opt_name:
		opt_name = os.path.basename(mycommand.split()[0])
	if debug:
		# Print commands and their arguments as they are executed.
		args.append("-x")
	args.append("-c")
	args.append(mycommand)
	return spawn(args, opt_name=opt_name, **keywords)


def spawn_sandbox(mycommand, opt_name=None, **keywords):
	if not sandbox_capable:
		return spawn_bash(mycommand, opt_name=opt_name, **keywords)
	args=[SANDBOX_BINARY]
	if not opt_name:
		opt_name = os.path.basename(mycommand.split()[0])
	args.append(mycommand)
	return spawn(args, opt_name=opt_name, **keywords)


# We need to make sure that any processes spawned are killed off when
# we exit. spawn() takes care of adding and removing pids to this list
# as it creates and cleans up processes.
spawned_pids = []
def cleanup():
	while spawned_pids:
		pid = spawned_pids.pop()
		try:
			if os.waitpid(pid, os.WNOHANG) == (0, 0):
				os.kill(pid, signal.SIGTERM)
				os.waitpid(pid, 0)
		except OSError:
			# This pid has been cleaned up outside
			# of spawn().
			pass
atexit.register(cleanup)


def spawn(mycommand, env={}, opt_name=None, fd_pipes=None, returnpid=False,
          uid=None, gid=None, groups=None, umask=None, logfile=None,
          path_lookup=True):

	# mycommand is either a str or a list
	if isinstance(mycommand, str):
		mycommand = mycommand.split()

	# If an absolute path to an executable file isn't given
	# search for it unless we've been told not to.
	binary = mycommand[0]
	if (not os.path.isabs(binary) or not os.path.isfile(binary)
	    or not os.access(binary, os.X_OK)):
		binary = path_lookup and find_binary(binary) or None
		if not binary:
			return -1

	# If we haven't been told what file descriptors to use
	# default to propogating our stdin, stdout and stderr.
	if fd_pipes is None:
		fd_pipes = {0:0, 1:1, 2:2}

	# mypids will hold the pids of all processes created.
	mypids = []

	if logfile:
		# Using a log file requires that stdout and stderr
		# are assigned to the process we're running.
		if 1 not in fd_pipes or 2 not in fd_pipes:
			raise ValueError(fd_pipes)

		# Create a pipe
		(pr, pw) = os.pipe()

		# Create a tee process, giving it our stdout and stderr
		# as well as the read end of the pipe.
		mypids.extend(spawn(('tee', '-i', '-a', logfile),
		              returnpid=True, fd_pipes={0:pr,
		              1:fd_pipes[1], 2:fd_pipes[2]}))

		# We don't need the read end of the pipe, so close it.
		os.close(pr)

		# Assign the write end of the pipe to our stdout and stderr.
		fd_pipes[1] = pw
		fd_pipes[2] = pw

	pid = os.fork()

	if not pid:
		try:
			_exec(binary, mycommand, opt_name, fd_pipes,
			      env, gid, groups, uid, umask)
		except Exception, e:
			# We need to catch _any_ exception so that it doesn't
			# propogate out of this function and cause exiting
			# with anything other than os._exit()
			sys.stderr.write("%s:\n   %s\n" % (e, " ".join(mycommand)))
			os._exit(1)

	# Add the pid to our local and the global pid lists.
	mypids.append(pid)
	spawned_pids.append(pid)

	# If we started a tee process the write side of the pipe is no
	# longer needed, so close it.
	if logfile:
		os.close(pw)

	# If the caller wants to handle cleaning up the processes, we tell
	# it about all processes that were created.
	if returnpid:
		return mypids

	# Otherwise we clean them up.
	while mypids:

		# Pull the last reader in the pipe chain. If all processes
		# in the pipe are well behaved, it will die when the process
		# it is reading from dies.
		pid = mypids.pop(0)

		# and wait for it.
		retval = os.waitpid(pid, 0)[1]

		# When it's done, we can remove it from the
		# global pid list as well.
		spawned_pids.remove(pid)

		if retval:
			# If it failed, kill off anything else that
			# isn't dead yet.
			for pid in mypids:
				if os.waitpid(pid, os.WNOHANG) == (0,0):
					os.kill(pid, signal.SIGTERM)
					os.waitpid(pid, 0)
				spawned_pids.remove(pid)

			# If it got a signal, return the signal that was sent.
			if (retval & 0xff):
				return ((retval & 0xff) << 8)

			# Otherwise, return its exit code.
			return (retval >> 8)

	# Everything succeeded
	return 0


def _exec(binary, mycommand, opt_name, fd_pipes, env, gid, groups, uid, umask):

	# If the process we're creating hasn't been given a name
	# assign it the name of the executable.
	if not opt_name:
		opt_name = os.path.basename(binary)

	# Set up the command's argument list.
	myargs = [opt_name]
	myargs.extend(mycommand[1:])

	# Set up the command's pipes.
	my_fds = {}
	# To protect from cases where direct assignment could
	# clobber needed fds ({1:2, 2:1}) we first dupe the fds
	# into unused fds.
	for fd in fd_pipes:
		my_fds[fd] = os.dup(fd_pipes[fd])
	# Then assign them to what they should be.
	for fd in my_fds:
		os.dup2(my_fds[fd], fd)
	# Then close _all_ fds that haven't been explictly
	# requested to be kept open.
	for fd in range(max_fd_limit):
		if fd not in my_fds:
			try:
				os.close(fd)
			except OSError:
				pass

	# Set requested process permissions.
	if gid:
		os.setgid(gid)
	if groups:
		os.setgroups(groups)
	if uid:
		os.setuid(uid)
	if umask:
		os.umask(umask)

	# And switch to the new process.
	os.execve(binary, myargs, env)


def find_binary(binary):
	from portage_const import DEFAULT_PATH
	for path in DEFAULT_PATH.split(":"):
		filename = "%s/%s" % (path, binary)
		if os.access(filename, os.X_OK) and os.path.isfile(filename):
			return filename
	return None
