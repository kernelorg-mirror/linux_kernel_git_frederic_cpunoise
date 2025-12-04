#!/usr/bin/python3

import re

def parse_cpulist(s):
	cpulist = []
	for e in s.split(","):
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
