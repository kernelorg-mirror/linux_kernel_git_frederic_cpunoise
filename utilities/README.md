# Utilities
> [!note]
>[drgn](https://drgn.readthedocs.io/en/latest/) is a debugger that can be used both on live systems and vmcore. Debugging symbols for the target kernel are needed (in SUSE the package is actually named `kernel-default-debuginfo`).
These are some utilities that may help investigate some specific aspects.

[[_TOC_]]

## Dump IRQs (dump_irq.py)
If IRQs are still present on Isolated CPU(s) you can use `dump_irq.py`. This is a drgn (Dragon) script currently supporting only x86_64 able to list IRQs on isolated and housekeeping CPUs. If IRQs are delivered to Isolated CPUs, it will shows also their name.

This is an example (the output is partial, isolated CPUs = 5,7,12):
```
somehost:~/drgn-scripts # ./dump_irq.py
=== Global IRQ Vector Matrix Stats ===
  Matrix Bits      : 256
  Vectors per CPU  : 204 (Usable limit)
  Global Available : 37173
  Global Reserved  : 516
  Total Allocated  : 1261

=== CPU Vector Occupancy ===
CPU   | ROLE         | ALLOCATED  | AVAILABLE  | MANAGED  
---------------------------------------------------------
0     | Housekeeping | 13         | 187        | 1
1     | Housekeeping | 7          | 193        | 1
...
10    | Housekeeping | 8          | 192        | 1
11    | Housekeeping | 7          | 193        | 1
12    | Isolated     | 6          | 195        | 0

=== IRQs effectively targeting isolated CPUs ===
CPU   | IRQ    | NAME
----------------------------------------
12    | 340    | qat0-bundle12
12    | 714    | qat1-bundle15
12    | 721    | qat1-bundle22
12    | 902    | qat2-bundle2
12    | 919    | qat2-bundle19
12    | 947    | qat2-bundle47
```

It can also be helpful to identify situations where there are too many isolated CPUs, and HKs CPUs are not enough to handle the amount of IRQs because vector space is exhausted with a subsequent broken affinity on one or more isolated CPUs.
