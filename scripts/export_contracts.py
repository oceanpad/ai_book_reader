import json
from pathlib import Path

from backend.contracts import ReaderContracts


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "frontend/src/api/schema.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    schema = ReaderContracts.model_json_schema(mode="serialization")
    target.write_text(json.dumps(schema, indent=2) + "\n")


if __name__ == "__main__":
    main()
