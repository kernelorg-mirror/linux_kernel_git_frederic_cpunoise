#!/usr/bin/python3
#
# Copyright (C) 2013-2026 Frederic Weisbecker
# Copyright (C) 2024-2026 Marco Crivellari
# SPDX-License-Identifier: GPL-3.0-or-later

import re
import sys
import statistics
import os
import argparse
from collections import defaultdict
from cpunoise_lib import parse_cpulist, get_nohz_full_cpus
from check_configs import check_configs
from log_output import Log

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

	def match_exit(self, ne):
		return self.vec == ne.vec

class CallbackNoise(Noise):
	def __init__(self, ts, func):
		super().__init__(ts)
		self.func = func

	def create_hist(self):
		return CallbackHist(self, self.func, self.name())

	def match_exit(self, ne):
		return type(self) is type(ne) and self.func == ne.func

class CsdNoise(CallbackNoise):
	def name(self):
		return "CSD (IPI)"

class TimerNoise(CallbackNoise):
	def name(self):
		return "Timer"

class WorkqueueNoise(CallbackNoise):
	def name(self):
		return "Workqueue"

class SyscallNoise(CallbackNoise):
	def name(self):
		return "Syscall"

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
		return f"Tick dep[{self.dep}]"

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
		return f"Sched switch: {self.next_task.comm}:{self.next_task.pid}"

class SoftirqHist(Hist):
	def __init__(self, n, vec):
		super().__init__(n)
		self.vec = vec

	def noise_match(self, n):
		return isinstance(n, SoftirqNoise) and self.vec == n.vec

	def __repr__(self):
		return f"Softirq[{self.vec}]"

class CallbackHist(Hist):
	def __init__(self, n, func, name):
		super().__init__(n)
		self.func = func
		self.noise_class = n.__class__
		self.name = name

	def noise_match(self, n):
		return isinstance(n, self.noise_class) and self.func == n.func

	def __repr__(self):
		return f"{self.name} (func = {self.func})"

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
		self.pop_noise_match_exit(ts, SchedPreemptNoise)

	def add_noise(self, n):
		self.nr_noise += 1
		self.add_hist(n)

	def push_noise(self, n):
		self.seq_nr_noise += 1
		self.noise_stack.append(n)

	def __pop_noise(self, ts):
		if not self.noise_stack:
			return
		n = self.noise_stack.pop()
		if ts is not None:
			n.complete(ts)
		self.add_noise(n)

	def pop_noise_match_class(self, ts, noise_class):
		if not self.noise_stack:
			return
		if not isinstance(self.noise_stack[-1], noise_class):
			print(f"Expected last event was sched_switch on pid {self.pid} ts: {ts}")
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
		if self.seq_nr_noise > 1:
			ts = None
		while self.noise_stack:
			self.__pop_noise(ts)
		self.seq_nr_noise = 0

	def kernel_exit(self, ts):
		if self.ctx == Task.CTX_KERNEL:
			self.noise_duration += ts - self.kernel_entry_ts
			if self.seq_nr_noise == 0:
				n = UnknownNoise(self.kernel_entry_ts)
				n.complete(ts)
				self.add_hist(n)
		elif self.ctx != Task.CTX_UNKNOWN:
			print(f"Unexpected kernel exit at {ts} (lost traces?)")

		self.ctx = Task.CTX_USER
		self.flush_noise_stack(ts)

	def kernel_enter(self, ts):
		if self.ctx not in (Task.CTX_USER, Task.CTX_UNKNOWN):
			print(f"Unexpected kernel entry at {ts} (lost traces?)")
		self.kernel_entry_ts = ts
		self.ctx = Task.CTX_KERNEL

	def is_awakened(self):
		return bool(self.noise_stack) and isinstance(self.noise_stack[-1], SchedLatNoise)

	def is_preempted(self):
		return self.curr_state == 'R' and not isinstance(self, IdleTask)

	def __repr__(self):
		ret = f"Task: {self.comm} Pid: {self.pid} cpus: {str(self.cpus).strip()}"
		if len(self.cpus) > 1:
			ret += " (Migrated!!!)"
		return ret

class IdleTask(Task):
	pass

taskdict = {}
idle_taskdict = {}

def get_task(pid, comm, cpu, state=None):
	if pid == 0:
		if cpu in idle_taskdict:
			return (False, idle_taskdict[cpu])
		t = IdleTask(pid, comm, cpu, state)
		idle_taskdict[cpu] = t
	else:
		if pid in taskdict:
			return (False, taskdict[pid])
		t = Task(pid, comm, cpu, state)
		taskdict[pid] = t
	return (True, t)


# --- Pre-compiled Regular Expressions for Log Parsing ---
RE_TICK_STOP            = re.compile(r"tick_stop: .+?dependency=(\w+)", re.S)
RE_RCU_DYNTICK          = re.compile(r"rcu_dyntick: (\S+)", re.S)
RE_SCHED_WAKEUP_1       = re.compile(r"sched_wakeup:\s+comm=(.+?)\s+pid=([0-9]+)")
RE_SCHED_WAKEUP_2       = re.compile(r"(?:sched_wakeup:\s+)?(.+?):([0-9]+)\s+\[([0-9]+)\]\s+CPU:([0-9]+)")
RE_SCHED_SWITCH_1       = re.compile(r"sched_switch:\s+prev_comm=.+? prev_pid=[0-9]+.+?prev_state=(\S+)\s+==> next_comm=(.+?) next_pid=([0-9]+)", re.S)
RE_SCHED_SWITCH_2       = re.compile(r"sched_switch:\s+.+?:[0-9]+.+?\[.+?\]\s+(\S+)\s+==> (.+?):([0-9]+)", re.S)
RE_SOFTIRQ_ENTRY        = re.compile(r"softirq_entry:\s+vec=[0-9] \[action=(.+?)\]", re.S)
RE_SOFTIRQ_EXIT         = re.compile(r"softirq_exit:\s+vec=[0-9] \[action=(.+?)\]", re.S)
RE_IRQ_WORK_ENTRY       = re.compile(r"irq_work_entry:.+", re.S)
RE_PAGE_FAULT_USER      = re.compile(r"page_fault_user:.+", re.S)
RE_RESCHEDULE_ENTRY     = re.compile(r"reschedule_entry:.+", re.S)
RE_PAGE_FAULT_KERNEL    = re.compile(r"page_fault_kernel:.+", re.S)
RE_LOCAL_TIMER_ENTRY    = re.compile(r"local_timer_entry:.+", re.S)
RE_CSD_FUNC_ENTRY       = re.compile(r"csd_function_entry:\s+func=([^,]+),", re.S)
RE_CSD_FUNC_EXIT        = re.compile(r"csd_function_exit:\s+func=([^,]+),", re.S)
RE_TIMER_EXPIRE_ENTRY   = re.compile(r"timer_expire_entry: timer=\S+ function=(\S+)", re.S)
RE_HRTIMER_EXPIRE_ENTRY = re.compile(r"hrtimer_expire_entry: hrtimer=\S+ .*?function=(\S+)", re.S)
RE_WQ_EXECUTE_START     = re.compile(r"workqueue_execute_start: work struct [^:]+: function (\S+)", re.S)
RE_WQ_EXECUTE_END       = re.compile(r"workqueue_execute_end: work struct [^:]+: function (\S+)", re.S)
RE_SYSCALL_ENTER        = re.compile(r"(sys_[^(]+)[(]", re.S)
RE_SYSCALL_EXIT         = re.compile(r"(sys_[^(]+) ->", re.S)
RE_UNHANDLED            = re.compile(r"([^: (]+).+", re.S)
TRACE_REG               = re.compile(r"^\s+(.+?)-([0-9]+)\s+\[([0-9]{3})\].+?([0-9]+.[0-9]{6}):\s+(.+?)$", re.S)


def tick_stop(line, task, cpu, ts):
	s = RE_TICK_STOP.search(line)
	if s is None:
		print(line)
		sys.exit(-1)
	dep = s.group(1)
	if dep != "NONE":
		n = TickDepNoise(ts, dep)
		task.push_noise(n)
	return True

def rcu_dyntick(line, task, cpu, ts):
	s = RE_RCU_DYNTICK.search(line)
	if s is None:
		print(line)
		sys.exit(-1)
	what = s.group(1)
	if what in ("Start", "StartIrq"):
		task.kernel_exit(ts)
	elif what in ("End", "EndIrq"):
		task.kernel_enter(ts)
	return True

def sched_wakeup(line, task, cpu, ts):
	s = RE_SCHED_WAKEUP_1.search(line)
	if s is None:
		s = RE_SCHED_WAKEUP_2.search(line)
		if s is None:
			print(line)
			sys.exit(-1)

	wake_comm, wake_pid = s.group(1), int(s.group(2))
	_, wake_task = get_task(wake_pid, wake_comm, cpu)

	n = SchedLatNoise(wake_task, ts)
	wake_task.push_noise(n)
	return True

def sched_switch(line, task, cpu, ts):
	s = RE_SCHED_SWITCH_1.search(line)
	if s is None:
		s = RE_SCHED_SWITCH_2.search(line)
		if s is None:
			print(line)
			sys.exit(-1)

	state, next_comm, next_pid = s.group(1), s.group(2), int(s.group(3))
	_, next_task = get_task(next_pid, next_comm, cpu, state)

	if next_task.is_awakened():
		next_task.pop_noise_match_class(ts, SchedLatNoise)

	task.sched_out(next_task, ts, state)
	return True

def softirq_entry(line, task, cpu, ts):
	s = RE_SOFTIRQ_ENTRY.search(line)
	if s is None:
		print(line)
		sys.exit(-1)
	n = SoftirqNoise(ts, s.group(1))
	task.push_noise(n)
	return True

def softirq_exit(line, task, cpu, ts):
	s = RE_SOFTIRQ_EXIT.search(line)
	if s is None:
		print(line)
		sys.exit(-1)
	n = SoftirqNoise(ts, s.group(1))
	task.pop_noise_match_exit(ts, n)
	return True

def irq_work_entry(line, task, cpu, ts):
	if not RE_IRQ_WORK_ENTRY.search(line):
		print(line)
		sys.exit(-1)
	task.push_noise(IrqWorkNoise(ts))
	return True

def page_fault_user(line, task, cpu, ts):
	if not RE_PAGE_FAULT_USER.search(line):
		print(line)
		sys.exit(-1)
	task.push_noise(PageFaultNoise(ts))
	return True

def reschedule_entry(line, task, cpu, ts):
	if not RE_RESCHEDULE_ENTRY.search(line):
		print(line)
		sys.exit(-1)
	task.push_noise(ReschedVecNoise(ts))
	return True

def page_fault_kernel(line, task, cpu, ts):
	if not RE_PAGE_FAULT_KERNEL.search(line):
		print(line)
		sys.exit(-1)
	task.push_noise(PageFaultNoise(ts))
	return True

def local_timer_entry(line, task, cpu, ts):
	if not RE_LOCAL_TIMER_ENTRY.search(line):
		print(line)
		sys.exit(-1)
	task.push_noise(LocalTimerNoise(ts))
	return True

def csd_function_entry(line, task, cpu, ts):
	s = RE_CSD_FUNC_ENTRY.search(line)
	if s is None:
		print(line)
		sys.exit(-1)
	task.push_noise(CsdNoise(ts, s.group(1)))
	return True

def csd_function_exit(line, task, cpu, ts):
	s = RE_CSD_FUNC_EXIT.search(line)
	if s is None:
		print(line)
		sys.exit(-1)
	task.pop_noise_match_exit(ts, CsdNoise(ts, s.group(1)))
	return True

def timer_expire_entry(line, task, cpu, ts):
	s = RE_TIMER_EXPIRE_ENTRY.search(line)
	if s is None:
		print(line)
		sys.exit(-1)
	task.push_noise(TimerNoise(ts, s.group(1)))
	return True

def hrtimer_expire_entry(line, task, cpu, ts):
	s = RE_HRTIMER_EXPIRE_ENTRY.search(line)
	if s is None:
		print(line)
		sys.exit(-1)
	task.push_noise(TimerNoise(ts, s.group(1)))
	return True

def workqueue_execute_start(line, task, cpu, ts):
	s = RE_WQ_EXECUTE_START.search(line)
	if s is None:
		print(line)
		sys.exit(-1)
	task.push_noise(WorkqueueNoise(ts, s.group(1)))
	return True

def workqueue_execute_end(line, task, cpu, ts):
	s = RE_WQ_EXECUTE_END.search(line)
	if s is None:
		print(line)
		sys.exit(-1)
	task.pop_noise_match_exit(ts, WorkqueueNoise(ts, s.group(1)))
	return True

def syscall_enter(line, task, cpu, ts):
	s = RE_SYSCALL_ENTER.search(line)
	if s is None:
		return False
	task.push_noise(SyscallNoise(ts, s.group(1)))
	return True

def syscall_exit(line, task, cpu, ts):
	s = RE_SYSCALL_EXIT.search(line)
	if s is None:
		return False
	task.pop_noise_match_exit(ts, SyscallNoise(ts, s.group(1)))
	return True

def syscall(line, task, cpu, ts):
	return syscall_enter(line, task, cpu, ts) or syscall_exit(line, task, cpu, ts)

unhandled_events = set()

def unhandled_event(line, task, cpu, ts):
	s = RE_UNHANDLED.match(line)
	if s is None:
		print(f"Unhandled event parsing issue: {line}")
		sys.exit(-1)
	unhandled_events.add(s.group(1))
	return True

# --- CMD-LINE PARSER ---
def parse_arguments():
	parser = argparse.ArgumentParser(description="Monitor sources of interference on isolated CPUs")

	parser.add_argument("-t", "--task", dest="exact_tasks", action="append", default=[],
						help="Tasks pattern to filter (can be specified multiple times). Matches any tasks that starts with the specified pattern")
	parser.add_argument("-p", "--pid-task", dest="pid_tasks", action="append", default=[],
						help="Match PID of the task")
	parser.add_argument("-c", "--cpulist", dest="cpulist_raw", type=str,
						help="List of nohz_full CPUs (ex: 2-11,13-18)")
	parser.add_argument("-o", "--check-config-only", dest="only_configs", action="store_true", default=False,
						help="Run a configuration check only. If a trace file is present, it will be ignored")
	parser.add_argument("-s", "--skip-configs", dest="skip_config", action="store_true", default=False,
						help="Skip the configuration check. Useful if noise_parser is only used to parse captured data of another machine")
	parser.add_argument("-C", "--no-color", dest="no_color", action="store_true", default=False,
						help="Disable colored output, even on a terminal. Output is already plain text when redirected to a file or pipe")
	parser.add_argument("trace_file", nargs='?', help="Trace file to parse")

	return parser.parse_args()

def task_matches_filters(task_name, task_pid, exact_tasks=None, pid_tasks=None):
	exact_tasks = exact_tasks or []
	pid_tasks = pid_tasks or []

	if not exact_tasks and not pid_tasks:
		return True
	if task_name in exact_tasks:
		return True
	if str(task_pid) in pid_tasks:
		return True
	return False

def get_hist_category(h):
	if isinstance(h, CallbackHist):
		return h.name

	category_map = {
		TickDepHist: "Tick dependency",
		SchedHist:   "Sched switch",
		SoftirqHist: "Softirq"
	}
	return category_map.get(type(h), str(h))

def task_duration_stats(t):
	durations = [sum(h.duration_list) for h in t.hists if h.duration_list]
	return sum(durations), bool(durations)

def print_table(title, rows, total_row, name_width=60, nr_width=10, dur_width=15):
	sep = "=" * (name_width + nr_width + dur_width + 2)

	print("\n" + sep)
	Log.header(title)
	print(sep)
	print(f"{'Name':<{name_width}} {'Nr':>{nr_width}} {'Duration (us)':>{dur_width}}")

	for name, nr, dur_str in rows:
		print(f"{name:<{name_width}} {nr:>{nr_width},} {dur_str:>{dur_width}}")

	print("-" * (name_width + nr_width + dur_width + 2))

	total_name, total_nr, total_dur = total_row
	print(f"{total_name:<{name_width}} {total_nr:>{nr_width},} {total_dur:>{dur_width},}\n")


events = [
	("tick_stop:", tick_stop),
	("rcu_dyntick:", rcu_dyntick),
	("sched_wakeup", sched_wakeup),
	("sched_switch", sched_switch),
	("softirq_entry", softirq_entry),
	("softirq_exit", softirq_exit),
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
	("workqueue_execute_end", workqueue_execute_end),
	("sys_", syscall),
	("", unhandled_event), # must be last!
]

if __name__ == "__main__":
	args = parse_arguments()

	if args.no_color:
		Log.enable_color = False

	cpulist = parse_cpulist(args.cpulist_raw) if args.cpulist_raw else None

	if cpulist is None:
		print("INFO: --cpulist param. not set: reading nohz_full from sysfs")
		cpulist = get_nohz_full_cpus()

	if not args.skip_config:
		check_configs(cpulist)

	if args.only_configs:
		sys.exit(0)

	if not args.trace_file or not os.path.exists(args.trace_file):
		print(f"File '{args.trace_file}' not found.\n")
		sys.exit(0)

	Log.header("Monitoring sources of interference on isolated cpus")

	fsize = os.path.getsize(args.trace_file)
	cursor = 0
	nr = 0

	with open(args.trace_file, 'r') as f:
		for line in f:
			cursor += len(line)
			if nr % 1000 == 0:  # Throttle stderr writes for performance
				sys.stderr.write(f"Processing {cursor / fsize * 100:.2f}%\r")
			nr += 1

			s = TRACE_REG.match(line)
			if s is not None:
				comm = s.group(1)
				pid = int(s.group(2))
				cpu = int(s.group(3))
				raw_ts = s.group(4).split(".")
				ts = (int(raw_ts[0]) * (10**6)) + int(raw_ts[1])
				rest = s.group(5)

				if cpulist is not None and cpu not in cpulist:
					continue

				is_new, task = get_task(pid, comm, cpu)
				if is_new:
					task.sched_in(0, cpu)

				for prefix, handler in events:
					if rest.startswith(prefix):
						if handler(rest, task, cpu, ts):
							break

	tasklist = list(taskdict.values())

	for t in tasklist:
		t.flush_noise_stack(None)

	print(f"\nUnhandled events: {','.join(unhandled_events)}\n")

	tasklist_sorted = sorted(tasklist, key=lambda t: t.noise_duration, reverse=True)
	category_summary = defaultdict(lambda: {'nr': 0, 'duration': 0, 'has_known_duration': False})
	total_summary_nr = 0
	total_summary_duration = 0

	valid_tasks = []

	for t in tasklist_sorted:
		if t.nr_hists == 0:
			continue

		if not task_matches_filters(t.comm, t.pid, args.exact_tasks, args.pid_tasks):
			continue

		valid_tasks.append(t)
		t.hists = sorted(t.hists, key=lambda h: sum(h.duration_list), reverse=True)

		print(f"{str(t):<65} {'Nr':>10} {'Duration (us)':>17}")

		for h in t.hists:
			duration_str = "Unknown" if not h.duration_list else str(sum(h.duration_list))
			print(f"    {str(h):<61} {h.nr_noise:>10} {duration_str:>17}")

			cat = get_hist_category(h)
			category_summary[cat]['nr'] += h.nr_noise
			total_summary_nr += h.nr_noise

			if h.duration_list:
				dur = sum(h.duration_list)
				category_summary[cat]['duration'] += dur
				category_summary[cat]['has_known_duration'] = True
				total_summary_duration += dur

		print("\n" + " " * 66 + "-" * 28)
		print(f"    {'Total':<61} {t.nr_hists:>10} {t.noise_duration:>17}\n")

	# --- Print Summaries ---

	task_rows = []
	for t in valid_tasks:
		task_total_dur, has_known = task_duration_stats(t)
		migrated_str = " (Migrated!!!)" if len(t.cpus) > 1 else ""
		task_str = f"{t.comm} (pid {t.pid}, cpus {str(t.cpus).strip()}){migrated_str}"
		dur_str = f"{task_total_dur:,}" if has_known else "Unknown"
		task_rows.append((task_str, t.nr_hists, dur_str, task_total_dur))

	task_rows.sort(key=lambda row: row[3], reverse=True)
	task_rows = [row[:3] for row in task_rows]

	print_table("=== Summary: Top Tasks by Noise Duration ===", task_rows,
				("Total", total_summary_nr, total_summary_duration))

	sorted_categories = sorted(category_summary.items(), key=lambda item: item[1]['duration'], reverse=True)

	category_rows = []
	for cat, stats in sorted_categories:
		dur_str = f"{stats['duration']:,}" if stats['has_known_duration'] else "Unknown"
		category_rows.append((cat, stats['nr'], dur_str))

	print_table("=== Summary: Noise by Category (all isolated CPUs) ===", category_rows,
				("Total", total_summary_nr, total_summary_duration))
