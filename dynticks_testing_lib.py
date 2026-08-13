#!/usr/bin/python3

import os, re
from itertools import groupby

CGROUP_ROOT = "/sys/fs/cgroup"
NOHZ_FULL_PATH = "/sys/devices/system/cpu/nohz_full"

def parse_cpulist(s):
	cpulist = []
	for e in s.strip().split(","):
		e = e.strip()
		# several sysfs files (cpuset.cpus, effective_affinity_list, ...) can
		# be empty: skip the empty fields instead of failing
		if not e or e in ["nohz","managed_irq","domain"]:
			continue

		if "-" not in e:
			cpulist.append(int(e))
		else:
			(start, end) = e.split("-")
			cpulist += range(int(start), int(end) + 1)
	return sorted(cpulist)

def get_cmdline_param(param_name):
	try:
		with open("/proc/cmdline", "r") as fhandle:
			cmdline = fhandle.read()
	except:
		return None

	param = re.search(r'\b' + re.escape(param_name) + r'=(\S+)', cmdline)
	if param:
		return param.group(1)

	return None

def read_file(path):
	try:
		with open(path, "r") as fhandle:
			return fhandle.read().strip()
	except (OSError, UnicodeDecodeError):
		return None

def read_int(path):
	value = read_file(path)
	if value is None:
		return None

	try:
		return int(value)
	except ValueError:
		return None

def safe_parse_cpulist(spec):
	# parse_cpulist() raises on anything that is not a number or a range
	try:
		return parse_cpulist(spec)
	except (ValueError, AttributeError):
		return None

def read_cpulist(path):
	value = read_file(path)
	if value is None or value in ("", "(null)"):
		return []

	return safe_parse_cpulist(value) or []

# The single source of truth for "which CPUs are running full dynticks": the
# kernel populates this file the same way whether it was requested via
# nohz_full= or via the equivalent isolcpus=nohz,<list> flag.
def get_nohz_full_cpus():
	return read_cpulist(NOHZ_FULL_PATH)

# Return the cgroup v2 isolated partition owning 'cpu', or None
def find_isolated_partition(cpu):
	for root, dirs, files in os.walk(CGROUP_ROOT):
		if "cpuset.cpus.partition" not in files:
			continue

		state = read_file(os.path.join(root, "cpuset.cpus.partition"))

		# "isolated invalid" means the partition did not take effect
		if state is None or not state.startswith("isolated") \
		   or state.startswith("isolated invalid"):
			continue

		if cpu in read_cpulist(os.path.join(root, "cpuset.cpus.effective")):
			return root

	return None

# Move a process into a cgroup v2 cgroup. Raises OSError on failure.
def cgroup_attach(cgroup, pid):
	with open(os.path.join(cgroup, "cgroup.procs"), "w") as fhandle:
		fhandle.write(str(pid))

def to_str_range(numbers):
	# the grouping below needs int arithmetic and a numeric sort: callers may
	# pass strings (e.g. os.listdir() entries) in an arbitrary order
	numbers = sorted(int(n) for n in numbers)
	ranges = []

	# Group by the difference between the value and its index
	for _, group in groupby(enumerate(numbers), lambda ix: ix[1] - ix[0]):
		group_list = list(group)
		first = group_list[0][1]
		last = group_list[-1][1]

		if first == last:
			ranges.append(str(first))
		else:
			ranges.append(f"{first}-{last}")

	return ",".join(ranges)


