#!/usr/bin/python3

def parse_cpulist(s):
	cpulist = []
	for e in s.split(","):
		if "-" not in e:
			cpulist.append(int(e))
		else:
			(start, end) = e.split("-")
			cpulist += range(int(start), int(end) + 1)
	return cpulist
