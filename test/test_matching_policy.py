import unittest
from core.matching.contracts import Purpose, ContractError
from core.matching.policy import load_policy, PolicyRegistry


def policy_data(**changes):
    data = {
        "policy_id": "fixture",
        "version": "1",
        "purpose": "functional_substitute",
        "category_ids": ["fixture"],
        "item_kind": "main",
        "state": "shadow",
        "freshness_hours": "36",
        "required_groups": ["contract"],
        "rules": [
            {
                "field": "dimensions",
                "operator": "eq",
                "unit": "mm",
                "required": True,
                "tolerance": "0",
                "value": None,
            }
        ],
        "required_certificates": [],
        "allow_rotation": False,
    }
    data.update(changes)
    return data


class PolicyTests(unittest.TestCase):
    def test_draft_never_active(self):
        self.assertFalse(load_policy(policy_data(state="draft")).auto_eligible)

    def test_missing_unit_rejected(self):
        data = policy_data()
        del data["rules"][0]["unit"]
        with self.assertRaises(ContractError):
            load_policy(data)

    def test_inverted_range_rejected(self):
        data = policy_data()
        data["rules"] = [
            dict(
                field="capacity",
                operator="range",
                unit="L",
                required=True,
                value=["3", "1"],
                tolerance="0",
            )
        ]
        with self.assertRaises(ContractError):
            load_policy(data)

    def test_registry_exact_category_only(self):
        registry = PolicyRegistry((load_policy(policy_data()),))
        self.assertIsNone(
            registry.get(Purpose.FUNCTIONAL_SUBSTITUTE, "fixture accessory")
        )

    def test_duplicate_registry_version_rejected(self):
        policy = load_policy(policy_data())
        with self.assertRaises(ContractError):
            PolicyRegistry((policy, policy))
