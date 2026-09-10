Make sure to have `nohz_full` configured in your system. The tool will automatically read `/sys/devices/system/cpu/nohz_full`.

To run:
```
$ ./run
```

Look at the resulting trace in the generated `trace` file.
hrtimer events should report you the tick events (unless you haven't
`CONFIG_HIGH_RES_TIMERS=y`) and `tick_stop` events tell you what's going on with full dynticks
behaviour.

trace.1.example shows an example of a successful full dynticks behaviour. The
tick got mostly stopped during the execution of the user loop.

> [!note]
> If you still experience issue, you can take a look to one of the [utilities](./utilities).

## Authors & Copyright

* Copyright (C) 2013-2026 Frederic Weisbecker
* Copyright (C) 2024-2026 Marco Crivellari

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version. See the [LICENSE](LICENSE) file for details.
