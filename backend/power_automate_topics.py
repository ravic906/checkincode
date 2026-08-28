"""
Topic taxonomy for the Power Automate Developer mock-interview role.

Same "new taxonomy file beside the existing one" precedent case_topics.py
set relative to topics.py, and role_topics.py's own docstring calls out
explicitly. Power Automate is a no-code/low-code tool -- there's no SQL
query-writing component to this role, so every topic here is conceptual
(open spoken discussion, no table_context/canonical query), unlike the
SQL-heavy roles in role_topics.ROLE_TOPIC_MIX.

Chapters mapped from a Power Automate reference book's table of contents
(Preface and "History of Automation" folded into Fundamentals -- neither
is real interview-question material on its own).
"""

POWER_AUTOMATE_TOPICS = [
    "Power Automate Fundamentals",
    "Notifications & Alerts",
    "SharePoint Integration",
    "Working with Files",
    "Microsoft Forms Integration",
    "Microsoft Teams Integration",
    "Approval Workflows",
    "Instant & Manual Flows",
    "Dataverse & Data Modeling",
    "Planner Integration",
    "AI Builder & Intelligent Automation",
]
