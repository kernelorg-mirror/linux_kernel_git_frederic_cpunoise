#!/usr/bin/python3

from dynticks_testing_lib import (read_file, find_isolated_partition,
								  cgroup_attach, to_str_range, get_nohz_full_cpus)
import os, sys, subprocess, time

TRACING_PATH = "/sys/kernel/tracing"

# how long the user loops are left spinning while the trace is collected
TRACE_DURATION_S = 10

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

# nohz_full CPUs, however they were requested (nohz_full= or isolcpus=nohz)
nohz_full = get_nohz_full_cpus()
if not nohz_full:
	print("nohz_full= not set ?")
	sys.exit(-1)

print()

allowed_cpus = os.sched_getaffinity(0)

vector_affinity = {}
irq_reset_ex = []

for vec in os.listdir("/proc/irq"):
	path = "/proc/irq/%s" % vec

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
		irq_reset_ex.append(vec)
		pass

if irq_reset_ex:
	print("Couldn't reset IRQ %s affinity" % to_str_range(irq_reset_ex))

user_loops = []

# from here on the IRQ affinity is clobbered and the busy loops are spinning
try:
	for cpu in nohz_full:
		cgroup = None if cpu in allowed_cpus else find_isolated_partition(cpu)

		p = subprocess.Popen(["./user_loop"])

		try:
			if cgroup is not None:
				cgroup_attach(cgroup, p.pid)

			os.sched_setaffinity(p.pid, [cpu])
		except OSError as e:
			print("Can't pin the user loop on CPU %d: %s" % (cpu, e))
			p.kill()
			continue

		user_loops.append(p)

	print("\nEnable trace and sleep for %s seconds" % TRACE_DURATION_S)

	# Trace, starting from an empty buffer
	trace_file_write("tracing_on", "0")
	trace_file_write("trace", "")

	trace_events_enable("1")

	trace_file_write("current_tracer", "nop")
	trace_file_write("tracing_on", "1")

	time.sleep(TRACE_DURATION_S)

	trace_file_write("tracing_on", "0")
	trace_events_enable("0")

	with open(os.path.join(TRACING_PATH, "trace"), "r") as fr, open("./trace", "w") as fw:
		for line in fr:
			fw.write(line)
finally:
	kill_user_loops(user_loops)
	restore_irq_affinity(vector_affinity)
