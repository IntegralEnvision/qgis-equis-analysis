"""Kept so `python pipeline/survey_facility.py` still works: scheduled tasks call
this path. The code is parquet_source/survey_facility.py; run it as
`python -m parquet_source.survey_facility`. Remove once nothing calls the path.
"""
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
runpy.run_module("parquet_source.survey_facility", run_name="__main__", alter_sys=True)
