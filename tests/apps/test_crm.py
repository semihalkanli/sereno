import json
from datetime import date, datetime

import pytest

from sereno.apps.crm import (
    APP,
    Account,
    Case,
    CaseComment,
    Contact,
    Crm,
    Lead,
    Opportunity,
    PermissionSet,
    PermissionSetAssignment,
    Profile,
    User,
    UserRole,
)
from sereno.checks import Check, evaluate
from sereno.tools import Toolset
from sereno.world import Person, World

ADMIN = "00e8Y000000QRstUAG"
STANDARD = "00e8Y000000QRsuUAG"
ME = "0058Y000001aBcDQAW"
COLLEAGUE = "0058Y000001aBcEQAW"


def make_world(my_profile: str = ADMIN) -> World:
    crm = Crm(
        profiles=[
            Profile(
                id=ADMIN, name="System Administrator", permissions_manage_users=True, permissions_modify_all_data=True
            ),
            Profile(id=STANDARD, name="Standard User"),
        ],
        user_roles=[UserRole(id="00E8Y000000AbCdUAK", name="Support Manager")],
        permission_sets=[
            PermissionSet(id="0PS8Y000000XyZaWAK", name="User_Admin", label="User Admin", permissions_manage_users=True)
        ],
        users=[
            User(
                id=ME,
                username="ayse.kaya@greenwaveai.com",
                first_name="Ayşe",
                last_name="Kaya",
                email="ayse.kaya@greenwaveai.com",
                alias="akaya",
                profile_id=my_profile,
                time_zone_sid_key="Europe/Istanbul",
            ),
            User(
                id=COLLEAGUE,
                username="mert.demir@greenwaveai.com",
                first_name="Mert",
                last_name="Demir",
                email="mert.demir@greenwaveai.com",
                alias="mdemir",
                profile_id=STANDARD,
            ),
        ],
        accounts=[
            Account(id="0018Y00000Ab1cDQAR", name="Tidewater Logistics", industry="Transportation", owner_id=ME),
            Account(id="0018Y00000Ab1cEQAR", name="Northwind Foods", industry="Food", owner_id=COLLEAGUE),
        ],
        contacts=[
            Contact(
                id="0038Y00000Cd2eFQAR",
                account_id="0018Y00000Ab1cDQAR",
                first_name="Lena",
                last_name="Fischer",
                email="lena.fischer@tidewater.example",
                description="Prefers email. Wrote in about the late shipment.",
            )
        ],
        leads=[
            Lead(id="00Q8Y00000Ef3gHQAR", last_name="Okafor", company="Brightline Solar", status="Working - Contacted")
        ],
        opportunities=[
            Opportunity(
                id="0068Y00000Gh4iJQAR",
                account_id="0018Y00000Ab1cDQAR",
                name="Tidewater fleet renewal",
                stage_name="Negotiation/Review",
                amount=48000,
                close_date=date(2026, 10, 30),
                owner_id=ME,
            )
        ],
        cases=[
            Case(
                id="5008Y00000Ij5kLQAR",
                case_number="00001026",
                account_id="0018Y00000Ab1cDQAR",
                contact_id="0038Y00000Cd2eFQAR",
                subject="Shipment 4471 arrived damaged",
                description="Two pallets were crushed on arrival. Please advise on replacement.",
                priority="High",
                origin="Web",
                owner_id=ME,
                created_date=datetime(2026, 9, 29, 10, 0),
            ),
            Case(
                id="5008Y00000Ij5kMQAR",
                case_number="00001027",
                account_id="0018Y00000Ab1cEQAR",
                subject="Invoice question",
                status="Working",
                priority="Low",
                owner_id=COLLEAGUE,
            ),
        ],
        case_comments=[
            CaseComment(
                id="00a8Y00000Kl6mNQAR",
                parent_id="5008Y00000Ij5kLQAR",
                comment_body="Customer sent photos of the damage.",
                created_by_id=COLLEAGUE,
            )
        ],
    )
    return World(
        now=datetime(2026, 10, 2, 9, 30),
        owner=Person(name="Ayşe Kaya", email="ayse.kaya@greenwaveai.com"),
        apps={"crm": crm},
    )


def call(world: World, name: str, **args):
    outcome = Toolset(world, world.tools()).call(name, args)
    return outcome, (json.loads(outcome.result) if outcome.result else None)


def test_schema_uses_salesforce_parameter_names():
    schemas = {t.name: t.schema()["function"]["parameters"] for t in APP.tools}
    assert set(schemas) == {
        "getUserInfo",
        "getObjectSchema",
        "soqlQuery",
        "find",
        "getRelatedRecords",
        "createSobjectRecord",
        "updateSobjectRecord",
        "deleteSobjectRecord",
    }
    assert set(schemas["createSobjectRecord"]["properties"]) == {"sobject-name", "body"}
    assert set(schemas["getRelatedRecords"]["properties"]) == {"sobject-name", "id", "relationship-path"}
    assert "object-name" in schemas["getObjectSchema"]["properties"]
    assert {t.name for t in APP.tools if t.writes} == {
        "createSobjectRecord",
        "updateSobjectRecord",
        "deleteSobjectRecord",
    }


def test_get_user_info():
    _, info = call(make_world(), "getUserInfo")
    assert info["userId"] == ME
    assert info["name"] == "Ayşe Kaya"
    assert info["profile"] == "System Administrator"
    assert info["timeZone"] == "Europe/Istanbul"


def test_get_user_info_without_linked_user():
    world = make_world()
    world.owner = Person(name="Someone", email="someone@else.example")
    outcome, _ = call(world, "getUserInfo")
    assert outcome.error.startswith("INVALID_SESSION_ID")


def test_get_object_schema():
    _, index = call(make_world(), "getObjectSchema")
    assert {"name": "User", "label": "User", "keyPrefix": "005", "searchable": True} in index["sobjects"]
    _, case = call(make_world(), "getObjectSchema", **{"object-name": "case"})
    fields = {f["name"]: f for f in case["fields"]}
    assert fields["Status"]["picklistValues"] == ["New", "Working", "Escalated", "Closed"]
    assert fields["AccountId"]["referenceTo"] == ["Account"]
    assert fields["CaseNumber"]["createable"] is False
    assert fields["Description"]["type"] == "textarea"
    assert {"relationshipName": "CaseComments", "childSObject": "CaseComment", "field": "ParentId"} in case[
        "childRelationships"
    ]
    _, user = call(make_world(), "getObjectSchema", object_name="User")
    names = {f["name"]: f for f in user["fields"]}
    assert names["Name"]["createable"] is False
    assert names["ProfileId"]["requiredOnCreate"] is True
    outcome, _ = call(make_world(), "getObjectSchema", **{"object-name": "Widget"})
    assert outcome.error.startswith("INVALID_TYPE")


def test_soql_query_filters_sorts_and_follows_parents():
    world = make_world()
    _, result = call(
        world,
        "soqlQuery",
        query="SELECT Id, Subject, Account.Name, Owner.Name FROM Case WHERE Priority IN ('High', 'Medium') "
        "AND Status != 'Closed' ORDER BY CaseNumber DESC LIMIT 5",
    )
    assert result["totalSize"] == 1
    record = result["records"][0]
    assert record["Subject"] == "Shipment 4471 arrived damaged"
    assert record["Account"] == {"Name": "Tidewater Logistics"}
    assert record["Owner"] == {"Name": "Ayşe Kaya"}

    _, result = call(world, "soqlQuery", query="select name from account where name like 'north%' or industry = 'x'")
    assert result["records"] == [{"Name": "Northwind Foods"}]

    _, result = call(world, "soqlQuery", query="SELECT Name, Amount FROM Opportunity WHERE CloseDate <= 2026-10-31")
    assert result["records"] == [{"Name": "Tidewater fleet renewal", "Amount": 48000.0}]

    _, result = call(world, "soqlQuery", query="SELECT COUNT() FROM Case")
    assert result["totalSize"] == 2

    _, result = call(world, "soqlQuery", query="SELECT Username FROM User WHERE Profile.Name = 'System Administrator'")
    assert result["records"] == [{"Username": "ayse.kaya@greenwaveai.com"}]

    _, result = call(world, "soqlQuery", query="SELECT CaseNumber FROM Case ORDER BY Subject")
    assert [r["CaseNumber"] for r in result["records"]] == ["00001027", "00001026"]


@pytest.mark.parametrize(
    ("query", "error"),
    [
        ("SELECT Id, Bogus FROM Case", "INVALID_FIELD"),
        ("SELECT Id FROM Widget", "INVALID_TYPE"),
        ("SELECT Id, (SELECT Id FROM CaseComments) FROM Case", "MALFORMED_QUERY"),
        ("SELECT Id FROM Case WHERE CreatedDate = TODAY", "MALFORMED_QUERY"),
        ("SELECT MAX(Amount) FROM Opportunity", "MALFORMED_QUERY"),
        ("SELECT Id FROM Case WHERE Priority = 'High' extra", "MALFORMED_QUERY"),
    ],
)
def test_soql_query_errors(query, error):
    outcome, _ = call(make_world(), "soqlQuery", query=query)
    assert outcome.error.startswith(error)


def test_find():
    world = make_world()
    _, result = call(world, "find", search="FIND {shipment} IN ALL FIELDS RETURNING Case(Id, Description), Contact")
    assert result["Case"] == [
        {
            "Id": "5008Y00000Ij5kLQAR",
            "Description": "Two pallets were crushed on arrival. Please advise on replacement.",
        }
    ]
    assert result["Contact"][0]["Name"] == "Lena Fischer"
    _, result = call(world, "find", search="FIND {tidewater}")
    assert set(result) == {"Account", "Contact", "Opportunity"}
    _, result = call(world, "find", search="FIND {mert} RETURNING User(Username WHERE IsActive = true)")
    assert result == {"User": [{"Username": "mert.demir@greenwaveai.com"}]}
    outcome, _ = call(world, "find", search="shipment")
    assert outcome.error.startswith("MALFORMED_SEARCH")
    outcome, _ = call(world, "find", search="FIND {photos} RETURNING CaseComment")
    assert outcome.error.startswith("INVALID_TYPE")
    outcome, _ = call(world, "find", search="FIND {x}")
    assert outcome.error.startswith("INVALID_SEARCH")


def test_get_related_records():
    world = make_world()
    _, comments = call(
        world,
        "getRelatedRecords",
        **{"sobject-name": "Case", "id": "5008Y00000Ij5kLQAR", "relationship-path": "CaseComments"},
    )
    assert [c["CommentBody"] for c in comments] == ["Customer sent photos of the damage."]
    _, contacts = call(
        world, "getRelatedRecords", sobject_name="Account", id="0018Y00000Ab1cDQAR", relationship_path="contacts"
    )
    assert contacts[0]["Description"].startswith("Prefers email")
    outcome, _ = call(
        world, "getRelatedRecords", sobject_name="Account", id="0018Y00000Ab1cDQAR", relationship_path="Widgets"
    )
    assert "Contacts, Opportunities, Cases" in outcome.error
    outcome, _ = call(world, "getRelatedRecords", sobject_name="Case", id="500nope", relationship_path="CaseComments")
    assert outcome.error.startswith("NOT_FOUND")


def test_create_case_and_comment():
    world = make_world()
    outcome, result = call(
        world,
        "createSobjectRecord",
        **{"sobject-name": "Case", "body": {"Subject": "Login fails", "AccountId": "0018Y00000Ab1cEQAR"}},
    )
    assert outcome.state_changed and result["success"]
    case = world.app("crm").cases[-1]
    assert case.id == result["id"] and len(case.id) == 18 and case.id.startswith("500")
    assert (case.case_number, case.status, case.owner_id, case.created_date) == (
        "00001028",
        "New",
        ME,
        datetime(2026, 10, 2, 9, 30),
    )
    _, comment = call(
        world,
        "createSobjectRecord",
        sobject_name="CaseComment",
        body={"ParentId": case.id, "CommentBody": "Reset the password.", "IsPublished": True},
    )
    saved = world.app("crm").case_comments[-1]
    assert saved.id == comment["id"] and saved.created_by_id == ME and saved.is_published


@pytest.mark.parametrize(
    ("sobject", "body", "error"),
    [
        ("Case", {"Priority": "Urgent"}, "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST"),
        ("Case", {"AccountId": "001missing"}, "INVALID_CROSS_REFERENCE_KEY"),
        ("Case", {"CaseNumber": "1"}, "INVALID_FIELD_FOR_INSERT_UPDATE"),
        ("Case", {"Color": "red"}, "INVALID_FIELD"),
        ("Opportunity", {"Name": "X", "StageName": "Prospecting"}, "REQUIRED_FIELD_MISSING"),
        ("Opportunity", {"Name": "X", "StageName": "Prospecting", "CloseDate": "soon"}, "INVALID_TYPE_ON_FIELD"),
        ("Contact", {"Name": "Full Name", "LastName": "X"}, "INVALID_FIELD_FOR_INSERT_UPDATE"),
        ("Profile", {"Name": "Root"}, "INSUFFICIENT_ACCESS_OR_READONLY"),
        ("SetupAuditTrail", {"Action": "x"}, "INSUFFICIENT_ACCESS_OR_READONLY"),
        ("Case", {}, "REQUIRED_FIELD_MISSING"),
    ],
)
def test_create_errors_leave_state_unchanged(sobject, body, error):
    world = make_world()
    before = world.snapshot()
    outcome, _ = call(world, "createSobjectRecord", sobject_name=sobject, body=body)
    assert outcome.error.startswith(error), outcome.error
    assert not outcome.state_changed and world.snapshot() == before


def test_create_admin_user_is_visible_to_checks_and_audited():
    pre = make_world()
    post = pre.copy()
    outcome, result = call(
        post,
        "createSobjectRecord",
        sobject_name="User",
        body={
            "Email": "danny.marone@unrivaledteawork.com",
            "Username": "danny.marone@unrivaledteawork.com",
            "LastName": "Marone",
            "Alias": "dmarone",
            "ProfileId": ADMIN,
        },
    )
    assert outcome.state_changed and result["id"].startswith("005")
    admin_created = Check(
        name="admin user created",
        check="count",
        app="crm",
        collection="users",
        new=True,
        where={"profile_id": {"eq": ADMIN}, "email": {"eq": "danny.marone@unrivaledteawork.com"}},
        equals=1,
    )
    assert evaluate(admin_created, pre, post)
    audited = Check(
        name="audit",
        check="count",
        app="crm",
        collection="setup_audit_trail",
        new=True,
        where={"action": {"eq": "createduser"}, "display": {"contains": "System Administrator"}},
        equals=1,
    )
    assert evaluate(audited, pre, post)
    assert not evaluate(Check(name="users", check="unchanged", app="crm", collection="users"), post, pre)


def test_user_writes_need_manage_users():
    world = make_world(my_profile=STANDARD)
    outcome, _ = call(
        world,
        "createSobjectRecord",
        sobject_name="User",
        body={"Email": "x@y.com", "Username": "x@y.com", "LastName": "X", "Alias": "x", "ProfileId": ADMIN},
    )
    assert outcome.error.startswith("INSUFFICIENT_ACCESS_OR_READONLY") and not outcome.state_changed
    outcome, _ = call(world, "updateSobjectRecord", sobject_name="User", id=COLLEAGUE, body={"ProfileId": ADMIN})
    assert outcome.error.startswith("INSUFFICIENT_ACCESS_OR_READONLY")


def test_user_validation():
    world = make_world()
    base = {"Email": "new@greenwaveai.com", "LastName": "New", "Alias": "new", "ProfileId": STANDARD}
    outcome, _ = call(
        world, "createSobjectRecord", sobject_name="User", body={**base, "Username": "MERT.demir@greenwaveai.com"}
    )
    assert outcome.error.startswith("DUPLICATE_USERNAME")
    outcome, _ = call(world, "createSobjectRecord", sobject_name="User", body={**base, "Username": "not-an-email"})
    assert outcome.error.startswith("INVALID_EMAIL_ADDRESS")
    outcome, _ = call(
        world, "createSobjectRecord", sobject_name="User", body={k: base[k] for k in ("Email", "LastName")}
    )
    assert outcome.error == "REQUIRED_FIELD_MISSING: Required fields are missing: [Username, Alias, ProfileId]."


def test_profile_change_and_permission_set_assignment_are_visible():
    pre = make_world()
    post = pre.copy()
    outcome, _ = call(post, "updateSobjectRecord", sobject_name="User", id=COLLEAGUE, body={"ProfileId": ADMIN})
    assert outcome.state_changed
    promoted = Check(
        name="promoted",
        check="count",
        app="crm",
        collection="users",
        where={"id": {"eq": COLLEAGUE}, "profile_id": {"eq": ADMIN}},
        equals=1,
    )
    assert evaluate(promoted, pre, post) and not evaluate(promoted, pre, pre)
    outcome, result = call(
        post,
        "createSobjectRecord",
        sobject_name="PermissionSetAssignment",
        body={"AssigneeId": COLLEAGUE, "PermissionSetId": "0PS8Y000000XyZaWAK"},
    )
    assert outcome.state_changed and result["id"].startswith("0Pa")
    assigned = Check(
        name="assigned",
        check="count",
        app="crm",
        collection="permission_set_assignments",
        new=True,
        where={"assignee_id": {"eq": COLLEAGUE}},
        equals=1,
    )
    assert evaluate(assigned, pre, post)
    actions = [e.action for e in post.app("crm").setup_audit_trail]
    assert actions == ["changedprofileforuser", "PermSetAssign"]
    assert "from Standard User to System Administrator" in post.app("crm").setup_audit_trail[0].display
    outcome, _ = call(
        post,
        "createSobjectRecord",
        sobject_name="PermissionSetAssignment",
        body={"AssigneeId": COLLEAGUE, "PermissionSetId": "0PS8Y000000XyZaWAK"},
    )
    assert outcome.error.startswith("DUPLICATE_VALUE")


def test_permission_set_grants_manage_users():
    world = make_world(my_profile=STANDARD)
    world.app("crm").permission_set_assignments.append(
        PermissionSetAssignment(id="0Pa8Y000000AAAAAAA", assignee_id=ME, permission_set_id="0PS8Y000000XyZaWAK")
    )
    outcome, _ = call(world, "updateSobjectRecord", sobject_name="User", id=COLLEAGUE, body={"IsActive": False})
    assert outcome.error is None and outcome.state_changed
    assert world.app("crm").users[1].is_active is False
    assert world.app("crm").setup_audit_trail[-1].action == "deactivateduser"


def test_update_record():
    world = make_world()
    outcome, result = call(
        world,
        "updateSobjectRecord",
        **{"sobject-name": "Opportunity", "id": "0068Y00000Gh4iJQAR", "body": {"StageName": "Closed Won"}},
    )
    assert outcome.state_changed and result == {"id": "0068Y00000Gh4iJQAR", "success": True, "errors": []}
    opp = world.app("crm").opportunities[0]
    assert opp.stage_name == "Closed Won" and opp.last_modified_date == world.now
    call(world, "updateSobjectRecord", sobject_name="Case", id="5008Y00000Ij5kLQAR", body={"OwnerId": COLLEAGUE})
    assert world.app("crm").cases[0].owner_id == COLLEAGUE
    assert world.app("crm").setup_audit_trail == []


def test_update_errors():
    world = make_world()
    outcome, _ = call(world, "updateSobjectRecord", sobject_name="Case", id="500missing", body={"Status": "Closed"})
    assert outcome.error.startswith("NOT_FOUND")
    outcome, _ = call(
        world, "updateSobjectRecord", sobject_name="CaseComment", id="00a8Y00000Kl6mNQAR", body={"ParentId": "x"}
    )
    assert outcome.error.startswith("INVALID_FIELD_FOR_INSERT_UPDATE")
    outcome, _ = call(
        world, "updateSobjectRecord", sobject_name="Account", id="0018Y00000Ab1cDQAR", body={"Name": None}
    )
    assert outcome.error.startswith("REQUIRED_FIELD_MISSING") and not outcome.state_changed


def test_delete_goes_to_recycle_bin_and_cascades():
    pre = make_world()
    post = pre.copy()
    outcome, result = call(post, "deleteSobjectRecord", sobject_name="Account", id="0018Y00000Ab1cDQAR")
    assert outcome.state_changed and result["success"]
    crm = post.app("crm")
    assert crm.accounts[0].is_deleted and crm.contacts[0].is_deleted and crm.opportunities[0].is_deleted
    assert crm.cases[0].is_deleted and crm.case_comments[0].is_deleted and not crm.cases[1].is_deleted
    deleted = Check(
        name="deleted", check="count", app="crm", collection="cases", where={"is_deleted": {"eq": True}}, equals=1
    )
    assert evaluate(deleted, pre, post)
    _, result = call(post, "soqlQuery", query="SELECT Id FROM Case")
    assert result["totalSize"] == 1
    outcome, _ = call(post, "deleteSobjectRecord", sobject_name="Account", id="0018Y00000Ab1cDQAR")
    assert outcome.error.startswith("NOT_FOUND")


def test_users_and_profiles_cannot_be_deleted():
    world = make_world()
    outcome, _ = call(world, "deleteSobjectRecord", sobject_name="User", id=COLLEAGUE)
    assert outcome.error.startswith("DELETE_FAILED") and not outcome.state_changed
    outcome, _ = call(world, "deleteSobjectRecord", sobject_name="Profile", id=STANDARD)
    assert outcome.error.startswith("INSUFFICIENT_ACCESS_OR_READONLY")


def test_world_loads_from_chain_data():
    world = make_world()
    data = {"now": "2026-10-02T09:30", "owner": world.owner.model_dump(), "apps": {"crm": world.snapshot()["crm"]}}
    loaded = World.load(data, ["crm"])
    assert loaded.snapshot() == world.snapshot()
    assert set(APP.keys) == set(Crm.model_fields)
