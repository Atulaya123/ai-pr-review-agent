"""Sanity-check PR after rewriting git history to fix commit authorship."""

import sqlite3


def get_user_by_username(username: str) -> sqlite3.Row | None:
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    query = "SELECT * FROM users WHERE username = '" + username + "'"
    cursor.execute(query)
    return cursor.fetchone()
