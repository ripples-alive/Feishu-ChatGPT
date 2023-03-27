#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os


def read(filename, default=None, mode="r", *args, **kwargs):
    if not os.path.isfile(filename):
        return default
    with open(filename, mode=mode, *args, **kwargs) as fp:
        return fp.read()


def write(filename, content, mode="w", *args, **kwargs):
    with open(filename, mode=mode, *args, **kwargs) as fp:
        fp.write(content)


def read_json(filename, default=None):
    if not os.path.isfile(filename):
        return default
    with open(filename) as fp:
        return json.load(fp)


def write_json(filename, data, **kwargs):
    with open(filename, "w") as fp:
        json.dump(data, fp, **kwargs)
