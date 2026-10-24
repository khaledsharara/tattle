"""Gateway mode: this server becomes the home network's DHCP server and default
gateway, so every device's internet traffic passes through it and can be
counted exactly, per device.

It runs as its own container (``python -m tattle.gateway run``) with network
capabilities, next to the unprivileged dashboard. The two share nothing but a
few files (see ``paths.py``): the dashboard only ever reads them.
"""
