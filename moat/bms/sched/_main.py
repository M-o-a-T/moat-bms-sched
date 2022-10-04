#!/usr/bin/env python3
"""
Basic tool support

"""
from getopt import getopt

import asyncclick as click

import logging  # pylint: disable=wrong-import-position

log = logging.getLogger()


@click.group()
async def main():
    """Battery Manager: Scheduling"""
    pass  # pylint: disable=unnecessary-pass

@main.command()
def analyze():
    print("YES")