"""
Topic vocabulary for data-analysis-library-specific Python problems --
pandas and numpy -- kept separate from py_topics.py's general Python
Cookbook taxonomy since these are a distinct, data-analyst-specific
skill area (DataFrame/array manipulation) rather than general-purpose
Python language features.

Same mechanism as stats_topics.py: not a track of its own, just a topic
vocabulary accepted for track='python' drafts alongside
py_topics.PY_GRADEABLE_TOPICS, graded by the exact same E2B execution
pipeline. The sandbox (pysandbox.py, via e2b_code_interpreter) has both
libraries pre-installed by default.
"""

DATA_LIBRARY_TOPICS = [
    "Pandas DataFrames",
    "NumPy Arrays",
]
