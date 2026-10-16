def get_user_by_name(name):
    # deliberately unparameterized — should trip the untrusted_content_boundary
    # invariant; this PR re-verifies the pipeline after adding Redis-backed
    # checkpointing, hybrid (vector + full-text) retrieval, and the HITL
    # escalation queue, none of which should change this outcome
    query = "SELECT * FROM users WHERE name = '" + name + "'"
    return db.execute(query)
