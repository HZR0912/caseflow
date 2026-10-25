"""Versioned compensation registry. The supplier owns before-images and CAS checks."""

import hashlib

UNDO_ACTIONS = {
    "identity.create_user": "identity.disable_user",
    "identity.create_mailbox": "identity.disable_mailbox",
    "code.invite_member": "code.remove_member",
    "money.create_order": "money.refund",
    "money.bind_seat": "money.unbind_seat",
    "ticket.assign_tickets": "ticket.restore_assignments",
}


def compensation_key(source_key):
    return "undo:" + hashlib.sha256(source_key.encode()).hexdigest()
