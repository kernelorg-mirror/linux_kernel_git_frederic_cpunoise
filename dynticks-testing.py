#!/usr/bin/python3

from dynticks_testing_lib import (parse_cpulist, read_file, find_isolated_partition,
								  cgroup_attach)
import os, sys, subprocess, time

TRACING_PATH = "/sys/kernel/tracing"
NOHZ_FULL_PATH = "/sys/devices/system/cpu/nohz_full"

# how long the user loops are left spinning while the trace is collected
TRACE_DURATION_S = 10

# the events noise_parse.py needs to account for the tick and for every noise
# source hitting the isolated CPUs
EVENTS = ["sched/sched_switch", "irq", "workqueue/workqueue_execute_start",
		  "timer/hrtimer_expire_entry", "timer/timer_expire_entry",
		  "timer/tick_stop", "irq_vectors", "workqueue/workqueue_execute_end"]

def file_write(path, buf):
	with open(path, "w") as fhandle:
		fhandle.write(buf)

def trace_file_write(path, buf):
	path = os.path.join(TRACING_PATH, path)

	# a missing file means the tracepoint does not exist on this kernel
	if not os.path.exists(path):
		print("Event not found: %s [Not supported arch? Missing tracepoint?]" % path)
		return

	file_write(path, buf)

def trace_events_enable(enable):
	for event in EVENTS:
		trace_file_write(os.path.join("events", event, "enable"), enable)

def kill_user_loops(user_loops):
	for p in user_loops:
		p.kill()

def restore_irq_affinity(vector_affinity):
	for vec in vector_affinity:
		try:
			file_write("/proc/irq/%s/smp_affinity" % vec, vector_affinity[vec])
		except OSError:
			pass

# Parse nohz_full=
cpulist = read_file(NOHZ_FULL_PATH)
if cpulist is None:
	print("Can't find %s" % NOHZ_FULL_PATH)
	sys.exit(-1)

if cpulist in ("(null)", ""):
	print("nohz_full= not set ?")
	sys.exit(-1)

print("nohz_full=%s" % cpulist)

nohz_full = parse_cpulist(cpulist)

# Work out how each nohz_full CPU can be reached, before touching anything.
#
# With isolcpus= the isolated CPUs stay in our affinity mask, so the user loop
# can be pinned straight away. A cgroup v2 isolated partition instead owns its
# CPUs exclusively: they are stripped from the cpuset of every process outside
# the partition, and sched_setaffinity() on them fails with EINVAL. In that
# case the loop has to join the partition first.
allowed_cpus = os.sched_getaffinity(0)
cpu_cgroup = {}

for cpu in nohz_full:
	if cpu in allowed_cpus:
		cpu_cgroup[cpu] = None
		continue

	cgroup = find_isolated_partition(cpu)
	if cgroup is None:
		print("CPU %d is not in the affinity mask of this process (%s) and no "
			  "cgroup v2 isolated partition owns it." % (cpu, sorted(allowed_cpus)))
		print("Run this script from a cgroup that can reach the nohz_full CPUs.")
		sys.exit(-1)

	print("CPU %d is owned by the isolated partition %s" % (cpu, cgroup))
	cpu_cgroup[cpu] = cgroup

# Move every IRQ to CPU 0 ("1" is the affinity mask of the first CPU): an
# interrupt firing on an isolated CPU would show up as noise in the trace. The
# masks we overwrite are saved and put back at the end of the run.
vector_affinity = {}
for vec in os.listdir("/proc/irq"):
	path = "/proc/irq/%s" % vec

	# IRQ 0 is the timer interrupt, its affinity can't be changed
	if not os.path.isdir(path) or vec == "0":
		continue

	path = os.path.join(path, "smp_affinity")
	affinity = read_file(path)
	if affinity is None:
		continue

	try:
		file_write(path, "1")
		vector_affinity[vec] = affinity
	except OSError:
		print("Couldn't reset IRQ %s affinity" % vec)

# Spawn one busy loop per isolated CPU: a CPU-bound task in userspace is what
# lets the kernel stop the tick, which is the behaviour under test.
user_loops = []
for cpu in nohz_full:
	p = subprocess.Popen(["./user_loop"])
	user_loops.append(p)

	# joining the partition widens the cpuset to the isolated CPUs, so it has
	# to happen before the loop can be pinned to one of them
	cgroup = cpu_cgroup[cpu]
	if cgroup is not None:
		cgroup_attach(cgroup, p.pid)

	os.sched_setaffinity(p.pid, [cpu])

# Trace, starting from an empty buffer
trace_file_write("tracing_on", "0")
trace_file_write("trace", "")

trace_events_enable("1")

trace_file_write("current_tracer", "nop")
trace_file_write("tracing_on", "1")

time.sleep(TRACE_DURATION_S)

trace_file_write("tracing_on", "0")
trace_events_enable("0")

# Dump the trace where noise_parse.py expects to find it
with open(os.path.join(TRACING_PATH, "trace"), "r") as fr, open("./trace", "w") as fw:
	for line in fr:
		fw.write(line)

kill_user_loops(user_loops)
restore_irq_affinity(vector_affinity)
