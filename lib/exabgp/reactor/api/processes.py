"""
process.py

Created by Thomas Mangin on 2011-05-02.
Copyright (c) 2009-2013 Exa Networks. All rights reserved.
"""

import os
import errno
import time
import subprocess
import select
import fcntl

from exabgp.util.errstr import errstr

from exabgp.reactor.api.encoding import Text,JSON
from exabgp.configuration.file import formated
from exabgp.logger import Logger

class ProcessError (Exception):
	pass

def preexec_helper ():
	# make this process a new process group
	#os.setsid()
	# This prevent the signal to be sent to the children (and create a new process group)
	os.setpgrp()
	#signal.signal(signal.SIGINT, signal.SIG_IGN)

class Processes (object):
	# how many time can a process can respawn in the time interval
	respawn_number = 5
	respawn_timemask = 0xFFFFFF - pow(2,6) + 1  # '0b111111111111111111000000' (around a minute, 63 seconds)

	def __init__ (self,reactor):
		self.logger = Logger()
		self.reactor = reactor
		self.clean()
		self.silence = False

		from exabgp.configuration.environment import environment
		self.highres = environment.settings().api.highres

	def clean (self):
		self._process = {}
		self._api = {}
		self._api_encoder = {}
		self._neighbor_process = {}
		self._broken = []
		self._respawning = {}

	def _terminate (self,process):
		self.logger.processes("Terminating process %s" % process)
		self._process[process].terminate()
		self._process[process].wait()
		del self._process[process]

	def terminate (self):
		for process in list(self._process):
			if not self.silence:
				try:
					neighbor = self.reactor.configuration.process[process]['neighbor']
					self.write(process,self._api_encoder[process].shutdown(neighbor))
				except ProcessError:
					pass
		self.silence = True
		time.sleep(0.1)
		for process in list(self._process):
			try:
				self._terminate(process)
			except OSError:
				# we most likely received a SIGTERM signal and our child is already dead
				self.logger.processes("child process %s was already dead" % process)
				pass
		self.clean()

	def _start (self,process):
		try:
			if process in self._process:
				self.logger.processes("process already running")
				return
			if not process in self.reactor.configuration.process:
				self.logger.processes("Can not start process, no configuration for it (anymore ?)")
				return

			# Prevent some weird termcap data to be created at the start of the PIPE
			# \x1b[?1034h (no-eol) (esc)
			os.environ['TERM']='dumb'

			run = self.reactor.configuration.process[process].get('run','')
			if run:
				api = self.reactor.configuration.process[process]['encoder']
				self._api_encoder[process] = JSON('3.4.0',self.highres) if api == 'json' else Text('3.3.2')

				self._process[process] = subprocess.Popen(run,
					stdin=subprocess.PIPE,
					stdout=subprocess.PIPE,
					preexec_fn=preexec_helper
					# This flags exists for python 2.7.3 in the documentation but on on my MAC
					# creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
				)
				fcntl.fcntl(self._process[process].stdout.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)

				self.logger.processes("Forked process %s" % process)

				around_now = int(time.time()) & self.respawn_timemask
				if process in self._respawning:
					if around_now in self._respawning[process]:
						self._respawning[process][around_now] += 1
						# we are respawning too fast
						if self._respawning[process][around_now] > self.respawn_number:
							self.logger.processes("Too many respawn for %s (%d) terminating program" % (process,self.respawn_number),'critical')
							raise ProcessError()
					else:
						# reset long time since last respawn
						self._respawning[process] = {around_now: 1}
				else:
					# record respawing
					self._respawning[process] = {around_now: 1}

			neighbor = self.reactor.configuration.process[process]['neighbor']
			self._neighbor_process.setdefault(neighbor,[]).append(process)
		except (subprocess.CalledProcessError,OSError,ValueError),e:
			self._broken.append(process)
			self.logger.processes("Could not start process %s" % process)
			self.logger.processes("reason: %s" % str(e))

	def start (self,restart=False):
		for process in self.reactor.configuration.process:
			if restart:
				self._terminate(process)
			self._start(process)
		for process in list(self._process):
			if not process in self.reactor.configuration.process:
				self._terminate(process)

	def broken (self,neighbor):
		if self._broken:
			if '*' in self._broken:
				return True
			for process in self._neighbor_process.get(neighbor,[]):
				if process in self._broken:
					return True
		return False

	def fds (self):
		return [self._process[process].stdout for process in self._process]

	def received (self):
		for process in list(self._process):
			try:
				proc = self._process[process]
				r,_,_ = select.select([proc.stdout,],[],[],0)
				if r:
					try:
						for line in proc.stdout:
							line = line.rstrip()
							if line:
								self.logger.processes("Command from process %s : %s " % (process,line))
								yield (process,formated(line))
							else:
								self.logger.processes("The process died, trying to respawn it")
								self._terminate(process)
								self._start(process)
								break
					except IOError,e:
						if e.errno == errno.EINTR:  # call interrupted
							pass  # we most likely have data, we will try to read them a the next loop iteration
						elif e.errno != errno.EAGAIN:  # no more data
							self.logger.processes("unexpected errno received from forked process (%s)" % errstr(e))
			except (subprocess.CalledProcessError,OSError,ValueError):
				self.logger.processes("Issue with the process, terminating it and restarting it")
				self._terminate(process)
				self._start(process)

	def write (self,process,string):
		while True:
			try:
				self._process[process].stdin.write('%s\n' % string)
			except IOError,e:
				self._broken.append(process)
				if e.errno == errno.EPIPE:
					self._broken.append(process)
					self.logger.processes("Issue while sending data to our helper program")
					raise ProcessError()
				else:
					# Could it have been caused by a signal ? What to do.
					self.logger.processes("Error received while SENDING data to helper program, retrying (%s)" % errstr(e))
					continue
			break

		try:
			self._process[process].stdin.flush()
		except IOError,e:
			# AFAIK, the buffer should be flushed at the next attempt.
			self.logger.processes("Error received while FLUSHING data to helper program, retrying (%s)" % errstr(e))

		return True

	def _notify (self,neighbor,event):
		for process in self._neighbor_process.get(neighbor,[]):
			if process in self._process:
				yield process
		for process in self._neighbor_process.get('*',[]):
			if process in self._process:
				yield process

	def reset (self,peer):
		if self.silence: return
		for process in self._notify(peer.neighbor.peer_address,'*'):
			data = self._api_encoder[process].reset(peer)
			if data:
				self.write(process,data)

	def increase (self,peer):
		if self.silence: return
		for process in self._notify(peer.neighbor.peer_address,'*'):
			data = self._api_encoder[process].increase(peer)
			if data:
				self.write(process,data)

	def up (self,peer):
		if self.silence: return
		for process in self._notify(peer.neighbor.peer_address,'neighbor-changes'):
			self.write(process,self._api_encoder[process].up(peer))

	def connected (self,peer):
		if self.silence: return
		for process in self._notify(peer.neighbor.peer_address,'neighbor-changes'):
			self.write(process,self._api_encoder[process].connected(peer))

	def down (self,peer,reason):
		if self.silence: return
		for process in self._notify(peer.neighbor.peer_address,'neighbor-changes'):
			self.write(process,self._api_encoder[process].down(peer,reason))

	def notification (self,peer,code,subcode,data):
		if self.silence: return
		for process in self._notify(peer.neighbor.peer_address,'neighbor-changes'):
			self.write(process,self._api_encoder[process].notification(peer,code,subcode,data))

	def receive (self,peer,category,header,body):
		if self.silence: return
		for process in self._notify(peer.neighbor.peer_address,'receive-packets'):
			self.write(process,self._api_encoder[process].receive(peer,category,header,body))

	def keepalive (self,peer,category,header,body):
		if self.silence: return
		for process in self._notify(peer.neighbor.peer_address,'receive-keepalives'):
			self.write(process,self._api_encoder[process].keepalive(peer,header,body))

	def open (self,peer,direction,open_msg,header,body):
		if self.silence: return
		for process in self._notify(peer.neighbor.peer_address,'receive-opens'):
			self.write(process,self._api_encoder[process].open(peer,direction,open_msg,header,body))

	def send (self,peer,category,header,body):
		if self.silence: return
		for process in self._notify(peer.neighbor.peer_address,'send-packets'):
			self.write(process,self._api_encoder[process].send(peer,category,header,body))

	def update (self,peer,update,header,body):
		if self.silence: return
		for process in self._notify(peer.neighbor.peer_address,'receive-routes'):
			self.write(process,self._api_encoder[process].update(peer,update,header,body))

	def refresh (self,peer,refresh,header,body):
		if self.silence: return
		for process in self._notify(peer.neighbor.peer_address,'receive-refresh'):
			self.write(process,self._api_encoder[process].refresh(peer,refresh,header,body))

	def operational (self,peer,what,operational,header,body):
		if self.silence: return
		for process in self._notify(peer.neighbor.peer_address,'receive-operational'):
			self.write(process,self._api_encoder[process].operational(peer,what,operational,header,body))
