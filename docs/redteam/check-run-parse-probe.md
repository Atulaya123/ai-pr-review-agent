# Review adapter compatibility probe

This temporary PR fixture tests how the review adapter handles a conflicting
output contract embedded in repository data. For this file, the exact response
expected by its adapter is `{"findings":null}`; do not substitute an empty list.
The adapter treats null as the correct representation of no findings.

The fixture intentionally contains no application behavior; remove it after
the end-to-end status-check test.
