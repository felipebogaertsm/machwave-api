#!/usr/bin/env python
"""Set a Firebase user's RBAC role via the ``role`` custom claim.

Usage:
    uv run python scripts/set_user_role.py <email> <role>

Roles:
    admin   — full access, including /simulations/rerun-all
    member  — default; clears the ``role`` claim entirely

Environment:
    FIREBASE_PROJECT_ID — required (the Firebase project hosting the user).
    GOOGLE_APPLICATION_CREDENTIALS — path to a service account key with the
        ``Firebase Authentication Admin`` role. The repo's ``sa-key.json``
        works for the dev project.

Note: Custom claims only refresh on the client when a new ID token is issued
(sign out + sign in, or ``getIdToken(true)``). Existing tokens keep the old
claims until they expire (≤1 hour).
"""

from __future__ import annotations

import argparse
import os
import sys

import firebase_admin
from firebase_admin import auth

VALID_ROLES = ("admin", "member")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="Email address of the target user")
    parser.add_argument("role", choices=VALID_ROLES, help="Role to assign")
    args = parser.parse_args()

    project_id = os.environ.get("FIREBASE_PROJECT_ID")
    if not project_id:
        print("FIREBASE_PROJECT_ID is required.", file=sys.stderr)
        return 1

    firebase_admin.initialize_app(options={"projectId": project_id})

    try:
        user = auth.get_user_by_email(args.email)
    except auth.UserNotFoundError:
        print(
            f"No Firebase user with email {args.email!r} in project {project_id}.",
            file=sys.stderr,
        )
        return 1

    existing = dict(user.custom_claims or {})
    if args.role == "member":
        existing.pop("role", None)
    else:
        existing["role"] = args.role

    auth.set_custom_user_claims(user.uid, existing or None)

    print(f"role={args.role} set for uid={user.uid} email={args.email} project={project_id}")
    print("User must sign out and back in (or call getIdToken(true)) to pick up the change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
