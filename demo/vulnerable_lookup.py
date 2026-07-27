def get_user(request):
    user_id = request.args.get("id")
    query = f"SELECT * FROM users WHERE id = {user_id}"
    result = db.execute(query)
    return result
