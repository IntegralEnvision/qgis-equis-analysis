"""Kept so `python pipeline/convert_existing.py` still works: scheduled tasks call
this path. The code is parquet_source/convert_existing.py; run it as
`python -m parquet_source.convert_existing`. Remove once nothing calls the path.
"""
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
runpy.run_module("parquet_source.convert_existing", run_name="__main__", alter_sys=True)
