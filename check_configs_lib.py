#!/usr/bin/python3

import os, re
from collections import defaultdict
from dynticks_testing_lib import parse_cpulist, get_cmdline_param
from enum import Enum

class Log:
	class Status(Enum):
		OK = "\033[92m"     # Green
		WARN = "\033[93m"   # Yellow
		FAIL = "\033[91m"   # Red
		INFO = "\033[94m"   # Blue
		RESET = "\033[0m"

	@classmethod
	def print(cls, status, category, message=""):
		reset = Log.Status.RESET.value
		status_name = status.name
		color = getattr(Log.Status, status_name, Log.Status.RESET).value

		if message:
			print(f"[{color}{status_name:^6}{reset}] {category:<22} | {message}")
		else:
			print(f"[{color}{status_name:^6}{reset}] {category:<22}")

	@classmethod
	def header(cls, title):
		print(f"\n{Log.Status.INFO.value}=== {title} ==={Log.Status.RESET.value}")

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
	seen_cpus = set()

	cpu_folders = [entry for entry in os.listdir("/sys/devices/system/cpu/") if re.match(r'^cpu\d+$', entry)]

	for hw_thread_s in cpu_folders:
		hw_thread = hw_thread_s.replace("cpu","")

		if hw_thread in seen_cpus:
			continue

		path = f"/sys/devices/system/cpu/cpu{hw_thread}/topology/thread_siblings_list"
		
		with open(path, "r") as s_list:
			hw_threads_list = s_list.read();
		
		hw_threads = parse_cpulist(hw_threads_list)

		# avoid checking same CPUs
		seen_cpus.update(str(cpu) for cpu in hw_threads)

		thread_siblings_list[hw_thread] = hw_threads

	return thread_siblings_list

def is_smt_enabled():
	# check whether SMT is enabled
	smt_path = "/sys/devices/system/cpu/smt/active"
	if  not os.path.exists(smt_path):
		Log.print(Log.Status.FAIL, "SMT_CHECK", f"Can't find {smt_path}")
		return 0

	smt = int(open(smt_path).read().strip())
	if smt:
		Log.print(Log.Status.WARN, "SMT_CHECK", "SMT is enabled. On nohz_full with isolated CPU it should be disabled.")

	return smt

def check_cpu_governor():
	governor_path = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
	if os.path.exists(governor_path):
		governor = open(governor_path).read().strip()
		Log.print(Log.Status.INFO, "CPU_GOVERNOR", f"CPU Governor used: {governor}")
	else:
		Log.print(Log.Status.FAIL, "CPU_GOVERNOR", f"Can't find {governor_path}")

def check_irq_affinity(cpulist):
	Log.header("IRQ AFFINITY ON NOHZ_FULL CPUs")
	#Log.print(Log.Status.INFO, "NOHZ_FULL_CPUS", f"nohz_full={cpulist}")
	smp_list = get_irq_nohzfull_intersect(cpulist)

	for cpus, vectors in smp_list.items():
		sorted_vectors = ', '.join(map(str, sorted(vectors)))
		sorted_cpus = ', '.join(map(str, sorted(cpus)))
		Log.print(Log.Status.FAIL, "IRQ_AFFINITY_ERR", f"NOHZ CPUS {sorted_cpus} not isolated from IRQs: {sorted_vectors}")

def check_siblings_thread(cpulist):
	Log.header("SIBLINGS THREAD <=> NOHZ_FULL")
	Log.print(Log.Status.INFO, "SMT_NOISE", "Non-isolated cores can cause noise. If both are isolated, they can interfere.")

	siblings_list = get_thread_siblings_list()
	# none of the siblings should be in cpulist
	for hw_thread in siblings_list:
		siblings_nohz_intersect = set(siblings_list[hw_thread]) & set(cpulist)

		if len(siblings_nohz_intersect):
			Log.print(Log.Status.WARN, "SMT_ISOLATION_ERR", f"SMT threads should NOT be isolated: {siblings_list[hw_thread]} | found: {list(siblings_nohz_intersect)}")

def err_nohz_isolated(nohz_full, res):
	Log.print(Log.Status.FAIL, "ISOLATION_MISMATCH", f"Not all nohz_full CPUs are isolated. nohz_full={nohz_full} | Found: {res}")

# root is '/sys/fs/cgroup/folder'
def is_cfs_quota_valid(root):
	status = Log.Status.FAIL
	# check 'cpu.max' (cpu quotas) if present
	cpu_quota = os.path.join(root, "cpu.max")
	if os.path.exists(cpu_quota):
		with open(cpu_quota, 'r') as f:
			quota  = f.read().strip()
			# period = f.read().strip()

		cfs_quota_str = "Found CFS quota on nohz_full %s. This is not valid." % quota

		if quota.split(None,1)[0].isnumeric() == False:
			cfs_quota_str = "CFS quota '%s'. This is considered valid on nohz_full." % quota
			status = Log.Status.OK
	else:
		cfs_quota_str  = "CFS Quota not set, this is valid on nohz_full."
		status = Log.Status.OK

	return status, cfs_quota_str

# root is '/sys/fs/cgroup/folder'
def is_isolated_cpuset_valid(root, partition_state, cpulist):
	cgroup_cpus = []
	invalid_state = partition_state.startswith("isolated invalid")

	# if "isolated invalid" is present, nohz_full is not
	# considered valid either
	if invalid_state:
		return Log.Status.FAIL, "Invalid partition configuration, check State above", cgroup_cpus

	cpus_file = os.path.join(root, "cpuset.cpus")
	if os.path.exists(cpus_file):
		with open(cpus_file, 'r') as f:
			cgroup_cpus = parse_cpulist(f.read().strip())

	res = sorted(list(set(cgroup_cpus) & set(cpulist)))
	invalid_state = cpulist != res

	if invalid_state:
		return Log.Status.FAIL, "Mismatch, nohz_full IS NOT part of cpuset", cgroup_cpus

	return Log.Status.OK, "nohz_full is part of cpuset. Ok!", cgroup_cpus

def check_isolcpus_cpuset(cpulist):
	Log.header("ISOLCPUS / CPUSET AND NOHZ_FULL")

	isolcpus = get_cmdline_param("isolcpus")
	if isolcpus is not None:
		Log.print(Log.Status.INFO, "ISOLCPUS_CMD", "isolcpus= found in cmdline")

		isolcpus_list = parse_cpulist(isolcpus)
		res = sorted(list(set(isolcpus_list) & set(cpulist)))
		if cpulist != res:
			err_nohz_isolated(cpulist, res)
		else:
			Log.print(Log.Status.OK, "ISOLCPUS_MATCH", "nohz_full is part of isolcpus. Ok!")
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

					partition_log = Log.Status.OK
					if partition_state.startswith("isolated invalid"):
						partition_log = Log.Status.FAIL

					Log.print(partition_log, "CGROUP_PARTITION", f"Path: {root} | State: {partition_state}")
					Log.print(cfs_state, "CPUSET_MATCH", f"cpuset: {sorted(cgroup_cpus)} | nohz_full: {sorted(cpulist)} | {cpuset_str}")
					Log.print(is_quota_valid, "CFS_QUOTA", f"CFS Quota: {quota_str}")

				except IOError as e:
					continue;

		if not found:
			Log.print(Log.Status.FAIL, "CPUSET_MISSING", f"nohz_full specified {cpulist} but no isolated partitions (isolcpus | cpusets) found!")

def check_configs(cpulist):
	smt = is_smt_enabled()
	check_cpu_governor()

	if cpulist:
		check_irq_affinity(cpulist)

		if smt:
			check_siblings_thread(cpulist)
			check_isolcpus_cpuset(cpulist)

