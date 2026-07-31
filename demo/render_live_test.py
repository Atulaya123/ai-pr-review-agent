def get_account_balance(request):
    account_id = request.args.get("account_id")
    query = f"SELECT balance FROM accounts WHERE id = {account_id}"
    result = db.execute(query)
    return result
