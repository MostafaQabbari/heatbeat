"""HTTP routes, version 1.

What this package does: one module per area (auth, plants, alarms, commands,
insights, recommendations). Routes check the request, call a repository, and
return typed responses. No SQL here.
"""
