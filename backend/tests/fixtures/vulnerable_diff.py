from backend.models.review import DiffFile

VULNERABLE_SQL_PATCH = '''diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -10,6 +10,9 @@ def get_user(request):
     user_id = request.args.get("id")
-    return db.query("SELECT * FROM users")
+    query = f"SELECT * FROM users WHERE id = {user_id}"
+    result = db.execute(query)
+    return result
'''

VULNERABLE_DIFF_FILES = [DiffFile(path="app.py", patch=VULNERABLE_SQL_PATCH)]

CLEAN_PATCH = '''diff --git a/utils.py b/utils.py
index 3333333..4444444 100644
--- a/utils.py
+++ b/utils.py
@@ -1,3 +1,6 @@
 def add(a, b):
     return a + b
+
+def subtract(a, b):
+    return a - b
'''

CLEAN_DIFF_FILES = [DiffFile(path="utils.py", patch=CLEAN_PATCH)]
