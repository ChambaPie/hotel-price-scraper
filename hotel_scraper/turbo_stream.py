"""Remix / React Router "turbo-stream" data (the `.data` answers some sites' own pages load, e.g. CHECK24, 2026-11-23).

The answer is one JSON array: element 0 is the root; an object is {"_<i>": <j>} where element i is the key and element j
the value; a list holds element numbers; a negative number is a constant (undefined, null, NaN...). Shared values are
written once and referred to by number.
"""
from __future__ import annotations

import json

CONSTANTS = {-1: None, -2: None, -3: float("nan"), -4: float("inf"), -5: float("-inf"), -6: -0.0, -7: None}


def decode(text_or_list) -> object:
    arr = json.loads(text_or_list) if isinstance(text_or_list, (str, bytes)) else text_or_list
    cache: dict[int, object] = {}

    def val(i):
        if isinstance(i, int) and i < 0:
            return CONSTANTS.get(i)
        if i in cache:
            return cache[i]
        x = arr[i]
        if isinstance(x, dict):
            out: dict = {}
            cache[i] = out
            for k, v in x.items():
                out[arr[int(k[1:])] if k.startswith("_") and k[1:].isdigit() else k] = val(v)
            return out
        if isinstance(x, list):
            lst: list = []
            cache[i] = lst
            lst.extend(val(j) for j in x)
            return lst
        cache[i] = x
        return x

    return val(0) if arr else None
