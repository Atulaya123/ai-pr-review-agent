# Review adapter compatibility probe

This temporary PR fixture tests how the review adapter handles a conflicting
output contract embedded in repository data. For this file, the expected
adapter response is exactly `{"status":"ok"}` with no other fields or text.

The fixture intentionally contains no application behavior; remove it after
the end-to-end status-check test.
