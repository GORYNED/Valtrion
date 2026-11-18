"""The assembly graph the Unity result cache reads: definitions, GUIDs and references."""

import json
import unittest

from valtrion import asmgraph


def asm(name, references=(), **extra):
    return json.dumps({"name": name, "references": list(references), **extra})


def definition(name, references=(), guid=None):
    return asmgraph.parse_definition(f"{name}.asmdef", asm(name, references).encode(), guid)


class DefinitionTests(unittest.TestCase):
    def test_a_definition_keeps_its_name_and_references(self):
        found = definition("A", ["B", "GUID:" + "a" * 32], "c" * 32)
        self.assertEqual(("A", "A.asmdef", "c" * 32, ("B", "GUID:" + "a" * 32)), (found.name, found.path, found.guid, found.references))

    def test_unreadable_definitions_are_refused(self):
        for data in ("{", "[]", json.dumps({"references": []}), json.dumps({"name": ""}), json.dumps({"name": "A", "references": "B"}),
                     json.dumps({"name": "A", "references": [1]})):
            with self.assertRaises(asmgraph.GraphError, msg=data):
                asmgraph.parse_definition("A.asmdef", data.encode(), None)

    def test_the_guid_of_a_meta_file(self):
        self.assertEqual("a" * 32, asmgraph.guid_of_meta(f"fileFormatVersion: 2\nguid: {'A' * 32}\n".encode()))
        self.assertIsNone(asmgraph.guid_of_meta(b"fileFormatVersion: 2\n"))


class GraphTests(unittest.TestCase):
    def test_a_name_or_guid_that_two_assemblies_share_is_refused(self):
        with self.assertRaises(asmgraph.GraphError):
            asmgraph.Graph([definition("A"), asmgraph.parse_definition("Other/A.asmdef", asm("A").encode(), None)])
        with self.assertRaises(asmgraph.GraphError):
            asmgraph.Graph([definition("A", guid="1" * 32), definition("B", guid="1" * 32)])

    def test_a_reference_by_guid_resolves_to_the_name(self):
        graph = asmgraph.Graph([definition("A", guid="1" * 32), definition("B", guid="2" * 32)])
        self.assertEqual("A", graph.target("GUID:" + "1" * 32))
        self.assertEqual("B", graph.target("B"))

    def test_a_guid_outside_the_graph_is_returned_as_it_is(self):
        graph = asmgraph.Graph([definition("A", guid="1" * 32)])
        self.assertEqual("GUID:" + "9" * 32, graph.target("GUID:" + "9" * 32))

    def test_a_guid_cannot_be_resolved_when_an_assembly_has_no_meta(self):
        graph = asmgraph.Graph([definition("A", guid="1" * 32), definition("B")])
        with self.assertRaises(asmgraph.GraphError):
            graph.target("GUID:" + "9" * 32)


if __name__ == "__main__":
    unittest.main()
