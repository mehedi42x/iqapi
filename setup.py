#!/usr/bin/env python3
"""iqapi — Modular IQ Option trading API + bot.

This is the pip-installable distribution.  It ships two importable
packages:

* ``iqoptionapi`` — the thin, bot-facing facade over ``iq_option_api``
                    (``from iqoptionapi import IQAPI``).  One file per
                    capability.
* ``bot``          — the modular live trader / backtester
                    (``bot`` command, ``python -m bot``).

The layered websocket engine those two run on is **not** bundled here; it
is the separate ``iq_option_api`` package (see the README).
"""

from pathlib import Path

from setuptools import find_packages, setup

HERE = Path(__file__).resolve().parent
ROOT_README = (HERE / "README.md").read_text(encoding="utf-8")

setup(
    name="iqapi",
    version="1.0.0",
    description="Modular IQ Option trading API + live bot (pip-installable)",
    long_description=ROOT_README,
    long_description_content_type="text/markdown",
    author="IQ Option",
    url="https://github.com/mehedi42x/iqapi",
    license="MIT",
    python_requires=">=3.9",
    packages=find_packages(include=["iqoptionapi", "iqoptionapi.*", "bot", "bot.*"]),
    include_package_data=True,
    package_data={
        "bot": [".env.example", "README.md"],
        "iqoptionapi": ["README.md"],
    },
    install_requires=[
        "websocket-client>=1.6.0",
        "requests>=2.28.0",
        "curl_cffi>=0.7.0",
        # The layered websocket engine behind iqoptionapi/ and bot/.  It is
        # not published to PyPI — install it from your own source/index first,
        # e.g.  pip install -e /path/to/iq_option_api
        "iq_option_api",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0",
            "wheel",
        ],
    },
    entry_points={
        "console_scripts": [
            # `bot` -> the live trader (bot.bot).  Preserves the CLI exit code.
            "bot = bot.bot:console_main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Operating System :: OS Independent",
        "Topic :: Office/Business :: Financial :: Investment",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
