"""Roster sync: pull the guild's member list from a Google Sheet and
apply leave/join/rename changes to BOTH local workbooks
(guild_scores.xlsx + league_scores.xlsx), each change human-confirmed.

Modules:
  google_sheets — OAuth (installed-app, loopback+PKCE) + Sheets v4 reads
  sheet_parse   — raw grid → {編號: SheetMember} with scan-stop rules
  diff          — sheet vs the two workbooks → reviewable SyncPlan
  apply         — in-place openpyxl writes (backup first)
"""
