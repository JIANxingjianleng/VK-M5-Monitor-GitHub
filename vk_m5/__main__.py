# -*- coding: utf-8 -*-
"""包入口：python -m vk_m5 [--selftest ...]"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
