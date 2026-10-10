"""Generation grammar for the existing normal SHORT→MID plan.

No source-owned headings or text enter the schema. The existing preparer still
validates exact addresses, dispositions, UTF-8 and all destructive decisions.
Inline alternatives avoid nested refs unsupported by some llama.cpp versions.
"""
import json


def _schema() -> dict:
    text = {"type": "string", "minLength": 1}
    notes = {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 1}}

    def closed(properties):
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}

    operation_base = {"heading": text, "reason": text, "notes": notes}
    operations = [closed({"type": {"const": kind}, **operation_base, "content": text})
                  for kind in ("replace_section", "append_to_section", "prepend_to_section", "add_section")]
    operations += [closed({"type": {"const": "add_section"}, **operation_base,
                           "content": text, "after": text}),
                   closed({"type": {"const": "delete_section"}, **operation_base})]
    edits = [closed({"filename": text, "action": {"const": "edit"},
                     "operations": {"type": "array", "minItems": 1,
                                    "items": {"anyOf": operations}}})]
    edits += [closed({"filename": text, "action": {"const": kind},
                      "content": text, "reason": text, "notes": notes})
              for kind in ("create", "rewrite")]
    return closed({
        "file_edits": {"type": "array", "items": {"anyOf": edits}},
        "discarded_notes": {"type": "array", "items": closed({
            "note": {"type": "integer", "minimum": 1},
            "reason": {"enum": ["already_in_bank", "superseded", "obsolete", "no_bank_value"]},
        })},
        "synthesis": text,
    })


NORMAL_RESPONSE_SCHEMA_JSON = json.dumps(_schema(), separators=(",", ":"))
