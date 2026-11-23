"""Kept so `python pipeline/create_slice_reports.py` still works: scheduled tasks call
this path. The code is parquet_source/create_slice_reports.py; run it as
`python -m parquet_source.create_slice_reports`. Remove once nothing calls the path.
"""
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
runpy.run_module("parquet_source.create_slice_reports", run_name="__main__", alter_sys=True)
