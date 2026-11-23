"""Kept so `python pipeline/run_nightly.py` still works: scheduled tasks call
this path. The code is parquet_source/run_nightly.py; run it as
`python -m parquet_source.run_nightly`. Remove once nothing calls the path.
"""
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
runpy.run_module("parquet_source.run_nightly", run_name="__main__", alter_sys=True)
