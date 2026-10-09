"""Tests for the Google Workspace tools and their safety limits."""

from __future__ import annotations

import base64
import json
from email import message_from_bytes

import httpx
import pytest
import respx

import openjarvis.tools.google_workspace as gw
from openjarvis.tools.approval_store import STATUS_PENDING, ApprovalStore

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL = "https://www.googleapis.com/calendar/v3"
DRIVE = "https://www.googleapis.com/drive/v3"
TASKS = "https://tasks.googleapis.com/tasks/v1"


@pytest.fixture(autouse=True)
def fake_token(monkeypatch):
    monkeypatch.setattr(gw, "_access_token", lambda refresh=False: "tok")


@pytest.fixture
def store(tmp_path):
    s = ApprovalStore(db_path=str(tmp_path / "approvals.db"))
    yield s
    s.close()


# ---------------------------------------------------------------------------
# The allowlist: deletion and sending are impossible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("DELETE", f"{DRIVE}/files/abc123"),
        ("DELETE", f"{GMAIL}/messages/abc123"),
        ("DELETE", f"{CAL}/calendars/primary/events/abc123"),
        ("POST", f"{GMAIL}/messages/send"),
        ("POST", f"{GMAIL}/drafts/send"),
        ("POST", f"{DRIVE}/files/abc123/copy"),
        ("POST", f"{DRIVE}/files/abc123/trash"),
        ("POST", f"{GMAIL}/messages/abc123/trash"),
        ("PATCH", f"{CAL}/calendars/primary/events/abc123"),
        ("POST", "https://people.googleapis.com/v1/people:createContact"),
    ],
)
def test_disallowed_requests_never_leave_the_process(method, url):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route().mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(gw.DisallowedRequestError):
            gw._request(method, url)
        assert not route.called


def test_drive_patch_cannot_trash_or_edit():
    with pytest.raises(gw.DisallowedRequestError):
        gw._request("PATCH", f"{DRIVE}/files/abc123", json={"trashed": True})
    with pytest.raises(gw.DisallowedRequestError):
        gw._request("PATCH", f"{DRIVE}/files/abc123", params={"trashed": "true"})


def test_no_executor_can_delete_or_send():
    assert set(gw.GOOGLE_ACTION_EXECUTORS) == {
        "drive_create_folder",
        "drive_move_file",
        "gmail_draft_reply",
        "calendar_create_event",
        "tasks_create_task",
    }


@respx.mock
def test_expired_token_is_refreshed_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        gw,
        "_access_token",
        lambda refresh=False: calls.append(refresh) or ("new" if refresh else "old"),
    )
    route = respx.get(f"{TASKS}/users/@me/lists").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"items": []})]
    )
    assert gw._request("GET", f"{TASKS}/users/@me/lists") == {"items": []}
    assert calls == [False, True]
    assert route.calls[-1].request.headers["Authorization"] == "Bearer new"


# ---------------------------------------------------------------------------
# Write tools only queue; nothing reaches Google before approval
# ---------------------------------------------------------------------------


@respx.mock(assert_all_mocked=True)
@pytest.mark.parametrize(
    ("tool_cls", "params", "expected_payload"),
    [
        (gw.DriveCreateFolderTool, {"name": " Invoices "}, {"name": "Invoices"}),
        (
            gw.DriveMoveFileTool,
            {"file_id": "file123", "target_folder_id": "fold456", "file_name": "a.pdf"},
            {"file_id": "file123", "target_folder_id": "fold456"},
        ),
        (
            gw.GmailDraftReplyTool,
            {"message_id": "msg123", "body": "Thanks!"},
            {"message_id": "msg123", "body": "Thanks!"},
        ),
        (
            gw.CalendarCreateEventTool,
            {
                "summary": "Dentist",
                "start": "2026-10-01T09:00:00+02:00",
                "end": "2026-10-01T10:00:00+02:00",
                "attendees": ["someone@example.com"],
            },
            {
                "summary": "Dentist",
                "start": "2026-10-01T09:00:00+02:00",
                "end": "2026-10-01T10:00:00+02:00",
            },
        ),
        (
            gw.TasksCreateTaskTool,
            {"title": "Buy milk", "due": "2026-10-02"},
            {"title": "Buy milk", "due": "2026-10-02"},
        ),
    ],
)
def test_write_tools_only_queue(store, tool_cls, params, expected_payload):
    result = tool_cls(store=store).execute(**params)
    assert result.success, result.content
    action = store.get_action(result.metadata["action_id"])
    assert action.status == STATUS_PENDING
    assert action.action_type == tool_cls.tool_id
    assert action.payload == expected_payload
    assert "approv" in result.content.lower()


@pytest.mark.parametrize(
    ("tool_cls", "params"),
    [
        (gw.DriveCreateFolderTool, {"name": ""}),
        (gw.DriveMoveFileTool, {"file_id": "../etc", "target_folder_id": "fold456"}),
        (gw.DriveMoveFileTool, {"file_id": "same123", "target_folder_id": "same123"}),
        (gw.GmailDraftReplyTool, {"message_id": "msg123", "body": "  "}),
        (gw.CalendarCreateEventTool, {"summary": "x", "start": "", "end": "y"}),
        (gw.TasksCreateTaskTool, {"title": "x", "due": "tomorrow"}),
    ],
)
def test_invalid_requests_are_rejected_without_queueing(store, tool_cls, params):
    result = tool_cls(store=store).execute(**params)
    assert not result.success
    assert store.list_pending() == []


# ---------------------------------------------------------------------------
# Executors (run after approval)
# ---------------------------------------------------------------------------


@respx.mock
def test_calendar_event_never_invites_anyone():
    route = respx.post(f"{CAL}/calendars/primary/events").mock(
        return_value=httpx.Response(200, json={"summary": "Dentist", "htmlLink": "l"})
    )
    ok, _ = gw.execute_google_action(
        "calendar_create_event",
        {
            "summary": "Dentist",
            "start": "2026-10-01",
            "end": "2026-10-02",
            "attendees": [{"email": "x@example.com"}],
            "guestsCanInviteOthers": True,
        },
    )
    assert ok
    request = route.calls.last.request
    body = json.loads(request.content)
    assert "attendees" not in body and "guestsCanInviteOthers" not in body
    assert body["start"] == {"date": "2026-10-01"}
    assert request.url.params["sendUpdates"] == "none"


@respx.mock
def test_draft_reply_is_saved_as_draft_in_thread():
    respx.get(f"{GMAIL}/messages/msg123").mock(
        return_value=httpx.Response(
            200,
            json={
                "threadId": "thr9",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Alice <alice@example.com>"},
                        {"name": "Subject", "value": "Budget"},
                        {"name": "Message-ID", "value": "<m1@example.com>"},
                    ]
                },
            },
        )
    )
    drafts = respx.post(f"{GMAIL}/drafts").mock(
        return_value=httpx.Response(200, json={"id": "d1"})
    )
    ok, message = gw.execute_google_action(
        "gmail_draft_reply", {"message_id": "msg123", "body": "Sounds good."}
    )
    assert ok and "not been sent" in message
    sent = json.loads(drafts.calls.last.request.content)["message"]
    assert sent["threadId"] == "thr9"
    mime = message_from_bytes(base64.urlsafe_b64decode(sent["raw"]))
    assert mime["To"] == "alice@example.com"
    assert mime["Subject"] == "Re: Budget"
    assert mime["In-Reply-To"] == "<m1@example.com>"
    assert mime.get_payload().strip() == "Sounds good."


@respx.mock
def test_move_file_only_reparents():
    respx.get(f"{DRIVE}/files/file123").mock(
        return_value=httpx.Response(200, json={"id": "file123", "parents": ["old1"]})
    )
    patch = respx.patch(f"{DRIVE}/files/file123").mock(
        return_value=httpx.Response(200, json={"id": "file123", "name": "a.pdf"})
    )
    ok, _ = gw.execute_google_action(
        "drive_move_file", {"file_id": "file123", "target_folder_id": "new2"}
    )
    assert ok
    request = patch.calls.last.request
    assert request.content == b""
    assert request.url.params["addParents"] == "new2"
    assert request.url.params["removeParents"] == "old1"


@respx.mock
def test_google_errors_become_failed_results():
    respx.post(f"{TASKS}/lists/@default/tasks").mock(
        return_value=httpx.Response(
            403, json={"error": {"message": "insufficient scope"}}
        )
    )
    ok, message = gw.execute_google_action("tasks_create_task", {"title": "x"})
    assert not ok and "insufficient scope" in message


# ---------------------------------------------------------------------------
# Approval route runs Google actions exactly once
# ---------------------------------------------------------------------------


@pytest.fixture
def client(store, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import openjarvis.server.approval_routes as ar

    monkeypatch.setattr(ar, "_store", store)
    app = fastapi.FastAPI()
    app.include_router(ar.router)
    return TestClient(app)


def test_approving_runs_the_action_once(client, store, monkeypatch):
    runs = []
    monkeypatch.setitem(
        gw.GOOGLE_ACTION_EXECUTORS,
        "tasks_create_task",
        lambda payload: runs.append(payload) or (True, "Created task"),
    )
    action_id = (
        gw.TasksCreateTaskTool(store=store)
        .execute(title="Call Bob")
        .metadata["action_id"]
    )

    first = client.post(f"/v1/approvals/{action_id}/approve")
    assert first.status_code == 200
    assert first.json() == {
        "status": "executed",
        "id": action_id,
        "result": "Created task",
    }
    assert store.get_action(action_id).status == "executed"

    again = client.post(f"/v1/approvals/{action_id}/approve")
    assert again.status_code == 409
    assert runs == [{"title": "Call Bob"}]


def test_denied_action_cannot_be_approved_later(client, store, monkeypatch):
    runs = []
    monkeypatch.setitem(
        gw.GOOGLE_ACTION_EXECUTORS,
        "drive_create_folder",
        lambda p: runs.append(p) or (True, ""),
    )
    action_id = (
        gw.DriveCreateFolderTool(store=store).execute(name="X").metadata["action_id"]
    )
    assert client.post(f"/v1/approvals/{action_id}/deny").status_code == 200
    assert client.post(f"/v1/approvals/{action_id}/approve").status_code == 409
    assert runs == []


def test_failed_action_is_marked_failed_not_approved(client, store, monkeypatch):
    monkeypatch.setitem(
        gw.GOOGLE_ACTION_EXECUTORS, "drive_create_folder", lambda p: (False, "boom")
    )
    action_id = (
        gw.DriveCreateFolderTool(store=store).execute(name="X").metadata["action_id"]
    )
    body = client.post(f"/v1/approvals/{action_id}/approve").json()
    assert body["status"] == "failed" and body["result"] == "boom"
    assert store.get_action(action_id).status == "failed"
    assert store.list_approved() == []


# ---------------------------------------------------------------------------
# gmail_pending_replies
# ---------------------------------------------------------------------------


def _msg(msg_id, sender, subject, labels, when_ms):
    return {
        "id": msg_id,
        "labelIds": labels,
        "internalDate": str(when_ms),
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ]
        },
    }


@respx.mock
def test_pending_replies_finds_threads_where_someone_else_wrote_last(monkeypatch):
    import time

    now = int(time.time() * 1000)
    day = 86_400_000
    threads = {
        # Anna wrote last 3 days ago -> pending
        "t1": [
            _msg("m1", "Anna <anna@example.com>", "Budget", ["INBOX"], now - 3 * day)
        ],
        # User replied last -> not pending
        "t2": [
            _msg("m2", "Bob <bob@example.com>", "Lunch", ["INBOX"], now - 2 * day),
            _msg("m3", "Me <me@example.com>", "Re: Lunch", ["SENT"], now - day),
        ],
        # Carol wrote last; a draft reply exists -> pending, flagged
        "t3": [
            _msg(
                "m4", "Carol <carol@example.com>", "Contract", ["INBOX"], now - 6 * day
            ),
            _msg("m5", "Me <me@example.com>", "Re: Contract", ["DRAFT"], now),
        ],
        # Promotion that slipped past the query -> ignored
        "t4": [
            _msg("m6", "Shop <deals@shop.com>", "Sale", ["CATEGORY_PROMOTIONS"], now)
        ],
        # Automated sender -> skipped and counted
        "t5": [_msg("m7", "GitHub <noreply@github.com>", "CI", ["INBOX"], now)],
    }
    listing = respx.get(f"{GMAIL}/threads").mock(
        return_value=httpx.Response(200, json={"threads": [{"id": t} for t in threads]})
    )
    for tid, msgs in threads.items():
        respx.get(f"{GMAIL}/threads/{tid}").mock(
            return_value=httpx.Response(200, json={"messages": msgs})
        )

    result = gw.GmailPendingRepliesTool().execute()

    assert result.success, result.content
    query = listing.calls.last.request.url.params["q"]
    assert "newer_than:10d" in query and "-category:promotions" in query
    assert "in:inbox" in query
    lines = [line for line in result.content.splitlines() if line.startswith("- ")]
    assert len(lines) == 2
    # Oldest first: Carol (6 days) before Anna (3 days)
    assert "Contract" in lines[0] and "draft reply already saved" in lines[0]
    assert "Budget" in lines[1] and "message_id=m1" in lines[1]
    assert "Lunch" not in result.content and "Sale" not in result.content
    assert "1 automated no-reply email(s) skipped" in result.content


@respx.mock
def test_pending_replies_clamps_days_and_reports_empty():
    route = respx.get(f"{GMAIL}/threads").mock(
        return_value=httpx.Response(200, json={})
    )
    result = gw.GmailPendingRepliesTool().execute(days=365)
    assert "newer_than:30d" in route.calls.last.request.url.params["q"]
    assert result.content.startswith("No emails are waiting")


@pytest.mark.parametrize(
    ("address", "automated"),
    [
        ("noreply@github.com", True),
        ("no-reply@accounts.google.com", True),
        ("do-not-reply@bank.example", True),
        ("notifications@github.com", True),
        ("news-noreply@site.example", True),
        ("anna@example.com", False),
        ("info@company.example", False),
        ("replyteam@example.com", False),
    ],
)
def test_automated_sender_detection(address, automated):
    assert bool(gw._AUTOMATED_SENDER_RE.search(address)) is automated
