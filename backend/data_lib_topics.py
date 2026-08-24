"""
Topic vocabulary for data-analysis-library-specific Python problems --
pandas and numpy -- kept separate from py_topics.py's general Python
Cookbook taxonomy since these are a distinct, data-analyst-specific
skill area (DataFrame/array manipulation) rather than general-purpose
Python language features.

Modeled on the relevant chapters of "Python for Data Analysis" (McKinney)
-- the standard reference for both libraries together -- same way
py_topics.py is modeled on the Python Cookbook. Only the gradeable,
computational chapters are represented (plotting/visualization and
modeling-library chapters are out of scope -- not assert-testable).

Same mechanism as stats_topics.py: not a track of its own, just a topic
vocabulary accepted for track='python' drafts alongside
py_topics.PY_GRADEABLE_TOPICS, graded by the exact same E2B execution
pipeline. The sandbox (pysandbox.py, via e2b_code_interpreter) has both
libraries pre-installed by default.
"""

DATA_LIBRARY_TOPICS = [
    "NumPy",
    "Pandas",
]
