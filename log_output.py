#!/usr/bin/python3

import sys
from enum import Enum

# fits every Log.header() title with room to spare; bump it if a longer one is added
HEADER_WIDTH = 36

class Log:
	# off automatically when stdout is redirected to a file/pipe; --no-color
	# forces it off even on a terminal
	enable_color = sys.stdout.isatty()

	class Status(Enum):
		OK = "\033[92m"     # Green
		WARN = "\033[93m"   # Yellow
		FAIL = "\033[91m"   # Red
		INFO = "\033[94m"   # Blue
		RESET = "\033[0m"

	@classmethod
	def print(cls, status, category, message=""):
		status_name = status.name
		reset = Log.Status.RESET.value if cls.enable_color else ""
		color = getattr(Log.Status, status_name, Log.Status.RESET).value if cls.enable_color else ""

		if message:
			print(f"[{color}{status_name:^6}{reset}] {category:<22} | {message}")
		else:
			print(f"[{color}{status_name:^6}{reset}] {category:<22}")

	@classmethod
	def header(cls, title):
		color = Log.Status.INFO.value if cls.enable_color else ""
		reset = Log.Status.RESET.value if cls.enable_color else ""
		border = "─" * HEADER_WIDTH

		print(f"\n{color}┌{border}┐{reset}")
		print(f"{color}│{title.center(HEADER_WIDTH)}│{reset}")
		print(f"{color}└{border}┘{reset}")
