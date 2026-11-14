import secrets
import json
import re
from datetime import timedelta
from urllib.parse import quote, unquote, urlencode, urlsplit

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from bson import ObjectId
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from joserfc.errors import JoseError
from pymongo.errors import DuplicateKeyError
import requests

from .core import (
    ROLES, Problem, atomic, cents_field, db, email_field, lock_accounts,
    now, object_id, requires, text_field,
)
from .onboarding import enqueue_onboarding

bp = Blueprint("accounts", __name__)


def username_field(form):
    username = text_field(form, "username", "a student username", limit=15, required=True).lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,14}", username):
        raise Problem("Use a student username of 3–15 letters, numbers, dots, underscores or hyphens, starting with a letter or number.")
    return username


def configure_oidc(app):
    oauth = OAuth(app)
    app.extensions["auth0"] = oauth.register(
        "auth0",
        client_id=app.config["AUTH0_CLIENT_ID"],
        client_secret=app.config["AUTH0_CLIENT_SECRET"],
        server_metadata_url=app.config["AUTH0_ORIGIN"] + "/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256", "default_timeout": 10},
    )


def login_destination(value):
    """Only return local paths, including after browser URL normalization."""
    if not isinstance(value, str):
        return url_for("home")
    try:
        parts = urlsplit(value)
        path = unquote(parts.path)
    except ValueError:
        return url_for("home")
    if (parts.scheme or parts.netloc or not path.startswith("/") or path.startswith("//")
            or "\\" in path or any(ord(character) < 32 or ord(character) == 127 for character in unquote(value))):
        return url_for("home")
    return value


@bp.get("/login")
def login():
    destination = login_destination(request.args.get("next"))
    if g.user:
        return redirect(destination)
    session["login_next"] = destination
    try:
        return current_app.extensions["auth0"].authorize_redirect(
            current_app.config["APP_BASE_URL"] + url_for("accounts.callback"),
            prompt="login", connection=current_app.config["AUTH0_CONNECTION"],
        )
    except (OAuthError, requests.RequestException, ValueError):
        raise Problem("Auth0 sign-in is temporarily unavailable.", 502) from None


@bp.get("/auth/callback")
def callback():
    config = current_app.config
    try:
        token = current_app.extensions["auth0"].authorize_access_token(
            leeway=30, claims_options={"iss": {"value": config["AUTH0_ORIGIN"] + "/"}},
        )
    except (OAuthError, JoseError, requests.RequestException, ValueError):
        raise Problem("Sign-in could not be verified. Start again from the sign-in page.", 401) from None

    identity = token.get("userinfo")
    audience = identity.get("aud") if identity else None
    client_id = config["AUTH0_CLIENT_ID"]
    if audience != client_id and not (isinstance(audience, list) and client_id in audience):
        raise Problem("Sign-in could not be verified. Start again from the sign-in page.", 401)
    subject = identity.get("sub") if identity else None
    account = db().users.find_one({"auth0_sub": subject, "active": True}) if subject else None
    if not account:
        raise Problem("Ask your administrator to set up an account for this sign-in.", 403)
    destination = login_destination(session.get("login_next"))
    session.clear()
    session["user_id"] = str(account["_id"])
    session["auth_expires_at"] = int((now() + current_app.permanent_session_lifetime).timestamp())
    session.permanent = True
    current_app.session_interface.regenerate(session)
    return redirect(destination)


@bp.post("/logout")
def logout():
    session.clear()
    config = current_app.config
    query = urlencode({
        "client_id": config["AUTH0_CLIENT_ID"],
        "post_logout_redirect_uri": config["APP_BASE_URL"] + "/",
    })
    return redirect(f"{config['AUTH0_ORIGIN']}/oidc/logout?{query}")


class ManagementError(Problem):
    def __init__(self, provider_status=None):
        super().__init__("Auth0 account management failed. Check the tenant settings and retry.", 502)
        self.provider_status = provider_status


class Auth0Management:
    def __init__(self):
        self.config = current_app.config
        self.origin = self.config["AUTH0_ORIGIN"]
        self.connection = self.config["AUTH0_CONNECTION"]
        self.token = None

    def call(self, method, path, **kwargs):
        headers = {"Authorization": f"Bearer {self.token}"} if path.startswith("/api/v2/") else {}
        try:
            response = requests.request(
                method, self.origin + path, headers=headers, timeout=(4, 10),
                allow_redirects=False, **kwargs,
            )
        except requests.RequestException:
            raise ManagementError() from None
        if not 200 <= response.status_code < 300:
            current_app.logger.warning("Auth0 management request %s returned HTTP %s", path, response.status_code)
            raise ManagementError(response.status_code)
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise ManagementError() from None

    def authorize(self):
        if self.token:
            return
        if not self.config["AUTH0_MANAGEMENT_CLIENT_ID"] or not self.config["AUTH0_MANAGEMENT_CLIENT_SECRET"]:
            raise Problem("Configure the Auth0 Management API client before provisioning accounts.", 503)
        result = self.call("POST", "/oauth/token", json={
            "grant_type": "client_credentials",
            "client_id": self.config["AUTH0_MANAGEMENT_CLIENT_ID"],
            "client_secret": self.config["AUTH0_MANAGEMENT_CLIENT_SECRET"],
            "audience": self.config["AUTH0_MANAGEMENT_AUDIENCE"],
        })
        self.token = result.get("access_token")
        if not self.token:
            raise ManagementError()

    def owned_subject(self, account):
        if account.get("username"):
            candidates = self.call("GET", "/api/v2/users", params={
                "q": "username:" + json.dumps(account["username"]) +
                     " AND identities.connection:" + json.dumps(self.connection),
                "search_engine": "v3", "per_page": 100,
            })
        else:
            candidates = self.call("GET", "/api/v2/users-by-email", params={"email": account["email"]})
        for candidate in candidates:
            if not any(identity.get("connection") == self.connection for identity in candidate.get("identities", [])):
                continue
            if account.get("username") and candidate.get("username", "").lower() != account["username"]:
                continue
            marker = (candidate.get("app_metadata") or {}).get("lesson_desk_account")
            if marker == str(account["_id"]) and candidate.get("user_id"):
                return candidate["user_id"]
            raise Problem("This sign-in identifier already belongs to an Auth0 account outside this portal.", 409)
        return None

    def create_subject(self, account, password):
        self.authorize()
        subject = self.owned_subject(account)
        if subject:
            return subject
        try:
            payload = {
                "connection": self.connection,
                "name": account["name"],
                "password": password,
                "app_metadata": {"lesson_desk_account": str(account["_id"])},
            }
            if account.get("email"):
                payload.update(email=account["email"], verify_email=False)
            if account.get("username"):
                payload["username"] = account["username"]
            result = self.call("POST", "/api/v2/users", json=payload)
        except ManagementError as error:
            if error.provider_status == 409:
                subject = self.owned_subject(account)
                if subject:
                    return subject
            raise
        if not result.get("user_id"):
            raise ManagementError()
        return result["user_id"]

    def set_username(self, subject, username):
        self.authorize()
        # Subjects come from Auth0; quote them before using them in a URL path.
        self.call("PATCH", "/api/v2/users/" + quote(subject, safe=""), json={
            "username": username, "connection": self.connection,
        })

    def delete_subject(self, account):
        self.authorize()
        subject = account.get("auth0_sub")
        if not subject:
            # A failed provisioning request may have created an identity before
            # its subject was saved locally. Only remove identities we own.
            try:
                subject = self.owned_subject(account)
            except Problem as error:
                if error.status == 409:
                    return
                raise
        if subject:
            try:
                self.call("DELETE", "/api/v2/users/" + quote(subject, safe=""))
            except ManagementError as error:
                if error.provider_status != 404:
                    raise

    def existing_user(self, email, subject=None):
        self.authorize()
        candidates = self.call("GET", "/api/v2/users-by-email", params={"email": email})
        candidates = [candidate for candidate in candidates if (
            candidate.get("email", "").lower() == email
            and candidate.get("user_id")
            and any(identity.get("connection") == self.connection for identity in candidate.get("identities", []))
            and (not subject or candidate["user_id"] == subject)
        )]
        if not candidates:
            raise Problem("No matching Auth0 user was found in the configured database connection. Check the email, user ID, and AUTH0_CONNECTION.", 404)
        if len(candidates) != 1:
            raise Problem("More than one Auth0 user matches this email. Enter the intended Auth0 user ID.", 409)
        if candidates[0].get("blocked"):
            raise Problem("This Auth0 user is blocked. Unblock the user in Auth0 before granting portal access.", 409)
        return candidates[0]

    def setup_link(self, subject):
        self.authorize()
        result = self.call("POST", "/api/v2/tickets/password-change", json={
            "user_id": subject,
            "client_id": self.config["AUTH0_CLIENT_ID"],
            "ttl_sec": int(timedelta(days=1).total_seconds()),
        })
        ticket = result.get("ticket", "")
        if not ticket.startswith("https://"):
            raise ManagementError()
        return ticket


def delete_account(account_id, *, management, actor_id=None):
    """Revoke access, remove Auth0 identities, then release local identifiers."""
    database = db()

    def begin(transaction):
        if actor_id is not None:
            lock_accounts(transaction, {actor_id: "admin"})
        account = database.users.find_one({"_id": account_id}, session=transaction)
        if not account:
            return []
        records = [account, *database.users.find({
            "provisioning_parent_id": account_id, "active": False,
        }, session=transaction)]
        for record in records:
            fields = {"active": False, "deletion_pending": True,
                      "deleted_at": record.get("deleted_at") or now()}
            if actor_id is not None:
                fields["deleted_by"] = actor_id
            database.users.update_one({"_id": record["_id"]}, {"$set": fields}, session=transaction)
        return records

    records = atomic(begin)
    if not records:
        return False
    try:
        for record in records:
            management.delete_subject(record)
    except Problem:
        # Keep the identifiers reserved until Auth0 confirms deletion. The
        # inactive row stays in the directory so an administrator can retry.
        raise Problem("Account deletion is pending because Auth0 could not remove the sign-in account. Check the Auth0 Management API settings and delete:users permission, then retry Delete account.", 502) from None

    def finish(transaction):
        if actor_id is not None:
            lock_accounts(transaction, {actor_id: "admin"})
        for record in records:
            account = database.users.find_one({"_id": record["_id"], "deletion_pending": True}, session=transaction)
            if not account:
                continue
            account_id = account["_id"]
            timestamp = account["deleted_at"]
            # Keep names for historical records without retaining a login,
            # contact details, a billing profile, or an account reservation.
            history = {key: account[key] for key in ("name", "role", "deleted_at", "deleted_by") if key in account}
            database.account_history.update_one({"_id": account_id}, {"$setOnInsert": history}, upsert=True, session=transaction)
            if account.get("provisioning_parent_id"):
                database.users.update_one({"_id": account["provisioning_parent_id"], "active": False}, {
                    "$pull": {"provisioning_children": account["username"]},
                }, session=transaction)
            child_ids = database.users.distinct("_id", {"parent_id": account_id}, session=transaction)
            database.users.update_many({"parent_id": account_id}, {
                "$unset": {"parent_id": "", "availability_parent_id": ""},
            }, session=transaction)
            database.enrollments.delete_many({"$or": [{"student_id": account_id}, {"tutor_id": account_id}]}, session=transaction)
            database.lesson_series.update_many({"active": True, "$or": [
                {"student_id": {"$in": [account_id, *child_ids]}}, {"tutor_id": account_id},
            ]}, {"$set": {"active": False, "stopped_at": timestamp, "stopped_by": actor_id}}, session=transaction)
            database.change_requests.update_many({"status": "pending", "$or": [
                {"student_id": account_id}, {"tutor_id": account_id}, {"parent_id": account_id},
            ]}, {"$set": {"status": "withdrawn", "resolved_at": timestamp}}, session=transaction)
            for collection in (database.weekly_availability, database.availability_exceptions):
                collection.delete_many({"$or": [
                    {"student_id": account_id}, {"tutor_id": account_id}, {"parent_id": account_id},
                ]}, session=transaction)
            for collection, query in (
                (database.onboarding_deliveries, {"$or": [{"parent_id": account_id}, {"student_id": account_id}]}),
                (database.lesson_reminder_deliveries, {"user_id": account_id}),
            ):
                collection.update_many({**query, "state": {"$in": ["queued", "sending", "failed"]}}, {
                    "$set": {"state": "skipped", "reason": "Account deleted."},
                    "$unset": {"lease": "", "lease_until": "", "next_at": ""},
                }, session=transaction)
            database.users.delete_one({"_id": account_id}, session=transaction)

    atomic(finish)
    return True


def remove_deleted_reservations(*, management, email=None, usernames=(), subject=None, actor_id=None):
    """Finish deletions from older versions when their identifiers are reused."""
    identifiers = ([{"email": email}] if email else []) + [{"username": username} for username in usernames]
    if subject:
        identifiers.append({"auth0_sub": subject})
    if identifiers:
        for account in db().users.find({"deleted_at": {"$exists": True}, "$or": identifiers}, {"_id": 1}):
            delete_account(account["_id"], management=management, actor_id=actor_id)


def provision(name, email, role, *, management, username=None, password=None, reuse=False, actor_id=None):
    remove_deleted_reservations(management=management, email=email,
                                usernames=[username] if username else [], actor_id=actor_id)
    users = db().users
    record = {
        "_id": ObjectId(), "name": name, "role": role,
        "created_at": now(), "active": False,
    }
    if email:
        record["email"] = email
    if username:
        record["username"] = username
    try:
        users.insert_one(record)
    except DuplicateKeyError:
        record = users.find_one({"username": username} if username else {"email": email})
        if not record or record.get("deleted_at") or record["role"] != role or (email and record.get("email") != email) or (record["name"] != name and not record["active"]):
            raise Problem("This email or username already has an account with different details.", 409) from None
        if "provisioning_children" in record or record.get("provisioning_parent_id"):
            raise Problem("Retry this pending family account with the same parent and child details.", 409) from None
        if record["active"]:
            if reuse:
                return record
            raise Problem("This email or username already has an account. Open the accounts list to create a setup link.", 409) from None

    # Reserve the Mongo account first. The Auth0 marker allows a safe retry after a partial failure.
    subject = management.create_subject(record, password or f"Aa7!{secrets.token_urlsafe(36)}")
    def activate(transaction):
        if actor_id is not None:
            lock_accounts(transaction, {actor_id: "admin"})
        result = users.update_one(
            {"_id": record["_id"], "active": False, "deleted_at": {"$exists": False},
             "provisioning_children": {"$exists": False}, "provisioning_parent_id": {"$exists": False}},
            {"$set": {"auth0_sub": subject, "active": True}}, session=transaction,
        )
        if not result.matched_count and not users.find_one({"_id": record["_id"], "active": True}, session=transaction):
            raise Problem("This account changed during setup. Reload the account directory.", 409)
        if result.matched_count and role == "parent":
            enqueue_onboarding(record["_id"], transaction=transaction)

    try:
        atomic(activate)
    except Problem:
        if not users.find_one({"_id": record["_id"], "deleted_at": {"$exists": False}}, {"_id": 1}):
            management.delete_subject({**record, "auth0_sub": subject})
        raise
    return users.find_one({"_id": record["_id"]})


def family_children(form):
    names, usernames = form.getlist("child_name"), form.getlist("child_username")
    if len(names) != len(usernames) or len(names) > 20:
        raise Problem("Enter a name and username for each child, with up to 20 children per parent.")
    children = []
    for name, username in zip(names, usernames):
        if not name.strip() and not username.strip():
            continue
        children.append({
            "name": text_field({"name": name}, "name", "a child's name", limit=100, required=True),
            "username": username_field({"username": username}),
        })
    if len({child["username"] for child in children}) != len(children):
        raise Problem("Choose a different username for each child.")
    return children


def provision_family(name, email, children, *, management, actor_id):
    """Keep a family pending until every identity is ready, then queue one welcome."""
    users = db().users
    usernames = sorted(child["username"] for child in children)
    remove_deleted_reservations(management=management, email=email, usernames=usernames, actor_id=actor_id)

    def reserve(transaction):
        lock_accounts(transaction, {actor_id: "admin"})
        parent = users.find_one({"email": email}, session=transaction)
        if parent:
            if (parent.get("active") or parent.get("deleted_at") or parent["role"] != "parent"
                    or parent["name"] != name or parent.get("auth0_sub")
                    or not set(parent.get("provisioning_children", usernames)).issubset(usernames)):
                raise Problem("This parent email already has an account with different details. Retry pending family setup with the same children.", 409)
            users.update_one({"_id": parent["_id"]}, {"$set": {"provisioning_children": usernames}}, session=transaction)
        else:
            parent = {"_id": ObjectId(), "name": name, "email": email, "role": "parent",
                      "active": False, "created_at": now(), "provisioning_children": usernames}
            users.insert_one(parent, session=transaction)
        records = [parent]
        for child in children:
            record = users.find_one({"username": child["username"]}, session=transaction)
            if record:
                if (record.get("active") or record.get("deleted_at") or record["role"] != "student"
                        or record["name"] != child["name"] or record.get("auth0_sub")
                        or record.get("provisioning_parent_id") != parent["_id"]):
                    raise Problem(f"The username {child['username']} already belongs to another account. Choose a different child username.", 409)
            else:
                record = {"_id": ObjectId(), **child, "role": "student", "active": False,
                          "created_at": now(), "provisioning_parent_id": parent["_id"]}
                users.insert_one(record, session=transaction)
            records.append(record)
        return records

    try:
        records = atomic(reserve)
    except DuplicateKeyError:
        raise Problem("An email or username was just reserved by another account. Reload and retry family setup.", 409) from None
    # Auth0's account marker makes these calls safe to retry after partial failure.
    subjects = {}

    def cleanup_deleted_subjects():
        for candidate in records:
            subject = subjects.get(candidate["_id"])
            if subject and not users.find_one({"_id": candidate["_id"], "deleted_at": {"$exists": False}}, {"_id": 1}):
                management.delete_subject({**candidate, "auth0_sub": subject})

    for record in records:
        try:
            subjects[record["_id"]] = management.create_subject(record, f"Aa7!{secrets.token_urlsafe(36)}")
        except Problem as error:
            cleanup_deleted_subjects()
            if error.status == 409:
                # A confirmed foreign identity cannot be fixed by retrying its
                # identifier. Release only our unused local reservations.
                def release(transaction):
                    lock_accounts(transaction, {actor_id: "admin"})
                    parent_id = records[0]["_id"]
                    if not users.find_one({"_id": parent_id, "active": False, "deleted_at": {"$exists": False},
                                           "provisioning_children": usernames}, session=transaction):
                        return
                    candidates = records if record["role"] == "parent" else [record]
                    for candidate in candidates:
                        users.delete_one({"_id": candidate["_id"], "active": False,
                                          "deleted_at": {"$exists": False}}, session=transaction)
                    if record["role"] == "student":
                        users.update_one({"_id": parent_id}, {
                            "$pull": {"provisioning_children": record["username"]},
                        }, session=transaction)

                atomic(release)
            raise

    def activate(transaction):
        lock_accounts(transaction, {actor_id: "admin"})
        parent_id = records[0]["_id"]
        for record in records:
            fields = {"active": True, "auth0_sub": subjects[record["_id"]]}
            if record["role"] == "student":
                fields["parent_id"] = parent_id
            query = {"_id": record["_id"], "active": False, "deleted_at": {"$exists": False}}
            if record["role"] == "parent":
                query["provisioning_children"] = usernames
            else:
                query["provisioning_parent_id"] = parent_id
            result = users.update_one(query, {
                "$set": fields, "$unset": {"provisioning_children": "", "provisioning_parent_id": ""},
            }, session=transaction)
            if not result.matched_count:
                raise Problem("A family account changed during setup. Reload the account directory.", 409)
        enqueue_onboarding(parent_id, transaction=transaction)

    try:
        atomic(activate)
    except Problem:
        cleanup_deleted_subjects()
        raise
    return users.find_one({"_id": records[0]["_id"]})


def link_existing_user(name, email, role, *, management, subject=None, actor_id=None):
    """Grant a local role to an explicitly selected existing identity without changing Auth0."""
    if role not in ROLES:
        raise Problem("Choose an account role.")
    remove_deleted_reservations(management=management, email=email, subject=subject, actor_id=actor_id)
    identity = management.existing_user(email, subject)
    fields = {
        "name": name, "email": email, "role": role, "active": True,
        "auth0_sub": identity["user_id"], "linked_at": now(),
    }
    if role == "student" and identity.get("username"):
        fields["username"] = identity["username"].lower()

    def link(transaction):
        if actor_id is not None:
            lock_accounts(transaction, {actor_id: "admin"})
        existing = db().users.find_one({"$or": [
            {"email": email}, {"auth0_sub": identity["user_id"]},
        ]}, session=transaction)
        if existing:
            if existing.get("deleted_at"):
                raise Problem("This account is being deleted. Retry after deletion finishes.", 409)
            if "provisioning_children" in existing or existing.get("provisioning_parent_id"):
                raise Problem("Retry this pending family account with the same parent and child details.", 409)
            if existing.get("active") or existing.get("auth0_sub"):
                raise Problem("This user already has portal access. Manage their role in the account directory.", 409)
            if existing.get("username") and existing["username"] != fields.get("username"):
                raise Problem("The pending portal username does not match this Auth0 account.", 409)
            db().users.update_one({"_id": existing["_id"]}, {"$set": fields}, session=transaction)
            account_id = existing["_id"]
        else:
            account_id = db().users.insert_one({**fields, "created_at": fields["linked_at"]}, session=transaction).inserted_id
        if role == "parent":
            enqueue_onboarding(account_id, existing_account=True, transaction=transaction)
        return account_id

    try:
        account_id = atomic(link)
    except DuplicateKeyError:
        raise Problem("This Auth0 user, email, or username is already linked to a portal account.", 409) from None
    return db().users.find_one({"_id": account_id})


@bp.get("/admin/accounts")
@requires("admin")
def index():
    users = list(db().users.find({"$or": [{"deleted_at": {"$exists": False}}, {"deletion_pending": True}]},
                                {"name": 1, "email": 1, "username": 1, "username_sync_pending": 1,
                                 "role": 1, "active": 1, "deletion_pending": 1}).sort("name", 1))
    deliveries = list(db().onboarding_deliveries.find().sort("created_at", -1).limit(100))
    names = {user["_id"]: user["name"] for user in users}
    return render_template("accounts.html", users=users, roles=ROLES, onboarding_deliveries=deliveries, names=names)


@bp.post("/admin/accounts")
@requires("admin")
def create():
    role = request.form.get("role")
    if role not in ROLES:
        raise Problem("Choose an account role.")
    name = text_field(request.form, "name", "a name", limit=100, required=True)
    management = Auth0Management()
    username = username_field(request.form) if role == "student" else None
    email = email_field(request.form) if role != "student" or request.form.get("email", "").strip() else None
    children = family_children(request.form)
    if children and role != "parent":
        raise Problem("Choose the parent role to create accounts with children.")
    if children:
        try:
            user = provision_family(name, email, children, management=management, actor_id=g.user["_id"])
        except ManagementError:
            raise Problem("Family setup is pending because Auth0 could not finish creating the accounts. Check the Auth0 settings, then retry with the same parent and child details. The welcome email will be queued after setup finishes.", 502) from None
    else:
        user = provision(name, email, role, username=username, management=management, actor_id=g.user["_id"])
    if role == "parent":
        flash("The parent and child accounts are ready. The onboarding email is queued with each child's username and password setup link."
              if children else "The parent onboarding email is queued. Child sign-in details will be emailed when their accounts are linked.")
        return redirect(url_for("accounts.index"), code=303)
    return render_template("account_setup.html", account=user, ticket=management.setup_link(user["auth0_sub"])), 201


@bp.route("/admin/accounts/<account_id>/delete", methods=["GET", "POST"])
@requires("admin")
def delete(account_id):
    account_id = object_id(account_id)
    if account_id == g.user["_id"]:
        raise Problem("Another administrator must delete your account.", 409)
    if request.method == "GET":
        account = db().users.find_one({"_id": account_id})
        if not account:
            abort(404)
        return render_template("account_delete.html", account=account)

    if not delete_account(account_id, management=Auth0Management(), actor_id=g.user["_id"]):
        abort(404)
    flash("Account and sign-in deleted. The email and username can be used again. Lesson, homework and invoice history is preserved.")
    return redirect(url_for("accounts.index"), code=303)


@bp.post("/admin/accounts/link")
@requires("admin")
def link_existing():
    name = text_field(request.form, "name", "a name", limit=100, required=True)
    email = email_field(request.form)
    subject = text_field(request.form, "auth0_sub", "an Auth0 user ID", limit=256)
    user = link_existing_user(
        name, email, request.form.get("role"), management=Auth0Management(),
        subject=subject or None, actor_id=g.user["_id"],
    )
    flash(f"Portal access granted to {user['email']}. They can sign in with their existing Auth0 password."
          + (" The parent onboarding email is queued." if user["role"] == "parent" else ""))
    return redirect(url_for("accounts.index"), code=303)


@bp.post("/admin/onboarding/<delivery_id>/retry")
@requires("admin")
def retry_onboarding(delivery_id):
    result = db().onboarding_deliveries.update_one({"_id": delivery_id, "state": "failed"}, {
        "$set": {"state": "queued", "attempts": 0, "next_at": now(), "retried_by": g.user["_id"]},
        "$unset": {"reason": "", "lease": "", "lease_until": ""},
    })
    if not result.matched_count:
        raise Problem("Only stopped onboarding emails can be retried. Reload the account directory.", 409)
    flash("Onboarding email delivery resumed. Fresh password setup links will be generated when it is sent.")
    return redirect(url_for("accounts.index"), code=303)


@bp.post("/admin/accounts/<account_id>/setup")
@requires("admin")
def setup(account_id):
    account = db().users.find_one({"_id": object_id(account_id), "active": True})
    if not account:
        abort(404)
    if account.get("username_sync_pending"):
        raise Problem("Save the pending student username before creating a password setup link.", 409)
    ticket = Auth0Management().setup_link(account["auth0_sub"])
    return render_template("account_setup.html", account=account, ticket=ticket)


@bp.post("/admin/accounts/<account_id>/username")
@requires("admin")
def set_username(account_id):
    account_id = object_id(account_id)
    username = username_field(request.form)

    def reserve(transaction):
        lock_accounts(transaction, {g.user["_id"]: "admin", account_id: "student"})
        account = db().users.find_one({"_id": account_id}, session=transaction)
        if not account:
            abort(404)
        if account.get("username") == username and not account.get("username_sync_pending"):
            return account, False
        if account.get("username") and not account.get("username_sync_pending"):
            raise Problem("This student already has a username. Renaming existing usernames is not supported.", 409)
        if account.get("username_sync_pending") and account.get("username") != username:
            raise Problem("Save the pending username first before choosing another username.", 409)
        db().users.update_one({"_id": account_id}, {"$set": {
            "username": username, "username_sync_pending": True,
        }}, session=transaction)
        return account, True

    try:
        account, changed = atomic(reserve)
    except DuplicateKeyError:
        raise Problem("This username already belongs to another account.", 409) from None
    if changed:
        try:
            Auth0Management().set_username(account["auth0_sub"], username)
        except ManagementError as error:
            if error.provider_status == 409:
                # A definitive identifier conflict cannot be fixed by retrying it.
                # Release this reservation so the administrator can choose another.
                update = {"$unset": {"username_sync_pending": ""}}
                update["$unset"]["username"] = ""
                db().users.update_one({"_id": account_id, "username": username}, update)
                raise Problem("This username already belongs to an Auth0 account. Choose another username.", 409) from None
            raise Problem("The username is reserved, but Auth0 could not save it. Check Flexible Identifiers and the update:users permission, then save the same username again.", 502) from None
        except Problem:
            raise Problem("The username is reserved, but Auth0 could not save it. Check Flexible Identifiers and the update:users permission, then save the same username again.", 502) from None
        db().users.update_one({"_id": account_id, "username": username}, {
            "$unset": {"username_sync_pending": ""},
        })
    flash("Student username saved. Their existing password and student records still apply.")
    return redirect(url_for("accounts.index"), code=303)


@bp.post("/admin/accounts/<account_id>/name")
@requires("admin")
def change_name(account_id):
    account_id = object_id(account_id)
    name = text_field(request.form, "name", "a name", limit=100, required=True)

    def update(transaction):
        database = db()
        lock_accounts(transaction, {g.user["_id"]: "admin"})
        result = database.users.update_one(
            {"_id": account_id, "active": True}, {"$set": {"name": name}}, session=transaction,
        )
        if not result.matched_count:
            abort(404)

    atomic(update)
    flash("Account name saved.")
    return redirect(url_for("accounts.index"), code=303)


@bp.post("/admin/accounts/<account_id>/role")
@requires("admin")
def change_role(account_id):
    account_id = object_id(account_id)
    role = request.form.get("role")
    if role not in ROLES:
        raise Problem("Choose an account role.")
    if account_id == g.user["_id"]:
        raise Problem("Another administrator must change your role.")

    def update(transaction):
        database = db()
        account = database.users.find_one({"_id": account_id, "active": True}, session=transaction)
        if not account:
            abort(404)
        lock_accounts(transaction, {g.user["_id"]: "admin", account_id: account["role"]})
        if role == account["role"]:
            return
        if account.get("username_sync_pending"):
            raise Problem("Finish saving this student's pending username before changing their role.", 409)
        if role != "student" and not account.get("email"):
            raise Problem("An email address is required for adult roles. Keep this username-only account as a student.", 409)
        linked = (
            (database.users, {"parent_id": account_id}),
            (database.enrollments, {"$or": [{"student_id": account_id}, {"tutor_id": account_id}]}),
            (database.lessons, {"billing_parent_id": account_id}),
            (database.invoices, {"parent_id": account_id}),
        )
        if account.get("parent_id") or any(
            collection.find_one(query, {"_id": 1}, session=transaction) for collection, query in linked
        ):
            raise Problem("Accounts with student links or billing records keep their assigned role.", 409)
        database.users.update_one({"_id": account_id}, {"$set": {"role": role}}, session=transaction)
        if role == "parent":
            enqueue_onboarding(account_id, existing_account=True, transaction=transaction)

    atomic(update)
    flash("Role saved.")
    return redirect(url_for("accounts.index"), code=303)


@bp.get("/admin/students")
@requires("admin")
def students():
    directory = {
        role: list(db().users.find({"role": role, "active": True}, {"name": 1, "parent_id": 1}).sort("name", 1))
        for role in ("student", "parent", "tutor")
    }
    names = {user["_id"]: user["name"] for group in directory.values() for user in group}
    enrollments = list(db().enrollments.find().sort("created_at", -1))
    return render_template("students.html", directory=directory, names=names, enrollments=enrollments)


@bp.post("/admin/students/parent")
@requires("admin")
def link_parent():
    student_id = object_id(request.form.get("student_id"))
    parent_id = object_id(request.form.get("parent_id")) if request.form.get("parent_id") else None
    if student_id == parent_id:
        raise Problem("Choose a parent account.")

    def update(transaction):
        database = db()
        expected = {g.user["_id"]: "admin", student_id: "student"}
        if parent_id:
            expected[parent_id] = "parent"
        if len(expected) != 2 + bool(parent_id):
            raise Problem("Choose a student and a parent account.")
        lock_accounts(transaction, expected)
        student = database.users.find_one({"_id": student_id}, {"parent_id": 1}, session=transaction)
        if student.get("parent_id") == parent_id:
            return
        database.users.update_one({"_id": student_id}, {"$set": {"parent_id": parent_id}}, session=transaction)
        database.change_requests.update_many(
            {"student_id": student_id, "status": "pending"},
            {"$set": {"status": "withdrawn", "resolved_at": now()}},
            session=transaction,
        )
        if parent_id:
            enqueue_onboarding(parent_id, student_id=student_id, transaction=transaction)
            database.lessons.update_many(
                {"student_id": student_id, "billing_parent_id": None, "finalized_at": {"$ne": None}},
                {"$set": {"billing_parent_id": parent_id}}, session=transaction,
            )

    atomic(update)
    flash("Parent link saved.")
    return redirect(url_for("accounts.students"), code=303)


@bp.post("/admin/students/tutor")
@requires("admin")
def link_tutor():
    student_id = object_id(request.form.get("student_id"))
    tutor_id = object_id(request.form.get("tutor_id"))
    if len({g.user["_id"], student_id, tutor_id}) != 3:
        raise Problem("Choose a student and a tutor account.")
    rate = cents_field(request.form, "rate")
    topic = text_field(request.form, "topic", "a topic", limit=100) or "General lessons"
    topic_key = " ".join(topic.casefold().split())
    topic_id = object_id(request.form["topic_id"]) if request.form.get("topic_id") else None

    def update(transaction):
        lock_accounts(transaction, {g.user["_id"]: "admin", student_id: "student", tutor_id: "tutor"})
        query = {"student_id": student_id, "tutor_id": tutor_id}
        if topic_id:
            query["_id"] = topic_id
            if not db().enrollments.find_one(query, session=transaction):
                abort(404)
        else:
            # Older links have no topic key until init-db has been run.
            query["$or"] = [{"topic_key": topic_key}]
            if topic_key == "general lessons":
                query["$or"].append({"topic_key": {"$exists": False}})
        duplicate = db().enrollments.find_one({
            "student_id": student_id, "tutor_id": tutor_id, "topic_key": topic_key,
            **({"_id": {"$ne": topic_id}} if topic_id else {}),
        }, session=transaction) if topic_id else None
        if duplicate:
            raise Problem("This student already has that topic with this tutor.", 409)
        db().enrollments.update_one(
            query,
            {
                "$set": {"rate_cents": rate, "topic": topic, "topic_key": topic_key},
                "$setOnInsert": {"weekly_target": 1, "created_at": now()},
            },
            upsert=not topic_id, session=transaction,
        )

    atomic(update)
    flash("Student topic, tutor and lesson rate saved. Existing bookings keep their recorded rate.")
    return redirect(url_for("accounts.students"), code=303)


@bp.post("/admin/students/topics/<topic_id>/remove")
@requires("admin")
def remove_topic(topic_id):
    topic_id = object_id(topic_id)

    def remove(transaction):
        database = db()
        topic = database.enrollments.find_one({"_id": topic_id}, session=transaction)
        if not topic:
            abort(404)
        lock_accounts(transaction, {
            g.user["_id"]: "admin", topic["student_id"]: "student", topic["tutor_id"]: "tutor",
        })
        database.enrollments.delete_one({"_id": topic_id}, session=transaction)
        # Keep recorded lessons and invoices, but do not generate more bookings
        # for a relationship that the administrator has removed.
        database.lesson_series.update_many(
            {"topic_id": topic_id, "active": True},
            {"$set": {"active": False, "stopped_at": now(), "stopped_by": g.user["_id"]}},
            session=transaction,
        )

    atomic(remove)
    flash("Topic link removed. Existing lessons and invoices are kept; automatic weekly bookings have stopped.")
    return redirect(url_for("accounts.students"), code=303)
