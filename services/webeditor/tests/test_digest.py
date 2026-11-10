"""流程文档 §3: the one scene-digest implementation, and the rules it must not bend.

The gateway is the only party here that digests a scene (R5), so this file is the contract
test for that single point: RFC 8785 canonical bytes, SHA-256, 64 lowercase hex. The hexes
below are **not** this module's own output frozen in place -- they came from an ECMAScript
peer (`JSON.parse` + UTF-16 code-unit key sort + `String(number)` + `crypto.subtle`), which
is what the colleague's Brain service and the browser bundle both speak. A change here that
disagrees with them is a protocol break, not a refactor.

The pairwise夹具 (`AgentBrain修改点.md` §A8.B3) still has to arrive; when it does it lands
in this file as more of the same comparisons.
"""
import json
import unittest
from pathlib import Path

from digest import canonical_json, scene_digest

ROOT = Path(__file__).resolve().parent.parent
SCENES = {
    "docs/ur5e_table_empty.json": "e7e5d7e7f0176793b267a81d2cb810e3a2043ad15d40d15bc4c7f345ea35096a",
    "docs/ur5e_passive_rig.json": "65dcbfecae1f3d3f112a7c97c0e99ce1206881cdff1984d08856a1cef410dc21",
}


def document(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class CanonicalFormTest(unittest.TestCase):
    def test_keys_sort_by_utf16_code_unit_not_by_collation(self):
        # Code-unit order puts uppercase before lowercase, digits before letters, and the
        # control characters before everything -- an "alphabetical" sort would move "a" and
        # would not move "\u0000", and both mistakes change the digest.
        value = {"b": 1, "a": 2, "A": 3, "0": 4, "\u0000ctl": 5, "\u001fend": 6, "é": 7}
        self.assertEqual(
            canonical_json(value),
            '{"\\u0000ctl":5,"\\u001fend":6,"0":4,"A":3,"a":2,"b":1,"é":7}',
        )

    def test_arrays_keep_their_order(self):
        self.assertEqual(canonical_json([1, [2, 3], {"a": [4, 5]}]), '[1,[2,3],{"a":[4,5]}]')

    def test_numbers_print_the_way_ecmascript_prints_them(self):
        cases = {
            1.0: "1",
            0.0: "0",
            -0.0: "0",
            100.0: "100",
            0.5: "0.5",
            1e20: "100000000000000000000",
            1e21: "1e+21",
            1.5e21: "1.5e+21",
            1e-6: "0.000001",
            1e-7: "1e-7",
            2 ** 53: "9007199254740992",
            # An integer literal is a double once parsed, so JSON.parse's rounding shows here.
            12345678901234567890: "12345678901234567000",
            5e-324: "5e-324",
            1.7976931348623157e308: "1.7976931348623157e+308",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(canonical_json(value), expected)

    def test_nan_and_infinity_have_no_canonical_form(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    canonical_json(value)
        with self.assertRaises(ValueError):
            canonical_json([1.0, float("inf")])

    def test_strings_escape_only_what_json_needs(self):
        self.assertEqual(canonical_json('a"b\\c'), '"a\\"b\\\\c"')
        self.assertEqual(canonical_json("\n\t\r\b\f\u0001"), '"\\n\\t\\r\\b\\f\\u0001"')
        # U+2028 is a line terminator for a JS *source* file, not for JSON: no escaping.
        self.assertEqual(canonical_json("\u2028\u2029"), '"\u2028\u2029"')

    def test_unicode_is_not_normalized(self):
        # A precomposed "é" and the same letter as "e" + combining acute are different
        # documents, exactly as §3.2 says: the digest covers the bytes on the wire, so
        # normalizing would hide a difference the transport really carries.
        self.assertNotEqual(scene_digest("\u00e9"), scene_digest("e\u0301"))

    def test_canonicalization_is_idempotent(self):
        value = document("docs/ur5e_passive_rig.json")
        once = canonical_json(value)
        self.assertEqual(canonical_json(json.loads(once)), once)
        self.assertEqual(scene_digest(json.loads(once)), scene_digest(value))


class SceneDigestTest(unittest.TestCase):
    def test_hex_shape(self):
        digest = scene_digest(document("docs/ur5e_table_empty.json"))
        self.assertEqual(len(digest), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in digest))

    def test_real_scenes_match_an_ecmascript_peer(self):
        for path, expected in SCENES.items():
            with self.subTest(path=path):
                self.assertEqual(scene_digest(document(path)), expected)

    def test_small_vectors_match_an_ecmascript_peer(self):
        cases = {
            '{"A":null,"a":[1,1e+21,[[{"é":true}]]],"b":1}':
                "9aad03a504c64dec71e5bf490ff8b481b6b5a84b700a17e3231122b6448518b5",
            '{"objects":[{"id":7,"now_joint_positions_rad":[0,1]}],"scene_id":1,"scene_sub_version":2,"scene_version":3}':
                "d1506a91f2c42be4da1cf8502afc35c819aefde1b8f863b77416f48c629c54f1",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(scene_digest(json.loads(text)), expected)

    def test_the_two_document_differences_that_must_move_the_digest(self):
        # §12.17 ①: the two "only one thing changed" cases Brain has to tell apart.
        base = document("docs/ur5e_passive_rig.json")

        def joints(angle: float) -> dict:
            value = json.loads(json.dumps(base))
            for obj in value["objects"]:
                for component in obj.get("components", []):
                    properties = component.get("properties") or {}
                    if component.get("component_type") == "RobotDriver" and "joint_sequence" in properties:
                        properties["now_joint_positions_rad"] = {properties["joint_sequence"][0]: angle}
                        return value
            raise AssertionError("no RobotDriver in the fixture declares a joint_sequence")

        untouched = scene_digest(base)
        home, moved = joints(0.0), joints(0.1)
        self.assertNotEqual(scene_digest(moved), scene_digest(home))
        self.assertNotEqual(scene_digest(moved), untouched)
        sub = dict(base, scene_sub_version=1)
        self.assertNotEqual(scene_digest(sub), untouched)
        # One object, one change: a byte inside a component never reads as a new version.
        self.assertEqual(scene_digest(json.loads(json.dumps(moved))), scene_digest(moved))

    def test_a_missing_sub_version_is_never_filled_with_zero(self):
        # §3.5: the 0-for-absent fallback is a view-layer reading. Digesting `0` for a
        # document that carries no key would split "read the old file" from "digest it".
        without = document("docs/ur5e_table_empty.json")
        with_zero = dict(without, scene_sub_version=0)
        self.assertNotIn("scene_sub_version", without)
        self.assertNotEqual(scene_digest(with_zero), scene_digest(without))

    def test_unknown_fields_are_part_of_the_document(self):
        # §2: the gateway validates "is it JSON, how big is it" and nothing else, so an
        # unrecognized key is exactly as digested as a recognized one. Dropping it would
        # break digest(parse(x).document) == digest(x) for whatever Brain keeps.
        base = document("docs/ur5e_table_empty.json")
        extended = dict(base, some_future_field={"kept": True})
        self.assertNotEqual(scene_digest(extended), scene_digest(base))

    def test_a_renamed_key_is_a_different_scene(self):
        base = document("docs/ur5e_table_empty.json")
        renamed = json.loads(json.dumps(base))
        renamed["scene_nme"] = renamed.pop("scene_name")
        self.assertNotEqual(scene_digest(renamed), scene_digest(base))

    def test_the_digest_is_the_gateways_and_only_the_gateways(self):
        # R5: one implementation. The browser bundle must never grow a second one, and the
        # release bundle must carry this file or the gateway cannot answer §5.1's header.
        import pack

        self.assertIn("digest.py", pack.RELEASE_FILES)
        self.assertFalse((ROOT / "editor" / "src" / "digest.ts").exists())
        for path in (ROOT / "editor" / "src").glob("*.ts"):
            with self.subTest(path=path.name):
                self.assertNotIn("crypto.subtle", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
