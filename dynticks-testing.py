#!/usr/bin/python3

from dynticks_testing_lib import parse_cpulist
import os, sys, subprocess, time

TRACING_PATH="/sys/kernel/tracing"

def file_write(path, buf):
	f = open(path, "w")
	f.write(buf)
	f.close()

def trace_file_write(path, buf):
	path = os.path.join(TRACING_PATH, path)
	f = open(path, "w")
	f.write(buf)
	f.close()

def trace_file_read(path):
	path = os.path.join(TRACING_PATH, path)
	return open(path).read()

# Parse nohz_full=
if not os.path.exists("/sys/devices/system/cpu/nohz_full"):
	print("Can't find /sys/devices/system/cpu/nohz_full")
	sys.exit(-1)

cpulist = open("/sys/devices/system/cpu/nohz_full").read()
if cpulist.strip() == "(null)":
	print("nohz_full= not set ?")
	sys.exit(-1)

print("nohz_full=%s" % cpulist)

nohz_full = parse_cpulist(cpulist)

# Reaffine IRQ vectors
vector_affinity = {}
for vec in os.listdir("/proc/irq"):
	path = "/proc/irq/%s" % vec
	if not os.path.isdir(path) or vec == "0":
		continue
	path = os.path.join(path, "smp_affinity")
	f = open(path, "r")
	affinity = f.read()
	f.close()
	try:
		file_write(path, "1")
		vector_affinity[vec] = affinity
	except OSError:
		print("Couldn't reset IRQ %s affinity" % vec)
		pass

# Spawn user loops
user_loops = []
for cpu in nohz_full:
	p = subprocess.Popen(["./user_loop"])
	user_loops.append(p)
	os.sched_setaffinity(p.pid, [cpu])

# Trace
trace_file_write("tracing_on", "0");
trace_file_write("trace", "");

EVENTS = ["sched/sched_switch", "irq", "workqueue/workqueue_execute_start", \
		  "timer/hrtimer_expire_entry", "timer/tick_stop"]

for event in EVENTS:
	trace_file_write(os.path.join("events", event, "enable"), "1")

trace_file_write("current_tracer", "nop");
trace_file_write("tracing_on", "1");

time.sleep(10)

trace_file_write("tracing_on", "0");

for event in EVENTS:
	trace_file_write(os.path.join("events", event, "enable"), "0")

# Dump
fr = open(os.path.join(TRACING_PATH, "trace"), "r")
fw = open("./trace", "w")
for l in fr:
	fw.write(l)
fr.close()
fw.close()

# Kill user loops
user_loop = []
for user_loop in user_loops:
	user_loop.kill()

# Restore IRQ vectors affinity
for vec in vector_affinity:
	path = "/proc/irq/%s/smp_affinity" % vec
	try:
		file_write(path, vector_affinity[vec])
	except OSError:
		pass


