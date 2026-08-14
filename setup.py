#!/usr/bin/env python3
"""iqapi — Modular IQ Option trading API + bot.

This is the pip-installable distribution.  It ships three importable
packages:

* ``iq_option_api`` — the bundled layered websocket/trading engine
                      (``from iq_option_api import IQOptionClient``).
* ``iqoptionapi``   — the thin, bot-facing facade over that engine
                      (``from iqoptionapi import IQAPI``).
* ``bot``           — the modular live trader / backtester
                      (``bot`` command, ``python -m bot``).
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
    packages=find_packages(
        include=[
            "iq_option_api",
            "iq_option_api.*",
            "iqoptionapi",
            "iqoptionapi.*",
            "bot",
            "bot.*",
        ]
    ),
    include_package_data=True,
    package_data={
        "bot": [".env.example", "README.md"],
        "iqoptionapi": ["README.md"],
    },
    install_requires=[
        "websocket-client>=1.6.0",
        "requests>=2.28.0",
        "curl_cffi>=0.7.0",
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
        "Programming Language :: Python :: 3.14",
        "Operating System :: OS Independent",
        "Topic :: Office/Business :: Financial :: Investment",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
