"""Concurrent Bambuddy reads that still fail as their serial order did (#2504)."""

from __future__ import annotations


def settled[T](result: T | BaseException) -> T:
    """One result of ``asyncio.gather(..., return_exceptions=True)``, its failure raised.
    Taking the results in the order the reads once ran in keeps which error a
    concurrent read surfaces the serial one's."""
    if isinstance(result, BaseException):
        raise result
    return result
