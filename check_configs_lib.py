#!/usr/bin/python3

import os, re
from collections import defaultdict
from dynticks_testing_lib import parse_cpulist

def get_irq_nohzfull_intersect(cpulist):
	smp_list = defaultdict(set)

	for vec in os.listdir("/proc/irq"):
		path = "/proc/irq/%s" % vec
		if not os.path.isdir(path) or vec == "0":
			continue
		path = os.path.join(path, "smp_affinity_list")
		f_list = open(path, "r")
		affinity = f_list.read()
		f_list.close()

		# Be sure IRQ affinity isn't set for nohz CPUs
		cpus = parse_cpulist(affinity)
		res = set(cpulist) & set(cpus)
		if len(res):
			smp_list[frozenset(res)].add(vec)

	return smp_list

def get_thread_siblings_list():
	thread_siblings_list = {};

	cpu_folders = [entry for entry in os.listdir("/sys/devices/system/cpu/") if re.match(r'^cpu\d+$', entry)]

	for hw_thread_s in cpu_folders:
		hw_thread = hw_thread_s.replace("cpu","")

		# take only 1 result, eg:
		# /sys/devices/system/cpu/cpu0/topology/thread_siblings_list:0,112
		# results in 2 match: for 0 and 112
		cpu_present = any(hw_thread in sublist for sublist in thread_siblings_list)
		if cpu_present:
			continue

		path = "/sys/devices/system/cpu/cpu%s/" % hw_thread
		
		path = os.path.join(path, "topology/thread_siblings_list")
		s_list = open(path, "r")
		hw_threads_list = s_list.read();
		s_list.close()
		
		hw_threads = parse_cpulist(hw_threads_list)
		thread_siblings_list[hw_thread] = hw_threads

	return thread_siblings_list

def is_smt_enabled():
	# check whether SMT is enabled
	smt_path = "/sys/devices/system/cpu/smt/active"
	if  not os.path.exists(smt_path):
		print("Can't find %s " % smt_path)

	smt = int(open(smt_path).read().strip())
	if smt:
		print("SMT is enabled. On nohz_full with isolated CPU it should be disabled.\n")

	return smt

def check_cpu_governor():
	governor_path = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
	if os.path.exists(governor_path):
		governor = open(governor_path).read().strip()
		print("CPU Governor used: %s" % governor)
	else:
		print("Can't find %s " % governor_path)

def check_irq_affinity(cpulist):
	print("nohz_full=%s\n" % cpulist)
	print("=== IRQ AFFINITY ON NOHZ_FULL CPUs  ===")
	smp_list = get_irq_nohzfull_intersect(cpulist)

	for cpus, vectors in smp_list.items():
		sorted_vectors = ', '.join(map(str, sorted(vectors)))
		sorted_cpus = ', '.join(map(str, sorted(cpus)))
		print("Wrong IRQ affinity on vectors:", end = " ")
		print(sorted_vectors, sep=", ")
		print("These CPUs are nohz_full:", end = " ")
		print(sorted_cpus, sep=", ")
		print()

def check_siblings_thread(cpulist):
	print("=== SIBLINGS THREAD <=> NOHZ_FULL  ===")
	print("The non-isolated core can be a source of noise, disturbing the isolated workload.")
	print("If both are isolated, they can interfere with each other.\n");

	siblings_list = get_thread_siblings_list()
	# none of the siblings should be in cpulist
	for hw_thread in siblings_list:
		siblings_nohz_intersect = set(siblings_list[hw_thread]) & set(cpulist)

		if len(siblings_nohz_intersect):
			print("!!!WARN!!! SMT threads should NOT be isolated:", end = " ")
			print(siblings_list[hw_thread], sep=", ", end=" ")
			print("found:", end=" ")
			print(siblings_nohz_intersect, sep=", ")
			print()

def check_configs(cpulist):
	smt = is_smt_enabled()
	check_cpu_governor()

	if cpulist:
		check_irq_affinity(cpulist)

		if smt:
			check_siblings_thread(cpulist)	


