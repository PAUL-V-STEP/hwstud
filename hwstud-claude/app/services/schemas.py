from __future__ import annotations

from typing import Any


def json_schema(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


ABBREVIATION_RESPONSE = json_schema(
    "abbreviations",
    {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "term": {"type": "string"},
                        "expansion": {"type": "string"},
                        "english": {"type": "string"},
                    },
                    "required": ["term", "expansion", "english"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    },
)

SOURCE_META_RESPONSE = json_schema(
    "source_meta",
    {
        "type": "object",
        "properties": {
            "type": {"type": "string"},
            "annotation": {"type": "string"},
        },
        "required": ["type", "annotation"],
        "additionalProperties": False,
    },
)

GRADING_RESPONSE = json_schema(
    "homework_review",
    {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["accept", "revise"]},
            "score": {"type": "integer", "minimum": 0, "maximum": 10},
            "errors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "criterion": {"type": "string"},
                        "description": {"type": "string"},
                        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": ["criterion", "description", "severity"],
                    "additionalProperties": False,
                },
            },
            "classification": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "comment_for_student": {"type": "string"},
            "similarity_matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "reference_id": {"type": "integer", "minimum": 1},
                        "match_percent": {"type": "number", "minimum": 0, "maximum": 100},
                    },
                    "required": ["reference_id", "match_percent"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "verdict", "score", "errors", "classification", "confidence",
            "comment_for_student", "similarity_matches",
        ],
        "additionalProperties": False,
    },
)
