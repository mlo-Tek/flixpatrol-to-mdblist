#!/usr/bin/env python3
"""Entry point for the mlo-Tek fork."""

import flixpatrol_to_mdblist as sync
from mlo_patches import install


if __name__ == "__main__":
    install(sync)
    sync.main()
