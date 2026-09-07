#!/usr/bin/env drgn
"""
This is a drgn (dragon) Python script.
For info visit: https://github.com/osandov/drgn

The script produce a table showing the actual vectors allocated and used,
and how much free space there is. If CPU Isolation is used and IRQs are
on one of the isolated CPUs, it prints also IRQ names.

E.g.

# ./dump_irq_on_hk.py --ignore-housekeeping
=== Global IRQ Vector Matrix Stats ===
  Matrix Bits      : 256
  Vectors per CPU  : 204 (Usable limit)
  Global Available : 37173
  Global Reserved  : 516
  Total Allocated  : 1261

=== CPU Vector Occupancy ===
CPU   | ROLE         | ALLOCATED  | AVAILABLE  | MANAGED
------------------------------------------------------
5     | Isolated     | 0          | 201        | 0
7     | Isolated     | 0          | 201        | 0
12    | Isolated     | 8          | 193        | 0

=== IRQs effectively targeting isolated CPUs ===
CPU   | IRQ    | NAME
----------------------------------------
12    | 340    | qat0-bundle12
12    | 714    | qat1-bundle15
12    | 721    | qat1-bundle22
12    | 902    | qat2-bundle2
12    | 919    | qat2-bundle19
12    | 947    | qat2-bundle47
12    | 963    | qat2-bundle63
12    | 977    | qat3-bundle12

--ignore-housekeeping
  This flags ignore housekeeping CPUs (shows only isolated)

--ignore-isolated
  If specified, ignore isolated CPUs (shows only HK)
"""

import drgn
import argparse

from drgn import cast
from drgn.helpers.linux.cpumask import for_each_cpu
from drgn.helpers.linux.percpu import per_cpu_ptr
from drgn.helpers.linux.mapletree import mt_for_each
from drgn.helpers.linux.radixtree import radix_tree_for_each

# -------------------------------------------------------------------------
# DRGN Struct Helpers
# -------------------------------------------------------------------------

def get_val(obj, attr, default=0):
    """Safely fetch a value from a drgn object, returning a default if missing."""
    try:
        if hasattr(obj, attr):
            val = getattr(obj, attr)
            return val.value_() if val else default
    except (AttributeError, drgn.FaultError, TypeError):
        pass
    return default

# -------------------------------------------------------------------------
# IRQ Iteration & Resolution
# -------------------------------------------------------------------------
# for_each_irq_desc and irq_desc_action_names taken from drgn Github repo

def for_each_irq_desc():
    """Iterate over all allocated interrupt descriptors (supports v6.5+ Maple Tree)."""
    irq_desc_type = prog.type("struct irq_desc *")

    try:
        # Kernel 6.5+ (Maple Tree)
        sparse_irqs = prog["sparse_irqs"]
        for irq, _, entry in mt_for_each(sparse_irqs.address_of_()):
            yield irq, cast(irq_desc_type, entry)
        return
    except drgn.ObjectNotFoundError:
        pass

    # Kernel < 6.5 (Radix Tree)
    for irq, entry in radix_tree_for_each(prog["irq_desc_tree"].address_of_()):
        yield irq, cast(irq_desc_type, entry)

def irq_desc_action_names(desc):
    """Get a list of action names set on an interrupt descriptor."""
    action = desc.action
    names = []
    while action:
        name = action.name
        if name:
            names.append(name.string_().decode('utf-8', 'ignore'))
        action = action.next
    return names

def irq_effective_cpus(desc):
    """CPUs this IRQ is effectively targeting."""
    common = desc.irq_common_data
    mask = common.effective_affinity if hasattr(common, "effective_affinity") else common.affinity
    return set(for_each_cpu(as_cpumask_ptr(mask)))

def irq_name(desc):
    names = irq_desc_action_names(desc)
    if names:
        return ", ".join(names)

    if desc.name:
        return desc.name.string_().decode('utf-8', 'ignore')
    return "(none)"

def collect_isolated_irqs(isolated_cpus):
    by_cpu = {cpu: [] for cpu in isolated_cpus}

    for irq, desc in for_each_irq_desc():
        try:
            cpus = irq_effective_cpus(desc)
        except (AttributeError, drgn.FaultError):
            continue

        # IRQ touches any isolated cpus?
        hit = cpus & isolated_cpus
        if not hit:
            continue

        name = irq_name(desc)
        for cpu in hit:
            by_cpu[cpu].append((irq, name))

    return by_cpu

# -------------------------------------------------------------------------
# CPUMask Helpers
# -------------------------------------------------------------------------

def as_cpumask_ptr(mask):
    try:
        return mask[0].address_of_()
    except TypeError:
        return mask.address_of_()


def get_isolated_cpus():
    try:
        mask_ptr = as_cpumask_ptr(prog["tick_nohz_full_mask"])
        if mask_ptr.value_() == 0:
            return set()
        return set(for_each_cpu(mask_ptr))
    except (KeyError, drgn.FaultError):
        return set()


def get_online_cpus():
    try:
        mask_ptr = as_cpumask_ptr(prog["__cpu_online_mask"])
        if mask_ptr.value_() == 0:
            raise KeyError("Online mask pointer is NULL")
        return list(for_each_cpu(mask_ptr))
    except (KeyError, drgn.FaultError):
        try:
            return list(range(prog["nr_cpu_ids"].value_()))
        except KeyError:
            return list(range(128))

# -------------------------------------------------------------------------
# Formatting and Output
# -------------------------------------------------------------------------

def print_global_stats(vm):
    print("=== Global IRQ Vector Matrix Stats ===")
    print(f"  Matrix Bits      : {get_val(vm, 'matrix_bits')}")
    print(f"  Vectors per CPU  : {get_val(vm, 'alloc_size')} (Usable limit)")
    print(f"  Global Available : {get_val(vm, 'global_available')}")
    print(f"  Global Reserved  : {get_val(vm, 'global_reserved')}")
    print(f"  Total Allocated  : {get_val(vm, 'total_allocated')}\n")

def print_isolated_irqs(isolated_cpus):
    print("=== IRQs effectively targeting isolated CPUs ===")
    if not isolated_cpus:
        print("(no isolated CPUs found)\n")
        return

    by_cpu = collect_isolated_irqs(isolated_cpus)
    print(f"{'CPU':<5} | {'IRQ':<6} | {'NAME'}")
    print("-" * 40)

    found = False
    for cpu in sorted(by_cpu):
        for irq, name in sorted(by_cpu[cpu]):
            found = True
            print(f"{cpu:<5} | {irq:<6} | {name}")

    if not found:
        print("(none)")
    print()

def dump_irq_matrix(args):
    try:
        vm = prog["vector_matrix"][0]
    except KeyError:
        print("Error: 'vector_matrix' not found. x86 APIC matrix allocator required.")
        return

    print_global_stats(vm)

    online_cpus = get_online_cpus()
    isolated_cpus = get_isolated_cpus()

    # Filter target CPUs based on CLI flags
    target_cpus = [
        cpu for cpu in online_cpus
        if not (cpu in isolated_cpus and args.ignore_isolated) and
           not (cpu not in isolated_cpus and args.ignore_housekeeping)
    ]

    print("=== CPU Vector Occupancy ===")
    print(f"{'CPU':<5} | {'ROLE':<12} | {'ALLOCATED':<10} | {'AVAILABLE':<10} | {'MANAGED':<10}")
    print("-" * 57)

    for cpu in target_cpus:
        role = "Isolated" if cpu in isolated_cpus else "Housekeeping"

        try:
            cmap = per_cpu_ptr(vm.maps, cpu)[0]
        except (drgn.FaultError, TypeError):
            continue

        if get_val(cmap, 'online', default=1) == 0:
            continue

        alloc_val = get_val(cmap, 'allocated')
        avail_val = get_val(cmap, 'available')
        man_val   = get_val(cmap, 'managed')

        warn = " <--- [WARNING: EXHAUSTION RISK]" if avail_val < 10 else ""
        print(f"{cpu:<5} | {role:<12} | {alloc_val:<10} | {avail_val:<10} | {man_val:<10}{warn}")

    print()
    if not args.ignore_isolated:
        print_isolated_irqs(isolated_cpus)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dump x86 IRQ Vector Matrix Occupancy & Names")
    parser.add_argument("--ignore-housekeeping", action="store_true", help="Hide Housekeeping CPUs from output")
    parser.add_argument("--ignore-isolated", action="store_true", help="Hide Isolated CPUs from output")
    args = parser.parse_args()

    dump_irq_matrix(args)
