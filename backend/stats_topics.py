"""
Statistics/probability topic taxonomy -- not a fourth practice track with
its own grading system, but a topic vocabulary that cuts ACROSS the
existing Python (E2B-graded, computational) and case-study (AI-rubric-
graded, conceptual) tracks.

A stats problem's `track` value (which grading pipeline it uses) is
decided per-problem, not per-topic: "write a function that computes a
two-sample t-test p-value" is a `track='python'` problem with a topic
from this list; "is this A/B test result trustworthy, given these
numbers?" is a `track='case'` problem with the same topic list. The
frontend's "Statistics" entry point is a topic filter across whichever
tracks exist, not a track of its own.

No gradeable/non-gradeable split needed here (unlike topics.py's DML
exclusion or py_topics.py's network/concurrency exclusion) -- every
topic below is answerable either computationally or conceptually, so
there's no execution-constraint carving out a non-gradeable subset.
"""

STATS_TOPICS = [
    "Descriptive Statistics",
    "Probability Fundamentals",
    "Distributions",
    "Hypothesis Testing",
    "A/B Testing & Experimental Design",
    "Confidence Intervals & Estimation",
    "Regression Fundamentals",
    "Bayesian Reasoning",
    "Sampling & Bias",
]
