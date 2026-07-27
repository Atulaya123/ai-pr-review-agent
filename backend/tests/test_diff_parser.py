from backend.integrations.diff_parser import get_valid_line_range, parse_unified_diff

NEW_FILE_PATCH = """diff --git a/demo/vulnerable_lookup.py b/demo/vulnerable_lookup.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/demo/vulnerable_lookup.py
@@ -0,0 +1,5 @@
+def get_user(request):
+    user_id = request.args.get("id")
+    query = f"SELECT * FROM users WHERE id = {user_id}"
+    result = db.execute(query)
+    return result
"""


def test_valid_line_range_matches_new_file_length():
    assert get_valid_line_range(NEW_FILE_PATCH) == (1, 5)


def test_valid_line_range_none_for_no_hunks():
    assert get_valid_line_range("not a real diff") is None


def test_parse_unified_diff_extracts_the_file():
    files = parse_unified_diff(NEW_FILE_PATCH)
    assert len(files) == 1
    assert files[0].path == "demo/vulnerable_lookup.py"
