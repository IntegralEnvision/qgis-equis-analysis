"""Kept so `python pipeline/set_reporting_units.py` still works: scheduled tasks call
this path. The code is parquet_source/set_reporting_units.py; run it as
`python -m parquet_source.set_reporting_units`. Remove once nothing calls the path.
"""
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
runpy.run_module("parquet_source.set_reporting_units", run_name="__main__", alter_sys=True)
