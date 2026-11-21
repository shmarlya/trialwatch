"""Who may do what in the web interface (ADR-006).

- actions.py: the chart of actions and the roles allowed each one, the single source of truth
- passwords.py: hashing and checking passwords (argon2id)
- sessions.py: signing in and out, and finding the user of a session cookie

Anyone not signed in may only browse. The SQL is in trialwatch.db.auth.
"""
