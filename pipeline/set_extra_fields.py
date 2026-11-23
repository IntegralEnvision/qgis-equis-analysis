"""Kept so `python pipeline/set_extra_fields.py` still works: scheduled tasks call
this path. The code is parquet_source/set_extra_fields.py; run it as
`python -m parquet_source.set_extra_fields`. Remove once nothing calls the path.
"""
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
runpy.run_module("parquet_source.set_extra_fields", run_name="__main__", alter_sys=True)
