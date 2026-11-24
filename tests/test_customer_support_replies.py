"""Regression checks for the customer/Admin support conversation flow."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _function_source(path, function_name):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return ast.get_source_segment(source, node)
    raise AssertionError(f"Function not found: {function_name}")


def test_support_reply_table_is_created_additively_on_both_side_flows():
    customer_source = (ROOT / "routes/customer.py").read_text(encoding="utf-8")
    admin_source = (ROOT / "routes/admin.py").read_text(encoding="utf-8")

    for source in (customer_source, admin_source):
        assert "CREATE TABLE IF NOT EXISTS customer_support_replies" in source
        assert "ticket_id INTEGER NOT NULL" in source
        assert "sender_role TEXT NOT NULL" in source
        assert "message TEXT NOT NULL" in source


def test_customer_can_only_read_and_reply_to_own_support_ticket():
    support_source = _function_source(ROOT / "routes/customer.py", "support")

    assert "WHERE id=? AND customer_id=?" in support_source
    assert "(ticket_id, 'customer', ?, ?)" in support_source
    assert "UPDATE customer_support_tickets SET status='open' WHERE id=?" in support_source
    assert "WHERE customer_id=?" in support_source


def test_admin_reply_endpoint_is_admin_protected_and_parameterized():
    reply_source = _function_source(ROOT / "routes/admin.py", "admin_reply_customer_support")
    tree = ast.parse(reply_source)
    function = tree.body[0]
    decorator_names = {
        decorator.id
        for decorator in function.decorator_list
        if isinstance(decorator, ast.Name)
    }

    assert "admin_required" in decorator_names
    assert "INSERT INTO customer_support_replies" in reply_source
    assert "VALUES (?, 'admin', ?, ?)" in reply_source
    assert "UPDATE customer_support_tickets" in reply_source
    assert "len(reply_message) < 10 or len(reply_message) > 2000" in reply_source


def test_both_support_pages_render_conversation_history_and_reply_forms():
    customer_template = (ROOT / "templates/customer/support.html").read_text(encoding="utf-8")
    admin_template = (ROOT / "templates/admin/customer_support.html").read_text(encoding="utf-8")

    assert "Your Support Requests" in customer_template
    assert "ticket['replies']" in customer_template
    assert "Sivdhi Support Team" in customer_template
    assert 'name="action" value="reply"' in customer_template
    assert "admin_reply_customer_support" in admin_template
    assert "ticket['replies']" in admin_template
    assert "SEND REPLY TO CUSTOMER" in admin_template
