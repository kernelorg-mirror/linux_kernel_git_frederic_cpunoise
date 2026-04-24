#!/usr/bin/python3

import os, re
from collections import defaultdict
from dynticks_testing_lib import parse_cpulist, get_cmdline_param

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
		return 0

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

def err_nohz_isolated(nohz_full, res):
	print("Not all the nohz_full CPUs are isolated:")
	print("nohz_full=%s\n" % nohz_full)
	print("Found:",end=" ")
	print(res, sep=", ")
	print()

# root is '/sys/fs/cgroup/folder'
def is_cfs_quota_valid(root):
	# check 'cpu.max' (cpu quotas) if present
	cpu_quota = os.path.join(root, "cpu.max")
	if os.path.exists(cpu_quota):
		with open(cpu_quota, 'r') as f:
			quota  = f.read().strip()
			# period = f.read().strip()

		cfs_quota_str = "Found CFS quota on nohz_full %s. This is not valid." % quota
		valid = quota.split(None,1)[0].isnumeric() == False

		if valid:
			cfs_quota_str = "CFS quota '%s'. This is considered valid on nohz_full." % quota
	else:
		cfs_quota_str  = "CFS Quota not set, this is valid on nohz_full."
		valid = True

	return valid, cfs_quota_str

# root is '/sys/fs/cgroup/folder'
def is_isolated_cpuset_valid(root, partition_state, cpulist):
	state_str = ""
	cgroup_cpus = []
	state = partition_state.startswith("isolated invalid")

	# if "isolated invalid" is present, nohz_full is not
	# considered valid either
	if state:
		return state, "Invalid partition configuration, check State above", cgroup_cpus

	cpus_file = os.path.join(root, "cpuset.cpus")
	if os.path.exists(cpus_file):
		with open(cpus_file, 'r') as f:
			cgroup_cpus = parse_cpulist(f.read().strip())

	res = sorted(list(set(cgroup_cpus) & set(cpulist)))
	state = cpulist != res

	if state:
		state_str = "Mismatch, nohz_full IS NOT part of cpuset"
	else:
		state_str = "nohz_full is part of cpuset. Ok!"

	return state, state_str, cgroup_cpus

def check_isolcpus_cpuset(cpulist):
	print("=== ISOLCPUS / CPUSET AND NOHZ_FULL ===")

	isolcpus = get_cmdline_param("isolcpus")
	if isolcpus is not None:
		print("isolcpus= found...")
		isolcpus_list = parse_cpulist(isolcpus)
		res = sorted(list(set(isolcpus_list) & set(cpulist)))
		if cpulist != res:
			err_nohz_isolated(cpulist, res)
		else:
			print("nohz_full is part of isolcpus. Ok!")
	else:
		found = False

		# Walk cgroup directory tree
		cgroup_root = "/sys/fs/cgroup"
		
		for root, dirs, files in os.walk(cgroup_root):
			# check if partitions are present
			if "cpuset.cpus.partition" in files:
				partition_file = os.path.join(root, "cpuset.cpus.partition")
				try:
					with open(partition_file, "r") as pf:
						partition_state = pf.read().strip()

					# we're only interested in isolated partition here
					if partition_state.startswith("isolated") is False:
						continue

					found = True

					# CFS should not be present
					is_quota_valid,quota_str = is_cfs_quota_valid(root)
					cfs_state, cpuset_str, cgroup_cpus = is_isolated_cpuset_valid(root, partition_state, cpulist)

					print(f"Found Partition: {root}")
					print(f"  State        : {partition_state}")
					print(f"  cpuset.cpus  : {sorted(cgroup_cpus)}")
					print(f"  nohz_full    : {sorted(cpulist)}")
					print(f"  Match        : {cpuset_str}")
					print(f"  CFS Quota    : {quota_str}")
					print(f"  Valid Quota? : {is_quota_valid}")
					print("-"*60)

				except IOError as e:
					continue;

		if not found:
			print(f"Error: nohz_full specified {cpulist} but no isolated partitions (isolcpus | cpusets) found!")


def check_configs(cpulist):
	smt = is_smt_enabled()
	check_cpu_governor()

	if cpulist:
		check_irq_affinity(cpulist)

		if smt:
			check_siblings_thread(cpulist)
			check_isolcpus_cpuset(cpulist)

