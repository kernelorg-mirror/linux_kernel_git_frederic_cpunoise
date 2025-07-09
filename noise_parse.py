#!/usr/bin/python3

import re, sys, statistics , os
from optparse import OptionParser
from collections import defaultdict
from dynticks_testing_lib import parse_cpulist, get_cmdline_param
from check_configs_lib import check_configs

class Noise:
	def __init__(self, ts):
		self.start = ts
		self.end = None

	def complete(self, end):
		self.end = end

	def duration(self):
		if self.end is None:
			return None
		else:
			return self.end - self.start

	def match_exit(self, ne):
		return False

	def histable(self):
		return True

class SchedLatNoise(Noise):
	def __init__(self, next_task, ts):
		super().__init__(ts)
		self.next_task = next_task

	def create_hist(self):
		return SchedLatHist(self)

	def histable(self):
		return not isinstance(self.next_task, IdleTask)

class IrqWorkNoise(Noise):
	def create_hist(self):
		return IrqWorkHist(self)

class LocalTimerNoise(Noise):
	def create_hist(self):
		return LocalTimerHist(self)

class PageFaultNoise(Noise):
	def create_hist(self):
		return PageFaultHist(self)

class ReschedVecNoise(Noise):
	def create_hist(self):
		return ReschedVecHist(self)

class SchedNoise(Noise):
	def __init__(self, next_task, ts):
		super().__init__(ts)
		self.next_task = next_task

	def create_hist(self):
		return SchedHist(self)

	def histable(self):
		return not isinstance(self.next_task, IdleTask)

class SchedPreemptNoise(Noise):
	def __init__(self, next_task, ts):
		super().__init__(ts)
		self.next_task = next_task

	def create_hist(self):
		return SchedPreemptHist(self)

	def match_exit(self, n):
		return isinstance(self, n)

	def histable(self):
		return not isinstance(self.next_task, IdleTask)

class TickDepNoise(Noise):
	def __init__(self, ts, dep):
		super().__init__(ts)
		self.dep = dep

	def create_hist(self):
		return TickDepHist(self, self.dep)

class SoftirqNoise(Noise):
	def __init__(self, ts, vec):
		super().__init__(ts)
		self.vec = vec

	def create_hist(self):
		return SoftirqHist(self, self.vec)

class CallbackNoise(Noise):
	def __init__(self, ts, func):
		super().__init__(ts)
		self.func = func

	def create_hist(self):
		return CallbackHist(self, self.func, self.name())

class CsdNoise(CallbackNoise):
	def name(self):
		return "CSD (IPI)"

	def match_exit(self, ne):
		return self.func == ne.func

class TimerNoise(CallbackNoise):
	def name(self):
		return "Timer"

class WorkqueueNoise(CallbackNoise):
	def name(self):
		return "Workqueue"

class SyscallNoise(CallbackNoise):
	def name(self):
		return "Syscall"

	def match_exit(self, ne):
		return self.func == ne.func

class UnknownNoise(Noise):
	def create_hist(self):
		return UnknownHist(self)

	def __repr__(self):
		return "Unknown"

class Hist:
	def __init__(self, n):
		self.nr_noise = 0
		self.duration_list = []
		self.add(n)

	def add(self, n):
		self.nr_noise += 1
		duration = n.duration()
		if duration is not None:
			self.duration_list.append(duration)

class TickDepHist(Hist):
	def __init__(self, n, dep):
		super().__init__(n)
		self.dep = dep

	def noise_match(self, n):
		return isinstance(n, TickDepNoise)

	def __repr__(self):
		return "Tick dep[%s]" % self.dep

class IrqWorkHist(Hist):
	def noise_match(self, n):
		return isinstance(n, IrqWorkNoise)

	def __repr__(self):
		return "Irq work"

class LocalTimerHist(Hist):
	def noise_match(self, n):
		return isinstance(n, LocalTimerNoise)

	def __repr__(self):
		return "Local timer"

class PageFaultHist(Hist):
	def noise_match(self, n):
		return isinstance(n, PageFaultNoise)

	def __repr__(self):
		return "Page fault"

class ReschedVecHist(Hist):
	def noise_match(self, n):
		return isinstance(n, ReschedVecNoise)

	def __repr__(self):
		return "Reschedule IPI"
	
class SchedLatHist(Hist):
	def __init__(self, n):
		super().__init__(n)
		self.next_task = n.next_task

	def noise_match(self, n):
		return isinstance(n, SchedLatNoise) and n.next_task is self.next_task

	def __repr__(self):
		return "Wakeup Latency"

class SchedPreemptHist(Hist):
	def __init__(self, n):
		super().__init__(n)
		self.next_task = n.next_task

	def noise_match(self, n):
		return isinstance(n, SchedPreemptNoise) and n.next_task is self.next_task

	def __repr__(self):
		return "Preempt. noise"

class SchedHist(Hist):
	def __init__(self, n):
		super().__init__(n)
		self.next_task = n.next_task

	def noise_match(self, n):
		return isinstance(n, SchedNoise) and n.next_task is self.next_task

	def __repr__(self):
		return "Sched switch: %s:%d" % (self.next_task.comm, self.next_task.pid)

class SoftirqHist(Hist):
	def __init__(self, n, vec):
		super().__init__(n)
		self.vec = vec

	def noise_match(self, n):
		return isinstance(n, SoftirqNoise) and self.vec == n.vec

	def __repr__(self):
		return "Softirq[%s]" % (self.vec)

class CallbackHist(Hist):
	def __init__(self, n, func, name):
		super().__init__(n)
		self.func = func
		self.noise_class = n.__class__
		self.name = name

	def noise_match(self, n):
		return isinstance(n, self.noise_class) and self.func == n.func

	def __repr__(self):
		return "%s (func = %s)" % (self.name, self.func)

class UnknownHist(Hist):
	def noise_match(self, n):
		return isinstance(n, UnknownNoise)

	def __repr__(self):
		return "Unknown"

class Task:
	current = {}

	CTX_UNKNOWN = -1
	CTX_KERNEL = 0
	CTX_USER = 1

	def __init__(self, pid, comm, cpu, state=None):
		self.pid = pid
		self.comm = comm
		self.cpus = set([cpu])
		self.curr_cpu = cpu
		self.curr_state = state
		self.noise_stack = []
		self.hists = []
		self.nr_noise = 0
		self.seq_nr_noise = 0
		self.nr_hists = 0
		self.noise_duration = 0
		self.ctx = Task.CTX_UNKNOWN
		self.kernel_entry_ts = -1

	def sched_out(self, next_task, ts, state):
		self.curr_state = state
		if self.is_preempted():
			n = SchedPreemptNoise(self, ts)
			self.push_noise(n)

		n = SchedNoise(next_task, ts)
		self.push_noise(n)
		next_task.sched_in(ts, self.curr_cpu)

	def sched_in(self, ts, cpu):
		self.curr_cpu = cpu
		self.cpus.add(cpu)
		Task.current[cpu] = self
		self.pop_noise_match_class(ts, SchedNoise)
		# if a match exist, the task has been previously preempted
		self.pop_noise_match_exit(ts, SchedPreemptNoise)
	
	def add_noise(self, n):
		self.nr_noise += 1
		self.add_hist(n)

	def push_noise(self, n):
		self.seq_nr_noise += 1
		self.noise_stack.append(n)

	def __pop_noise(self, ts):
		if len(self.noise_stack) == 0:
			return
		n = self.noise_stack.pop()
		if ts is not None:
			n.complete(ts)
		self.add_noise(n)

	def pop_noise_match_class(self, ts, noise_class):
		if len(self.noise_stack) == 0:
			return
		if not isinstance(self.noise_stack[-1], noise_class):
			print("Expected last event was sched_switch on pid %d ts: %d" % (self.pid, ts))
			return
		self.__pop_noise(ts)

	def pop_noise_match_exit(self, ts, ne):
		for n in self.noise_stack[::-1]:
			if n.match_exit(ne):
				self.noise_stack.remove(n)
				n.complete(ts)
				self.add_noise(n)
				return

	def add_hist(self, n):
		if not n.histable():
			return
		self.nr_hists += 1
		for h in self.hists:
			if h.noise_match(n):
				h.add(n)
				return
		self.hists.append(n.create_hist())

	def flush_noise_stack(self, ts):
		# Single event can account their duration here
		if self.seq_nr_noise > 1:
			ts = None

		while len(self.noise_stack):
			self.__pop_noise(ts)

		self.seq_nr_noise = 0

	def kernel_exit(self, ts):
		if self.ctx == Task.CTX_KERNEL:
			self.noise_duration += ts - self.kernel_entry_ts
			# Issue unknown noise if none happened during kernel round-trip
			if self.seq_nr_noise == 0:
				n = UnknownNoise(self.kernel_entry_ts)
				n.complete(ts)
				self.add_hist(n)
		elif self.ctx != Task.CTX_UNKNOWN:
			print("Unexpected kernel exit at %d (lost traces?)" % ts)

		self.ctx = Task.CTX_USER
		self.flush_noise_stack(ts)


	def kernel_enter(self, ts):
		if self.ctx != Task.CTX_USER and self.ctx != Task.CTX_UNKNOWN:
			print("Unexpected kernel entry at %d (lost traces?)" % ts)
		self.kernel_entry_ts = ts
		self.ctx = Task.CTX_KERNEL

	def is_akawened(self):
		return len(self.noise_stack) > 0 and isinstance(self.noise_stack[-1], SchedLatNoise)

	def is_preempted(self):
		return self.curr_state == 'R' and not isinstance(self, IdleTask)

	def __repr__(self):
		ret = "Task: %s Pid: %d" % (self.comm, self.pid)
		ret += " cpus: %s" % self.cpus.__repr__().strip()
		if len(self.cpus) > 1:
			ret += " (Migrated!!!)"
		return ret

class IdleTask(Task):
	pass

taskdict = { }
idle_taskdict = { }

def get_task(pid, comm, cpu, state=None):
	if pid == 0:
		if cpu in idle_taskdict:
			return (False, idle_taskdict[cpu])
		else:
			t = IdleTask(pid, comm, cpu, state)
			idle_taskdict[cpu] = t
	else:
		if pid in taskdict:
			return (False, taskdict[pid])
		else:
			t = Task(pid, comm, cpu, state)
			taskdict[pid] = t
	return (True, t)

def tick_stop(line, task, cpu, ts):
	# tick_stop: success=1 dependency=NONE
	s = re.search("tick_stop: .+?dependency=(\w+)", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	dep = s.group(1)
	if dep != "NONE":
		n = TickDepNoise(ts, dep)
		task.push_noise(n)

	return True

def rcu_dyntick(line, task, cpu, ts):
	s = re.search("rcu_dyntick: (\S+)", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	what = s.group(1)
	if what in ("Start", "StartIrq"):
		task.kernel_exit(ts)
	elif what in ("End", "EndIrq"):
		task.kernel_enter(ts)

	return True

def sched_wakeup(line, task, cpu, ts):
	s = re.search("(?:sched_wakeup:\s+)?(.+?):([0-9]+)\s+\[([0-9]+)\]\s+CPU:([0-9]+)", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	(wake_comm, wake_pid) = (s.group(1), int(s.group(2)))
	(_, wake_task) = get_task(wake_pid, wake_comm, cpu)

	n = SchedLatNoise(wake_task, ts)
	wake_task.push_noise(n)
	return True

def sched_switch(line, task, cpu, ts):
	# trace format
	# ex: sched_switch: prev_comm=dynticks-testin prev_pid=140026 prev_prio=120 prev_state=S ==> next_comm=swapper/21 next_pid=0 next_prio=120
	s = re.search("sched_switch:\s+prev_comm=.+? prev_pid=[0-9]+.+?prev_state=(\w)\s+==> next_comm=(.+?) next_pid=([0-9]+)", line, re.S)
	if s is None:
		# trace-cmd format
		# ex: sched_switch:         swapper/27:0 [120] R ==> kworker/27:1:578 [120]
		s = re.search("sched_switch:\s+.+?:[0-9]+.+?\[.+?\]\s+([A-Z])\s+==> (.+?):([0-9]+)", line, re.S)
		if s is None:
			print(line)
			sys.exit(-1)
			return False

	(state, next_comm, next_pid) = (s.group(1), s.group(2), int(s.group(3)))
	(is_new, next_task) = get_task(next_pid, next_comm, cpu, state)

	# True: the task has been woken up
	if next_task.is_akawened():
		next_task.pop_noise_match_class(ts, SchedLatNoise)

	task.sched_out(next_task, ts, state)

	return True

def softirq_entry(line, task, cpu, ts):
	s = re.search("softirq_entry:\s+vec=[0-9] \[action=(.+?)\]", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	vec = s.group(1)
	n = SoftirqNoise(ts, vec)
	#n.complete(ts)
	task.push_noise(n)

	return True

def irq_work_entry(line, task, cpu, ts):
	s = re.search("irq_work_entry:.+", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	n = IrqWorkNoise(ts)
	#n.complete(ts)
	task.push_noise(n)

	return True

def page_fault_user(line, task, cpu, ts):
	s = re.search("page_fault_user:.+", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	n = PageFaultNoise(ts)
	#n.complete(ts)
	task.push_noise(n)

	return True

def reschedule_entry(line, task, cpu, ts):
	s = re.search("reschedule_entry:.+", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	n = ReschedVecNoise(ts)
	#n.complete(ts)
	task.push_noise(n)

	return True

def page_fault_kernel(line, task, cpu, ts):
	s = re.search("page_fault_kernel:.+", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	n = PageFaultNoise(ts)
	#n.complete(ts)
	task.push_noise(n)
	
	return True

def local_timer_entry(line, task, cpu, ts):
	s = re.search("local_timer_entry:.+", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	n = LocalTimerNoise(ts)
	#n.complete(ts)
	task.push_noise(n)

	return True

def csd_function_entry(line, task, cpu, ts):
	s = re.search("csd_function_entry:\s+func=([^,]+),", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	func = s.group(1)
	n = CsdNoise(ts, func)
	#n.complete(ts)
	task.push_noise(n)

	return True

def csd_function_exit(line, task, cpu, ts):
	s = re.search("csd_function_exit:\s+func=([^,]+),", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	func = s.group(1)
	n = CsdNoise(ts, func)
	task.pop_noise_match_exit(ts, n)
	
	return True

def timer_expire_entry(line, task, cpu, ts):
	s = re.search("timer_expire_entry: timer=\S+ function=(\S+)", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	timer = s.group(1)
	n = TimerNoise(ts, timer)
	#n.complete(ts)
	task.push_noise(n)

	return True

def hrtimer_expire_entry(line, task, cpu, ts):
	# trace format:
	# hrtimer_expire_entry: hrtimer=0xff41d0a32a1c7db8 now=7260211852499 function=hrtimer_wakeup/0x0
	# trace-cmd format:
	# hrtimer_expire_entry: hrtimer=00000000fd65e54e function=tick_nohz_handler now=340292315972110
	s = re.search("hrtimer_expire_entry: hrtimer=\S+ .*?function=(\S+)", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	timer = s.group(1)
	n = TimerNoise(ts, timer)
	#n.complete(ts)
	task.push_noise(n)

	return True

def workqueue_execute_start(line, task, cpu, ts):
	s = re.search("workqueue_execute_start: work struct [^:]+: function (\S+)", line, re.S)
	if s is None:
		print(line)
		sys.exit(-1)
		return False
	work = s.group(1)
	n = WorkqueueNoise(ts, work)
	#n.complete(ts)
	task.push_noise(n)

	return True

def syscall_enter(line, task, cpu, ts):
	s = re.search("(sys_[^(]+)[(]", line, re.S)
	if s is None:
		return False
	name = s.group(1)
	n = SyscallNoise(ts, name)
	#n.complete(ts)
	task.push_noise(n)
	return True

def syscall_exit(line, task, cpu, ts):
	s = re.search("(sys_[^(]+) ->", line, re.S)
	if s is None:
		return False
	name = s.group(1)
	n = SyscallNoise(ts, name)
	#n.complete(ts)
	task.pop_noise_match_exit(ts, n)

	return True

def syscall(line, task, cpu, ts):
	if syscall_enter(line, task, cpu, ts):
		return True
	elif syscall_exit(line, task, cpu, ts):
		return True
	else:
		return False

unhandled_events = set()

def unhandled_event(line, task, cpu, ts):
	s = re.match("([^: (]+).+", line, re.S)
	if s is None:
		print("Unhandled event parsing issue: %s" % line)
		sys.exit(-1)
	unhandled_events.add(s.group(1))

	return True

"""""""""""""""""
 CMD-LINE PARSER
"""""""""""""""""
def parse_arguments():
	parser = OptionParser(usage="usage: %prog [options] trace_file")

	parser.add_option("-t", "--task",
						dest="exact_tasks",
						action="append",
						default=[],
						help="Tasks pattern to filter (can be specified multiple times"
							"It matches any tasks that starts with the specified pattern"
	)

	parser.add_option("-p", "--pid-task",
						dest="pid_tasks",
						action="append",
						help="Match PID of the task",
						default=[]
	)

	parser.add_option("-c", "--cpulist",
						action="callback",
						callback=cpulist_callback,
						type="string", nargs=1, dest="cpulist_raw",
						help="List of nohz_full CPUs (ex: 2-11,13-18)"
	)

	(options, args) = parser.parse_args()

	trace_file = args[0]

	return options, trace_file

def task_matches_filters(task_name, task_pid, exact_tasks=None, pid_tasks=None):
	exact_tasks = exact_tasks or []
	pid_tasks = pid_tasks or []

	# No flags provided
	if not exact_tasks and not pid_tasks:
		return True

	# -t flag: match exact task name
	if task_name in exact_tasks:
		return True

	# -p flag: match the PID
	if str(task_pid) in pid_tasks:
		return True

	return False

cpulist = None

def cpulist_callback(option, opt, value, parser):
	global cpulist
	cpulist = parse_cpulist(value)

"""""""""""""""""
 / CMD-LINE PARSER
"""""""""""""""""

events = [
	("tick_stop:", tick_stop),
	("rcu_dyntick:", rcu_dyntick),
	("sched_wakeup", sched_wakeup),
	("sched_switch", sched_switch),
	("softirq_entry", softirq_entry),
	("irq_work_entry", irq_work_entry),
	("page_fault_user", page_fault_user),
	("reschedule_entry", reschedule_entry),
	("local_timer_entry", local_timer_entry),
	("page_fault_kernel", page_fault_kernel),
	("csd_function_exit", csd_function_exit),
	("csd_function_entry", csd_function_entry),
	("timer_expire_entry", timer_expire_entry),
	("hrtimer_expire_entry", hrtimer_expire_entry),
	("workqueue_execute_start", workqueue_execute_start),
	("sys_", syscall),
	("", unhandled_event), # must be last!
]

TRACE_REG = re.compile("^\s+(.+?)-([0-9]+)\s+\[([0-9]{3})\].+?([0-9]+.[0-9]{6}):\s+(.+?)$", re.S)
nr = 0

(options, trace_file) = parse_arguments()

try:
	f = open(trace_file)
except FileNotFoundError as e:
	print("File '%s' not found.\n" % trace_file)
	exit(0)

fsize = os.path.getsize(trace_file)
cursor = 0

# if --cpulist is not set, read nohz_full
if cpulist is None:
	print("INFO: --cpulist param. not set: reading nohz_full from /proc/cmdline")
	nohz_list = get_cmdline_param("nohz_full")
	if nohz_list:
		cpulist = parse_cpulist(nohz_list)

# Check system configurations...
# (smt, irq affinity, siblings thread, governor,...)
check_configs(cpulist)

while True:
	line = f.readline()
	if not line:
		break
	cursor += float(len(line))
	sys.stderr.write("Processing %f%%\r" % (cursor / fsize * 100))
	#sys.stderr.write("Processing line: %d\r" % nr)
	nr += 1
	s = TRACE_REG.match(line)
	if s is not None:
		comm = s.group(1)
		pid =  int(s.group(2))
		cpu = int(s.group(3))
		raw_ts = s.group(4).split(".")
		ts = (int(raw_ts[0]) * (10**6)) + int(raw_ts[1])
		rest = s.group(5)

		if cpulist is not None and cpu not in cpulist:
			continue

		(is_new, task) = get_task(pid, comm, cpu)
		if is_new:
			task.sched_in(0, cpu)

		for e in events:
			if rest.startswith(e[0]):
				if e[1](rest, task, cpu, ts):
					break
		#print("%s %d %d %d" % (comm, pid, cpu, ts))

tasklist = [i[1] for i in taskdict.items()]

for t in tasklist:
	t.flush_noise_stack(None)

print("Unhandled events: %s" % ",".join(unhandled_events))
print()

#tasklist_sorted = sorted(tasklist, key = lambda t : t.nr_hists, reverse = True)
def sort_duration(t):
	total = 0
	for h in t.hists:
		total += sum(h.duration_list)
	return total

tasklist_sorted = sorted(tasklist, key = lambda t : t.noise_duration, reverse = True)
#tasklist_sorted = sorted(tasklist, key = sort_duration, reverse = True)
#sorted(tasklist, key = lambda t : t.nr_hists, reverse = True)

for t in tasklist_sorted:
	if t.nr_hists == 0:
		continue
		
	if task_matches_filters(t.comm, t.pid, options.exact_tasks, options.pid_tasks) == False:
		continue

	#t.hists = sorted(t.hists, key = lambda h : h.nr_noise, reverse = True)
	#t.hists = sorted(t.hists, key = lambda h : sum(h.duration_list), reverse = True)
	t.hists = sorted(t.hists, key = lambda h : sum(h.duration_list), reverse = True)
	print(t)
	total_duration = 0
	for h in t.hists:
		print("\t{: <45} Nr:{: >10}".format(*[h.__repr__(), h.nr_noise]), end = "")
		if len(h.duration_list) == 0:
			print("\tDuration:     Unknown")
		else:
			duration = sum(h.duration_list)
			print("\tDuration:{: >12}".format(duration))
			total_duration += duration
		#print("Nr: %d\n" % len(h.noise_list))
	print()
	print("\t%s%s" % (" " * 46, "Total: {: >6}".format(t.nr_hists)), end = "")
	print("\tTotal: {: >14}\n".format(t.noise_duration))
"""
for snap in sorted(Snaps):
	for e in Snaps[snap]:
		(ts, pid, waketime) = e
		print("Lat: %d of %s:%d switch at %d wake at %d" % (snap, Tasks[pid].comm, pid, ts, waketime))

print("Total: %d Avg: %d Stdev: %d" % (sum(Delays), statistics.mean(Delays), statistics.stdev(Delays)))
"""
