import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gql_client  # noqa: E402
import sonar_schema_check as sc  # noqa: E402


def _nn(name, kind="INPUT_OBJECT"):
    return {"name": None, "kind": "NON_NULL", "ofType": {"name": name, "kind": kind, "ofType": None}}


def fake_schema(id_scalar="Int64Bit", update_input="UpdateTicketMutationInput"):
    types = {
        id_scalar: {"name": id_scalar, "kind": "SCALAR"},
        "CreateTicketCommentMutationInput": {"name": "CreateTicketCommentMutationInput", "kind": "INPUT_OBJECT"},
        "UpdateTicketMutationInput": {"name": "UpdateTicketMutationInput", "kind": "INPUT_OBJECT"},
    }
    fields = {
        "Mutation": [
            {"name": "createTicketComment", "args": [{"name": "input", "type": _nn("CreateTicketCommentMutationInput")}]},
            {"name": "updateTicket", "args": [{"name": "id", "type": _nn(id_scalar, "SCALAR")},
                                              {"name": "input", "type": _nn(update_input)}]},
        ],
        "Query": [{"name": "tickets", "args": []}, {"name": "ticket_comments", "args": []}],
    }

    def execute(query, variables=None, **kw):
        n = (variables or {}).get("n")
        if "fields" in query:
            return {"__type": {"fields": fields.get(n, [])}}
        return {"__type": types.get(n)}

    return execute


class SchemaCheckTest(unittest.TestCase):
    def run_check(self, **kw):
        with mock.patch.object(gql_client, "execute", fake_schema(**kw)), \
             mock.patch.object(gql_client, "id_scalar", return_value="Int64Bit"):
            return sc.check()

    def test_all_names_match(self):
        self.assertTrue(all(ok for _, ok, _ in self.run_check()))

    def test_wrong_input_type_is_reported(self):
        res = self.run_check(update_input="TicketUpdateInput")
        failed = [label for label, ok, _ in res if not ok]
        self.assertIn("updateTicket(input:) type", failed)

    def test_missing_scalar_is_reported(self):
        with mock.patch.object(gql_client, "execute", fake_schema(id_scalar="Int")), \
             mock.patch.object(gql_client, "id_scalar", return_value="Int64Bit"):
            res = sc.check()
        failed = [label for label, ok, _ in res if not ok]
        self.assertIn("id scalar Int64Bit", failed)

    def test_main_exit_codes(self):
        with mock.patch.object(gql_client, "execute", fake_schema()), \
             mock.patch.object(gql_client, "id_scalar", return_value="Int64Bit"):
            self.assertEqual(sc.main(), 0)
        with mock.patch.object(gql_client, "execute", side_effect=gql_client.GraphQLError("config", "no token")):
            self.assertEqual(sc.main(), 2)


if __name__ == "__main__":
    unittest.main()
