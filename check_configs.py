#!/usr/bin/python3

import os, re, sys, gzip
from collections import defaultdict
from cpunoise_lib import (parse_cpulist, get_cmdline_param, read_file,
								  read_int, read_cpulist, safe_parse_cpulist,
								  get_nohz_full_cpus,to_str_range)
from log_output import Log

# max number of items (IRQ vectors, workqueues, ...) listed in a single message
MAX_LISTED = 12

# deepest C-state exit latency (us) tolerated on a nohz_full CPU
CSTATE_LATENCY_WARN_US = 100

# vm.stat_interval (s) below which vmstat_update is a relevant noise source
STAT_INTERVAL_WARN_S = 10

# isolcpus= accepts a list of flags before the CPU list
ISOLCPUS_FLAGS = ("nohz", "domain", "managed_irq")

WQ_ROOT = "/sys/devices/virtual/workqueue"

def parse_cpumask(mask):
	# "ffff" and "0000ffff,ffffffff" are both valid hex cpumasks
	if mask is None:
		return []

	bits = mask.replace(",", "").strip()
	if not bits:
		return []

	try:
		value = int(bits, 16)
	except ValueError:
		return []

	return [cpu for cpu in range(value.bit_length()) if value >> cpu & 1]

def read_cpumask(path):
	return parse_cpumask(read_file(path))

def expand_cpu_spec(spec):
	# a kernel cmdline CPU spec also accepts the "all" keyword
	if spec is None:
		return None

	spec = spec.strip()
	if spec == "all":
		return get_online_cpus()

	return safe_parse_cpulist(spec)

def has_cmdline_flag(flag):
	cmdline = read_file("/proc/cmdline")
	if cmdline is None:
		return False

	return flag in cmdline.split()

def get_online_cpus():
	return read_cpulist("/sys/devices/system/cpu/online")

def intersect(a, b):
	return sorted(set(a) & set(b))

def truncate(items):
	items = list(items)
	if len(items) <= MAX_LISTED:
		return ", ".join(items)

	return "%s, ... +%d more" % (", ".join(items[:MAX_LISTED]), len(items) - MAX_LISTED)

def get_running_procs(prefixes):
	found = []

	for entry in os.listdir("/proc"):
		if not entry.isdigit():
			continue

		comm = read_file("/proc/%s/comm" % entry)
		if comm and comm.startswith(prefixes):
			found.append((entry, comm))

	return found

def get_task_affinity(pid):
	status = read_file("/proc/%s/status" % pid)
	if status is None:
		return None

	s = re.search(r'^Cpus_allowed_list:\s*(\S+)', status, re.M)
	if s is None:
		return None

	return safe_parse_cpulist(s.group(1))

_kernel_config = None

def get_kernel_config():
	global _kernel_config

	if _kernel_config is not None:
		return _kernel_config

	_kernel_config = {}
	data = None

	if os.path.exists("/proc/config.gz"):
		try:
			with gzip.open("/proc/config.gz", "rt") as f:
				data = f.read()
		except (OSError, EOFError):
			data = None

	if data is None:
		release = os.uname().release
		for path in ["/boot/config-%s" % release, "/lib/modules/%s/config" % release]:
			data = read_file(path)
			if data is not None:
				break

	if data is None:
		return _kernel_config

	for line in data.splitlines():
		line = line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue

		key, value = line.split("=", 1)
		_kernel_config[key] = value.strip('"')

	return _kernel_config

REQUIRED_CONFIGS = [
	("CONFIG_NO_HZ_FULL", "full dynticks support"),
	("CONFIG_HIGH_RES_TIMERS", "high resolution timers (needed by nohz_full)"),
	("CONFIG_RCU_NOCB_CPU", "RCU callback offloading"),
	("CONFIG_CPU_ISOLATION", "isolcpus / housekeeping CPU support"),
]

PREEMPT_MODELS = [
	"CONFIG_PREEMPT_RT",
	"CONFIG_PREEMPT",
	"CONFIG_PREEMPT_VOLUNTARY",
	"CONFIG_PREEMPT_NONE",
]

def get_thread_siblings_list():
	thread_siblings_list = {}
	seen_cpus = set()

	for entry in sorted(os.listdir("/sys/devices/system/cpu/")):
		if not re.match(r'^cpu\d+$', entry):
			continue

		cpu = int(entry[3:])
		if cpu in seen_cpus:
			continue

		siblings = read_cpulist(f"/sys/devices/system/cpu/{entry}/topology/thread_siblings_list")

		seen_cpus.update(siblings)
		thread_siblings_list[cpu] = siblings

	return thread_siblings_list

def check_siblings_thread(cpulist):
	Log.header("SIBLINGS THREAD <=> NOHZ_FULL")
	
	smt_path = "/sys/devices/system/cpu/smt/active"
	smt = read_int(smt_path)

	if smt is None:
		Log.print(Log.Status.FAIL, "SMT_CHECK", f"Can't find {smt_path}")
		return

	Log.print(Log.Status.INFO, "SMT_NOISE", "Non-isolated cores can cause noise. If both are isolated, they can interfere.")

	siblings_list = []

	if smt:
		Log.print(Log.Status.WARN, "SMT_CHECK", "SMT is enabled. On nohz_full with isolated CPU it should be disabled.")
		siblings_list = get_thread_siblings_list()
	else:
		Log.print(Log.Status.OK, "SMT_CHECK", "SMT is disabled.")

	# none of the siblings should be in cpulist
	for hw_thread in siblings_list:
		siblings_nohz_intersect = intersect(siblings_list[hw_thread], cpulist)

		if siblings_nohz_intersect:
			Log.print(Log.Status.WARN, "SMT_ISOLATION", f"SMT threads should NOT be isolated: {siblings_list[hw_thread]} | found: {siblings_nohz_intersect}")

def check_cpu_governor(cpulist):
	Log.header("CPU FREQUENCY / IDLE")

	# CPU0 is a housekeeping CPU: its governor says nothing about the isolated
	# ones, so report the nohz_full CPUs when we know them
	cpus = sorted(cpulist) if cpulist else [0]

	governors = defaultdict(set)
	drivers = set()
	missing = []

	for cpu in cpus:
		governor = read_file(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor")
		if governor is None:
			missing.append(cpu)
			continue

		governors[governor].add(cpu)

		driver = read_file(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_driver")
		if driver:
			drivers.add(driver)

	if missing:
		Log.print(Log.Status.WARN, "CPU_GOVERNOR",
				  f"No cpufreq sysfs for CPUs {to_str_range(missing)} (driver missing or BIOS controlled)")

	for governor, gov_cpus in governors.items():
		Log.print(Log.Status.INFO, "CPU_GOVERNOR", f"CPUs {to_str_range(gov_cpus)}: governor '{governor}'")

		if governor != "performance":
			Log.print(Log.Status.WARN, "CPU_GOVERNOR_POLICY",
					  f"CPUs {to_str_range(gov_cpus)}: governor '{governor}' allows frequency "
					  "transitions, a jitter source. 'performance' is recommended.")

	if len(governors) > 1:
		Log.print(Log.Status.WARN, "CPU_GOVERNOR_MIX", "nohz_full CPUs do not share the same governor")

	if drivers:
		Log.print(Log.Status.INFO, "CPU_FREQ_DRIVER", f"scaling driver: {', '.join(sorted(drivers))}")

	check_turbo_boost()
	check_cpu_idle(cpus)

# no_turbo and boost use opposite polarity: no_turbo=1 means turbo is OFF,
# boost=1 means it's ON. Only one of the two exists, depending on the driver.
def check_turbo_boost():
	no_turbo = read_int("/sys/devices/system/cpu/intel_pstate/no_turbo")
	if no_turbo is not None:
		if no_turbo:
			Log.print(Log.Status.OK, "CPU_TURBO", "intel_pstate/no_turbo=1: turbo disabled. Ok!")
		else:
			Log.print(Log.Status.WARN, "CPU_TURBO",
					  "intel_pstate/no_turbo=0: turbo boost is enabled, a source of "
					  "frequency-transition jitter.")
		return

	boost = read_int("/sys/devices/system/cpu/cpufreq/boost")
	if boost is None:
		return

	if boost:
		Log.print(Log.Status.WARN, "CPU_TURBO",
				  "cpufreq/boost=1: turbo boost is enabled, a source of frequency-transition jitter.")
	else:
		Log.print(Log.Status.OK, "CPU_TURBO", "cpufreq/boost=0: turbo disabled. Ok!")

def get_cpu_idle_states(cpu):
	path = f"/sys/devices/system/cpu/cpu{cpu}/cpuidle"
	states = []

	if not os.path.isdir(path):
		return states

	for state in sorted(os.listdir(path)):
		if not re.match(r'^state\d+$', state):
			continue

		name = read_file(os.path.join(path, state, "name"))
		latency = read_int(os.path.join(path, state, "latency"))

		if name is None or read_int(os.path.join(path, state, "disable")):
			continue

		states.append((name, latency if latency is not None else 0))

	return states

def check_cpu_idle(cpus):
	if has_cmdline_flag("idle=poll"):
		Log.print(Log.Status.INFO, "CPU_IDLE", "idle=poll set: CPUs never enter an idle state")

	# group the CPUs sharing the same idle configuration to keep the output short
	signatures = defaultdict(set)

	for cpu in cpus:
		states = get_cpu_idle_states(cpu)
		if states:
			signatures[tuple(states)].add(cpu)

	for states, state_cpus in signatures.items():
		desc = ", ".join(f"{name}({latency}us)" for name, latency in states)
		deepest = max(latency for _, latency in states)
		too_deep = deepest > CSTATE_LATENCY_WARN_US

		status = Log.Status.WARN if too_deep else Log.Status.INFO
		Log.print(status, "CPU_IDLE_STATES", f"CPUs {to_str_range(state_cpus)}: enabled C-states: {desc}")

		if too_deep:
			Log.print(Log.Status.WARN, "CPU_IDLE_LATENCY",
					  f"Deepest enabled C-state costs {deepest}us to exit: it adds jitter on isolated CPUs")

	# current global PM QoS target: reading it registers no request
	#try:
	#	with open("/dev/cpu_dma_latency", "rb") as f:
	#		latency = int.from_bytes(f.read(4), sys.byteorder)
	#	Log.print(Log.Status.INFO, "CPU_DMA_LATENCY", f"PM QoS cpu_dma_latency target: {latency}us")
	#except (OSError, ValueError):
	#	pass

def get_irq_name(vec):
	path = "/proc/irq/%s" % vec

	try:
		names = [e for e in os.listdir(path) if os.path.isdir(os.path.join(path, e))]
	except OSError:
		return None

	return names[0] if names else None

def describe_vectors(vectors):
	desc = []

	for vec in sorted(vectors, key=lambda v: int(v) if v.isdigit() else -1):
		name = get_irq_name(vec)
		desc.append(f"{vec} ({name})" if name else vec)

	return truncate(desc)

def get_irq_nohzfull_intersect(cpulist, attr="smp_affinity_list"):
	smp_list = defaultdict(set)

	for vec in os.listdir("/proc/irq"):
		path = "/proc/irq/%s" % vec
		if not os.path.isdir(path) or vec == "0":
			continue

		cpus = intersect(read_cpulist(os.path.join(path, attr)), cpulist)
		if cpus:
			smp_list[frozenset(cpus)].add(vec)

	return smp_list

def check_irq_affinity(cpulist):
	Log.header("IRQ AFFINITY ON NOHZ_FULL CPUs")

	smp_list = get_irq_nohzfull_intersect(cpulist, "smp_affinity_list")

	for cpus, vectors in smp_list.items():
		Log.print(Log.Status.FAIL, "IRQ_AFFINITY",
				  f"NOHZ CPUS {to_str_range(cpus)} not isolated from IRQs: {describe_vectors(vectors)}")

	# smp_affinity_list is only the *requested* mask: managed interrupts
	# (per-queue NVMe/NIC vectors) ignore it, so they look clean there while
	# still firing on an isolated CPU. effective_affinity_list is the real one.
	requested = set().union(*smp_list.values())
	managed = {}

	for cpus, vectors in get_irq_nohzfull_intersect(cpulist, "effective_affinity_list").items():
		only_effective = vectors - requested
		if only_effective:
			managed[cpus] = only_effective

	for cpus, vectors in managed.items():
		Log.print(Log.Status.FAIL, "IRQ_EFFECTIVE",
				  f"NOHZ CPUS {to_str_range(cpus)} are the effective target of IRQs: {to_str_range(vectors)}")

	if managed:
		Log.print(Log.Status.INFO, "IRQ_EFFECTIVE_HINT",
				  "These are likely managed IRQs: they ignore smp_affinity_list. "
				  "Use isolcpus=managed_irq to keep them off the isolated CPUs.")

	default_mask = read_cpumask("/proc/irq/default_smp_affinity")
	default_affinity = intersect(default_mask, cpulist)

	# irqaffinity= is the boot-time request; default_smp_affinity is the
	# runtime state it should have produced
	irqaffinity = get_cmdline_param("irqaffinity")
	if irqaffinity is not None:
		requested = safe_parse_cpulist(irqaffinity)
		Log.print(Log.Status.INFO, "IRQAFFINITY_CMDLINE", f"irqaffinity={to_str_range(requested)} in /proc/cmdline")
		if requested is not None and default_mask and sorted(requested) != sorted(default_mask):
			Log.print(Log.Status.WARN, "IRQAFFINITY_MISMATCH",
					  f"cmdline irqaffinity={to_str_range(requested)} but /proc/irq/default_smp_affinity="
					  f"{to_str_range(default_mask)}")

	if default_affinity:
		Log.print(Log.Status.WARN, "IRQ_DEFAULT_AFFINITY",
				  f"/proc/irq/default_smp_affinity includes nohz_full CPUs {to_str_range(default_affinity)}: "
				  "new IRQs will be allowed on them")

	if not smp_list and not managed and not default_affinity:
		Log.print(Log.Status.OK, "IRQ_AFFINITY", "No IRQ is routed to the nohz_full CPUs.")

IRQBALANCE_CONFIGS = [
	"/etc/sysconfig/irqbalance",
	"/etc/default/irqbalance",
	"/etc/conf.d/irqbalance",
]

def get_irqbalance_banned():
	for path in IRQBALANCE_CONFIGS:
		data = read_file(path)
		if data is None:
			continue

		s = re.search(r'^\s*IRQBALANCE_BANNED_CPULIST\s*=\s*"?([^"\n#]+)"?', data, re.M)
		if s:
			return safe_parse_cpulist(s.group(1).strip()), path

		s = re.search(r'^\s*IRQBALANCE_BANNED_CPUS\s*=\s*"?([^"\n#]+)"?', data, re.M)
		if s:
			return parse_cpumask(s.group(1).strip()), path

	return None, None

def check_irqbalance(cpulist):
	Log.header("IRQBALANCE")

	procs = get_running_procs(("irqbalance",))
	if not procs:
		Log.print(Log.Status.OK, "IRQBALANCE", "irqbalance is not running.")
		return

	pids = ", ".join(pid for pid, _ in procs)
	Log.print(Log.Status.INFO, "IRQBALANCE", f"irqbalance is running (pid: {pids})")

	banned, path = get_irqbalance_banned()

	if banned is None:
		Log.print(Log.Status.WARN, "IRQBALANCE_BANNED",
				  "No IRQBALANCE_BANNED_CPULIST / IRQBALANCE_BANNED_CPUS found: irqbalance "
				  "can move IRQs back onto the isolated CPUs at runtime.")
		Log.print(Log.Status.INFO, "IRQBALANCE_HINT",
				  "Recent irqbalance versions read /sys/devices/system/cpu/isolated and "
				  "nohz_full on their own, but banning the CPUs explicitly is safer.")
		return

	missing = sorted(set(cpulist) - set(banned))
	if missing:
		Log.print(Log.Status.FAIL, "IRQBALANCE_BANNED",
				  f"{path}: nohz_full CPUs {to_str_range(missing)} are NOT banned from irqbalance")
	else:
		Log.print(Log.Status.OK, "IRQBALANCE_BANNED",
				  f"{path}: all nohz_full CPUs are banned from irqbalance. Ok!")

def check_rcu_nocbs(cpulist):
	Log.header("RCU CALLBACK OFFLOADING")

	default_all = get_kernel_config().get("CONFIG_RCU_NOCB_CPU_DEFAULT_ALL") == "y"
	nocbs = expand_cpu_spec(get_cmdline_param("rcu_nocbs"))

	if nocbs is not None:
		Log.print(Log.Status.INFO, "RCU_NOCBS", f"rcu_nocbs={to_str_range(nocbs)}")
	elif default_all:
		nocbs = get_online_cpus()
		Log.print(Log.Status.INFO, "RCU_NOCBS",
				  "No rcu_nocbs= in cmdline but CONFIG_RCU_NOCB_CPU_DEFAULT_ALL=y")
	else:
		nocbs = []
		Log.print(Log.Status.WARN, "RCU_NOCBS",
				  "No rcu_nocbs= in /proc/cmdline. nohz_full implies callback offloading, "
				  "but setting it explicitly is recommended.")

	if nocbs:
		missing = sorted(set(cpulist) - set(nocbs))
		if missing:
			Log.print(Log.Status.FAIL, "RCU_NOCBS_COVERAGE",
					  f"nohz_full CPUs {to_str_range(missing)} are NOT in rcu_nocbs: RCU callbacks "
					  "will run there and keep the tick alive.")
		else:
			Log.print(Log.Status.OK, "RCU_NOCBS_COVERAGE", "All nohz_full CPUs offload RCU callbacks. Ok!")

	if has_cmdline_flag("rcu_nocb_poll"):
		Log.print(Log.Status.INFO, "RCU_NOCB_POLL",
				  "rcu_nocb_poll set: offload kthreads poll instead of being woken by the isolated CPUs")

	# offloading is pointless if the offload kthreads can run back on the
	# isolated CPUs
	threads = get_running_procs(("rcuo",))
	if not threads:
		Log.print(Log.Status.WARN, "RCU_OFFLOAD_THREADS",
				  "No rcuo* kthread found: RCU callback offloading looks inactive.")
		return

	offenders = defaultdict(set)

	for pid, comm in threads:
		affinity = get_task_affinity(pid)
		if affinity is None:
			continue

		cpus = intersect(affinity, cpulist)
		if cpus:
			offenders[frozenset(cpus)].add(comm)

	for cpus, comms in offenders.items():
		Log.print(Log.Status.FAIL, "RCU_OFFLOAD_AFFINITY",
				  f"RCU offload kthreads can run on nohz_full CPUs {to_str_range(cpus)}: {truncate(sorted(comms))}")

	if not offenders:
		Log.print(Log.Status.OK, "RCU_OFFLOAD_AFFINITY",
				  "All rcuo* kthreads are pinned to housekeeping CPUs. Ok!")

def check_workqueue_affinity(cpulist):
	Log.header("WORKQUEUE AFFINITY")

	isolated_path = os.path.join(WQ_ROOT, "cpumask_isolated")
	if os.path.exists(isolated_path):
		wq_isolated = read_cpumask(isolated_path)
		missing = sorted(set(cpulist) - set(wq_isolated))

		if missing:
			Log.print(Log.Status.WARN, "WQ_CPUMASK_ISOLATED",
					  f"nohz_full CPUs {to_str_range(missing)} are not in cpumask_isolated. "
					  "Use isolcpus=domain or workqueue.unbound_cpus= to exclude them.")
		else:
			Log.print(Log.Status.OK, "WQ_CPUMASK_ISOLATED",
					  f"Workqueue isolated cpumask: {to_str_range(wq_isolated)}")

def check_watchdog(cpulist):
	Log.header("WATCHDOG")

	if has_cmdline_flag("nowatchdog"):
		Log.print(Log.Status.INFO, "WATCHDOG_CMDLINE", "nowatchdog set in /proc/cmdline")

	watchdog = read_int("/proc/sys/kernel/watchdog")

	if watchdog is None:
		Log.print(Log.Status.WARN, "WATCHDOG", "Can't read /proc/sys/kernel/watchdog")
	elif watchdog:
		Log.print(Log.Status.INFO, "WATCHDOG", "The soft-lockup watchdog is enabled.")
	else:
		Log.print(Log.Status.OK, "WATCHDOG", "The soft-lockup watchdog is disabled. Ok!")

	if read_int("/proc/sys/kernel/nmi_watchdog"):
		Log.print(Log.Status.INFO, "NMI_WATCHDOG", "nmi_watchdog is enabled.")

	if not watchdog:
		return

	# the watchdog arms a per-CPU hrtimer and wakes the watchdog/N kthread: a
	# guaranteed periodic tick source on every CPU it covers. It defaults to
	# the housekeeping CPUs, but the sysctl can be widened back to include
	# the isolated ones.
	cpumask = read_cpulist("/proc/sys/kernel/watchdog_cpumask")
	cpus = intersect(cpumask, cpulist)

	if cpus:
		Log.print(Log.Status.WARN, "WATCHDOG_CPUMASK",
				  f"watchdog_cpumask includes nohz_full CPUs {to_str_range(cpus)}: the "
				  "soft-lockup hrtimer will keep ticking there")
	else:
		Log.print(Log.Status.OK, "WATCHDOG_CPUMASK", "watchdog_cpumask excludes the nohz_full CPUs. Ok!")

def check_noise_knobs(cpulist):
	Log.header("KERNEL NOISE KNOBS")

	# timers queued on an isolated CPU should be pulled to a housekeeping one
	timer_migration = read_int("/proc/sys/kernel/timer_migration")
	if timer_migration == 1:
		Log.print(Log.Status.OK, "TIMER_MIGRATION", "kernel.timer_migration=1. Ok!")
	elif timer_migration == 0:
		Log.print(Log.Status.WARN, "TIMER_MIGRATION",
				  "kernel.timer_migration=0: timers will not be moved off the isolated CPUs")

	check_mce(cpulist)
	check_clocksource()

	# NUMA balancing drives IPIs and page faults on the isolated CPUs
	numa_balancing = read_int("/proc/sys/kernel/numa_balancing")
	if numa_balancing:
		Log.print(Log.Status.WARN, "NUMA_BALANCING",
				  "kernel.numa_balancing=1: it injects page faults and IPIs on isolated CPUs")
	elif numa_balancing == 0:
		Log.print(Log.Status.OK, "NUMA_BALANCING", "kernel.numa_balancing=0. Ok!")

	check_thp()

def check_mce(cpulist):
	# the MCE poll timer is a classic x86 nohz_full noise source
	offenders = defaultdict(set)
	found = False

	for cpu in sorted(cpulist):
		interval = read_int(f"/sys/devices/system/machinecheck/machinecheck{cpu}/check_interval")
		if interval is None:
			continue

		found = True
		if interval:
			offenders[interval].add(cpu)

	if not found:
		return

	for interval, cpus in offenders.items():
		Log.print(Log.Status.WARN, "MCE_CHECK_INTERVAL",
				  f"CPUs {to_str_range(cpus)}: machine check poll timer every {interval}s. "
				  "Set check_interval to 0 on the isolated CPUs.")

	if not offenders:
		Log.print(Log.Status.OK, "MCE_CHECK_INTERVAL", "MCE polling is disabled on the nohz_full CPUs. Ok!")

def check_clocksource():
	current = read_file("/sys/devices/system/clocksource/clocksource0/current_clocksource")
	if current is None:
		return

	if current == "tsc":
		Log.print(Log.Status.OK, "CLOCKSOURCE", "Current clocksource: tsc. Ok!")
	else:
		available = read_file("/sys/devices/system/clocksource/clocksource0/available_clocksource")
		Log.print(Log.Status.WARN, "CLOCKSOURCE",
				  f"Current clocksource is '{current}', not 'tsc': reading it is slower and "
				  f"adds jitter. Available: {available}")

	# the TSC watchdog periodically compares the TSC against a slower clocksource
	tsc = get_cmdline_param("tsc")
	if tsc:
		Log.print(Log.Status.INFO, "TSC_PARAM", f"tsc={tsc} in /proc/cmdline")
	else:
		Log.print(Log.Status.INFO, "TSC_WATCHDOG",
				  "No tsc= in cmdline: consider tsc=nowatchdog to avoid the periodic "
				  "TSC watchdog checks.")

def check_thp():
	thp = read_file("/sys/kernel/mm/transparent_hugepage/enabled")
	if thp is None:
		return

	if "[always]" in thp:
		Log.print(Log.Status.WARN, "TRANSPARENT_HUGEPAGE",
				  f"transparent_hugepage={thp}: khugepaged causes TLB shootdown IPIs on "
				  "isolated CPUs. 'madvise' or 'never' is recommended.")
	else:
		Log.print(Log.Status.OK, "TRANSPARENT_HUGEPAGE", f"transparent_hugepage={thp}. Ok!")

	# unlike 'enabled', 'always' here defrags synchronously in the fault path
	# itself: a much sharper stall than khugepaged's asynchronous background work
	defrag = read_file("/sys/kernel/mm/transparent_hugepage/defrag")
	if defrag is None:
		return

	if "[always]" in defrag:
		Log.print(Log.Status.WARN, "THP_DEFRAG",
				  f"transparent_hugepage/defrag={defrag}: page faults can trigger "
				  "synchronous compaction. 'madvise' or 'never' is recommended.")
	else:
		Log.print(Log.Status.OK, "THP_DEFRAG", f"transparent_hugepage/defrag={defrag}. Ok!")

def check_net_rps_xps(cpulist):
	Log.header("NETWORK RPS / XPS")

	net_root = "/sys/class/net"
	if not os.path.isdir(net_root):
		return

	offenders = defaultdict(set)

	for iface in sorted(os.listdir(net_root)):
		queues = os.path.join(net_root, iface, "queues")
		if iface == "lo" or not os.path.isdir(queues):
			continue

		for queue in sorted(os.listdir(queues)):
			if queue.startswith("rx-"):
				mask_file = "rps_cpus"
			elif queue.startswith("tx-"):
				mask_file = "xps_cpus"
			else:
				continue

			cpus = intersect(read_cpumask(os.path.join(queues, queue, mask_file)), cpulist)
			if cpus:
				offenders[(iface, mask_file, frozenset(cpus))].add(queue)

	for (iface, mask_file, cpus), queues in offenders.items():
		Log.print(Log.Status.WARN, "NET_%s" % mask_file.upper(),
				  f"{iface}: {mask_file} steers packets to nohz_full CPUs {to_str_range(cpus)} "
				  f"({len(queues)} queue(s))")

	if not offenders:
		Log.print(Log.Status.OK, "NET_RPS_XPS", "No RX/TX queue steers packets to the nohz_full CPUs. Ok!")

def err_nohz_isolated(nohz_full, res):
	Log.print(Log.Status.FAIL, "ISOLATION_MISMATCH", f"Not all nohz_full CPUs are isolated. nohz_full={nohz_full} | Found: {res}")

def get_cgroup_version():
	if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
		return 2
	if os.path.isdir("/sys/fs/cgroup/cpuset"):
		return 1

	return 0

def split_isolcpus(value):
	# isolcpus= accepts "domain,managed_irq,2-11": the flags matter, keep them
	flags = []
	cpu_tokens = []

	for token in value.split(","):
		token = token.strip()
		if not token:
			continue

		if token in ISOLCPUS_FLAGS:
			flags.append(token)
		else:
			cpu_tokens.append(token)

	cpus = expand_cpu_spec(",".join(cpu_tokens)) if cpu_tokens else []

	return flags, cpus or []

# root is '/sys/fs/cgroup/folder'
def is_cfs_quota_valid(root):
	quota = read_file(os.path.join(root, "cpu.max"))

	if quota is None:
		return Log.Status.OK, "CFS Quota not set, this is valid on nohz_full."

	if quota.split(None, 1)[0].isnumeric():
		return Log.Status.FAIL, "Found CFS quota on nohz_full %s. This is not valid." % quota

	return Log.Status.OK, "CFS quota '%s'. This is considered valid on nohz_full." % quota

# root is '/sys/fs/cgroup/folder'
#
# Return the CPUs a cgroup v2 isolated partition is about. Whether they really
# are isolated depends on the partition state and is decided by the caller.
def partition_cpus(root, partition_state):
	requested = read_cpulist(os.path.join(root, "cpuset.cpus"))

	# an invalid partition is demoted to a member: cpuset.cpus.effective then
	# reports the parent CPUs instead of the ones we asked to isolate, so the
	# requested list is the only meaningful one left to report
	if partition_state.startswith("isolated invalid"):
		return requested

	# .effective is the mask the kernel really granted: it accounts for the
	# parent cpuset and for the offline CPUs. Old kernels may not expose it.
	return read_cpulist(os.path.join(root, "cpuset.cpus.effective")) or requested

# Return the nohz_full CPUs that the cgroup v2 isolated partitions are isolating
def check_cpuset_v2(cpulist):
	covered = set()
	unrelated = 0

	for root, dirs, files in os.walk("/sys/fs/cgroup"):
		if "cpuset.cpus.partition" not in files:
			continue

		partition_state = read_file(os.path.join(root, "cpuset.cpus.partition"))

		if partition_state is None or not partition_state.startswith("isolated"):
			continue

		cgroup_cpus = partition_cpus(root, partition_state)

		# nohz_full can be split across several partitions: every partition is
		# checked against the nohz_full CPUs it holds, not against the whole
		# list. The union is verified by the caller.
		mine = intersect(cgroup_cpus, cpulist)
		if not mine:
			unrelated += 1
			continue

		if partition_state.startswith("isolated invalid"):
			Log.print(Log.Status.FAIL, "CGROUP_PARTITION", f"Path: {root} | State: {partition_state}")
			Log.print(Log.Status.FAIL, "CPUSET_MATCH",
					  f"cpuset: {to_str_range(cgroup_cpus)} | nohz_full CPUs {to_str_range(mine)} are NOT "
					  "isolated: the partition did not take effect")
			continue

		covered.update(mine)

		Log.print(Log.Status.OK, "CGROUP_PARTITION", f"Path: {root} | State: {partition_state}")
		Log.print(Log.Status.OK, "CPUSET_MATCH",
				  f"cpuset: {to_str_range(cgroup_cpus)} | isolating nohz_full CPUs: {to_str_range(mine)}. Ok!")

		is_quota_valid, quota_str = is_cfs_quota_valid(root)
		Log.print(is_quota_valid, "CFS_QUOTA", f"CFS Quota: {quota_str}")

	if unrelated:
		Log.print(Log.Status.INFO, "CGROUP_PARTITION",
				  f"{unrelated} more isolated partition(s) hold no nohz_full CPU: not checked")

	return covered

# Return the nohz_full CPUs that the cgroup v1 cpusets are isolating
def check_cpuset_v1(cpulist):
	# on cgroup v1 the isolated partition is a cpuset with
	# cpuset.sched_load_balance disabled
	cpuset_root = "/sys/fs/cgroup/cpuset"
	covered = set()
	balanced = []

	for root, dirs, files in os.walk(cpuset_root):
		# the root cpuset always holds every CPU: it is not an isolation
		if root == cpuset_root or "cpuset.cpus" not in files:
			continue

		# effective_cpus is the mask the kernel really applies
		cgroup_cpus = read_cpulist(os.path.join(root, "cpuset.effective_cpus")) or \
					  read_cpulist(os.path.join(root, "cpuset.cpus"))

		# nohz_full can be split across several cpusets: every cpuset is checked
		# against the nohz_full CPUs it holds, not against the whole list
		mine = intersect(cgroup_cpus, cpulist)
		if not mine:
			continue

		load_balance = read_int(os.path.join(root, "cpuset.sched_load_balance"))

		if load_balance != 0:
			# the ancestors of an isolated cpuset hold its CPUs too and are
			# normally still load balanced: report them only if no other cpuset
			# ends up isolating those CPUs
			balanced.append((root, cgroup_cpus, mine, load_balance))
			continue

		covered.update(mine)

		Log.print(Log.Status.OK, "CPUSET_V1_PARTITION",
				  f"Path: {root} | cpus: {to_str_range(cgroup_cpus)} | isolating nohz_full CPUs: "
				  f"{to_str_range(mine)} | sched_load_balance=0. Ok!")

		# the CFS quota lives in the cpu controller hierarchy on v1
		relpath = os.path.relpath(root, cpuset_root)
		quota = read_int(os.path.join("/sys/fs/cgroup/cpu", relpath, "cpu.cfs_quota_us"))

		if quota is None:
			continue

		if quota == -1:
			Log.print(Log.Status.OK, "CFS_QUOTA", "CFS Quota not set, this is valid on nohz_full.")
		else:
			Log.print(Log.Status.FAIL, "CFS_QUOTA",
					  f"Found CFS quota {quota} on nohz_full. This is not valid.")

	for root, cgroup_cpus, mine, load_balance in balanced:
		left = sorted(set(mine) - covered)
		if not left:
			continue

		Log.print(Log.Status.FAIL, "CPUSET_V1_PARTITION",
				  f"Path: {root} | cpus: {to_str_range(cgroup_cpus)} | sched_load_balance={load_balance}: "
				  f"nohz_full CPUs {to_str_range(left)} are still part of a scheduling domain")

	return covered

def check_isolcpus(cpulist, isolcpus):
	Log.print(Log.Status.INFO, "ISOLCPUS_CMD", "isolcpus= found in cmdline")

	flags, isolcpus_list = split_isolcpus(isolcpus)

	# the flags keep managed IRQs and unbound work away from the isolated CPUs
	if flags:
		Log.print(Log.Status.INFO, "ISOLCPUS_FLAGS", f"isolcpus flags: {', '.join(flags)}")
	else:
		Log.print(Log.Status.INFO, "ISOLCPUS_FLAGS",
				  "No isolcpus flag set: 'domain' is implied. Consider adding "
				  "'managed_irq' to keep managed interrupts off the isolated CPUs.")

	if "managed_irq" not in flags:
		Log.print(Log.Status.WARN, "ISOLCPUS_MANAGED_IRQ",
				  "isolcpus=managed_irq is not set: managed IRQs can still target "
				  "the isolated CPUs.")

	if get_cgroup_version() > 0:
		return

	res = intersect(isolcpus_list, cpulist)
	if cpulist != res:
		err_nohz_isolated(cpulist, res)
	else:
		Log.print(Log.Status.OK, "ISOLCPUS_MATCH", "nohz_full is part of isolcpus. Ok!")

# nohz_full= and isolcpus=nohz,<list> are equivalent ways to request full
# dynticks: either is authoritative, nohz_full= takes precedence if both are set
def get_requested_nohz_full():
	requested = get_cmdline_param("nohz_full")
	if requested is not None:
		return expand_cpu_spec(requested)

	isolcpus = get_cmdline_param("isolcpus")
	if isolcpus is None:
		return None

	flags, cpus = split_isolcpus(isolcpus)
	return cpus if "nohz" in flags else None

def check_isolcpus_cpuset(cpulist):
	Log.header("ISOLCPUS / CPUSET AND NOHZ_FULL")

	# the applied mask can differ from the requested one: CPU0 is always kept
	# as housekeeping and the mask is clamped to CONFIG_NR_CPUS
	effective = get_nohz_full_cpus()
	requested = get_requested_nohz_full()

	if not effective:
		Log.print(Log.Status.FAIL, "NOHZ_FULL_MASK",
				  "/sys/devices/system/cpu/nohz_full is empty: full dynticks is NOT active")
	else:
		Log.print(Log.Status.INFO, "NOHZ_FULL_MASK", f"Effective nohz_full: {to_str_range(effective)}")

	if requested is None:
		Log.print(Log.Status.WARN, "NOHZ_FULL_CMDLINE",
				  "No nohz_full= or isolcpus=nohz found in /proc/cmdline")
	elif effective and sorted(requested) != sorted(effective):
		dropped = sorted(set(requested) - set(effective))
		Log.print(Log.Status.FAIL, "NOHZ_FULL_MISMATCH",
				  f"cmdline nohz_full={to_str_range(requested)} but kernel applied "
				  f"{to_str_range(effective)} | dropped: {to_str_range(dropped)}")

	isolated = read_cpulist("/sys/devices/system/cpu/isolated")
	Log.print(Log.Status.INFO, "ISOLATED_MASK", f"/sys/devices/system/cpu/isolated: {to_str_range(isolated)}")

	if not cpulist:
		return

	if 0 in cpulist:
		Log.print(Log.Status.FAIL, "CPU0_NOHZ_FULL",
				  "CPU0 is in the nohz_full list. The kernel always keeps CPU0 as housekeeping.")

	online = get_online_cpus()
	if not online:
		return

	not_online = sorted(set(cpulist) - set(online))
	if not_online:
		Log.print(Log.Status.WARN, "NOHZ_FULL_OFFLINE", f"nohz_full CPUs not online: {to_str_range(not_online)}")


	isolcpus = get_cmdline_param("isolcpus")
	if isolcpus is not None:
		check_isolcpus(cpulist, isolcpus)

	version = get_cgroup_version()

	if version == 2:
		Log.print(Log.Status.INFO, "CGROUP_VERSION",
				  "cgroup v2 hierarchy detected: checking /sys/fs/cgroup")
		covered = check_cpuset_v2(cpulist)
	elif version == 1:
		Log.print(Log.Status.INFO, "CGROUP_VERSION",
				  "cgroup v1 hierarchy detected: checking /sys/fs/cgroup/cpuset")
		covered = check_cpuset_v1(cpulist)
	else:
		Log.print(Log.Status.WARN, "CGROUP_VERSION", "No cgroup hierarchy found under /sys/fs/cgroup")
		covered = set()

	if isolcpus is None:
		if not covered:
			Log.print(Log.Status.FAIL, "CPUSET_MISSING",
					  f"nohz_full specified {to_str_range(cpulist)} but no isolated partitions (isolcpus | cpusets) found!")
		elif set(cpulist) - covered:
			err_nohz_isolated(cpulist, sorted(covered))
		else:
			Log.print(Log.Status.OK, "CPUSET_MATCH",
					  f"every nohz_full CPU ({to_str_range(cpulist)}) is in an isolated partition. Ok!")

def check_configs(cpulist):
	if not cpulist:
		Log.print(Log.Status.WARN, "NO_CPULIST",
				  "No nohz_full CPU list available: skipping the per-CPU checks.")
		return
	
	# the isolation check must run regardless of SMT: it is the most important
	# one and SMT is expected to be *disabled* on a tuned nohz_full machine
	check_isolcpus_cpuset(cpulist)
	check_siblings_thread(cpulist)
	check_cpu_governor(cpulist)
	check_irqbalance(cpulist)
	check_irq_affinity(cpulist)
	check_rcu_nocbs(cpulist)
	check_workqueue_affinity(cpulist)
	check_watchdog(cpulist)
	check_noise_knobs(cpulist)
	check_net_rps_xps(cpulist)

	print()
